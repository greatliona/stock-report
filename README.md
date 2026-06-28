# 右側交易冷酷分析師 R1.0.1

Streamlit app：輸入台股股號/股名或美股 ticker，當下查資料、產生簡短防禦型報告，並把當次報告存到 Supabase。

## R1.0.1 範圍

- 不建立股票資料庫
- 不預先同步股票池
- 不判斷 Nasdaq 前 900 大
- 只在你輸入時查資料
- Supabase 只新增並使用 `public.r101_stock_reports`
- 查詢紀錄預設收起，可展開翻閱

## Supabase 安全隔離

請只執行：

```sql
sql/001_create_r101_stock_reports.sql
```

這份 SQL 只會：

- `create table if not exists public.r101_stock_reports`
- 對這張表啟用 RLS
- 對這張表新增 anon select/insert policy

它不會 DROP、不會 DELETE、不會 ALTER 你的其他資料表。

## Streamlit secrets

請在 Streamlit Cloud 的 Secrets 裡放：

```toml
SUPABASE_URL = "https://your-project.supabase.co"
SUPABASE_ANON_KEY = "your-anon-public-key"
SUPABASE_TABLE = "r101_stock_reports"
APP_PASSWORD = "your-private-password"
OPENAI_API_KEY = "sk-your-openai-key"
OPENAI_MODEL = "gpt-4.1-mini"
```

`OPENAI_API_KEY` 可先不放；沒有 key 時會產生規則版報告。

## 本機執行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## 資料來源

- TWSE OpenAPI
- TPEx OpenAPI
- SEC company facts
- Yahoo chart quote / RSS news

資料查不到時，報告會標示「不清楚」，不補數字。
