from __future__ import annotations

import csv
import io
import json
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests


HEADERS = {
    "User-Agent": "StockReportR101/1.0.1 contact@example.com",
    "Accept": "application/json,text/csv,text/html;q=0.9,*/*;q=0.8",
}

TWSE_BASE = "https://openapi.twse.com.tw/v1"
TPEX_BASE = "https://www.tpex.org.tw/openapi/v1"

GROWTH_NEWS_TERMS = (
    "營收",
    "展望",
    "成長",
    "獲利",
    "毛利",
    "訂單",
    "接單",
    "需求",
    "庫存",
    "降價",
    "砍單",
    "AI",
    "revenue",
    "sales",
    "earnings",
    "guidance",
    "outlook",
    "forecast",
    "growth",
    "margin",
    "demand",
    "backlog",
    "inventory",
    "pricing",
)


@dataclass
class SourceRecord:
    label: str
    url: str
    status: str = "ok"
    detail: str | None = None


@dataclass
class StockLookupResult:
    user_input: str
    symbol: str | None
    company_name: str | None
    market: str | None
    metrics: dict[str, Any] = field(default_factory=dict)
    news: list[dict[str, Any]] = field(default_factory=list)
    sources: list[SourceRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_storage_metrics(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "company_name": self.company_name,
            "market": self.market,
            "metrics": self.metrics,
            "news": self.news,
            "warnings": self.warnings,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def to_storage_sources(self) -> list[dict[str, Any]]:
        return [source.__dict__ for source in self.sources]


class DataSourceError(RuntimeError):
    pass


def lookup_stock(user_input: str) -> StockLookupResult:
    query = user_input.strip()
    if not query:
        raise DataSourceError("請輸入股號、股名或美股 ticker。")

    if _looks_like_us_ticker(query):
        return lookup_us_stock(query.upper())

    taiwan_result = lookup_taiwan_stock(query)
    if taiwan_result.symbol:
        return taiwan_result

    if re.fullmatch(r"[A-Za-z.]{1,10}", query):
        return lookup_us_stock(query.upper())

    taiwan_result.warnings.append("無法辨識市場；請改用台股股號或美股 ticker。")
    return taiwan_result


def lookup_taiwan_stock(query: str) -> StockLookupResult:
    result = StockLookupResult(user_input=query, symbol=None, company_name=None, market=None)

    listed = _match_twse_company(query)
    otc = _match_tpex_company(query)

    if listed and otc:
        result.warnings.append("同時在上市與上櫃資料中找到相近公司，已優先使用上市資料。")

    if listed:
        return _build_twse_result(query, listed)
    if otc:
        return _build_tpex_result(query, otc)

    result.warnings.append("查不到對應的上市或上櫃公司。")
    return result


def lookup_us_stock(ticker: str) -> StockLookupResult:
    ticker = ticker.upper().strip()
    result = StockLookupResult(
        user_input=ticker,
        symbol=ticker,
        company_name=None,
        market="US",
    )

    cik_info = _get_sec_cik_for_ticker(ticker)
    if cik_info:
        result.company_name = cik_info.get("title")
        result.sources.append(
            SourceRecord("SEC ticker mapping", "https://www.sec.gov/files/company_tickers.json")
        )
        submission = _fetch_sec_submission(cik_info["cik_str"])
        if submission:
            result.sources.append(
                SourceRecord(
                    "SEC company submission",
                    f"https://data.sec.gov/submissions/CIK{int(cik_info['cik_str']):010d}.json",
                )
            )
            result.metrics.update(_parse_sec_company_profile(submission))
        facts = _fetch_sec_companyfacts(cik_info["cik_str"])
        if facts:
            result.sources.append(
                SourceRecord(
                    "SEC company facts",
                    f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik_info['cik_str']):010d}.json",
                )
            )
            sec_metrics = _parse_sec_metrics(facts)
            result.metrics.update(sec_metrics)
        else:
            result.warnings.append("SEC companyfacts 查無可用財報資料。")
    else:
        result.warnings.append("SEC ticker mapping 查無此 ticker，可能是非美國申報公司或代號輸入錯誤。")

    profile = _fetch_yfinance_profile(ticker)
    if profile:
        result.sources.append(SourceRecord("Yahoo Finance profile", f"yfinance:{ticker}"))
        result.metrics.update(profile)
        result.company_name = result.company_name or profile.get("company_name")

    quote = _fetch_yahoo_chart_quote(ticker)
    if quote:
        result.sources.append(
            SourceRecord("Yahoo chart quote", f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}")
        )
        result.metrics.update(quote)
        result.company_name = result.company_name or quote.get("company_name")
    else:
        result.warnings.append("查不到即時/近期股價，實質本益比可能無法推算。")

    _finalize_valuation_metrics(result)
    result.news = _fetch_yahoo_rss_news(ticker)
    if result.news:
        result.sources.append(
            SourceRecord("Yahoo Finance RSS", f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}")
        )
    return result


