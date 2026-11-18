-- ClipForge initial schema. Run in Supabase SQL editor (or `supabase db push`).
create extension if not exists pgcrypto;

-- ───────────────────────── core entities ─────────────────────────
create table if not exists streamers (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  platform text not null default 'youtube',
  channel_url text not null unique,           -- e.g. https://www.youtube.com/@IShowSpeed
  handle text,
  language text default 'en',
  permission_status text not null default 'not_asked'
    check (permission_status in ('not_asked','asked','granted','denied','open_policy')),
  layout_preset jsonb,                         -- {"cam":[x,y,w,h]} normalized 0..1
  priority int not null default 50,
  weight real not null default 1.0,
  watch boolean not null default true,
  notes text,
  created_at timestamptz not null default now()
);

create table if not exists streams (
  id uuid primary key default gen_random_uuid(),
  streamer_id uuid references streamers(id) on delete set null,
  video_id text not null unique,
  url text not null,
  title text,
  status text not null default 'live'
    check (status in ('live','recording','stop_requested','ended','error')),
  concurrent_viewers int,
  started_at timestamptz,
  recording_node text,
  capture_level text,                          -- C1/C2/C3 that worked
  chat_delay_s real not null default 5,
  last_seen_live timestamptz default now(),
  created_at timestamptz not null default now()
);

create table if not exists stream_signals (
  id bigserial primary key,
  stream_id uuid references streams(id) on delete cascade,
  ts timestamptz not null,
  chat_rate real, hype_rate real, audio_lufs real, viewers int, score real
);
create index if not exists stream_signals_stream_ts on stream_signals(stream_id, ts desc);

create table if not exists moments (
  id uuid primary key default gen_random_uuid(),
  stream_id uuid references streams(id) on delete cascade,
  peak_ts timestamptz not null,
  window_start timestamptz, window_end timestamptz,
  signal_score real not null,
  reasons jsonb,
  status text not null default 'new',
  created_at timestamptz not null default now()
);

