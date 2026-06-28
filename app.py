from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import streamlit as st

from src.data_sources import DataSourceError, lookup_stock, to_pretty_json
from src.reporting import build_report
from src.storage import DEFAULT_TABLE_NAME, StorageUnavailable, fetch_reports, insert_report, is_configured


APP_VERSION = "R1.0.4"


st.set_page_config(
    page_title="右側交易冷酷分析師",
    layout="centered",
)


def main() -> None:
    _inject_style()
    _require_password_if_configured()

    st.title("右側交易冷酷分析師")
    st.caption(f"{APP_VERSION}｜即時查詢，報告存檔，不建立股票資料庫")

    config = _load_config()
    _show_storage_status(config)
    _show_ai_guard_status(config)

    with st.form("lookup_form", clear_on_submit=False):
        user_input = st.text_input("輸入股號、股名或美股 ticker", placeholder="例如：2330、台積電、NVDA")
        submitted = st.form_submit_button("產生報告", type="primary")

    if submitted:
        _handle_lookup(user_input, config)

    _render_history(config)


def _handle_lookup(user_input: str, config: dict[str, Any]) -> None:
    if not user_input.strip():
        st.warning("請先輸入股票。")
        return

    with st.status("查詢資料並產生報告中...", expanded=True) as status:
        try:
            result = lookup_stock(user_input)
            st.write("資料查詢完成")
            ai_decision = _ai_guard_decision(config)
            if ai_decision["allowed"]:
                st.write(
                    f"AI 保險絲通過：本月估算已用 ${ai_decision['spent']:.4f}，"
                    f"安全上限 ${ai_decision['stop_at']:.4f}"
                )
                openai_api_key = config.get("openai_api_key")
            else:
                st.write(f"AI 已停用：{ai_decision['reason']}，改用免費規則版報告")
                openai_api_key = None
            report = build_report(
                result,
                openai_api_key=openai_api_key,
                model=config.get("openai_model"),
                input_price_per_1m=config.get("ai_input_price_per_1m"),
                output_price_per_1m=config.get("ai_output_price_per_1m"),
            )
            st.write("報告產生完成")

            saved_row = None
            if config["supabase_ready"]:
                saved_row = insert_report(
                    supabase_url=config.get("supabase_url"),
                    supabase_key=config.get("supabase_anon_key"),
                    table_name=config["supabase_table"],
                    user_input=user_input.strip(),
                    symbol=result.symbol,
                    company_name=result.company_name,
                    market=result.market,
                    report=report,
                    metrics_json=result.to_storage_metrics(),
                    sources_json=result.to_storage_sources(),
                )
                st.write("已存進 Supabase")
            else:
                st.write("Supabase 尚未設定，本次只顯示不存檔")
            status.update(label="完成", state="complete", expanded=False)
        except DataSourceError as exc:
            status.update(label="查詢失敗", state="error", expanded=True)
            st.error(str(exc))
            return
        except Exception as exc:
            status.update(label="發生錯誤", state="error", expanded=True)
            st.error(f"這次查詢失敗：{exc}")
            return

    st.subheader("最新報告")
    st.markdown(report)

    with st.expander("這次抓到的原始摘要", expanded=False):
        st.code(to_pretty_json(result.to_storage_metrics()), language="json")
        st.code(to_pretty_json(result.to_storage_sources()), language="json")

    if saved_row:
        st.success(f"已存檔：{saved_row.get('created_at', '時間由 Supabase 記錄')}")


def _render_history(config: dict[str, Any]) -> None:
    with st.expander("查詢紀錄", expanded=False):
        if not config["supabase_ready"]:
            st.info("Supabase 設定完成後，這裡會顯示過去所有報告。")
            return

        try:
            rows = fetch_reports(
                supabase_url=config.get("supabase_url"),
                supabase_key=config.get("supabase_anon_key"),
                table_name=config["supabase_table"],
                limit=100,
            )
        except StorageUnavailable as exc:
            st.warning(str(exc))
            return
        except Exception as exc:
            st.error(f"讀取歷史紀錄失敗：{exc}")
            return

        if not rows:
            st.caption("目前還沒有查詢紀錄。")
            return

        keyword = st.text_input("搜尋紀錄", placeholder="輸入股號、名稱或市場", key="history_search")
        filtered = _filter_rows(rows, keyword)
        st.caption(f"顯示 {len(filtered)} / {len(rows)} 筆")

        for row in filtered:
            label = _history_label(row)
            with st.container(border=True):
                st.markdown(f"**{label}**")
                st.caption(f"注意時間：{_fmt_created_at(row.get('created_at'))}")
                st.markdown(row.get("report") or "報告內容空白")
                with st.expander("當時數據與來源", expanded=False):
                    st.code(json.dumps(row.get("metrics_json") or {}, ensure_ascii=False, indent=2), language="json")
                    st.code(json.dumps(row.get("sources_json") or [], ensure_ascii=False, indent=2), language="json")


def _load_config() -> dict[str, Any]:
    supabase_url = _secret("SUPABASE_URL")
    supabase_anon_key = _secret("SUPABASE_ANON_KEY")
    table_name = _secret("SUPABASE_TABLE") or DEFAULT_TABLE_NAME
    return {
        "supabase_url": supabase_url,
        "supabase_anon_key": supabase_anon_key,
        "supabase_table": table_name,
        "supabase_ready": is_configured(supabase_url, supabase_anon_key),
        "openai_api_key": _secret("OPENAI_API_KEY"),
        "openai_model": _secret("OPENAI_MODEL") or "gpt-4.1-mini",
        "ai_monthly_budget_usd": _secret_float("AI_MONTHLY_BUDGET_USD"),
        "ai_stop_buffer_usd": _secret_float("AI_STOP_BUFFER_USD", default=0.05),
        "ai_estimated_report_cost_usd": _secret_float("AI_ESTIMATED_REPORT_COST_USD", default=0.02),
        "ai_input_price_per_1m": _secret_float("AI_INPUT_PRICE_PER_1M_USD", default=0.40),
        "ai_output_price_per_1m": _secret_float("AI_OUTPUT_PRICE_PER_1M_USD", default=1.60),
    }


