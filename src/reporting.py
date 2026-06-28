from __future__ import annotations

import re
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


DEFAULT_AI_INPUT_PRICE_PER_1M = 0.40
DEFAULT_AI_OUTPUT_PRICE_PER_1M = 1.60


def build_report(
    result: StockLookupResult,
    openai_api_key: str | None = None,
    model: str | None = None,
    input_price_per_1m: float | None = None,
    output_price_per_1m: float | None = None,
) -> str:
    fallback = build_rule_based_report(result)
    if not openai_api_key:
        result.metrics["ai_usage"] = {"enabled": False, "reason": "OpenAI API key not used"}
        return fallback

    try:
        from openai import OpenAI

        selected_model = model or "gpt-4.1-mini"
        client = OpenAI(api_key=openai_api_key)
        response = client.responses.create(
            model=selected_model,
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
        result.metrics["ai_usage"] = _ai_usage_payload(
            response=response,
            model=selected_model,
            input_price_per_1m=input_price_per_1m,
            output_price_per_1m=output_price_per_1m,
        )
        text = getattr(response, "output_text", None)
        return text.strip() if text else fallback
    except Exception as exc:
        result.metrics["ai_usage"] = {"enabled": False, "reason": f"AI failed: {exc}"}
        return f"{fallback}\n\n---\nAI 改寫失敗，已保留規則版報告。錯誤：{exc}"


def _ai_usage_payload(
    *,
    response: Any,
    model: str,
    input_price_per_1m: float | None,
    output_price_per_1m: float | None,
) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    input_tokens = _usage_value(usage, "input_tokens")
    output_tokens = _usage_value(usage, "output_tokens")
    total_tokens = _usage_value(usage, "total_tokens")
    input_price = input_price_per_1m or DEFAULT_AI_INPUT_PRICE_PER_1M
    output_price = output_price_per_1m or DEFAULT_AI_OUTPUT_PRICE_PER_1M
    cost = None
    if input_tokens is not None and output_tokens is not None:
        cost = (input_tokens / 1_000_000 * input_price) + (output_tokens / 1_000_000 * output_price)
    return {
        "enabled": True,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": cost,
        "input_price_per_1m_usd": input_price,
        "output_price_per_1m_usd": output_price,
        "pricing_note": "App-side estimate for budget guard; check OpenAI billing for official charges.",
    }


def _usage_value(usage: Any, key: str) -> int | None:
    if usage is None:
        return None
    if isinstance(usage, dict):
        value = usage.get(key)
    else:
        value = getattr(usage, key, None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


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
        "## 三部曲背景處理結果",
        f"- 嚴格核實數據：{_verification_line(result)}",
        f"- 右側過濾：{_right_side_comment(metrics)}",
        f"- 反方批判：{_risk_comment(metrics)}",
        "",
        "## 1. 產業定位與核心業務",
        f"- 產業：{_industry_line(metrics)}",
        f"- 公司名稱：{_fmt(metrics.get('full_company_name') or result.company_name)}",
        f"- 它賣什麼：{_business_line(metrics)}",
        f"- 錢怎麼賺：{_revenue_model(metrics)}",
        f"- 客戶為何需要它：{_customer_problem(metrics)}",
        f"- 市場地位：{_market_position(metrics, result)}",
        f"- 產業鏈位置：{_industry_position(metrics)}",
        f"- 護城河判斷：{_moat_comment(metrics)}",
        "",
        "## 2. 營收狀況與成長動能評估",
        f"- 最新營收期間：{_fmt(metrics.get('revenue_period') or metrics.get('latest_revenue_period'))}",
        f"- 最新營收年增率：{_revenue_yoy_text(result)}",
        f"- 累計營收年增率：{_fmt_pct(metrics.get('cumulative_revenue_yoy_pct'))}",
        f"- 近四期營收 YoY 趨勢：{_revenue_series_text(metrics)}",
        f"- 市場補充成長率：{_fmt_pct(metrics.get('revenue_growth_pct'))}",
        f"- 最新單季 EPS：{_fmt_num(metrics.get('latest_quarter_eps'))}",
        f"- 近四季累計 EPS：{_ttm_eps_text(metrics)}",
        f"- 實質本益比：{_fmt_num(metrics.get('effective_pe'))}（{_fmt(metrics.get('effective_pe_method'))}）",
        f"- PS 估值：{_fmt_num(metrics.get('price_to_sales_ttm'))}",
        f"- 成長定位：{_growth_stage(metrics)}",
        f"- 是否符合右側邏輯：{_right_side_comment(metrics)}",
        "",
        "## 3. 最近營運新聞及展望",
    ]

    if news:
        for item in news:
            lines.append(f"- {item.get('date') or '日期不清楚'}｜{item.get('title') or '標題不清楚'}｜{_news_readthrough(item)}")
    else:
        lines.append("- 查無明確與營收或成長展望直接相關的近期新聞/重大訊息。")

    lines.extend(
        [
            "",
            "## 4. 潛在疑慮與風險",
            f"- 股價位置：{_price_position(metrics)}",
            f"- 毛利率：{_fmt_pct(metrics.get('gross_margin_pct'))}",
            f"- 毛利率年變化：{_fmt_pct_points(metrics.get('gross_margin_yoy_change_points'))}",
            f"- 營益率：{_fmt_pct(metrics.get('operating_margin_pct'))}",
            f"- 淨利率：{_fmt_pct(metrics.get('net_margin_pct'))}",
            f"- 研發費用 / 營收：{_fmt_pct(metrics.get('research_development_ratio_pct'))}",
            f"- 本益比：{_fmt_num(metrics.get('official_pe') or metrics.get('trailing_pe'))}",
            f"- 自由現金流：{_fmt_num(metrics.get('free_cashflow'))}",
            f"- 折舊/資本開支包袱：{_capex_comment(metrics)}",
            f"- 反方檢視：{_risk_comment(metrics)}",
            "",
            "## 5. 是否與 AI 產業有關",
            f"- AI 關聯：{_ai_comment(result)}",
            f"- AI 敘事證據：{_ai_evidence(result)}",
            f"- AI 分類：{_ai_classification(result)}",
            "- 防禦結論：若無法從營收、EPS、毛利率或客戶需求資料證明 AI 帶來實質獲利，先視為題材，不視為基本面。",
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


def _verification_line(result: StockLookupResult) -> str:
    metrics = result.metrics
    revenue_label = "單月營收 YoY" if metrics.get("monthly_revenue_yoy_pct") is not None else "單季營收 YoY"
    if result.market == "US" and metrics.get("monthly_revenue_yoy_pct") is None:
        revenue_note = "美股通常不揭露單月營收，已改核實最新單季營收 YoY"
    else:
        revenue_note = f"{revenue_label}={_fmt_pct(metrics.get('monthly_revenue_yoy_pct') or metrics.get('latest_quarter_revenue_yoy_pct'))}"
    return (
        f"{revenue_note}；最新單季 EPS={_fmt_num(metrics.get('latest_quarter_eps'))}；"
        f"近四季 EPS={_ttm_eps_text(metrics)}；實質本益比={_fmt_num(metrics.get('effective_pe'))}"
    )


def _right_side_comment(metrics: dict[str, Any]) -> str:
    flags = metrics.get("risk_flags") or []
    revenue_yoy = _first_present(metrics, "monthly_revenue_yoy_pct", "latest_quarter_revenue_yoy_pct", "revenue_growth_pct")
    eps = _as_float(metrics.get("latest_quarter_eps"))
    operating_margin = _as_float(metrics.get("operating_margin_pct"))
    trend = metrics.get("revenue_yoy_trend")
    cumulative_yoy = _as_float(metrics.get("cumulative_revenue_yoy_pct"))
    if metrics.get("near_one_year_high") and eps is not None and eps <= 0:
        return "高風險：股價接近一年高點，但 EPS 仍非正數，基本面與股價有背離。"
    if revenue_yoy is not None and revenue_yoy > 20 and trend == "連續放大" and eps is not None and eps > 0:
        return "較符合右側：營收 YoY 連續放大且 EPS 為正，但仍要檢查估值是否透支。"
    if revenue_yoy is not None and cumulative_yoy is not None and revenue_yoy > 20 and cumulative_yoy < 5:
        return "不乾淨：單月 YoY 強但累計 YoY 不強，偏低基期或短單假暴衝。"
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
    if metrics.get("near_one_year_high") and _as_float(metrics.get("latest_quarter_eps")) is not None and _as_float(metrics.get("latest_quarter_eps")) <= 0:
        comments.append("股價接近一年高點但獲利仍弱，這是典型先漲後驗證的脆弱結構")
    if _as_float(metrics.get("gross_margin_yoy_change_points")) is not None and _as_float(metrics.get("gross_margin_yoy_change_points")) < -3:
        comments.append("毛利率年減超過 3 個百分點，可能有價格壓力或成本壓力")
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
    evidence = _ai_evidence_items(result)
    if evidence:
        return f"有 {len(evidence)} 個 AI 相關線索；但這只是敘事來源，仍需看營收與 EPS 是否真正受惠。"
    return "目前資料看不出直接 AI 關聯。"


def _ai_classification(result: StockLookupResult) -> str:
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
    if any(term in blob for term in ("gpu", "accelerator", "算力", "晶圓代工", "先進製程", "hpc")):
        return "核心 AI 供應鏈或算力基礎設施，但仍需看 AI 需求是否反映到 EPS。"
    if any(term in blob for term in ("software", "devops", "security", "supply chain", "automation")) and "ai" in blob:
        return "AI 外圍工具/軟體受惠敘事，不是核心算力股。"
    if "ai" in blob:
        return "有 AI 敘事，但目前資料不足以證明是核心 AI。"
    return "非明確 AI 標的。"


def _ai_evidence(result: StockLookupResult) -> str:
    items = _ai_evidence_items(result)
    if not items:
        return "查無具體 AI 敘事證據。"
    return "；".join(items[:3])


def _ai_evidence_items(result: StockLookupResult) -> list[str]:
    evidence = []
    business = str(result.metrics.get("business_summary") or "")
    if _contains_ai_term(business):
        evidence.append(f"公司業務摘要：{_ai_business_reason(business)}")
    for item in result.news:
        text = f"{item.get('title', '')} {item.get('summary', '')}"
        if _contains_ai_term(text):
            title = item.get("title") or "未命名新聞"
            evidence.append(f"新聞「{title}」：{_ai_news_reason(text)}")
    return evidence


def _contains_ai_term(text: str) -> bool:
    lowered = text.lower()
    return bool(
        re.search(r"(^|[^a-z])ai([^a-z]|$)", lowered)
        or any(term in lowered for term in ("artificial intelligence", "gpu", "hpc", "machine learning", "mlops", "data science", "llm", "算力", "人工智慧", "伺服器"))
    )


def _ai_business_reason(text: str) -> str:
    lowered = text.lower()
    if "jfrog ml" in lowered or "mlops" in lowered:
        return "產品線含 JFrog ML / MLOps，服務資料科學團隊建置、訓練、部署與監控模型；這是 AI/ML 開發流程工具，不是算力核心。"
    if "gpu" in lowered or "hpc" in lowered:
        return "公司業務摘要出現 GPU/HPC 線索，可能靠近算力供應鏈。"
    return _compact_sentence(text, 120)


def _ai_news_reason(text: str) -> str:
    lowered = text.lower()
    if "cloud business" in lowered and "artificial intelligence" in lowered:
        return "市場把雲端業務成長與 AI 供應鏈定位連在一起，但仍只是外部敘事，需看財報是否轉成 EPS。"
    if "promising ai stocks" in lowered or "ai stocks" in lowered:
        return "被市場文章/券商敘事歸類為 AI 股票，這不是營收證據，只是題材標籤。"
    if "ai capex" in lowered:
        return "新聞討論 AI 資本支出循環，需確認公司是否直接受惠。"
    if "artificial intelligence" in lowered or re.search(r"(^|[^a-z])ai([^a-z]|$)", lowered):
        return "新聞出現 AI 關鍵字，但未必代表公司有核心 AI 技術或直接獲利。"
    return _compact_sentence(text, 100)


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


def _market_position(metrics: dict[str, Any], result: StockLookupResult) -> str:
    name_blob = f"{result.company_name or ''} {metrics.get('full_company_name') or ''} {metrics.get('business_summary') or ''}".lower()
    if "台積電" in name_blob or "taiwan semiconductor" in name_blob or "tsmc" in name_blob:
        return "晶圓代工全球龍頭；這是少數能直接連到 AI/HPC 算力需求的核心供應鏈。"
    if "jfrog" in name_blob:
        return "軟體供應鏈/DevOps 平台商；市場排名不清楚，不應直接當成壟斷型平台。"
    if metrics.get("filer_category"):
        return f"{metrics.get('filer_category')}；實際市占/排名不清楚。"
    return "不清楚；缺少市占與排名資料。"


def _business_line(metrics: dict[str, Any]) -> str:
    summary = str(metrics.get("business_summary") or "").strip()
    if not summary:
        return _business_from_industry(metrics)
    return _translate_business_summary(summary)


def _revenue_model(metrics: dict[str, Any]) -> str:
    summary = str(metrics.get("business_summary") or "").lower()
    industry = " ".join(
        str(metrics.get(key) or "").lower()
        for key in ("industry", "sector", "sec_sic_description", "full_company_name")
    )
    if "jfrog" in summary or ("software supply chain" in summary and "artifactory" in summary):
        return "主要靠企業軟體平台訂閱、雲端用量、企業級安全/治理模組與大型客戶擴充使用量賺錢。"
    if "software" in summary or "software" in industry:
        return "通常靠訂閱授權、雲端用量、企業合約與增購模組賺錢；重點看續約率、淨留存率與銷售效率。"
    if "台灣積體電路" in industry or "台積電" in industry:
        return "靠晶圓代工產能、先進製程價格、客戶投片量與高階封裝需求賺錢。"
    if "半導體" in industry:
        return "靠半導體產品/製造/設備/材料出貨賺錢；重點看報價、稼動率與客戶拉貨。"
    return "不清楚；目前資料不足以判斷收入模式。"


def _customer_problem(metrics: dict[str, Any]) -> str:
    summary = str(metrics.get("business_summary") or "").lower()
    industry = " ".join(str(metrics.get(key) or "").lower() for key in ("industry", "sector", "sec_sic_description"))
    if "jfrog" in summary or ("artifactory" in summary and "xray" in summary):
        return "企業軟體團隊需要管理大量開源/內部套件、掃描漏洞、控管軟體供應鏈風險，並把程式碼穩定部署到正式環境。"
    if "software" in summary or "software" in industry:
        return "企業想降低流程成本、提高自動化與資料可見度；但同類 SaaS 競爭通常很擠。"
    if "台積電" in str(metrics.get("full_company_name") or ""):
        return "晶片設計公司需要高良率、高效能與大規模先進製程產能，自己蓋廠成本與難度太高。"
    if "半導體" in industry:
        return "下游客戶需要效能、成本、交期與良率；景氣反轉時砍單會很直接。"
    return "不清楚；缺少客戶結構與使用場景資料。"


def _revenue_yoy_text(result: StockLookupResult) -> str:
    metrics = result.metrics
    if metrics.get("monthly_revenue_yoy_pct") is not None:
        return f"{_fmt_pct(metrics.get('monthly_revenue_yoy_pct'))}（台股最新單月營收）"
    if metrics.get("latest_quarter_revenue_yoy_pct") is not None:
        suffix = "美股未揭露單月營收，改用最新單季營收 YoY" if result.market == "US" else "最新單季營收 YoY"
        return f"{_fmt_pct(metrics.get('latest_quarter_revenue_yoy_pct'))}（{suffix}）"
    return "不清楚"


def _revenue_series_text(metrics: dict[str, Any]) -> str:
    series = metrics.get("revenue_yoy_series") or []
    if not series:
        return "不清楚，缺少連續月份/季度資料。"
    bits = [f"{item.get('period')}：{_fmt_pct(item.get('yoy_pct'))}" for item in series]
    trend = metrics.get("revenue_yoy_trend") or "不清楚"
    return f"{'；'.join(bits)}。趨勢：{trend}"


def _ttm_eps_text(metrics: dict[str, Any]) -> str:
    value = metrics.get("ttm_eps")
    method = metrics.get("ttm_eps_method")
    if value is None:
        return "不清楚"
    if method:
        return f"{_fmt_num(value)}（{method}）"
    return f"{_fmt_num(value)}（逐季資料加總）"


def _growth_stage(metrics: dict[str, Any]) -> str:
    revenue_yoy = _first_present(metrics, "monthly_revenue_yoy_pct", "latest_quarter_revenue_yoy_pct", "revenue_growth_pct")
    eps = _as_float(metrics.get("latest_quarter_eps"))
    trend = metrics.get("revenue_yoy_trend")
    if revenue_yoy is None:
        return "不清楚"
    if revenue_yoy < 0:
        return "衰退中，不是右側。"
    if revenue_yoy >= 30 and trend == "連續放大" and eps is not None and eps > 0:
        return "高成長且加速中。"
    if revenue_yoy >= 20 and eps is not None and eps <= 0:
        return "營收高成長，但獲利未跟上；市場買的是轉盈想像。"
    if revenue_yoy >= 20:
        return "高成長，但是否加速需看連續資料。"
    if revenue_yoy >= 5:
        return "溫和成長。"
    return "接近停滯，注意高基期或需求降溫。"


def _news_readthrough(item: dict[str, Any]) -> str:
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    if any(term in text for term in ("price target", "raises pt", "analyst", "rating")):
        return "市場敘事偏估值/券商喊價，不能當基本面證據"
    if any(term in text for term in ("revenue", "earnings", "guidance", "outlook")):
        return "與營收/獲利展望相關，需核對實際財報"
    if _contains_ai_term(f" {text} "):
        return "AI 敘事出現，但這只是敘事；要看是否轉成營收、毛利率與 EPS"
    return "偏市場情緒，參考價值有限"


def _price_position(metrics: dict[str, Any]) -> str:
    close = _as_float(metrics.get("latest_close"))
    high = _as_float(metrics.get("one_year_high") or metrics.get("fifty_two_week_high"))
    drawdown = _as_float(metrics.get("drawdown_from_one_year_high_pct"))
    if close is None:
        return "不清楚"
    if high is None:
        return f"最新收盤價 {_fmt_num(close)}；一年高點資料不清楚。"
    near = "接近一年高點" if close >= high * 0.95 else "未接近一年高點"
    return f"最新收盤價 {_fmt_num(close)}，一年高點 {_fmt_num(high)}，{near}，距高點 {_fmt_pct(drawdown)}"


def _capex_comment(metrics: dict[str, Any]) -> str:
    text = " ".join(str(metrics.get(key) or "") for key in ("industry", "sector", "sec_sic_description", "business_summary")).lower()
    if any(term in text for term in ("semiconductor", "半導體", "晶圓", "foundry")):
        return "半導體/製造屬高資本支出產業，折舊與產能利用率是硬風險；若無現金流/折舊細項，先標資料不足。"
    if any(term in text for term in ("software", "application", "prepackaged")):
        return "軟體業折舊壓力通常不是主風險，真正風險是研發/銷售費用吃掉毛利、成長降速後估值壓縮。"
    return "不清楚，缺少折舊、資本支出或現金流細項。"


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
        return "軟體供應鏈平台。產品包含 JFrog Artifactory 套件儲存庫、Curation 開源套件治理、Xray 安全掃描、Distribution/平台工具，用來管控軟體從開發、掃描到發布的流程。"
    if "semiconductor" in lower:
        return "半導體相關業務；需進一步看製造、設計、設備或材料位置。"
    if len(summary) <= 220:
        return summary
    return summary[:219].rstrip() + "…"


def _compact_sentence(text: str, max_len: int) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        return "不清楚"
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


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


def _fmt_pct_points(value: Any) -> str:
    if value is None:
        return "不清楚"
    try:
        return f"{float(value):+.2f} 個百分點"
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