def _build_twse_result(query: str, company: dict[str, Any]) -> StockLookupResult:
    symbol = company.get("公司代號")
    name = company.get("公司簡稱") or company.get("公司名稱")
    result = StockLookupResult(
        user_input=query,
        symbol=symbol,
        company_name=name,
        market="TWSE",
        metrics={
            "industry": company.get("產業別"),
            "full_company_name": company.get("公司名稱"),
        },
        sources=[
            SourceRecord("上市公司基本資料", f"{TWSE_BASE}/opendata/t187ap03_L"),
        ],
    )

    revenue = _find_by_code(_get_json(f"{TWSE_BASE}/opendata/t187ap05_L"), "公司代號", symbol)
    result.sources.append(SourceRecord("上市公司每月營業收入", f"{TWSE_BASE}/opendata/t187ap05_L"))
    if revenue:
        result.metrics.update(_parse_tw_revenue(revenue))
        result.metrics["industry"] = revenue.get("產業別") or result.metrics.get("industry")
    else:
        result.warnings.append("查不到最新月營收。")

    eps = _find_by_code(_get_json(f"{TWSE_BASE}/opendata/t187ap14_L"), "公司代號", symbol)
    result.sources.append(SourceRecord("上市公司 EPS 統計", f"{TWSE_BASE}/opendata/t187ap14_L"))
    if eps:
        result.metrics.update(_parse_tw_eps(eps, market="TWSE"))
    else:
        result.warnings.append("查不到最新單季 EPS。")

    income = _find_income_statement_twse(symbol)
    if income:
        result.metrics.update(_parse_income_quality(income))
    else:
        result.warnings.append("查不到最新損益表細項，毛利率/營益率可能不完整。")

    pe_row = _find_by_code(_get_json(f"{TWSE_BASE}/exchangeReport/BWIBBU_ALL"), "Code", symbol)
    result.sources.append(SourceRecord("上市個股本益比", f"{TWSE_BASE}/exchangeReport/BWIBBU_ALL"))
    if pe_row:
        result.metrics.update(
            {
                "official_pe": _to_float(pe_row.get("PEratio")),
                "dividend_yield": _to_float(pe_row.get("DividendYield")),
                "pb_ratio": _to_float(pe_row.get("PBratio")),
                "valuation_date": _roc_date_to_iso(pe_row.get("Date")),
            }
        )

    price_row = _find_by_code(_get_json(f"{TWSE_BASE}/exchangeReport/STOCK_DAY_ALL"), "Code", symbol)
    result.sources.append(SourceRecord("上市個股日成交資訊", f"{TWSE_BASE}/exchangeReport/STOCK_DAY_ALL"))
    if price_row:
        result.metrics.update(_parse_twse_price(price_row))

    result.news = _fetch_tw_major_news(symbol, market="TWSE")
    if result.news:
        result.sources.append(SourceRecord("上市公司重大訊息", f"{TWSE_BASE}/opendata/t187ap04_L"))

    _finalize_valuation_metrics(result)
    return result


