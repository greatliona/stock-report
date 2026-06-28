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
        f"- 產業：{_industry_line(metrics)}",
        f"- 公司名稱：{_fmt(metrics.get('full_company_name') or result.company_name)}",
        f"- 核心業務：{_business_line(metrics)}",
        f"- 產業鏈位置：{_industry_position(metrics)}",
        f"- 護城河判斷：{_moat_comment(metrics)}",
        "",
        "## 2. 營收狀況與成長動能評估",
        f"- 最新月/季營收期間：{_fmt(metrics.get('revenue_period') or metrics.get('latest_revenue_period'))}",
        f"- 營收年增率：{_fmt_pct(metrics.get('monthly_revenue_yoy_pct') or metrics.get('latest_quarter_revenue_yoy_pct'))}",
        f"- 累計營收年增率：{_fmt_pct(metrics.get('cumulative_revenue_yoy_pct'))}",
        f"- 市場補充成長率：{_fmt_pct(metrics.get('revenue_growth_pct'))}",
        f"- 最新單季 EPS：{_fmt_num(metrics.get('latest_quarter_eps'))}",
        f"- TTM EPS：{_fmt_num(metrics.get('ttm_eps'))}",
        f"- 實質本益比：{_fmt_num(metrics.get('effective_pe'))}（{_fmt(metrics.get('effective_pe_method'))}）",
        f"- PS 估值：{_fmt_num(metrics.get('price_to_sales_ttm'))}",
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
            f"- 淨利率：{_fmt_pct(metrics.get('net_margin_pct'))}",
            f"- 研發費用 / 營收：{_fmt_pct(metrics.get('research_development_ratio_pct'))}",
            f"- 本益比：{_fmt_num(metrics.get('official_pe') or metrics.get('trailing_pe'))}",
            f"- 自由現金流：{_fmt_num(metrics.get('free_cashflow'))}",
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
    revenue_yoy = _first_present(metrics, "monthly_revenue_yoy_pct", "latest_quarter_revenue_yoy_pct", "revenue_growth_pct")
    eps = _as_float(metrics.get("latest_quarter_eps"))
    operating_margin = _as_float(metrics.get("operating_margin_pct"))
    if revenue_yoy is not None and revenue_yoy > 20 and eps is not None and eps <= 0:
        return "營收有右側加速味道，但 EPS 仍虧損；這是成長股敘事，不是獲利右側。"
    if revenue_yoy is not None and revenue_yoy > 20 and operating_margin is not None and operating_margin < 0:
        return "營收在長，但營益率仍為負；市場買的是未來轉盈，容錯率低。"
    if "營收動能偏強" in flags and "實質本益比偏高" not in flags and "實質本益比極高" not in flags:
        return "有成長訊號，但仍需確認 EPS 是否同步放大。"
    if "單月跳升但累計不強，疑似低基期或短單" in flags:
        return "不符合乾淨右側，可能只是低基期或短單造成的假轉強。"
    if "營收年增率為負" in flags or "最新 EPS 非正數" in flags:
        return "基本面仍偏弱，右側條件不足。"
    return "不清楚，資料不足以判定是否已轉強。"


def _risk_comment(metrics: dict[str, Any]) -> str:
    flags = metrics.get("risk_flags") or []
    comments = []
    if flags:
        comments.append("；".join(flags))
    if _as_float(metrics.get("ttm_eps")) is not None and _as_float(metrics.get("ttm_eps")) <= 0:
        comments.append("TTM EPS 為負，不能用本益比安慰自己，只能看成長能否換成利潤")
    if _as_float(metrics.get("operating_margin_pct")) is not None and _as_float(metrics.get("operating_margin_pct")) < 0:
        comments.append("營益率為負，表示本業還沒證明規模經濟")
    if _as_float(metrics.get("price_to_sales_ttm")) is not None and _as_float(metrics.get("price_to_sales_ttm")) > 10:
        comments.append("PS 偏高，任何成長降速都可能被估值修理")
    if not comments:
        return "沒有足夠資料指出明確風險，但這不是安全，只是資料不完整。"
    return "；".join(comments)


def _ai_comment(result: StockLookupResult) -> str:
    blob = " ".join(
        [
            str(result.company_name or ""),
            str(result.metrics.get("industry") or ""),
            str(result.metrics.get("sector") or ""),
            str(result.metrics.get("business_summary") or ""),
            " ".join(str(item.get("title", "")) for item in result.news),
            " ".join(str(item.get("summary", "")) for item in result.news),
        ]
    ).lower()
    if "ai" in blob or "人工智慧" in blob or "伺服器" in blob or "gpu" in blob:
        return "有 AI 相關敘事，但仍需看營收與 EPS 是否真正受惠。"
    return "目前資料看不出直接 AI 關聯。"


