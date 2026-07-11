#!/usr/bin/env python3
"""
rh_data.py — Robinhood-MCP-backed data provider for the trading desk.
================================================================================
Wraps robinhood_mcp.RobinhoodMCP to deliver exactly what the Example Workflow
needs, in the shapes the deterministic engines expect:

  * daily_closes(symbol, lookback_days) -> list[float]   (old->new)
  * multi_daily_closes([symbols])       -> {sym: list[float]}
  * last_quote(symbol)                  -> {price, prev_close}
  * holding(symbol)                     -> bool  (scans equity positions)

One MCP session is reused across all calls (connect() does the handshake).
Raises StepError (imported name-compatible with run_desk) on any failure so the
orchestrator's fail-safe can abort cleanly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import robinhood_mcp as R


class StepError(Exception):
    pass


class RHData:
    def __init__(self) -> None:
        try:
            self.mcp = R.connect()
        except Exception as e:  # noqa: BLE001
            raise StepError(f"Robinhood MCP connect/init failed: {e}")
        self._accounts: Optional[list[dict]] = None

    # -- historicals -------------------------------------------------------
    def _bars(self, symbols: list[str], lookback_days: int) -> dict:
        start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)
                 ).strftime("%Y-%m-%dT00:00:00Z")
        try:
            res = self.mcp.call_tool("get_equity_historicals", {
                "symbols": symbols, "start_time": start,
                "interval": "day", "bounds": "regular",
                "adjustment_type": "split",
            })
        except Exception as e:  # noqa: BLE001
            raise StepError(f"get_equity_historicals failed for {symbols}: {e}")
        if not isinstance(res, dict) or "data" not in res:
            raise StepError(f"unexpected historicals payload for {symbols}: "
                            f"{str(res)[:160]}")
        out: dict[str, list[float]] = {}
        for r in res["data"].get("results", []):
            sym = r.get("symbol")
            closes = []
            for b in r.get("bars", []):
                cp = b.get("close_price")
                if cp is not None:
                    try:
                        closes.append(float(cp))
                    except (TypeError, ValueError):
                        pass
            if sym:
                out[sym] = closes
        return out

    def daily_closes(self, symbol: str, lookback_days: int = 800) -> list[float]:
        data = self._bars([symbol], lookback_days)
        closes = data.get(symbol, [])
        if len(closes) < 60:
            raise StepError(f"only {len(closes)} daily bars for {symbol} "
                            f"(need >=60; ideal >=220)")
        return closes

    def multi_daily_closes(self, symbols: list[str],
                           lookback_days: int = 800) -> dict[str, list[float]]:
        """Fetch in batches of <=10 (MCP per-call limit)."""
        out: dict[str, list[float]] = {}
        for i in range(0, len(symbols), 10):
            batch = symbols[i:i + 10]
            out.update(self._bars(batch, lookback_days))
        missing = [s for s in symbols if len(out.get(s, [])) < 60]
        if missing:
            raise StepError(f"insufficient bars for {missing}")
        return out

    # -- quote -------------------------------------------------------------
    def last_quote(self, symbol: str) -> dict:
        try:
            res = self.mcp.call_tool("get_equity_quotes", {"symbols": [symbol]})
        except Exception as e:  # noqa: BLE001
            raise StepError(f"get_equity_quotes failed for {symbol}: {e}")
        # Shape mirrors historicals: data.results[]
        results = []
        if isinstance(res, dict):
            results = res.get("data", {}).get("results", []) or res.get("results", [])
        if not results:
            raise StepError(f"no quote returned for {symbol}: {str(res)[:160]}")
        r0 = results[0]
        # Robinhood nests live fields under a 'quote' sub-object and the
        # official close under a 'close' sub-object. Flatten both.
        q = dict(r0.get("quote", r0))
        close_obj = r0.get("close", {})
        def _f(src, *keys):
            for k in keys:
                v = src.get(k)
                if v not in (None, ""):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
            return None
        price = _f(q, "last_trade_price", "last_extended_hours_trade_price",
                   "last_non_reg_trade_price", "ask_price", "bid_price")
        prev_close = _f(q, "previous_close", "adjusted_previous_close")
        if prev_close is None and isinstance(close_obj, dict):
            prev_close = _f(close_obj, "price")
        if price is None:
            raise StepError(f"quote missing price for {symbol}: {q}")
        return {"price": price, "prev_close": prev_close}

    def session_close(self, symbol: str) -> dict:
        """Official last-completed-session close + its date.

        After the 4pm ET close, Robinhood's quote 'close' sub-object reports
        today's settled close. Returns {'price': float, 'date': 'YYYY-MM-DD'}.
        """
        try:
            res = self.mcp.call_tool("get_equity_quotes", {"symbols": [symbol]})
        except Exception as e:  # noqa: BLE001
            raise StepError(f"get_equity_quotes failed for {symbol}: {e}")
        results = []
        if isinstance(res, dict):
            results = res.get("data", {}).get("results", []) or res.get("results", [])
        if not results:
            raise StepError(f"no quote returned for {symbol}: {str(res)[:160]}")
        close_obj = results[0].get("close", {}) or {}
        price = close_obj.get("price")
        date = close_obj.get("date")
        if price is None:
            # Fall back to previous_close from the live quote block.
            q = results[0].get("quote", results[0])
            price = q.get("previous_close") or q.get("adjusted_previous_close")
        if price is None:
            raise StepError(f"no session close for {symbol}: {str(results[0])[:160]}")
        return {"price": float(price), "date": date}

    # -- positions ---------------------------------------------------------
    def accounts(self) -> list[dict]:
        if self._accounts is None:
            try:
                res = self.mcp.call_tool("get_accounts", {})
            except Exception as e:  # noqa: BLE001
                raise StepError(f"get_accounts failed: {e}")
            accts = []
            if isinstance(res, dict):
                accts = (res.get("data", {}).get("results")
                         or res.get("results") or res.get("accounts") or [])
            self._accounts = accts or []
        return self._accounts

    def holding(self, symbol: str) -> Optional[bool]:
        """True if any brokerage account holds >0 shares of `symbol`.

        Returns None (unknown) if positions can't be read — the caller then
        falls back to its persisted paper state so a transient positions
        failure does not corrupt the holding flag.
        """
        try:
            accts = self.accounts()
            for a in accts:
                acct_no = a.get("account_number") or a.get("account_id")
                if not acct_no:
                    continue
                res = self.mcp.call_tool("get_equity_positions",
                                         {"account_number": acct_no})
                results = []
                if isinstance(res, dict):
                    results = (res.get("data", {}).get("results")
                               or res.get("results") or [])
                for p in results:
                    if p.get("symbol", "").upper() == symbol.upper():
                        qty = p.get("quantity") or p.get("shares") or 0
                        try:
                            if float(qty) > 0:
                                return True
                        except (TypeError, ValueError):
                            pass
            return False
        except Exception:  # noqa: BLE001
            return None
