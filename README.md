# 右側交易冷酷分析師 R1.0.4

Streamlit app：輸入台股股號/股名或美股 ticker，當下查資料、產生簡短防禦型報告，並把當次報告存到 Supabase。

## R1.0.4 範圍

- 不建立股票資料庫
- 不預先同步股票池
- 不判斷 Nasdaq 前 900 大
- 只在你輸入時查資料
- Supabase 只新增並使用 `public.r101_stock_reports`
- 查詢紀錄預設收起，可展開翻閱
- 免費版會抓 SEC/Yahoo/yfinance 的產業、業務摘要、毛利率、營益率、PS 等欄位
- 虧損成長股會標明本益比不適用，改用營收成長、毛利率、營益率與現金流檢查
- OpenAI API 有保險絲：沒設定預算不會呼叫，快到上限自動切回免費規則版

## R1.0.4 修正

- 新增 OpenAI API 預算保險絲
- AI 使用紀錄會存 token 與估算成本到 Supabase 同一張報告表
- 每次呼叫前加總本月 AI 估算花費，接近安全上限就停用 AI
- 有 `OPENAI_API_KEY` 但沒設定 `AI_MONTHLY_BUDGET_USD` 時，預設不呼叫 OpenAI

## R1.0.3 修正

- 報告改為明確輸出「三部曲背景處理結果」
- 五大段落對齊原始規格：產業定位、營收動能、新聞展望、反方風險、AI 純度
- 補強右側過濾：低基期假暴衝、連續動能、股價高點與獲利背離
- 補強新聞解讀：把券商喊價、AI 敘事、財報展望分開看
- 補強折舊/資本開支與技術替代風險語句

- 補強美股產業面，不再只列「不清楚」
- 補強公司核心業務與產業鏈位置
- 補強毛利率、營益率、淨利率、研發費用率
- 補強虧損股估值判讀

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

AI_MONTHLY_BUDGET_USD = "5.00"
AI_STOP_BUFFER_USD = "0.50"
AI_ESTIMATED_REPORT_COST_USD = "0.02"
```

`OPENAI_API_KEY` 可先不放；沒有 key 時會產生規則版報告。
若要使用 OpenAI API，必須設定 `AI_MONTHLY_BUDGET_USD`，否則 app 會自動阻止 AI 呼叫。

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
