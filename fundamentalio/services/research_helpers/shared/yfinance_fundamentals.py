"""
Assemble fundamentals dicts from Yahoo Finance via yfinance.

"""

from __future__ import annotations

import logging
import math
from typing import Any

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

def display_exchange_code(yahoo_symbol: str, yahoo_exchange_mic: str | None) -> str:
    """Return Yahoo-provided exchange MIC for display purposes only."""
    del yahoo_symbol
    return (yahoo_exchange_mic or "").strip().upper()


def resolve_yahoo_ticker(company_symbol: str, exchange_symbol: str) -> str:
    """
    Return the canonical Yahoo ticker used by yfinance.Ticker.

    Yahoo symbols are persisted directly (e.g. AAPL, VOD.L), so no exchange
    suffix mapping is needed anymore. I know I should have removed exchange_symbol.
    My previous financial data API provider required exchange_symbol.
    """
    sym = (company_symbol or "").strip().upper()
    del exchange_symbol
    if not sym:
        raise ValueError("company_symbol is required.")
    return sym


def _json_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


def _df_to_period_dict(df: pd.DataFrame | None) -> dict[str, dict[str, Any]]:
    if df is None or df.empty:
        return {}
    cols = list(df.columns)
    try:
        cols = sorted(cols, key=lambda c: pd.Timestamp(c), reverse=True)
    except Exception:
        cols = sorted(cols, reverse=True)
    out: dict[str, dict[str, Any]] = {}
    for col in cols:
        if hasattr(col, "strftime"):
            key = col.strftime("%Y-%m-%d")
        else:
            key = str(col)[:10]
        row_dict: dict[str, Any] = {}
        for idx, val in df[col].items():
            if pd.isna(val):
                continue
            row_dict[str(idx)] = _json_scalar(val)
        if row_dict:
            out[key] = row_dict
    return out


def _build_general(info: dict[str, Any]) -> dict[str, Any]:
    officers_raw = info.get("companyOfficers") or []
    officers: dict[str, dict[str, Any]] = {}
    if isinstance(officers_raw, list):
        for i, off in enumerate(officers_raw, start=1):
            if isinstance(off, dict):
                officers[str(i)] = {
                    "Name": off.get("name"),
                    "Title": off.get("title"),
                }

    code = info.get("symbol") or info.get("underlyingSymbol")
    return {
        "Code": code,
        "Name": info.get("longName") or info.get("shortName"),
        "Exchange": info.get("exchange"),
        "CurrencyCode": info.get("currency"),
        "CountryName": info.get("country"),
        "FiscalYearEnd": info.get("lastFiscalYearEnd"),
        "Sector": info.get("sector"),
        "Industry": info.get("industry"),
        "Description": info.get("longBusinessSummary"),
        "WebURL": info.get("website"),
        "FullTimeEmployees": info.get("fullTimeEmployees"),
        "Officers": officers,
    }


def _subset_info(info: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: _json_scalar(info[k]) for k in keys if k in info and info[k] is not None}


def _build_fundamentals_meta(info: dict[str, Any]) -> dict[str, Any]:
    meta = _subset_info(
        info,
        (
            "financialCurrency",
            "exchange",
            "exchangeTimezoneName",
            "timeZoneFullName",
            "quoteType",
        ),
    )
    meta["statementLineItemsSource"] = "YahooFinance"
    return meta


def _statement_df_pretty_or_property(
    ticker: yf.Ticker,
    *,
    getter_name: str,
    freq: str,
    property_fallbacks: tuple[str, ...],
) -> pd.DataFrame | None:
    """Prefer documented getters with pretty row labels; fall back to ticker properties."""
    getter = getattr(ticker, getter_name, None)
    if callable(getter):
        try:
            df = getter(freq=freq, pretty=True)
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df
        except Exception:
            logger.warning(
                "yfinance %s(freq=%r, pretty=True) failed",
                getter_name,
                freq,
                exc_info=True,
            )
    for name in property_fallbacks:
        cand = getattr(ticker, name, None)
        if isinstance(cand, pd.DataFrame) and not cand.empty:
            return cand
    for name in property_fallbacks:
        cand = getattr(ticker, name, None)
        if isinstance(cand, pd.DataFrame):
            return cand
    return None