def _build_tpex_result(query: str, company: dict[str, Any]) -> StockLookupResult:
    symbol = company.get("SecuritiesCompanyCode")
    name = company.get("CompanyAbbreviation") or company.get("CompanyName")
    result = StockLookupResult(
        user_input=query,
        symbol=symbol,
        company_name=name,
        market="TPEX",
        metrics={
            "industry": company.get("SecuritiesIndustryCode"),
            "full_company_name": company.get("CompanyName"),
        },
        sources=[
            SourceRecord("上櫃公司基本資料", f"{TPEX_BASE}/mopsfin_t187ap03_O"),
        ],
    )

    revenue = _find_by_code(_get_json(f"{TPEX_BASE}/mopsfin_t187ap05_O"), "公司代號", symbol)
    result.sources.append(SourceRecord("上櫃公司每月營業收入", f"{TPEX_BASE}/mopsfin_t187ap05_O"))
    if revenue:
        result.metrics.update(_parse_tw_revenue(revenue))
        result.metrics["industry"] = revenue.get("產業別") or result.metrics.get("industry")
    else:
        result.warnings.append("查不到最新月營收。")

    eps = _find_by_code(_get_json(f"{TPEX_BASE}/mopsfin_t187ap14_O"), "SecuritiesCompanyCode", symbol)
    result.sources.append(SourceRecord("上櫃公司 EPS 統計", f"{TPEX_BASE}/mopsfin_t187ap14_O"))
    if eps:
        result.metrics.update(_parse_tw_eps(eps, market="TPEX"))
    else:
        result.warnings.append("查不到最新單季 EPS。")

    income = _find_income_statement_tpex(symbol)
    if income:
        result.metrics.update(_parse_income_quality(income))
    else:
        result.warnings.append("查不到最新損益表細項，毛利率/營益率可能不完整。")

    pe_row = _find_by_code(_get_json(f"{TPEX_BASE}/tpex_mainboard_peratio_analysis"), "SecuritiesCompanyCode", symbol)
    result.sources.append(SourceRecord("上櫃個股本益比", f"{TPEX_BASE}/tpex_mainboard_peratio_analysis"))
    if pe_row:
        result.metrics.update(
            {
                "official_pe": _to_float(pe_row.get("PriceEarningRatio")),
                "dividend_yield": _to_float(pe_row.get("YieldRatio")),
                "pb_ratio": _to_float(pe_row.get("PriceBookRatio")),
                "valuation_date": _roc_date_to_iso(pe_row.get("Date")),
            }
        )

    price_row = _find_by_code(
        _get_json(f"{TPEX_BASE}/tpex_mainboard_daily_close_quotes"),
        "SecuritiesCompanyCode",
        symbol,
    )
    result.sources.append(SourceRecord("上櫃股票行情", f"{TPEX_BASE}/tpex_mainboard_daily_close_quotes"))
    if price_row:
        result.metrics.update(_parse_tpex_price(price_row))

    result.news = _fetch_tw_major_news(symbol, market="TPEX")
    if result.news:
        result.sources.append(SourceRecord("上櫃公司重大訊息", f"{TPEX_BASE}/mopsfin_t187ap04_O"))

    _finalize_valuation_metrics(result)
    return result


_CACHE: dict[str, tuple[float, Any]] = {}


def _get_json(url: str, ttl_seconds: int = 900) -> Any:
    cached = _CACHE.get(url)
    if cached and time.time() - cached[0] < ttl_seconds:
        return cached[1]
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    data = response.json()
    _CACHE[url] = (time.time(), data)
    return data


def _get_text(url: str, ttl_seconds: int = 900) -> str:
    cached = _CACHE.get(url)
    if cached and time.time() - cached[0] < ttl_seconds:
        return cached[1]
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    text = response.text
    _CACHE[url] = (time.time(), text)
    return text


def _match_twse_company(query: str) -> dict[str, Any] | None:
    rows = _get_json(f"{TWSE_BASE}/opendata/t187ap03_L")
    return _match_company(rows, query, code_key="公司代號", name_keys=("公司簡稱", "公司名稱"))


def _match_tpex_company(query: str) -> dict[str, Any] | None:
    rows = _get_json(f"{TPEX_BASE}/mopsfin_t187ap03_O")
    return _match_company(
        rows,
        query,
        code_key="SecuritiesCompanyCode",
        name_keys=("CompanyAbbreviation", "CompanyName"),
    )


