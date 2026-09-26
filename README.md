# broker-state-mcp

A small, **read-only** [MCP](https://modelcontextprotocol.io) server that gives Claude
grounded access to live brokerage state across two venues — **Alpaca** (equities/crypto)
and **Kraken** (crypto spot) — so an agent can answer *"what's my exposure?"* from data
instead of guessing from prose.

Seven typed tools. Zero that can place, modify, or cancel an order.

```
get_account                 Alpaca equity, buying power, cash, day P&L
get_positions               Alpaca open positions with unrealized P&L
get_recent_orders           Alpaca order history, newest first
get_kraken_balance          Kraken per-asset balances + total USD value
get_kraken_positions        Kraken coin holdings (non-stable, dust filtered)
get_kraken_recent_trades    Kraken fills, newest first
get_exposure_summary        Both venues in one call, with a combined USD figure
```

## Why it exists

I run automated trading bots with real (small) money and use Claude Code as an
operator console over them. Early on, the model would estimate positions from log
excerpts and be confidently wrong. This server replaces inference with a tool call.

## Design decisions

**Read-only by construction, not by instruction.** There is no order-placement tool
because there is no order-placement code. A prompt injection, a hallucinated tool name,
or a careless `bypassPermissions` run cannot reach a mutating endpoint that does not
exist. Telling a model "please don't trade" is a request; not shipping the capability
is a guarantee.

**Per-venue fault isolation.** `get_exposure_summary` wraps each venue independently.
If Kraken is rate-limiting, you still get Alpaca, with the Kraken error surfaced in-band
rather than the whole call failing. For an operator dashboard, partial truth with an
explicit gap beats a stack trace.

**Lazy Kraken client.** The `ccxt` client is constructed on first use, so the server
starts (and the Alpaca tools work) even if Kraken credentials are absent. The Alpaca
headers are read at import time because a missing key there should fail loudly and
early — the two venues have different failure philosophies on purpose.

**Dust filtering.** `get_kraken_positions` drops holdings under `min_usd` (default `$1`).
A grid bot accumulates fractional leftovers; they are noise in an exposure view.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Register with Claude Code (user scope). Use the **paper** Alpaca endpoint until you
have a reason not to:

```bash
claude mcp add broker-state -s user \
  -e ALPACA_KEY_ID=... -e ALPACA_SECRET_KEY=... \
  -e ALPACA_BASE_URL=https://paper-api.alpaca.markets \
  -e KRAKEN_API_KEY=... -e KRAKEN_API_SECRET=... \
  -- "$PWD/.venv/bin/python3" "$PWD/server.py"
```

Credentials are read from the environment only. Nothing is written to disk.

**Use read-only API keys.** Both Alpaca and Kraken let you scope a key to
query-only permissions. This server never needs more, so do not give it more —
defense in depth is cheap here.

## Example

> *"What's my total exposure right now?"*

```json
{
  "alpaca":  { "account": { "equity": 975.74, "day_pl": 0.52, ... }, "positions": [ ... ] },
  "kraken":  { "balance": { "assets": { "USD": {...}, "USDT": {...} }, "total_usd": 438.26 },
               "positions": [] },
  "combined_usd": 1414.00
}
```

## Transport

stdio. Run it directly to sanity-check it starts:

```bash
ALPACA_KEY_ID=x ALPACA_SECRET_KEY=x .venv/bin/python3 -c "import server; print('ok')"
```

## License

MIT
