#!/usr/bin/env python3
"""Signal Arena cloud runner for watch and review records.

The runner is intentionally conservative about data quality: if authenticated
account data is unavailable, it writes a record and refuses to trade.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any
from urllib import error, parse, request
from zoneinfo import ZoneInfo


BASE_URL = os.getenv("SIGNAL_ARENA_BASE_URL", "https://signal.coze.com").rstrip("/")
HEADER_NAME = "agent-auth-api-key"
BJ_TZ = ZoneInfo("Asia/Shanghai")
US_RATE = 7.25
HK_RATE = 0.92

SLOTS = {
    "1000": {"label": "A股/港股开盘盯盘", "record": "1000-watch", "market": "CN"},
    "1445": {"label": "A股/港股尾盘风控", "record": "1445-watch", "market": "CN"},
    "2200": {"label": "美股开盘盯盘", "record": "2200-watch", "market": "US"},
    "0030": {"label": "美股盘中盯盘", "record": "0030-watch", "market": "US"},
    "0345": {"label": "美股收盘前风控", "record": "0345-watch", "market": "US"},
}
REVIEW_LABEL = "每日复盘调整"

CORE_US = {"gb_nvda", "gb_amd", "gb_aapl", "gb_amzn"}
SATELLITE_US = {"gb_arm", "gb_adi", "gb_mu", "gb_wdc", "gb_on"}
MAINLINE_HINTS = {
    "nvda",
    "amd",
    "arm",
    "adi",
    "mu",
    "wdc",
    "on",
    "app",
    "avgo",
    "tsm",
    "smci",
    "aapl",
    "amzn",
    "msft",
    "googl",
}


@dataclass
class ApiResult:
    ok: bool
    data: Any = None
    error: str | None = None


class ArenaClient:
    def __init__(self, api_key: str | None):
        self.api_key = api_key

    def get(self, endpoint: str, params: dict[str, Any] | None = None, auth: bool = True) -> ApiResult:
        return self._request("GET", endpoint, params=params, auth=auth)

    def post(self, endpoint: str, body: dict[str, Any], auth: bool = True) -> ApiResult:
        return self._request("POST", endpoint, body=body, auth=auth)

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> ApiResult:
        url = f"{BASE_URL}{endpoint}"
        if params:
            url = f"{url}?{parse.urlencode(params)}"
        headers = {"Content-Type": "application/json"}
        if auth and self.api_key:
            headers[HEADER_NAME] = self.api_key
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        req = request.Request(url, data=payload, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=25) as resp:
                raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            if parsed.get("success") is False:
                message = parsed.get("message") or parsed.get("error") or "api returned success=false"
                return ApiResult(False, parsed, str(message))
            return ApiResult(True, parsed.get("data", parsed))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            return ApiResult(False, None, f"HTTP {exc.code}: {detail[:500]}")
        except Exception as exc:  # noqa: BLE001 - record all API/runtime failures.
            return ApiResult(False, None, str(exc))


def load_api_key(repo_root: Path) -> tuple[str | None, str]:
    key = os.getenv("SIGNAL_ARENA_API_KEY")
    if key:
        return key, "SIGNAL_ARENA_API_KEY"

    local_config = repo_root / ".agent-world.json"
    if local_config.exists():
        try:
            data = json.loads(local_config.read_text(encoding="utf-8"))
            key = data.get("api_key")
            if key:
                return key, ".agent-world.json"
        except Exception:
            return None, "invalid .agent-world.json"
    return None, "missing"


def now_bj() -> datetime:
    return datetime.now(BJ_TZ)


def infer_slot(now: datetime) -> str:
    current_minutes = now.hour * 60 + now.minute
    best_slot = "2200"
    best_delta = 10_000
    for slot in SLOTS:
        slot_minutes = int(slot[:2]) * 60 + int(slot[2:])
        delta = abs(current_minutes - slot_minutes)
        delta = min(delta, 24 * 60 - delta)
        if delta < best_delta:
            best_slot = slot
            best_delta = delta
    return best_slot


def cny_value(symbol: str, price: float, shares: int) -> float:
    if symbol.startswith("gb_"):
        return price * shares * US_RATE
    if symbol.startswith("hk"):
        return price * shares * HK_RATE
    return price * shares


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def get_portfolio_snapshot(client: ArenaClient) -> dict[str, Any]:
    endpoints = {
        "home": client.get("/api/v1/arena/home"),
        "portfolio": client.get("/api/v1/arena/portfolio"),
        "trades": client.get("/api/v1/arena/trades"),
        "top_movers": client.get("/api/v1/arena/top-movers", auth=False),
        "leaderboard": client.get("/api/v1/arena/leaderboard", auth=False),
    }
    return endpoints


def portfolio_data(snapshot: dict[str, ApiResult]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    p = snapshot["portfolio"].data or {}
    account = p.get("portfolio") or {}
    holdings = p.get("holdings") or []
    return account, holdings


def score_candidate(candidate: dict[str, Any], held_symbols: set[str], market: str) -> int:
    symbol = str(candidate.get("symbol", "")).lower()
    change_rate = as_float(candidate.get("change_rate"))
    score = 0
    score += min(35, max(0, int(change_rate * 350)))
    if market == "US" and (symbol in CORE_US or symbol in SATELLITE_US or symbol.replace("gb_", "") in MAINLINE_HINTS):
        score += 25
    elif market in {"CN", "HK"} and change_rate >= 0.04:
        score += 20
    if symbol not in held_symbols:
        score += 10
    if change_rate > 0.12:
        score -= 10
    if symbol in held_symbols:
        score += 5
    return max(0, min(100, score))


def build_candidates(snapshot: dict[str, ApiResult], market: str, held_symbols: set[str]) -> list[dict[str, Any]]:
    movers = ((snapshot["top_movers"].data or {}).get("movers") or {}).get(market, [])
    candidates = []
    for item in movers:
        enriched = dict(item)
        enriched["score"] = score_candidate(enriched, held_symbols, market)
        candidates.append(enriched)
    return sorted(candidates, key=lambda item: item.get("score", 0), reverse=True)


def plan_trades(
    account: dict[str, Any],
    holdings: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    slot: str,
) -> list[dict[str, Any]]:
    total_value = as_float(account.get("total_value")) or as_float(account.get("cash")) + as_float(account.get("holdings_value"))
    cash = as_float(account.get("cash"))
    max_single_value = total_value * 0.20
    min_cash_buffer = total_value * 0.01
    actions: list[dict[str, Any]] = []
    held_symbols = {str(h.get("symbol", "")).lower() for h in holdings}

    for holding in holdings:
        symbol = str(holding.get("symbol", "")).lower()
        shares = as_int(holding.get("shares"))
        market_value = as_float(holding.get("market_value"))
        profit_rate = as_float(holding.get("profit_rate"))
        if shares <= 0:
            continue
        if profit_rate <= -0.08:
            actions.append(
                {
                    "symbol": symbol,
                    "action": "sell",
                    "shares": max(1, math.ceil(shares / 2)),
                    "reason": f"触发硬止损：浮亏 {profit_rate:.2%} 接近/超过 8%，先减半控制回撤。",
                }
            )
        elif market_value > max_single_value:
            excess = market_value - max_single_value
            current_price = as_float(holding.get("current_price"))
            excess_shares = max(1, math.ceil(excess / max(cny_value(symbol, current_price, 1), 1)))
            actions.append(
                {
                    "symbol": symbol,
                    "action": "sell",
                    "shares": min(shares, excess_shares),
                    "reason": "触发单股 20% 上限，降回模型硬风控范围。",
                }
            )
        elif profit_rate >= 0.15 and not any(str(c.get("symbol", "")).lower() == symbol for c in candidates[:5]):
            actions.append(
                {
                    "symbol": symbol,
                    "action": "sell",
                    "shares": max(1, math.floor(shares / 2)),
                    "reason": f"浮盈 {profit_rate:.2%} 达到止盈区且未处在涨幅榜前列，分批止盈。",
                }
            )

    if actions:
        return actions[:3]

    if SLOTS[slot]["market"] != "US":
        return []

    investable_cash = max(0.0, cash - min_cash_buffer)
    if total_value <= 0 or investable_cash / total_value < 0.02:
        return []

    for candidate in candidates:
        symbol = str(candidate.get("symbol", "")).lower()
        if candidate.get("score", 0) < 75:
            continue
        price = as_float(candidate.get("price"))
        if price <= 0:
            continue
        existing_value = 0.0
        for holding in holdings:
            if str(holding.get("symbol", "")).lower() == symbol:
                existing_value = as_float(holding.get("market_value"))
                break
        room = max_single_value - existing_value
        budget = min(investable_cash, room)
        shares = math.floor(budget / cny_value(symbol, price, 1))
        if shares >= 1:
            relation = "已持仓补强" if symbol in held_symbols else "涨幅榜新增强势标的"
            actions.append(
                {
                    "symbol": symbol,
                    "action": "buy",
                    "shares": shares,
                    "reason": f"{relation}，模型评分 {candidate['score']}，用于维持冲榜满仓但不突破单股 20%。",
                }
            )
            break
    return actions[:2]


def execute_trades(client: ArenaClient, actions: list[dict[str, Any]], execute: bool) -> list[dict[str, Any]]:
    results = []
    for action in actions:
        if not execute:
            results.append({**action, "status": "planned"})
            continue
        result = client.post("/api/v1/arena/trade", action)
        results.append({**action, "status": "submitted" if result.ok else "failed", "api_error": result.error, "api_data": result.data})
    return results


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_无_"
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def write_record(
    repo_root: Path,
    mode: str,
    slot: str,
    credential_source: str,
    snapshot: dict[str, ApiResult] | None,
    candidates: list[dict[str, Any]],
    trade_results: list[dict[str, Any]],
    execute: bool,
    note: str | None = None,
) -> Path:
    ts = now_bj()
    records_dir = repo_root / "records"
    records_dir.mkdir(exist_ok=True)
    suffix = "review" if mode == "review" else SLOTS[slot]["record"]
    title = REVIEW_LABEL if mode == "review" else SLOTS.get(slot, {}).get("label", "投资模型")
    path = records_dir / f"{ts:%Y-%m-%d}-{suffix}.md"

    lines = [
        f"# {ts:%Y-%m-%d} {title}记录",
        "",
        f"- 运行时间：{ts:%Y-%m-%d %H:%M:%S} 北京时间",
        f"- 运行模式：{mode}",
        f"- 时间节点：{slot}",
        f"- 交易执行：{'开启' if execute else '关闭/仅记录'}",
        f"- 凭据来源：{credential_source}",
    ]
    if note:
        lines.extend(["", f"> {note}"])

    if not snapshot:
        lines.extend(["", "## 结果", "", "未取得账户数据，未执行交易。"])
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    account, holdings = portfolio_data(snapshot)
    lines.extend(
        [
            "",
            "## API 状态",
            "",
            md_table(
                ["端点", "状态", "错误"],
                [[name, "ok" if result.ok else "failed", result.error or ""] for name, result in snapshot.items()],
            ),
            "",
            "## 账户概览",
            "",
            f"- 总资产：{as_float(account.get('total_value')):.2f}",
            f"- 现金：{as_float(account.get('cash')):.2f}",
            f"- 持仓市值：{as_float(account.get('holdings_value')):.2f}",
            f"- 收益率：{as_float(account.get('return_rate')):.2%}",
            "",
            "## 持仓",
            "",
            md_table(
                ["symbol", "名称", "股数", "现价", "市值", "盈亏率"],
                [
                    [
                        h.get("symbol", ""),
                        h.get("name", ""),
                        as_int(h.get("shares")),
                        f"{as_float(h.get('current_price')):.3f}",
                        f"{as_float(h.get('market_value')):.2f}",
                        f"{as_float(h.get('profit_rate')):.2%}",
                    ]
                    for h in holdings
                ],
            ),
            "",
            "## 候选评分",
            "",
            md_table(
                ["symbol", "名称", "涨跌幅", "评分"],
                [
                    [c.get("symbol", ""), c.get("name", ""), f"{as_float(c.get('change_rate')):.2%}", c.get("score", 0)]
                    for c in candidates[:10]
                ],
            ),
            "",
            "## 动作",
            "",
        ]
    )
    if trade_results:
        lines.append(
            md_table(
                ["动作", "symbol", "股数", "状态", "理由", "错误"],
                [
                    [
                        item.get("action", ""),
                        item.get("symbol", ""),
                        item.get("shares", ""),
                        item.get("status", ""),
                        item.get("reason", ""),
                        item.get("api_error") or "",
                    ]
                    for item in trade_results
                ],
            )
        )
    else:
        lines.append("未触发交易。满仓、止损、止盈和单股 20% 上限均未给出强制动作。")

    lines.extend(
        [
            "",
            "## 复盘点",
            "",
            "- 后续复盘必须核对本次动作是否按 15 分钟结算成交。",
            "- 若 API 字段变化导致账户数据缺失，本次记录标记为数据缺失样本，不用于调参。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(args: argparse.Namespace) -> int:
    repo_root = Path.cwd()
    mode = args.mode
    slot = args.slot if args.slot != "auto" else infer_slot(now_bj())
    if slot not in SLOTS:
        raise SystemExit(f"Unknown slot: {slot}")

    api_key, credential_source = load_api_key(repo_root)
    if not api_key:
        path = write_record(
            repo_root,
            mode,
            slot,
            credential_source,
            None,
            [],
            [],
            False,
            "缺少 SIGNAL_ARENA_API_KEY，云端任务只记录不交易。",
        )
        print(f"record={path}")
        return 0

    client = ArenaClient(api_key)
    snapshot = get_portfolio_snapshot(client)
    if not snapshot["home"].ok or not snapshot["portfolio"].ok:
        path = write_record(
            repo_root,
            mode,
            slot,
            credential_source,
            snapshot,
            [],
            [],
            False,
            "认证账户数据不可用，已拒绝交易。",
        )
        print(f"record={path}")
        return 0

    account, holdings = portfolio_data(snapshot)
    held_symbols = {str(h.get("symbol", "")).lower() for h in holdings}
    candidates = build_candidates(snapshot, SLOTS[slot]["market"], held_symbols)
    actions = [] if mode == "review" else plan_trades(account, holdings, candidates, slot)
    trade_results = execute_trades(client, actions, args.execute and mode != "review")
    path = write_record(repo_root, mode, slot, credential_source, snapshot, candidates, trade_results, args.execute)
    print(f"record={path}")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["watch", "review"], default="watch")
    parser.add_argument("--slot", choices=["auto", *SLOTS.keys()], default="auto")
    parser.add_argument("--execute", action="store_true", help="Submit planned trades to Signal Arena.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
