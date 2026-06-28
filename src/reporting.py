from __future__ import annotations

from typing import Any

from .data_sources import StockLookupResult, to_pretty_json


SYSTEM_PROMPT = """你是專精於台股與美股右側交易、防禦性思維的冷酷資深分析師。
你的工作是戳破泡沫，不是幫公司寫業配。

硬性規則：
- 只能使用使用者提供的 JSON 資料，不得自行補數字。
- 缺資料就寫「不清楚」，不得推測或編造。
- 略過新聞稿和法說會的片面樂觀語氣，優先找反方風險。
- 不提供買進、賣出、目標價或保證式結論。
- 回覆要使用繁體中文，條列式、簡短、直接。

請嚴格輸出五段：
1. 產業定位與核心業務
2. 營收狀況與成長動能評估
3. 最近營運新聞及展望
4. 潛在疑慮與風險
5. 是否與 AI 產業有關
"""


def build_report(result: StockLookupResult, openai_api_key: str | None = None, model: str | None = None) -> str:
    fallback = build_rule_based_report(result)
    if not openai_api_key:
        return fallback

    try:
        from openai import OpenAI

        client = OpenAI(api_key=openai_api_key)
        response = client.responses.create(
            model=model or "gpt-4.1-mini",
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "請根據以下資料產生報告。若資料不足，直接寫不清楚。\n\n"
                        f"{to_pretty_json(_report_payload(result))}"
                    ),
                },
            ],
        )
        text = getattr(response, "output_text", None)
        return text.strip() if text else fallback
    except Exception as exc:
        return f"{fallback}\n\n---\nAI 改寫失敗，已保留規則版報告。錯誤：{exc}"


def build_rule_based_report(result: StockLookupResult) -> str:
    metrics = result.metrics
    news = result.news[:5]
    warnings = result.warnings

    title = f"{result.symbol or result.user_input} {result.company_name or ''}".strip()
    market = result.market or "不清楚"

    lines = [
        f"# {title}",
        "",
        f"- 市場：{market}",
        f"- 資料品質：{_quality_label(result)}",
        f"- 主要風險標籤：{_join_or_unknown(metrics.get('risk_flags'))}",
        "",
        "## 1. 產業定位與核心業務",
        f"- 產業：{_fmt(metrics.get('industry'))}",
        f"- 公司名稱：{_fmt(metrics.get('full_company_name') or result.company_name)}",
        "- 護城河判斷：若沒有市占、客戶集中度、技術門檻資料，先視為不清楚；不能只靠題材認定有護城河。",
        "",
        "## 2. 營收狀況與成長動能評估",
        f"- 最新月/季營收期間：{_fmt(metrics.get('revenue_period') or metrics.get('latest_revenue_period'))}",
        f"- 營收年增率：{_fmt_pct(metrics.get('monthly_revenue_yoy_pct') or metrics.get('latest_quarter_revenue_yoy_pct'))}",
        f"- 累計營收年增率：{_fmt_pct(metrics.get('cumulative_revenue_yoy_pct'))}",
        f"- 最新單季 EPS：{_fmt_num(metrics.get('latest_quarter_eps'))}",
        f"- TTM EPS：{_fmt_num(metrics.get('ttm_eps'))}",
        f"- 實質本益比：{_fmt_num(metrics.get('effective_pe'))}（{_fmt(metrics.get('effective_pe_method'))}）",
        f"- 右側判斷：{_right_side_comment(metrics)}",
        "",
        "## 3. 最近營運新聞及展望",
    ]

    if news:
        for item in news:
            lines.append(f"- {item.get('date') or '日期不清楚'}｜{item.get('title') or '標題不清楚'}")
    else:
        lines.append("- 查無明確與營收或成長展望直接相關的近期新聞/重大訊息。")

    lines.extend(
        [
            "",
            "## 4. 潛在疑慮與風險",
            f"- 毛利率：{_fmt_pct(metrics.get('gross_margin_pct'))}",
            f"- 營益率：{_fmt_pct(metrics.get('operating_margin_pct'))}",
            f"- 官方本益比：{_fmt_num(metrics.get('official_pe'))}",
            f"- 反方檢視：{_risk_comment(metrics)}",
            "",
            "## 5. 是否與 AI 產業有關",
            f"- AI 關聯：{_ai_comment(result)}",
            "- 防禦結論：若無法從營收、EPS 或客戶需求資料證明 AI 帶來實質獲利，先視為題材，不視為基本面。",
        ]
    )

    if warnings:
        lines.extend(["", "## 資料缺口", *[f"- {warning}" for warning in warnings]])

    return "\n".join(lines)


def _report_payload(result: StockLookupResult) -> dict[str, Any]:
    return {
        "user_input": result.user_input,
        "symbol": result.symbol,
        "company_name": result.company_name,
        "market": result.market,
        "metrics": result.metrics,
        "news": result.news,
        "warnings": result.warnings,
        "sources": result.to_storage_sources(),
    }


def _quality_label(result: StockLookupResult) -> str:
    metrics = result.metrics
    if not result.symbol:
        return "紅燈：無法辨識股票"
    missing_core = [
        metrics.get("latest_quarter_eps") is None,
        metrics.get("effective_pe") is None,
        (metrics.get("monthly_revenue_yoy_pct") is None and metrics.get("latest_quarter_revenue_yoy_pct") is None),
    ]
    if any(missing_core):
        return "黃燈：核心資料有缺口"
    return "綠燈：核心資料完整"


def _right_side_comment(metrics: dict[str, Any]) -> str:
    flags = metrics.get("risk_flags") or []
    if "營收動能偏強" in flags and "實質本益比偏高" not in flags and "實質本益比極高" not in flags:
        return "有成長訊號，但仍需確認 EPS 是否同步放大。"
    if "單月跳升但累計不強，疑似低基期或短單" in flags:
        return "不符合乾淨右側，可能只是低基期或短單造成的假轉強。"
    if "營收年增率為負" in flags or "最新 EPS 非正數" in flags:
        return "基本面仍偏弱，右側條件不足。"
    return "不清楚，資料不足以判定是否已轉強。"


def _risk_comment(metrics: dict[str, Any]) -> str:
    flags = metrics.get("risk_flags") or []
    if not flags:
        return "沒有足夠資料指出明確風險，但這不是安全，只是資料不完整。"
    return "；".join(flags)


def _ai_comment(result: StockLookupResult) -> str:
    blob = " ".join(
        [
            str(result.company_name or ""),
            str(result.metrics.get("industry") or ""),
            " ".join(str(item.get("title", "")) for item in result.news),
            " ".join(str(item.get("summary", "")) for item in result.news),
        ]
    ).lower()
    if "ai" in blob or "人工智慧" in blob or "伺服器" in blob or "gpu" in blob:
        return "有 AI 相關敘事，但仍需看營收與 EPS 是否真正受惠。"
    return "目前資料看不出直接 AI 關聯。"


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "不清楚"
    return str(value)


def _fmt_num(value: Any) -> str:
    if value is None:
        return "不清楚"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "不清楚"
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return str(value)


def _join_or_unknown(values: Any) -> str:
    if not values:
        return "不清楚"
    return "、".join(str(value) for value in values)
