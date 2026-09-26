#!/usr/bin/env python3
"""Read-only MCP server exposing live Alpaca + Kraken account/bot state to Claude Code.

Read-only by design: only GET / fetch endpoints are exposed. There is intentionally
no tool that can place, modify, or cancel an order. Claude can SEE your positions and
P&L across both venues; it cannot touch them.

Register with Claude Code:
    claude mcp add broker-state -s user \
      -e ALPACA_KEY_ID=... -e ALPACA_SECRET_KEY=... \
      -e ALPACA_BASE_URL=https://paper-api.alpaca.markets \
      -e KRAKEN_API_KEY=... -e KRAKEN_API_SECRET=... \
      -- /path/to/.venv/bin/python3 /path/to/server.py
"""
import os

import ccxt
import requests
from mcp.server.fastmcp import FastMCP

BASE = os.environ.get("ALPACA_BASE_URL", "https://api.alpaca.markets")
HEADERS = {
    "APCA-API-KEY-ID": os.environ["ALPACA_KEY_ID"],
    "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
}

mcp = FastMCP("broker-state")


def _get(path, **params):
    r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def get_account() -> dict:
    """Account snapshot: equity, buying power, cash, and P&L for the day."""
    a = _get("/v2/account")
    equity, last = float(a["equity"]), float(a["last_equity"])
    return {
        "equity": equity,
        "buying_power": float(a["buying_power"]),
        "cash": float(a["cash"]),
        "day_pl": round(equity - last, 2),
        "day_pl_pct": round((equity - last) / last * 100, 2) if last else 0,
        "status": a["status"],
    }


@mcp.tool()
def get_positions() -> list:
    """All open positions with size, entry, current price, and unrealized P&L."""
    return [{
        "symbol": p["symbol"],
        "qty": float(p["qty"]),
        "avg_entry": float(p["avg_entry_price"]),
        "current": float(p["current_price"]),
        "market_value": round(float(p["market_value"]), 2),
        "unrealized_pl": round(float(p["unrealized_pl"]), 2),
        "unrealized_pl_pct": round(float(p["unrealized_plpc"]) * 100, 2),
    } for p in _get("/v2/positions")]


@mcp.tool()
def get_recent_orders(limit: int = 20) -> list:
    """Most recent orders (filled/open/canceled), newest first."""
    return [{
        "symbol": o["symbol"],
        "side": o["side"],
        "qty": o["qty"],
        "type": o["order_type"],
        "status": o["status"],
        "filled_avg_price": o.get("filled_avg_price"),
        "submitted_at": o["submitted_at"],
    } for o in _get("/v2/orders", status="all", limit=limit, direction="desc")]


# ── Kraken (ccxt) — read-only spot balances/trades. No order tool exists. ──────
_KRAKEN = None
_STABLE = {"USD", "ZUSD", "USDT", "USDC", "DAI"}


def _kraken():
    """Lazy ccxt.kraken client so the server still loads if Kraken creds are absent."""
    global _KRAKEN
    if _KRAKEN is None:
        _KRAKEN = ccxt.kraken({
            "apiKey": os.environ.get("KRAKEN_API_KEY", ""),
            "secret": os.environ.get("KRAKEN_API_SECRET", ""),
            "enableRateLimit": True,
        })
    return _KRAKEN


def _kraken_usd(ex, asset, amt):
    """USD value of `amt` of `asset` (stablecoins ≈ 1:1; else fetch a ticker)."""
    if asset in _STABLE:
        return amt
    for quote in ("USD", "USDT"):
        try:
            return amt * ex.fetch_ticker(f"{asset}/{quote}")["last"]
        except ccxt.BaseError:
            continue  # pair not listed on this quote; try the next
    return None


@mcp.tool()
def get_kraken_balance() -> dict:
    """Kraken spot account: non-zero per-asset balances (free/total) and total USD value."""
    ex = _kraken()
    bal = ex.fetch_balance()
    assets, total_usd = {}, 0.0
    for asset, amt in bal.get("total", {}).items():
        if not amt or amt <= 0:
            continue
        usd = _kraken_usd(ex, asset, amt)
        assets[asset] = {
            "amount": round(amt, 8),
            "free": round(bal.get("free", {}).get(asset, 0) or 0, 8),
            "usd": round(usd, 2) if usd is not None else None,
        }
        if usd:
            total_usd += usd
    return {"assets": assets, "total_usd": round(total_usd, 2)}


@mcp.tool()
def get_kraken_positions(min_usd: float = 1.0) -> list:
    """Kraken coin holdings (the grid bot's inventory) with current price + USD value —
    i.e. your live crypto exposure on Kraken. Stablecoins and sub-`min_usd` dust excluded."""
    ex = _kraken()
    bal = ex.fetch_balance()
    rows = []
    for asset, amt in bal.get("total", {}).items():
        if not amt or amt <= 0 or asset in _STABLE:
            continue
        px = None
        for quote in ("USD", "USDT"):
            try:
                px = ex.fetch_ticker(f"{asset}/{quote}")["last"]
                break
            except ccxt.BaseError:
                continue  # pair not listed on this quote; try the next
        val = round(amt * px, 2) if px else None
        if val is not None and val < min_usd:
            continue  # dust
        rows.append({
            "asset": asset,
            "amount": round(amt, 8),
            "price": round(px, 6) if px else None,
            "usd_value": val,
        })
    rows.sort(key=lambda r: r["usd_value"] or 0, reverse=True)
    return rows


@mcp.tool()
def get_kraken_recent_trades(limit: int = 20) -> list:
    """Most recent Kraken trades (fills), newest first."""
    ex = _kraken()
    trades = sorted(ex.fetch_my_trades(limit=limit),
                    key=lambda t: t.get("timestamp") or 0, reverse=True)[:limit]
    return [{
        "symbol": t.get("symbol"),
        "side": t.get("side"),
        "amount": t.get("amount"),
        "price": t.get("price"),
        "cost": round(t.get("cost") or 0, 2),
        "datetime": t.get("datetime"),
    } for t in trades]


@mcp.tool()
def get_exposure_summary() -> dict:
    """TOTAL exposure across BOTH venues in one call — use this for 'what's my exposure?'.
    Alpaca account equity + open positions, plus Kraken balances + crypto holdings, with a
    combined USD figure. Each venue is independent: if one errors the other still returns."""
    out = {}
    try:
        out["alpaca"] = {"account": get_account(), "positions": get_positions()}
    except Exception as e:
        out["alpaca"] = {"error": str(e)}
    try:
        out["kraken"] = {"balance": get_kraken_balance(), "positions": get_kraken_positions()}
    except Exception as e:
        out["kraken"] = {"error": str(e)}
    acct = out.get("alpaca", {}).get("account")
    alpaca_eq = acct.get("equity") if isinstance(acct, dict) else None
    kbal = out.get("kraken", {}).get("balance")
    kraken_usd = kbal.get("total_usd") if isinstance(kbal, dict) else None
    out["combined_usd"] = round((alpaca_eq or 0) + (kraken_usd or 0), 2)
    return out


if __name__ == "__main__":
    mcp.run()  # stdio transport