def _match_company(
    rows: list[dict[str, Any]],
    query: str,
    code_key: str,
    name_keys: tuple[str, ...],
) -> dict[str, Any] | None:
    query_norm = query.strip().lower()
    for row in rows:
        if str(row.get(code_key, "")).strip().lower() == query_norm:
            return row
    for row in rows:
        names = [str(row.get(key, "")).strip().lower() for key in name_keys]
        if query_norm in names:
            return row
    for row in rows:
        names = [str(row.get(key, "")).strip().lower() for key in name_keys]
        if any(query_norm and query_norm in name for name in names):
            return row
    return None


def _find_by_code(rows: list[dict[str, Any]], key: str, symbol: str | None) -> dict[str, Any] | None:
    if not symbol:
        return None
    for row in rows:
        if str(row.get(key, "")).strip() == str(symbol).strip():
            return row
    return None


def _parse_tw_revenue(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "revenue_period": _roc_month_to_iso(row.get("資料年月")),
        "monthly_revenue": _to_float(row.get("營業收入-當月營收")),
        "monthly_revenue_yoy_pct": _to_float(row.get("營業收入-去年同月增減(%)")),
        "monthly_revenue_mom_pct": _to_float(row.get("營業收入-上月比較增減(%)")),
        "cumulative_revenue": _to_float(row.get("累計營業收入-當月累計營收")),
        "cumulative_revenue_yoy_pct": _to_float(row.get("累計營業收入-前期比較增減(%)")),
        "revenue_note": row.get("備註"),
    }


def _parse_tw_eps(row: dict[str, Any], market: str) -> dict[str, Any]:
    year = row.get("年度") or row.get("Year")
    season = row.get("季別") or row.get("Season")
    eps = row.get("基本每股盈餘(元)") if market == "TWSE" else row.get("基本每股盈餘")
    return {
        "latest_eps_period": _roc_quarter_to_label(year, season),
        "latest_quarter_eps": _to_float(eps),
        "quarter_revenue": _to_float(row.get("營業收入")),
        "quarter_operating_profit": _to_float(row.get("營業利益")),
        "quarter_net_income": _to_float(row.get("稅後淨利")),
    }


def _parse_income_quality(row: dict[str, Any]) -> dict[str, Any]:
    revenue = _to_float(row.get("營業收入"))
    gross_profit = _to_float(row.get("營業毛利（毛損）淨額") or row.get("營業毛利（毛損）"))
    operating_profit = _to_float(row.get("營業利益（損失）"))
    return {
        "gross_margin_pct": _safe_pct(gross_profit, revenue),
        "operating_margin_pct": _safe_pct(operating_profit, revenue),
    }


def _parse_twse_price(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "price_date": _roc_date_to_iso(row.get("Date")),
        "latest_close": _to_float(row.get("ClosingPrice") or row.get("Close") or row.get("TradeValue")),
        "daily_change": _to_float(row.get("Change")),
        "trading_volume": _to_float(row.get("TradeVolume")),
    }


def _parse_tpex_price(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "price_date": _roc_date_to_iso(row.get("Date")),
        "latest_close": _to_float(row.get("Close")),
        "daily_change": _to_float(row.get("Change")),
        "trading_volume": _to_float(row.get("TradingShares")),
    }


def _find_income_statement_twse(symbol: str | None) -> dict[str, Any] | None:
    if not symbol:
        return None
    endpoints = (
        "t187ap06_L_ci",
        "t187ap06_L_basi",
        "t187ap06_L_bd",
        "t187ap06_L_fh",
        "t187ap06_L_ins",
        "t187ap06_L_mim",
    )
    for endpoint in endpoints:
        row = _find_by_code(_get_json(f"{TWSE_BASE}/opendata/{endpoint}"), "公司代號", symbol)
        if row:
            return row
    return None


def _find_income_statement_tpex(symbol: str | None) -> dict[str, Any] | None:
    if not symbol:
        return None
    endpoints = (
        "mopsfin_t187ap06_O_ci",
        "mopsfin_t187ap06_O_basi",
        "mopsfin_t187ap06_O_bd",
        "mopsfin_t187ap06_O_fh",
        "mopsfin_t187ap06_O_ins",
        "mopsfin_t187ap06_O_mim",
    )
    for endpoint in endpoints:
        row = _find_by_code(_get_json(f"{TPEX_BASE}/{endpoint}"), "SecuritiesCompanyCode", symbol)
        if row:
            return row
    return None