def _build_highlights(info: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "marketCap",
        "enterpriseValue",
        "profitMargins",
        "grossMargins",
        "operatingMargins",
        "ebitda",
        "totalRevenue",
        "revenueGrowth",
        "earningsGrowth",
        "revenuePerShare",
        "heldPercentInsiders",
        "heldPercentInstitutions",
        "returnOnAssets",
        "returnOnEquity",
        "mostRecentQuarter",
        "dividendRate",
        "dividendYield",
        "exDividendDate",
        "payoutRatio",
    )
    return _subset_info(info, keys)


def _build_valuation(info: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "trailingPE",
        "forwardPE",
        "pegRatio",
        "priceToBook",
        "priceToSalesTrailing12Months",
        "enterpriseToRevenue",
        "enterpriseToEbitda",
        "beta",
        "fiftyTwoWeekHigh",
        "fiftyTwoWeekLow",
        "targetMeanPrice",
        "recommendationKey",
        "numberOfAnalystOpinions",
    )
    return _subset_info(info, keys)


def _build_shares_stats(info: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "sharesOutstanding",
        "floatShares",
        "sharesShort",
        "shortRatio",
        "shortPercentOfFloat",
        "sharesPercentSharesOut",
        "impliedSharesOutstanding",
    )
    return _subset_info(info, keys)


def _build_insider_transactions(ticker: yf.Ticker) -> dict[str, dict[str, Any]]:
    try:
        df = ticker.insider_transactions
    except Exception:
        logger.warning("Could not load insider transactions.", exc_info=True)
        return {}
    if df is None or df.empty:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for i, row in df.head(40).iterrows():
        start = row.get("Start Date")
        if hasattr(start, "isoformat"):
            ds = start.isoformat()[:10]
        else:
            ds = str(start)[:10] if start is not None else None
        shares = row.get("Shares")
        value = row.get("Value")
        price = None
        if shares and pd.notna(shares) and float(shares) != 0 and value is not None and pd.notna(value):
            try:
                price = float(value) / float(shares)
            except (TypeError, ValueError, ZeroDivisionError):
                price = None
        key = str(i)
        out[key] = {
            "date": ds,
            "ownerName": row.get("Insider"),
            "transactionCode": (str(row.get("Transaction") or "")[:8] or None),
            "transactionPrice": _json_scalar(price),
            "transactionAmount": _json_scalar(shares),
        }
    return out


def _build_earnings_annual(income_stmt: pd.DataFrame | None) -> dict[str, dict[str, Any]]:
    if income_stmt is None or income_stmt.empty:
        return {}
    out: dict[str, dict[str, Any]] = {}
    cols = list(income_stmt.columns)
    try:
        cols = sorted(cols, key=lambda c: pd.Timestamp(c), reverse=True)
    except Exception:
        cols = sorted(cols, reverse=True)
    for col in cols[:20]:
        if hasattr(col, "strftime"):
            key = col.strftime("%Y-%m-%d")
        else:
            key = str(col)[:10]
        col_data = income_stmt[col]
        row: dict[str, Any] = {}
        for label in (
            "Total Revenue",
            "Net Income",
            "Diluted EPS",
            "Basic EPS",
            "EBITDA",
            "Operating Income",
            "Gross Profit",
        ):
            if label in col_data.index:
                val = col_data.loc[label]
                if pd.notna(val):
                    row[label] = _json_scalar(val)
        if row:
            out[key] = row
    return out


