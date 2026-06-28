from __future__ import annotations

from typing import Any


DEFAULT_TABLE_NAME = "r101_stock_reports"


class StorageUnavailable(RuntimeError):
    pass


def is_configured(supabase_url: str | None, supabase_key: str | None) -> bool:
    return bool(supabase_url and supabase_key)


def insert_report(
    *,
    supabase_url: str | None,
    supabase_key: str | None,
    table_name: str,
    user_input: str,
    symbol: str | None,
    company_name: str | None,
    market: str | None,
    report: str,
    metrics_json: dict[str, Any],
    sources_json: list[dict[str, Any]],
) -> dict[str, Any] | None:
    client = _client(supabase_url, supabase_key)
    payload = {
        "user_input": user_input,
        "symbol": symbol,
        "company_name": company_name,
        "market": market,
        "report": report,
        "metrics_json": metrics_json,
        "sources_json": sources_json,
    }
    response = client.table(table_name).insert(payload).execute()
    rows = getattr(response, "data", None)
    return rows[0] if rows else None


def fetch_reports(
    *,
    supabase_url: str | None,
    supabase_key: str | None,
    table_name: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    client = _client(supabase_url, supabase_key)
    response = (
        client.table(table_name)
        .select("id,created_at,user_input,symbol,company_name,market,report,metrics_json,sources_json")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return getattr(response, "data", None) or []


def _client(supabase_url: str | None, supabase_key: str | None):
    if not is_configured(supabase_url, supabase_key):
        raise StorageUnavailable("Supabase 尚未設定，無法存取歷史紀錄。")
    try:
        from supabase import create_client
    except ImportError as exc:
        raise StorageUnavailable("尚未安裝 supabase 套件。") from exc
    return create_client(supabase_url, supabase_key)