def _fetch_tw_major_news(symbol: str | None, market: str) -> list[dict[str, Any]]:
    if not symbol:
        return []
    url = f"{TWSE_BASE}/opendata/t187ap04_L" if market == "TWSE" else f"{TPEX_BASE}/mopsfin_t187ap04_O"
    try:
        rows = _get_json(url)
    except Exception:
        return []

    code_key = "公司代號" if market == "TWSE" else "SecuritiesCompanyCode"
    title_key = "主旨 " if market == "TWSE" else "Subject"
    date_key = "發言日期" if market == "TWSE" else "Date"
    detail_key = "說明" if market == "TWSE" else "Description"
    matched = []
    for row in rows:
        if str(row.get(code_key, "")).strip() != str(symbol):
            continue
        text = f"{row.get(title_key, '')} {row.get(detail_key, '')}"
        if not _mentions_growth_topic(text):
            continue
        matched.append(
            {
                "date": _roc_date_to_iso(row.get(date_key)),
                "title": str(row.get(title_key, "")).strip(),
                "summary": _compact_text(str(row.get(detail_key, "")).strip(), max_len=240),
                "source": "重大訊息",
            }
        )
    return matched[:5]


def _get_sec_cik_for_ticker(ticker: str) -> dict[str, Any] | None:
    try:
        mapping = _get_json("https://www.sec.gov/files/company_tickers.json", ttl_seconds=86400)
    except Exception:
        return None
    for item in mapping.values():
        if str(item.get("ticker", "")).upper() == ticker:
            return item
    return None


def _fetch_sec_companyfacts(cik: int | str) -> dict[str, Any] | None:
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json"
    try:
        return _get_json(url, ttl_seconds=3600)
    except Exception:
        return None


def _parse_sec_metrics(facts: dict[str, Any]) -> dict[str, Any]:
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    revenue_points = _latest_quarter_points(us_gaap, (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ), preferred_units=("USD",))
    eps_points = _latest_quarter_points(us_gaap, (
        "EarningsPerShareDiluted",
        "EarningsPerShareBasic",
    ), preferred_units=("USD/shares", "USD/shares"))

    metrics: dict[str, Any] = {}
    if revenue_points:
        latest_revenue = revenue_points[-1]
        comparable = _same_quarter_previous_year(revenue_points, latest_revenue)
        revenue_yoy_series = _latest_yoy_series(revenue_points, count=4)
        metrics.update(
            {
                "latest_revenue_period": latest_revenue.get("frame") or latest_revenue.get("end"),
                "latest_quarter_revenue": _to_float(latest_revenue.get("val")),
                "latest_quarter_revenue_yoy_pct": _growth_pct(latest_revenue, comparable),
                "revenue_yoy_series": revenue_yoy_series,
                "revenue_yoy_trend": _series_trend([item.get("yoy_pct") for item in revenue_yoy_series]),
                "ttm_revenue": _sum_last_values(revenue_points, 4),
            }
        )
    if eps_points:
        latest_eps = eps_points[-1]
        metrics.update(
            {
                "latest_eps_period": latest_eps.get("frame") or latest_eps.get("end"),
                "latest_quarter_eps": _to_float(latest_eps.get("val")),
                "ttm_eps": _sum_last_values(eps_points, 4),
            }
        )

    latest_revenue_value = _to_float((revenue_points[-1] if revenue_points else {}).get("val"))
    gross_profit = _latest_quarter_value(us_gaap, ("GrossProfit",))
    gross_profit_points = _latest_quarter_points(us_gaap, ("GrossProfit",), preferred_units=("USD",))
    operating_income = _latest_quarter_value(us_gaap, ("OperatingIncomeLoss",))
    net_income = _latest_quarter_value(us_gaap, ("NetIncomeLoss",))
    r_and_d = _latest_quarter_value(us_gaap, ("ResearchAndDevelopmentExpense",))

    if latest_revenue_value:
        gross_margin = _safe_pct(gross_profit, latest_revenue_value)
        previous_gross_margin = _previous_same_quarter_margin(revenue_points, gross_profit_points)
        metrics.update(
            {
                "gross_margin_pct": gross_margin,
                "gross_margin_yoy_change_points": (
                    gross_margin - previous_gross_margin
                    if gross_margin is not None and previous_gross_margin is not None
                    else None
                ),
                "operating_margin_pct": _safe_pct(operating_income, latest_revenue_value),
                "net_margin_pct": _safe_pct(net_income, latest_revenue_value),
                "research_development_ratio_pct": _safe_pct(r_and_d, latest_revenue_value),
            }
        )
    return metrics