def _build_outstanding_shares_annual(balance_sheet: pd.DataFrame | None) -> dict[str, Any]:
    if balance_sheet is None or balance_sheet.empty:
        return {"annual": {}}
    annual: dict[str, dict[str, Any]] = {}
    cols = list(balance_sheet.columns)
    try:
        cols = sorted(cols, key=lambda c: pd.Timestamp(c), reverse=True)
    except Exception:
        cols = sorted(cols, reverse=True)
    for col in cols[:12]:
        if hasattr(col, "strftime"):
            key = col.strftime("%Y-%m-%d")
        else:
            key = str(col)[:10]
        col_data = balance_sheet[col]
        for label in ("Ordinary Shares Number", "Share Issued", "Common Stock"):
            if label in col_data.index:
                raw = col_data.loc[label]
                if pd.notna(raw):
                    try:
                        mln = float(raw) / 1_000_000.0
                    except (TypeError, ValueError):
                        mln = None
                    annual[key] = {"date": key, "sharesMln": _json_scalar(mln)}
                    break
    return {"annual": annual}


def build_fundamentals(company_symbol: str, exchange_symbol: str) -> dict[str, Any]:
    """
    Return a dict shaped fundamentals JSON for extract_data().

    Raises ValueError if the ticker cannot be resolved or Yahoo has no profile.
    """
    yahoo_ticker = resolve_yahoo_ticker(company_symbol, exchange_symbol)
    ticker = yf.Ticker(yahoo_ticker)
    try:
        info = ticker.info or {}
    except Exception as exc:
        logger.exception("yfinance Ticker.info failed for %s", yahoo_ticker)
        raise ValueError("Yahoo Finance request failed.") from exc

    if not info.get("symbol"):
        raise ValueError(f"No Yahoo Finance data for ticker {yahoo_ticker!r}.")

    balance = _statement_df_pretty_or_property(
        ticker,
        getter_name="get_balance_sheet",
        freq="yearly",
        property_fallbacks=("balance_sheet",),
    )
    q_balance = _statement_df_pretty_or_property(
        ticker,
        getter_name="get_balance_sheet",
        freq="quarterly",
        property_fallbacks=("quarterly_balance_sheet",),
    )
    income_for_earnings = getattr(ticker, "income_stmt", None)
    income = _statement_df_pretty_or_property(
        ticker,
        getter_name="get_income_stmt",
        freq="yearly",
        property_fallbacks=("income_stmt",),
    )
    q_income = _statement_df_pretty_or_property(
        ticker,
        getter_name="get_income_stmt",
        freq="quarterly",
        property_fallbacks=("quarterly_income_stmt",),
    )
    cash = _statement_df_pretty_or_property(
        ticker,
        getter_name="get_cash_flow",
        freq="yearly",
        property_fallbacks=("cash_flow", "cashflow"),
    )
    q_cash = _statement_df_pretty_or_property(
        ticker,
        getter_name="get_cash_flow",
        freq="quarterly",
        property_fallbacks=("quarterly_cash_flow", "quarterly_cashflow"),
    )

    return {
        "FundamentalsMeta": _build_fundamentals_meta(info),
        "General": _build_general(info),
        "Highlights": _build_highlights(info),
        "Valuation": _build_valuation(info),
        "SharesStats": _build_shares_stats(info),
        "InsiderTransactions": _build_insider_transactions(ticker),
        "outstandingShares": _build_outstanding_shares_annual(balance),
        "Earnings": {"Annual": _build_earnings_annual(income_for_earnings)},
        "Financials": {
            "Balance_Sheet": {
                "quarterly": _df_to_period_dict(q_balance),
                "yearly": _df_to_period_dict(balance),
            },
            "Cash_Flow": {
                "quarterly": _df_to_period_dict(q_cash),
                "yearly": _df_to_period_dict(cash),
            },
            "Income_Statement": {
                "quarterly": _df_to_period_dict(q_income),
                "yearly": _df_to_period_dict(income),
            },
        },
    }