def _industry_line(metrics: dict[str, Any]) -> str:
    parts = [
        metrics.get("sector"),
        metrics.get("industry"),
        metrics.get("sec_sic_description"),
        metrics.get("industry") if not metrics.get("sector") else None,
    ]
    seen = []
    for part in parts:
        if part and part not in seen:
            seen.append(str(part))
    return " / ".join(seen) if seen else "不清楚"


def _business_line(metrics: dict[str, Any]) -> str:
    summary = str(metrics.get("business_summary") or "").strip()
    if not summary:
        return _business_from_industry(metrics)
    return _translate_business_summary(summary)


def _industry_position(metrics: dict[str, Any]) -> str:
    industry_text = " ".join(
        str(metrics.get(key) or "")
        for key in ("sector", "industry", "sec_sic_description", "business_summary", "full_company_name")
    ).lower()
    if any(term in industry_text for term in ("software", "application", "prepackaged", "devops", "cloud")):
        return "偏軟體平台/企業工具層，靠訂閱、用量、雲端採用與企業導入速度吃飯。"
    if any(term in industry_text for term in ("semiconductor", "chip", "foundry", "半導體", "積體電路")):
        if "台積電" in industry_text or "tsmc" in industry_text or "台灣積體電路" in industry_text:
            return "晶圓代工核心層，受先進製程需求、AI/HPC 客戶資本支出與產能利用率牽動。"
        return "半導體供應鏈，景氣循環、資本支出、庫存修正與大客戶需求會直接影響評價。"
    if any(term in industry_text for term in ("retail", "consumer", "restaurant")):
        return "偏消費/通路端，需求韌性、展店效率與毛利控管比題材更重要。"
    return "不清楚，資料不足以判斷產業鏈位置。"


def _moat_comment(metrics: dict[str, Any]) -> str:
    gross_margin = _as_float(metrics.get("gross_margin_pct"))
    operating_margin = _as_float(metrics.get("operating_margin_pct"))
    summary = " ".join(
        str(metrics.get(key) or "")
        for key in ("business_summary", "industry", "full_company_name")
    ).lower()
    if gross_margin is not None and gross_margin > 65 and "software" in summary:
        if operating_margin is not None and operating_margin < 0:
            return "毛利率有軟體公司樣子，但營益率仍負，護城河還沒轉成獲利。"
        return "毛利率偏高，可能有軟體平台定價能力；仍需確認留存率與競爭壓力。"
    if gross_margin is not None and gross_margin > 50 and ("半導體" in summary or "積體電路" in summary):
        return "毛利率和營益率偏強，顯示定價/製程/規模優勢，但高資本支出與景氣循環仍是硬風險。"
    if gross_margin is not None and gross_margin < 25:
        return "毛利率偏薄，較像辛苦生意，抗降價能力有限。"
    return "不清楚；缺市占、客戶集中度與留存率資料，不能只靠題材說有護城河。"


def _business_from_industry(metrics: dict[str, Any]) -> str:
    industry = str(metrics.get("industry") or "")
    name = str(metrics.get("full_company_name") or "")
    if "台灣積體電路" in name or "台積電" in name:
        return "晶圓代工與先進製程製造，主要替全球晶片設計公司生產高階與成熟製程晶片。"
    if "半導體" in industry:
        return "半導體相關業務；需再確認它是設計、製造、封測、設備或材料，不能一律當核心 AI 股。"
    if "電子零組件" in industry:
        return "電子零組件供應鏈；通常受客戶拉貨、價格壓力與庫存循環影響。"
    if "電腦及週邊" in industry:
        return "電腦/伺服器/週邊硬體供應鏈；需看是否只是組裝代工，還是有關鍵技術與客戶黏著。"
    if "航運" in industry:
        return "航運運輸業，獲利高度受運價、供需循環與燃油成本影響。"
    if "金融" in industry:
        return "金融業，核心看利差、放款品質、資本適足率與呆帳循環。"
    return "不清楚，公開資料缺少公司業務摘要。"


def _translate_business_summary(summary: str) -> str:
    lower = summary.lower()
    if "software supply chain" in lower and "artifactory" in lower:
        return "軟體供應鏈平台，核心產品包含套件儲存庫、開源套件治理、安全掃描與軟體發布流程管理。"
    if "semiconductor" in lower:
        return "半導體相關業務；需進一步看製造、設計、設備或材料位置。"
    if len(summary) <= 220:
        return summary
    return summary[:219].rstrip() + "…"


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


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_present(metrics: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _as_float(metrics.get(key))
        if value is not None:
            return value
    return None