create table if not exists clips (
  id uuid primary key default gen_random_uuid(),
  moment_id uuid references moments(id) on delete set null,
  stream_id uuid references streams(id) on delete set null,
  streamer_id uuid references streamers(id) on delete set null,
  source text not null default 'live',         -- live | vod
  window_offset_s real,                        -- window start relative to stream start (for &t=)
  start_s real, end_s real,                    -- relative to window
  category text, signal_score real, llm_score real, final_score real,
  transcript jsonb,                            -- [{w,s,e}]
  brief jsonb,                                 -- Claude's clip_brief result (or fallback)
  layout text,
  raw_path text,                               -- local path on engine
  video_path text,                             -- storage path
  sheet_path text,                             -- storage path of QA contact sheet
  title text, description text, hashtags text[], hook_text text,
  qa_score real, qa_attempts int not null default 0,
  status text not null default 'cutting' check (status in
    ('cutting','awaiting_brief','editing','awaiting_qa','review','approved','scheduled','published','rejected','error')),
  status_reason text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists clips_status on clips(status, created_at desc);

create table if not exists qa_reports (
  id uuid primary key default gen_random_uuid(),
  clip_id uuid references clips(id) on delete cascade,
  score real, checks jsonb, issues text[], verdict text, by_claude boolean default false,
  created_at timestamptz not null default now()
);

create table if not exists publications (
  id uuid primary key default gen_random_uuid(),
  clip_id uuid references clips(id) on delete cascade,
  channel text default 'buffer',
  buffer_post_id text, youtube_video_id text, url text,
  scheduled_for timestamptz, published_at timestamptz,
  status text not null default 'queued', error text,
  created_at timestamptz not null default now()
);

create table if not exists metrics (
  id bigserial primary key,
  publication_id uuid references publications(id) on delete cascade,
  checked_at timestamptz not null default now(),
  views int, avg_view_pct real, likes int, comments int, subs_gained int
);

-- ───────────────────────── agents & orchestration ─────────────────────────
create table if not exists agents (
  id text primary key,                         -- e.g. "cutter@engine-1"
  type text not null,
  node text,
  status text not null default 'idle' check (status in ('idle','working','error','stopped','offline')),
  current_job_id uuid,
  current_action text,
  next_steps jsonb default '[]'::jsonb,
  stats jsonb default '{}'::jsonb,
  last_heartbeat timestamptz default now(),
  started_at timestamptz default now()
);

create table if not exists agent_events (
  id bigserial primary key,
  agent_id text,
  level text not null default 'info',          -- info | warn | error | success
  message text not null,
  data jsonb,
  created_at timestamptz not null default now()
);
create index if not exists agent_events_created on agent_events(created_at desc);

create table if not exists jobs (
  id uuid primary key default gen_random_uuid(),
  type text not null,                          -- record_stream | cut | clip_vod | edit | qa | publish
  payload jsonb not null default '{}'::jsonb,
  status text not null default 'queued' check (status in ('queued','running','done','failed','cancelled')),
  priority int not null default 50,
  attempts int not null default 0,
  max_attempts int not null default 3,
  locked_by text, locked_at timestamptz,
  run_after timestamptz not null default now(),
  result jsonb, error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists jobs_claim on jobs(status, type, priority desc, created_at);

create table if not exists llm_requests (
  id uuid primary key default gen_random_uuid(),
  kind text not null check (kind in ('clip_brief','clip_qa','plan','task')),
  requested_by text,
  clip_id uuid references clips(id) on delete cascade,
  task_id uuid,
  payload jsonb not null,
  attachments text[] default '{}',             -- storage paths (contact sheets)
  priority int not null default 50,            -- 0..100 (>=85 wakes Claude immediately)
  status text not null default 'pending' check (status in ('pending','claimed','done','failed','expired')),
  claimed_by text, claimed_at timestamptz,
  result jsonb,
  consumed boolean not null default false,     -- engine picked up the result
  deadline_at timestamptz not null default now() + interval '10 minutes',
  created_at timestamptz not null default now(),
  done_at timestamptz
);
create index if not exists llm_requests_status on llm_requests(status, priority desc, created_at);

create table if not exists tasks (
  id uuid primary key default gen_random_uuid(),
  title text not null,
  instruction text not null,
  assigned_agent text not null default 'claude',
  status text not null default 'pending',
  result text,
  created_by uuid default auth.uid(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists plans (
  id bigserial primary key,
  created_at timestamptz not null default now(),
  horizon text default '2h',
  plan jsonb not null,
  rationale text
);

create table if not exists rules (
  version serial primary key,
  rules jsonb not null,
  active boolean not null default true,
  note text,
  created_at timestamptz not null default now()
);

create table if not exists settings (
  key text primary key,
  value jsonb not null,
  updated_at timestamptz not null default now()
);

create table if not exists heartbeats (
  id bigserial primary key,
  ts timestamptz not null default now(),
  summary jsonb
);

insert into settings(key, value) values
  ('autopilot_level', '0'),
  ('kill_switch', '{"publishing": false, "all": false}'),
  ('max_parallel_streams', '2'),
  ('daily_post_cap', '10'),
  ('min_post_gap_minutes', '30'),
  ('bridge', '{"state":"sleeping","last_wake_at":null,"last_seen_at":null,"wakes_today":0}')
on conflict (key) do nothing;

-- ───────────────────────── functions ─────────────────────────
-- Safely claim the next job of the given types (many workers can call this concurrently).
create or replace function claim_job(p_types text[], p_worker text)
returns setof jobs language plpgsql as $$
begin
  return query
  update jobs j set status = 'running', locked_by = p_worker, locked_at = now(),
                    attempts = j.attempts + 1, updated_at = now()
  where j.id = (
    select id from jobs
    where status = 'queued' and type = any(p_types) and run_after <= now()
    order by priority desc, created_at
    limit 1 for update skip locked)
  returning j.*;
end $$;

-- Claude Bridge: claim a batch of pending requests (highest priority first).
create or replace function claim_llm_requests(p_limit int, p_claimer text)
returns setof llm_requests language plpgsql as $$
begin
  return query
  update llm_requests r set status = 'claimed', claimed_by = p_claimer, claimed_at = now()
  where r.id in (
    select id from llm_requests
    where status = 'pending' or (status = 'claimed' and claimed_at < now() - interval '15 minutes')
    order by priority desc, created_at
    limit p_limit for update skip locked)
  returning r.*;
end $$;

-- A task assigned to Claude becomes a bridge request automatically.
create or replace function task_to_llm_request() returns trigger language plpgsql as $$
begin
  if new.assigned_agent = 'claude' then
    insert into llm_requests(kind, requested_by, task_id, payload, priority, deadline_at)
    values ('task', 'dashboard', new.id,
            jsonb_build_object('title', new.title, 'instruction', new.instruction),
            90, now() + interval '60 minutes');
  end if;
  return new;
end $$;
drop trigger if exists tasks_to_bridge on tasks;
create trigger tasks_to_bridge after insert on tasks for each row execute function task_to_llm_request();

-- ───────────────────────── security (dashboard = authenticated users) ─────────────────────────
do $$
declare t text;
begin
  foreach t in array array['streamers','streams','stream_signals','moments','clips','qa_reports','publications',
                           'metrics','agents','agent_events','jobs','llm_requests','tasks','plans','rules',
                           'settings','heartbeats']
  loop
    execute format('alter table %I enable row level security', t);
    execute format('drop policy if exists "auth full access" on %I', t);
    execute format('create policy "auth full access" on %I for all to authenticated using (true) with check (true)', t);
  end loop;
end $$;

-- ───────────────────────── realtime ─────────────────────────
do $$
declare t text;
begin
  foreach t in array array['agents','agent_events','jobs','clips','tasks','plans','llm_requests','streams','heartbeats','settings']
  loop
    begin
      execute format('alter publication supabase_realtime add table %I', t);
    exception when duplicate_object then null;
    end;
  end loop;
end $$;

-- ───────────────────────── storage ─────────────────────────
insert into storage.buckets (id, name, public) values ('clips', 'clips', false)
on conflict (id) do nothing;
drop policy if exists "auth read clips" on storage.objects;
create policy "auth read clips" on storage.objects for select to authenticated using (bucket_id = 'clips');
