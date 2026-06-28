-- R1.0.1 專用查詢紀錄表
-- 安全原則：
-- 1. 只新增 public.r101_stock_reports
-- 2. 不 DROP、不清空、不修改任何既有資料表
-- 3. RLS policy 只套用在 public.r101_stock_reports
-- 4. Streamlit 請使用 anon key，不要使用 service_role key

create table if not exists public.r101_stock_reports (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  user_input text not null,
  symbol text,
  company_name text,
  market text,
  report text not null,
  metrics_json jsonb not null default '{}'::jsonb,
  sources_json jsonb not null default '[]'::jsonb
);

comment on table public.r101_stock_reports is
  'R1.0.1 stock report history. Created for Streamlit app only.';

alter table public.r101_stock_reports enable row level security;

do $$
begin
  if not exists (
    select 1
    from pg_policies
    where schemaname = 'public'
      and tablename = 'r101_stock_reports'
      and policyname = 'r101_stock_reports_select_anon'
  ) then
    create policy r101_stock_reports_select_anon
      on public.r101_stock_reports
      for select
      to anon
      using (true);
  end if;

  if not exists (
    select 1
    from pg_policies
    where schemaname = 'public'
      and tablename = 'r101_stock_reports'
      and policyname = 'r101_stock_reports_insert_anon'
  ) then
    create policy r101_stock_reports_insert_anon
      on public.r101_stock_reports
      for insert
      to anon
      with check (true);
  end if;
end $$;
