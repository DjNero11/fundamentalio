import logging
from typing import Any

logger = logging.getLogger(__name__)


class FundamentalsAPIError(Exception):
    """Raised when company fundamentals could not be fetched from Yahoo Finance."""


def fetch_fundamentals(
    company_symbol: str,
    exchange_symbol: str,
    api_token: str | None = None,
) -> dict[str, Any]:
    """
    Load fundamentals-shaped data for extract_data() via yfinance.

    The ``api_token`` parameter is retained only for backward compatibility with Financial data API; it is ignored here since we are using Yahoo Finance.
    """
    del api_token 
    try:
        from fundamentalio.services.research_helpers.shared import (
            yfinance_fundamentals as yf_fun,
        )

        return yf_fun.build_fundamentals(company_symbol, exchange_symbol)
    except ValueError as exc:
        logger.error(
            "Yahoo Finance fundamentals lookup failed.",
            extra={"company_symbol": company_symbol, "exchange_symbol": exchange_symbol},
        )
        raise FundamentalsAPIError(str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "Unexpected error fetching Yahoo Finance fundamentals.",
            extra={"company_symbol": company_symbol, "exchange_symbol": exchange_symbol},
        )
        raise FundamentalsAPIError("Yahoo Finance fundamentals request failed.") from exc
