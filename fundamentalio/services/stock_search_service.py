"""
Company search via Yahoo Finance (yfinance).
"""

from __future__ import annotations

import logging
from typing import Any

import yfinance as yf

logger = logging.getLogger(__name__)

SEARCH_TIMEOUT = 10
_MAX_SEARCH = 15


class ExternalAPIError(Exception):
    """Raised when the upstream Yahoo Finance search fails."""


def _is_equity_quote(quote: dict[str, Any]) -> bool:
    return str(quote.get("quoteType") or "").upper() == "EQUITY"


def _fast_info_snapshot(yahoo_symbol: str) -> tuple[str, float | None, str | None]:
    """Return (symbol, previous_close, currency) from Ticker.fast_info."""
    prev: float | None = None
    cur: str | None = None
    try:
        fi = yf.Ticker(yahoo_symbol).fast_info
        if fi is not None:
            prev = fi.get("previousClose") or fi.get("regularMarketPreviousClose")
            cur = fi.get("currency")
            if prev is not None:
                prev = float(prev)
    except Exception:
        logger.warning("fast_info failed for symbol", extra={"symbol": yahoo_symbol}, exc_info=True)
    return yahoo_symbol, prev, cur


def search_companies(query: str) -> list[dict[str, Any]]:
    """
    Search stocks via Yahoo Finance and return a normalized list of results.

    At most 15 equity (common stock) hits are returned.

    Normalized result shape:
    {
        "name": str,
        "code": str,              # Yahoo ticker (e.g. AAPL, VOD.L)
        "exchange_code": str,     # Yahoo exchange MIC for display only
        "previous_close": float | None,
        "currency": str | None,
    }
    """
    query = query.strip()
    if not query:
        raise ValueError("Query must be a non-empty string.")

    try:
        search = yf.Search(
            query,
            max_results=_MAX_SEARCH,
            news_count=0,
            lists_count=0,
            timeout=SEARCH_TIMEOUT,
            raise_errors=True,
        )
        search.search()
    except Exception as exc:
        logger.exception("Yahoo Finance search request failed.")
        raise ExternalAPIError("Yahoo Finance search failed.") from exc

    raw_quotes = search.quotes or []
    equity_quotes = [q for q in raw_quotes if isinstance(q, dict) and _is_equity_quote(q)]
    if not equity_quotes and raw_quotes:
        logger.info(
            "No equity quotes in Yahoo search response; returning empty results.",
            extra={"query": query},
        )

    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for item in equity_quotes[:_MAX_SEARCH]:
        symbol = (item.get("symbol") or "").strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        name = (item.get("longname") or item.get("shortname") or "").strip()
        exch_code = (str(item.get("exchange") or "").strip().upper())
        rows.append(
            {
                "name": name,
                "code": symbol,
                "exchange_code": exch_code,
                "previous_close": None,
                "currency": None,
            }
        )

    if not rows:
        return []

    for r in rows:
        try:
            _, prev, cur = _fast_info_snapshot(r["code"])
        except Exception:
            logger.warning("fast_info task failed", extra={"symbol": r["code"]}, exc_info=True)
            prev, cur = (None, None)
        r["previous_close"] = prev
        r["currency"] = cur

    return rows