def _fetch_sec_submission(cik: int | str) -> dict[str, Any] | None:
    url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
    try:
        return _get_json(url, ttl_seconds=3600)
    except Exception:
        return None


def _parse_sec_company_profile(submission: dict[str, Any]) -> dict[str, Any]:
    exchanges = submission.get("exchanges") or []
    tickers = submission.get("tickers") or []
    return {
        "sec_company_name": submission.get("name"),
        "sec_sic": submission.get("sic"),
        "sec_sic_description": submission.get("sicDescription"),
        "filer_category": submission.get("category"),
        "entity_type": submission.get("entityType"),
        "exchange": exchanges[0] if exchanges else None,
        "sec_ticker": tickers[0] if tickers else None,
    }


def _fetch_yfinance_profile(ticker: str) -> dict[str, Any] | None:
    try:
        import yfinance as yf

        info = yf.Ticker(ticker).info or {}
    except Exception:
        return None
    if not info:
        return None
    return {
        "company_name": info.get("longName") or info.get("shortName"),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "business_summary": _compact_text(info.get("longBusinessSummary") or "", 900),
        "website": info.get("website"),
        "market_cap": _to_float(info.get("marketCap")),
        "enterprise_value": _to_float(info.get("enterpriseValue")),
        "trailing_pe": _to_float(info.get("trailingPE")),
        "forward_pe": _to_float(info.get("forwardPE")),
        "price_to_sales_ttm": _to_float(info.get("priceToSalesTrailing12Months")),
        "yahoo_gross_margin_pct": _ratio_to_pct(info.get("grossMargins")),
        "yahoo_operating_margin_pct": _ratio_to_pct(info.get("operatingMargins")),
        "revenue_growth_pct": _ratio_to_pct(info.get("revenueGrowth")),
        "earnings_growth_pct": _ratio_to_pct(info.get("earningsGrowth")),
        "free_cashflow": _to_float(info.get("freeCashflow")),
        "fifty_two_week_high": _to_float(info.get("fiftyTwoWeekHigh")),
        "fifty_two_week_low": _to_float(info.get("fiftyTwoWeekLow")),
    }


def _latest_quarter_points(
    us_gaap: dict[str, Any],
    tags: tuple[str, ...],
    preferred_units: tuple[str, ...],
) -> list[dict[str, Any]]:
    best_points: list[dict[str, Any]] = []
    best_end = ""
    for tag in tags:
        fact = us_gaap.get(tag)
        if not fact:
            continue
        units = fact.get("units", {})
        for unit in preferred_units:
            values = units.get(unit)
            if not values:
                continue
            points = [
                item
                for item in values
                if item.get("form") in {"10-Q", "10-K"}
                and item.get("frame")
                and re.fullmatch(r"CY\d{4}Q[1-4]", str(item.get("frame")))
                and _to_float(item.get("val")) is not None
            ]
            points.sort(key=lambda item: str(item.get("end") or item.get("frame")))
            if points and str(points[-1].get("end") or "") > best_end:
                best_points = points
                best_end = str(points[-1].get("end") or "")
    return best_points


def _latest_quarter_value(us_gaap: dict[str, Any], tags: tuple[str, ...]) -> float | None:
    points = _latest_quarter_points(us_gaap, tags, preferred_units=("USD",))
    if not points:
        return None
    return _to_float(points[-1].get("val"))


def _same_quarter_previous_year(points: list[dict[str, Any]], latest: dict[str, Any]) -> dict[str, Any] | None:
    frame = str(latest.get("frame", ""))
    match = re.fullmatch(r"CY(\d{4})Q([1-4])", frame)
    if not match:
        return None
    target = f"CY{int(match.group(1)) - 1}Q{match.group(2)}"
    for item in points:
        if item.get("frame") == target:
            return item
    return None


