-- Keep the free Render engine awake: Supabase pings /health every 10 minutes (free, runs inside your database).
-- 1) Replace the URL below with your engine URL from Render (e.g. https://clipforge-engine.onrender.com)
-- 2) Run this in Supabase → SQL Editor.
create extension if not exists pg_cron;
create extension if not exists pg_net;

select cron.unschedule('clipforge-keepalive')
where exists (select 1 from cron.job where jobname = 'clipforge-keepalive');

select cron.schedule(
  'clipforge-keepalive',
  '*/10 * * * *',
  $$ select net.http_get(url := 'https://REPLACE-WITH-YOUR-ENGINE.onrender.com/health', timeout_milliseconds := 30000) $$
);