def _secret(name: str) -> str | None:
    try:
        value = st.secrets.get(name)
    except Exception:
        return None
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _secret_float(name: str, default: float | None = None) -> float | None:
    value = _secret(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _show_storage_status(config: dict[str, Any]) -> None:
    if config["supabase_ready"]:
        st.success(f"Supabase 已設定，將只寫入 `{config['supabase_table']}`。")
    else:
        st.info("Supabase 尚未設定：可以先測報告，但不會存檔。")


def _show_ai_guard_status(config: dict[str, Any]) -> None:
    if not config.get("openai_api_key"):
        st.info("AI 改寫未啟用：目前使用免費規則版報告。")
        return
    if config.get("ai_monthly_budget_usd") is None:
        st.warning("偵測到 OpenAI API key，但沒有設定 AI_MONTHLY_BUDGET_USD，所以保險絲會阻止 AI 呼叫。")
        return
    decision = _ai_guard_decision(config)
    if decision["allowed"]:
        st.success(
            f"AI 保險絲已啟用：本月估算 ${decision['spent']:.4f} / "
            f"安全上限 ${decision['stop_at']:.4f}，接近上限會自動改用免費規則版。"
        )
    else:
        st.warning(f"AI 保險絲已停用 OpenAI：{decision['reason']}")


def _ai_guard_decision(config: dict[str, Any]) -> dict[str, Any]:
    if not config.get("openai_api_key"):
        return {"allowed": False, "reason": "沒有 OPENAI_API_KEY"}
    budget = config.get("ai_monthly_budget_usd")
    if budget is None or budget <= 0:
        return {"allowed": False, "reason": "沒有設定 AI_MONTHLY_BUDGET_USD"}
    if not config["supabase_ready"]:
        return {"allowed": False, "reason": "Supabase 未設定，無法持久追蹤 AI 花費"}

    spent = _current_month_ai_spend(config)
    stop_buffer = max(float(config.get("ai_stop_buffer_usd") or 0), 0)
    estimated_next = max(float(config.get("ai_estimated_report_cost_usd") or 0), 0)
    stop_at = max(float(budget) - stop_buffer, 0)
    remaining_to_stop = stop_at - spent
    if remaining_to_stop < estimated_next:
        return {
            "allowed": False,
            "reason": (
                f"本月估算已用 ${spent:.4f}，安全上限 ${stop_at:.4f}，"
                f"不足以保留下一次估算 ${estimated_next:.4f}"
            ),
            "spent": spent,
            "stop_at": stop_at,
        }
    return {"allowed": True, "reason": "ok", "spent": spent, "stop_at": stop_at}


def _current_month_ai_spend(config: dict[str, Any]) -> float:
    try:
        rows = fetch_reports(
            supabase_url=config.get("supabase_url"),
            supabase_key=config.get("supabase_anon_key"),
            table_name=config["supabase_table"],
            limit=500,
        )
    except Exception:
        return 0.0
    now = datetime.now(timezone.utc)
    total = 0.0
    for row in rows:
        created_at = _parse_datetime(row.get("created_at"))
        if not created_at or created_at.year != now.year or created_at.month != now.month:
            continue
        metrics_json = row.get("metrics_json") or {}
        metrics = metrics_json.get("metrics") or metrics_json
        usage = metrics.get("ai_usage") or {}
        cost = usage.get("estimated_cost_usd")
        try:
            total += float(cost or 0)
        except (TypeError, ValueError):
            continue
    return total


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _require_password_if_configured() -> None:
    password = _secret("APP_PASSWORD")
    if not password:
        return
    if st.session_state.get("authenticated"):
        return

    st.title("右側交易冷酷分析師")
    entered = st.text_input("輸入 app 密碼", type="password")
    if st.button("進入", type="primary"):
        if entered == password:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("密碼錯誤。")
    st.stop()


def _filter_rows(rows: list[dict[str, Any]], keyword: str) -> list[dict[str, Any]]:
    keyword = keyword.strip().lower()
    if not keyword:
        return rows
    filtered = []
    for row in rows:
        haystack = " ".join(
            str(row.get(key, ""))
            for key in ("user_input", "symbol", "company_name", "market", "report")
        ).lower()
        if keyword in haystack:
            filtered.append(row)
    return filtered


def _history_label(row: dict[str, Any]) -> str:
    symbol = row.get("symbol") or row.get("user_input") or "未知股票"
    name = row.get("company_name") or ""
    market = row.get("market") or "市場不清楚"
    return f"{symbol} {name}｜{market}".strip()


def _fmt_created_at(value: str | None) -> str:
    if not value:
        return "不清楚"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M:%S %Z").strip()
    except ValueError:
        return value


def _inject_style() -> None:
    st.markdown(
        """
        <style>
        .stApp {
            background: #f5f6f8;
            color: #1d1d1f;
        }
        h1, h2, h3 {
            letter-spacing: 0;
        }
        div[data-testid="stForm"] {
            border: 1px solid #d9dde3;
            border-radius: 8px;
            padding: 1rem;
            background: #ffffff;
        }
        div[data-testid="stExpander"] {
            border-radius: 8px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