def _latest_yoy_series(points: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    series = []
    for point in points:
        previous = _same_quarter_previous_year(points, point)
        yoy = _growth_pct(point, previous)
        if yoy is None:
            continue
        series.append(
            {
                "period": point.get("frame") or point.get("end"),
                "value": _to_float(point.get("val")),
                "yoy_pct": yoy,
            }
        )
    return series[-count:]


def _series_trend(values: list[Any]) -> str | None:
    clean = [_to_float(value) for value in values]
    clean = [value for value in clean if value is not None]
    if len(clean) < 3:
        return None
    if all(later > earlier for earlier, later in zip(clean, clean[1:])):
        return "連續放大"
    if all(later < earlier for earlier, later in zip(clean, clean[1:])):
        return "連續降溫"
    return "震盪"


def _previous_same_quarter_margin(
    revenue_points: list[dict[str, Any]],
    gross_profit_points: list[dict[str, Any]],
) -> float | None:
    if not revenue_points or not gross_profit_points:
        return None
    latest_revenue = revenue_points[-1]
    previous_revenue = _same_quarter_previous_year(revenue_points, latest_revenue)
    if not previous_revenue:
        return None
    previous_gross = None
    for point in gross_profit_points:
        if point.get("frame") == previous_revenue.get("frame"):
            previous_gross = point
            break
    return _safe_pct(_to_float((previous_gross or {}).get("val")), _to_float(previous_revenue.get("val")))


def _fetch_yahoo_chart_quote(ticker: str) -> dict[str, Any] | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=1y&interval=1d"
    try:
        data = _get_json(url, ttl_seconds=300)
    except Exception:
        return None
    result = (data.get("chart", {}).get("result") or [None])[0]
    if not result:
        return None
    meta = result.get("meta", {})
    closes = [
        _to_float(value)
        for value in ((result.get("indicators", {}).get("quote") or [{}])[0].get("close") or [])
    ]
    closes = [value for value in closes if value is not None]
    latest_close = _to_float(meta.get("regularMarketPrice") or meta.get("chartPreviousClose"))
    year_high = max(closes) if closes else None
    return {
        "latest_close": latest_close,
        "price_date": datetime.now(timezone.utc).date().isoformat(),
        "currency": meta.get("currency"),
        "exchange": meta.get("exchangeName"),
        "company_name": meta.get("longName") or meta.get("shortName"),
        "one_year_high": year_high,
        "near_one_year_high": bool(latest_close and year_high and latest_close >= year_high * 0.95),
        "drawdown_from_one_year_high_pct": (
            (latest_close - year_high) / year_high * 100
            if latest_close is not None and year_high
            else None
        ),
    }


def _fetch_yahoo_rss_news(ticker: str) -> list[dict[str, Any]]:
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
    try:
        text = _get_text(url, ttl_seconds=900)
        root = ET.fromstring(text)
    except Exception:
        return []

    items = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        description = (item.findtext("description") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        combined = f"{title} {description}"
        if not _mentions_growth_topic(combined):
            continue
        items.append(
            {
                "date": pub_date,
                "title": title,
                "summary": _compact_text(description, 260),
                "url": link,
                "source": "Yahoo Finance RSS",
            }
        )
    return items[:5]


def _finalize_valuation_metrics(result: StockLookupResult) -> None:
    latest_close = _to_float(result.metrics.get("latest_close"))
    official_pe = _first_float(result.metrics, "official_pe", "trailing_pe")
    ttm_eps = _to_float(result.metrics.get("ttm_eps"))

    if ttm_eps is None and latest_close and official_pe and official_pe > 0:
        result.metrics["ttm_eps"] = latest_close / official_pe
        result.metrics["ttm_eps_method"] = "由最新收盤價與官方本益比反推"
        ttm_eps = result.metrics["ttm_eps"]

    if official_pe:
        result.metrics["effective_pe"] = official_pe
        result.metrics["effective_pe_method"] = "官方/資料源揭露本益比"
    elif latest_close and ttm_eps and ttm_eps > 0:
        result.metrics["effective_pe"] = latest_close / ttm_eps
        result.metrics["effective_pe_method"] = "最新收盤價 / TTM EPS"
    elif ttm_eps is not None and ttm_eps <= 0:
        result.metrics["effective_pe"] = None
        result.metrics["effective_pe_method"] = "TTM EPS 為負，本益比不適用"
        result.warnings.append("TTM EPS 為負，本益比不適用；應改看營收成長、毛利率、營益率與現金流。")
    else:
        result.metrics["effective_pe"] = None
        result.warnings.append("缺少正數 TTM EPS 或收盤價，無法推算實質本益比。")

    _add_risk_flags(result)


def _add_risk_flags(result: StockLookupResult) -> None:
    metrics = result.metrics
    flags = []
    revenue_yoy = _first_float(metrics, "monthly_revenue_yoy_pct", "latest_quarter_revenue_yoy_pct")
    cumulative_yoy = _to_float(metrics.get("cumulative_revenue_yoy_pct"))
    eps = _to_float(metrics.get("latest_quarter_eps"))
    pe = _to_float(metrics.get("effective_pe"))
    gross_margin = _to_float(metrics.get("gross_margin_pct"))
    operating_margin = _to_float(metrics.get("operating_margin_pct"))
    price_to_sales = _to_float(metrics.get("price_to_sales_ttm"))

    if revenue_yoy is None:
        flags.append("營收年增率不清楚")
    elif revenue_yoy < 0:
        flags.append("營收年增率為負")
    elif cumulative_yoy is not None and revenue_yoy > 20 and cumulative_yoy < 5:
        flags.append("單月跳升但累計不強，疑似低基期或短單")
    elif revenue_yoy > 20:
        flags.append("營收動能偏強")

    if eps is None:
        flags.append("最新 EPS 不清楚")
    elif eps <= 0:
        flags.append("最新 EPS 非正數")

    if pe is None:
        flags.append("估值無法判斷")
    elif pe > 60:
        flags.append("實質本益比極高")
    elif pe > 35:
        flags.append("實質本益比偏高")

    if gross_margin is not None and gross_margin < 15:
        flags.append("毛利率偏薄")
    if operating_margin is not None and operating_margin < 5:
        flags.append("營益率偏薄")
    if price_to_sales is not None and price_to_sales > 15:
        flags.append("PS 估值偏高")
    if metrics.get("near_one_year_high") and eps is not None and eps <= 0:
        flags.append("股價接近一年高但 EPS 仍非正數，股價與獲利背離")

    metrics["risk_flags"] = flags


def _mentions_growth_topic(text: str) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in GROWTH_NEWS_TERMS)


def _looks_like_us_ticker(query: str) -> bool:
    cleaned = query.strip()
    return bool(re.fullmatch(r"[A-Za-z]{1,5}([.-][A-Za-z]{1,2})?", cleaned))


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "N/A", "nan", "None"}:
        return None
    text = text.replace("+", "")
    try:
        number = float(text)
    except ValueError:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _ratio_to_pct(value: Any) -> float | None:
    number = _to_float(value)
    if number is None:
        return None
    return number * 100


