from typing import Any


def safe_first_n_from_dict(data_dict, n):
    """
    Zwraca pierwsze n elementów ze słownika (po values),
    zachowując kolejność API.
    """
    if not isinstance(data_dict, dict):
        return []
    return list(data_dict.values())[:n]


def _omit_none_values(row: dict[str, Any]) -> dict[str, Any]:
    """Drop keys with value None to keep LLM payloads compact."""
    return {k: v for k, v in row.items() if v is not None}


def _period_key_str(period_key: Any) -> str:
    if hasattr(period_key, "strftime"):
        return period_key.strftime("%Y-%m-%d")
    s = str(period_key)
    return s[:10] if len(s) >= 10 else s


def _first_n_period_rows(period_dict: Any, n: int) -> list[dict[str, Any]]:
    """
    Build rows with leading ``date`` from a period-keyed dict (newest-first order preserved).
    """
    if not isinstance(period_dict, dict) or n <= 0:
        return []
    rows: list[dict[str, Any]] = []
    for period_key, metrics in list(period_dict.items())[:n]:
        if not isinstance(metrics, dict):
            continue
        pk = _period_key_str(period_key)
        row: dict[str, Any] = {"date": pk}
        for k, v in metrics.items():
            if k == "date":
                continue
            row[str(k)] = v
        rows.append(_omit_none_values(row))
    return rows


def extract_data(json_data):
    general = json_data.get("General", {})

    meta_raw = json_data.get("FundamentalsMeta")
    fundamentals_meta = meta_raw if isinstance(meta_raw, dict) else {}

    extracted = {
        # ===== GENERAL =====
        "General": {
            "Code": general.get("Code"),
            "Name": general.get("Name"),
            "Exchange": general.get("Exchange"),
            "CurrencyCode": general.get("CurrencyCode"),
            "CountryName": general.get("CountryName"),
            "FiscalYearEnd": general.get("FiscalYearEnd"),
            "Sector": general.get("Sector"),
            "Industry": general.get("Industry"),
            "Description": general.get("Description"),
            "WebURL": general.get("WebURL"),
            "FullTimeEmployees": general.get("FullTimeEmployees"),
        },
        "FundamentalsMeta": fundamentals_meta,
    }

    # ===== OFFICERS =====
    officers_dict = general.get("Officers", {})
    if isinstance(officers_dict, dict):
        extracted["General"]["Officers"] = [
            {
                "Name": officer.get("Name"),
                "Title": officer.get("Title"),
            }
            for officer in officers_dict.values()
        ]
    else:
        extracted["General"]["Officers"] = []

    # ===== HIGHLIGHTS / VALUATION / SHARESTATS =====
    extracted["Highlights"] = json_data.get("Highlights", {})
    extracted["Valuation"] = json_data.get("Valuation", {})
    extracted["SharesStats"] = json_data.get("SharesStats", {})

    # ===== INSIDER TRANSACTIONS (first 20) =====
    insider = json_data.get("InsiderTransactions", {})
    if isinstance(insider, dict):
        insider_values = list(insider.values())[:20]
    elif isinstance(insider, list):
        insider_values = insider[:20]
    else:
        insider_values = []

    extracted["InsiderTransactions"] = [
        _omit_none_values(
            {
                "date": tx.get("date"),
                "ownerName": tx.get("ownerName"),
                "transactionCode": tx.get("transactionCode"),
                "transactionPrice": tx.get("transactionPrice"),
                "transactionAmount": tx.get("transactionAmount"),
            }
        )
        for tx in insider_values
        if isinstance(tx, dict)
    ]

    # ===== OUTSTANDING SHARES → annual (first 10) =====
    outstanding = json_data.get("outstandingShares", {})
    annual_shares = outstanding.get("annual", {})

    extracted["OutstandingSharesAnnual"] = [
        _omit_none_values(
            {
                "date": item.get("date"),
                "sharesMln": item.get("sharesMln"),
            }
        )
        for item in safe_first_n_from_dict(annual_shares, 10)
        if isinstance(item, dict)
    ]

    # ===== EARNINGS → Annual (first 15) =====
    earnings = json_data.get("Earnings", {}).get("Annual", {})
    extracted["EarningsAnnual"] = _first_n_period_rows(earnings, 15)

    # ===== FINANCIALS =====
    financials = json_data.get("Financials", {})

    # Balance Sheet
    balance_sheet = financials.get("Balance_Sheet", {})
    extracted["BalanceSheetQuarterly"] = _first_n_period_rows(
        balance_sheet.get("quarterly", {}), 6
    )
    extracted["BalanceSheetYearly"] = _first_n_period_rows(
        balance_sheet.get("yearly", {}), 5
    )

    # Cash Flow
    cash_flow = financials.get("Cash_Flow", {})
    extracted["CashFlowQuarterly"] = _first_n_period_rows(
        cash_flow.get("quarterly", {}), 6
    )
    extracted["CashFlowYearly"] = _first_n_period_rows(
        cash_flow.get("yearly", {}), 5
    )

    # Income Statement
    income_statement = financials.get("Income_Statement", {})
    extracted["IncomeStatementQuarterly"] = _first_n_period_rows(
        income_statement.get("quarterly", {}), 6
    )
    extracted["IncomeStatementYearly"] = _first_n_period_rows(
        income_statement.get("yearly", {}), 5
    )

    return extracted