def _first_float(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _to_float(data.get(key))
        if value is not None:
            return value
    return None


def _safe_pct(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator * 100


def _growth_pct(latest: dict[str, Any], previous: dict[str, Any] | None) -> float | None:
    latest_value = _to_float(latest.get("val"))
    previous_value = _to_float(previous.get("val")) if previous else None
    if latest_value is None or previous_value in (None, 0):
        return None
    return (latest_value - previous_value) / abs(previous_value) * 100


def _sum_last_values(points: list[dict[str, Any]], count: int) -> float | None:
    values = [_to_float(item.get("val")) for item in points[-count:]]
    if len(values) < count or any(value is None for value in values):
        return None
    return float(sum(value for value in values if value is not None))


def _roc_month_to_iso(value: Any) -> str | None:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{5}", text):
        return None
    return f"{int(text[:3]) + 1911}-{text[3:5]}"


def _roc_date_to_iso(value: Any) -> str | None:
    text = str(value or "").strip().replace("/", "")
    if not re.fullmatch(r"\d{7}", text):
        return None
    return f"{int(text[:3]) + 1911}-{text[3:5]}-{text[5:7]}"


def _roc_quarter_to_label(year: Any, season: Any) -> str | None:
    if not year or not season:
        return None
    try:
        western_year = int(str(year)) + 1911
    except ValueError:
        return None
    return f"{western_year}Q{season}"


def _compact_text(text: str, max_len: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def to_pretty_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)
