# ClipForge — Multi-Agent Live Clipping Platform (GTA 6)

> Goal: watch the biggest GTA 6 live streams **while they are live**, detect viral moments in real time,
> cut + edit them into vertical Shorts, write titles/captions, QA them, and publish to YouTube via Buffer —
> with a live dashboard showing what every agent is doing and planning, and the ability to assign tasks.

Launch: **GTA 6 releases Thu Nov 19, 2026.** Launch week = maximum search demand, minimum competition. Speed matters more than perfection: target **moment → published in under 15 minutes**.

---

## 1. Architecture at a glance

```
                ┌──────────────────────── SUPABASE ────────────────────────┐
                │ Postgres (state + job queue)  ·  Storage (clips)  ·  Realtime │
                └───────▲──────────▲───────────▲───────────▲──────────────────┘
                        │          │           │           │ realtime
   ┌────────────────────┴───┐ ┌────┴─────┐ ┌───┴──────┐ ┌──┴──────────────────┐
   │ RENDER: worker (Docker) │ │ RENDER:  │ │ RENDER:  │ │ RENDER: dashboard    │
   │  multi-agent runtime    │ │ cron     │ │ capture  │ │ (Next.js)            │
   │  Detector, Cutter,      │ │ */10 min │ │ node(s)  │ │ Mission Control,     │
   │  Editor, Writer, QA,    │ │ Supervisor│ │ Recorder │ │ agents, clips,       │
   │  Publisher, Analyst,    │ │ heartbeat│ │ + Chat   │ │ tasks, rules         │
   │  Assistant              │ │ + Scout  │ │ listener │ │                      │
   └─────────────────────────┘ └──────────┘ └──────────┘ └──────────────────────┘
          │ llm_requests table ◄──► CLAUDE BRIDGE (Claude Code on a scheduler, no paid LLM API)
          │ local faster-whisper (free, on the worker CPU)
          └── Buffer API → YouTube Shorts       │ YouTube live (cloud capture, see §6)
```

**Key design choice:** every agent talks only to Supabase. No agent calls another directly. Work moves through a
`jobs` table (a queue), and every action is logged to `agent_events`. That gives us:
- the dashboard sees everything in real time (Supabase Realtime),
- agents can run on **any machine** (Render, your Mac, a VPS) — just start the process with the env vars,
- crashes are safe: the 10-min heartbeat re-queues stuck jobs,
- you can assign tasks by inserting a row (from the dashboard).

---

## 2. The agents (multi-agent system)

| # | Agent | Runs where | Job | Output |
|---|---|---|---|---|
| 0 | **Supervisor** | Cron, every 10 min (heartbeat) + on demand | Health-checks all agents, re-queues stuck jobs, decides which streams get recording slots, writes the **plan** for the next 2 hours (shown on dashboard), enforces kill switch + quotas | `plans`, `heartbeats` |
| 1 | **Scout** | Called by Supervisor each heartbeat | Finds live GTA 6 streams: checks the watchlist channels' `/live` pages (no API quota) + YouTube search for new big streams; ranks by concurrent viewers & growth | `streams` |
| 2 | **Recorder** (1 per stream) | Capture node | Records the live stream into a rolling buffer of 6-second segments (last 20 min kept), no re-encode | segments on disk |
| 3 | **Chat Listener** (1 per stream) | Capture node | Reads live chat (no API quota via `chat-downloader`), computes message velocity + hype-keyword rate per second | `stream_signals` |
| 4 | **Detector** | Worker | Every 10 s, scores the latest window (chat spike + audio spike + viewer jump). When score passes threshold → waits for post-roll → creates a **moment** | `moments` |
| 5 | **Cutter** | Worker | Pulls the segments around the moment, transcribes (word timestamps), asks Claude to pick exact start/end + category + virality score | `clips` (raw) |
| 6 | **Editor** | Worker | Builds the vertical Short per CLIP_RULES.md: layout, captions, hook text, zooms, SFX, loudness, credit. Uploads MP4 to Supabase Storage | `clips` (rendered) |
| 7 | **Writer** | Worker | Title, description, hashtags, hook text (Claude), following the title rules | `clips.copy` |
| 8 | **QA** | Worker | Automated technical checks + Claude review of frames/transcript/title against the rulebook → score + pass/fail + reasons | `qa_reports` |
| 9 | **Publisher** | Worker | Schedules approved clips via Buffer (fallback: YouTube Data API), respects cadence rules, records the posted URL | `publications` |
| 10 | **Analyst** | Cron (hourly) | Pulls views/retention at 1h/3h/24h, updates category & streamer weights → the system learns what goes viral | `metrics`, `weights` |
| 11 | **Assistant** | Worker | Handles **tasks you assign** from the dashboard in plain English ("clip 12:30–13:10 of this VOD", "rewrite today's titles", "find trending GTA 6 topics") | `tasks` |
| 12 | **Claude (the Bridge)** | Claude Code session on a scheduler (see §12) | The only "brain": answers every `llm_requests` row — clip briefs (cut points, score, hook, title, description), visual QA, the Supervisor's plan, free-text tasks. No paid LLM API | `llm_requests.result` |

Every agent:
- registers itself in `agents` and heartbeats every 15 s (`status`, `current_job`, `current_action`, `next_steps`),
- claims work with a safe queue claim (`FOR UPDATE SKIP LOCKED`), so you can run 1 or 10 copies of an agent,
- logs every step to `agent_events` (that's the live feed on the dashboard).

### Autopilot levels (set on dashboard)
- **L0 Manual** – everything waits for your Approve click. *(Start here tonight.)*
- **L1 Assisted** – clips with final score ≥ 80 **and** QA ≥ 90 auto-publish; the rest wait for you.
- **L2 Full auto** – everything that passes QA publishes; you only watch.
- **Kill switch** – pauses publishing instantly; a second switch pauses all agents.

---

## 3. How live clipping works (no waiting for the stream to end)

```
t = live edge
 ├─ Recorder writes seg_000123.ts (6 s) every 6 s → ring buffer of last 20 min
 ├─ Chat Listener writes per-second chat stats
 ├─ Detector (every 10 s): score window → SPIKE at t-4s
 │     chat lags the action by ~4–8 s → shift peak back (per-stream calibrated)
 ├─ wait 15 s post-roll (let the reaction finish)
 ├─ Cutter: concat segments [peak-40s, peak+15s] → transcribe → Claude picks exact cut (15–45 s)
 ├─ Editor: render 1080×1920 (~1–2 min)        ← all while the stream keeps going
 ├─ Writer + QA (~30 s)
 └─ Publisher → Buffer → YouTube Shorts         ⇒ ~5–15 min after the moment happened
```

Recording keeps running the whole time; we never touch the stream file except to read finished segments.

---

## 4. Supabase schema (first migration)

| Table | Key columns |
|---|---|
| `streamers` | id, name, platform, channel_url, handle, language, **permission_status** (not_asked/asked/granted/denied/open_policy), layout_preset (facecam box coords), priority, weight |
| `streams` | id, streamer_id, video_id, url, title, status (live/recording/ended), concurrent_viewers, started_at, recording_node, chat_delay_s |
| `stream_signals` | stream_id, ts, chat_rate, hype_rate, audio_lufs, viewers (1 row / sec; pruned after 24 h) |
| `moments` | id, stream_id, peak_ts, signal_score, reasons jsonb, status |
| `clips` | id, moment_id, start_s, end_s, category, llm_score, final_score, transcript jsonb, layout, raw_path, video_path, title, description, hashtags, hook_text, status (cutting/editing/writing/qa/review/approved/scheduled/published/rejected) |
| `qa_reports` | clip_id, score, checks jsonb, issues text[], verdict |
| `publications` | clip_id, channel, buffer_update_id, youtube_video_id, url, scheduled_for, published_at, status |
| `metrics` | publication_id, checked_at, views, avg_view_pct, likes, comments, subs_gained |
| `agents` | id, type, node, status (idle/working/error/stopped), current_job_id, current_action, next_steps jsonb, last_heartbeat |
| `agent_events` | id, agent_id, level, message, data jsonb, created_at  ← live feed |
| `jobs` | id, type, payload jsonb, status (queued/running/done/failed), priority, attempts, locked_by, locked_at, run_after |
| `tasks` | id, title, instruction, assigned_agent, status, result, created_by — **your assignments** |
| `plans` | id, created_at, horizon, plan jsonb, rationale — Supervisor's plan |
| `rules` | version, rules jsonb, active — editable rulebook (seeded from CLIP_RULES.md) |
| `settings` | autopilot_level, kill_switch, max_parallel_streams, daily_post_cap, … |
| `heartbeats` | ts, summary jsonb (agents up, queue depth, errors) |
| `llm_requests` | id, kind (clip_brief/clip_qa/plan/task), requested_by, payload jsonb, attachments text[] (frame contact sheets in Storage), priority, status (pending/claimed/done/failed/expired), claimed_at, result jsonb, deadline_at |

Plus a `claim_job(agent_type text)` SQL function, Realtime enabled on `agents`, `agent_events`, `jobs`, `clips`, `tasks`, `plans`, and a private Storage bucket `clips`.

---

## 5. Dashboard (Next.js + Supabase Realtime, login-protected)

1. **Mission Control** – pipeline flow diagram (Discover → Record → Detect → Cut → Edit → Write → QA → Publish) with live counts per stage, animated as jobs move; active agents with "doing now / next"; live event feed; last heartbeat; kill switches; autopilot level.
2. **Live Streams** – every stream being recorded: viewers, live hype graph (chat + audio score over time with detected moments as dots), start/stop recording, "clip the last 60 s now" button.
3. **Clips** – review queue with the video player, score breakdown, QA report, transcript; buttons **Approve · Edit title · Re-render · Reject · Publish now**.
4. **Agents** – one card per agent: status, current action, planned next steps, last 50 events, restart/pause, **Assign task**.
5. **Tasks** – create a task in plain English, choose an agent (or "auto"), watch progress + result.
6. **Plan** – the Supervisor's current 2-hour plan and why (which streams, posting schedule, risks).
7. **Publishing** – queue/calendar, Buffer status, links to live Shorts.
8. **Analytics** – views & retention per clip, per streamer, per category, best posting hours.
9. **Streamers** – watchlist + permission tracker + facecam layout preset editor.
10. **Rules** – edit thresholds and rulebook (versioned, takes effect without redeploy).
11. **Settings** – API connection health, YouTube quota used today, Claude spend today.

---

## 6. Render deployment

| Service | Type | Notes |
|---|---|---|
| `clipforge-worker` | Background Worker (Docker: Python + ffmpeg) | Runs Detector/Cutter/Editor/Writer/QA/Publisher/Assistant. Needs CPU for rendering → Pro plan (4 GB / 2 CPU) or bigger; scale by adding instances |
| `clipforge-capture` | Background Worker (Docker) | Recorder + Chat Listener. 1 instance ≈ 3–5 streams at 1080p30 copy-mode |
| `clipforge-heartbeat` | Cron Job `*/10 * * * *` | Supervisor + Scout + stuck-job recovery + plan |
| `clipforge-analyst` | Cron Job `0 * * * *` | Metrics pull & weight updates |
| `clipforge-dashboard` | Web Service (Node) | Next.js dashboard |

All defined in one `render.yaml` (Blueprint) so one push deploys everything.

### ⚠️ Biggest technical risk: YouTube blocks datacenter IPs — cloud capture solution (no Mac)
YouTube often answers cloud IPs (Render included) with "Sign in to confirm you're not a bot". The capture agent
gets a **escalation ladder**; the Supervisor detects bot-check errors and moves up a level automatically:

| Level | Method | Cost |
|---|---|---|
| C1 | Latest `yt-dlp` + **PO-token provider plugin** (`bgutil-ytdlp-pot-provider`) | Free |
| C2 | C1 + **cookies from a burner Google account** (never your main account) | Free |
| C3 | C2 through a **static ISP proxy** (unlimited bandwidth plans, a few $/month per IP) | ~$2–5/mo |

Everything runs on **Render only** — no other servers.

**Light capture mode** (cuts bandwidth ~95%, makes C3 cheap): record **audio only** continuously (≈60 MB/hour/stream)
for the loudness signal, and pull **video only for detected moments** from the live DVR window
(`[peak−40 s, peak+15 s]`). Full-video ring-buffer mode stays available when the IP is clean.

**First thing we build tonight is a 15-minute capture test on Render** — it tells us which level we need before we build anything on top.

### Quotas to respect
- YouTube Data API default **10,000 units/day**: `search.list` = 100 units (so max ~1 search per 30 min), `videos.list` = 1 unit. Watchlist `/live` checks use **zero** quota. Direct uploads cost 1,600 units each (~6/day) — another reason to publish via **Buffer**.
- **Buffer API** is a limited beta with personal API keys; Buffer supports YouTube Shorts. We'll verify your key tonight; the Publisher has a YouTube-direct fallback and a "notify me" fallback.

---

## 7. Repo layout

```
clipforge/
├── worker/
│   ├── Dockerfile                # python 3.12 + ffmpeg + fonts
│   ├── main.py                   # ROLE=worker|capture → starts agents
│   ├── heartbeat.py              # cron entry (Supervisor + Scout)
│   ├── core/  db.py queue.py events.py claude.py transcribe.py media.py rules.py buffer.py youtube.py
│   └── agents/ base.py supervisor.py scout.py recorder.py chat.py detector.py cutter.py
│               editor.py writer.py qa.py publisher.py analyst.py assistant.py
├── dashboard/                    # Next.js + Tailwind + Supabase JS
├── supabase/migrations/001_init.sql
├── rules/clip_rules.yaml         # machine version of CLIP_RULES.md
├── assets/fonts/ assets/sfx/     # open-licence fonts, own/royalty-free SFX only
├── .claude/skills/gta6-clipper/  # the operator skill (see §9)
├── render.yaml
└── .env.example
```

---

## 8. Tonight's build order (realistic: ~8–10 focused hours for a working end-to-end system)

| Step | What | Done when |
|---|---|---|
| 1 | **Accounts & keys** (you): Supabase project, Render account + GitHub repo, YouTube Data API key (free), Buffer API key, burner Google account for cookies. **No LLM or transcription API needed** | Keys in `.env` (you paste them in the file — not in chat) |
| 1b | **Capture test on Render** (C1→C2→C3 ladder) on a live GTA 6 stream | We know which capture level works |
| 2 | Supabase migration: tables, `claim_job`, storage bucket, realtime | Tables visible in Supabase |
| 3 | Core libs + agent base class (register, heartbeat, events, claim jobs) | A dummy agent shows up "working" in the DB |
| 4 | Recorder + Chat Listener on **one** live stream (on your Mac) | Segments + chat signals flowing |
| 5 | Detector + Cutter | Real moments appear with scores; raw clips cut |
| 6 | Editor (layout, captions, hook, loudness) | First good-looking vertical Short on disk |
| 7 | Writer + QA | Clip has title/description + QA report |
| 8 | Publisher (Buffer) at autopilot L0 | First Short published after your approval ✅ |
| 9 | Dashboard: Mission Control, Clips, Agents, Tasks | You can watch & approve live |
| 10 | Deploy to Render (worker, cron heartbeat, dashboard); capture stays on Mac if YouTube blocks Render | Heartbeat rows every 10 min |
| 11 | Scout + Supervisor plan + multiple streams in parallel | System picks streams itself |
| Day 2+ | Analyst learning loop, Twitch/Kick sources (most top GTA streamers are there), more channels/languages, L1 autopilot | |

"Perfect" comes from the learning loop over the first week, not from night one — but the night-one system will be end-to-end and live.

---

## 9. The `gta6-clipper` skill (deep-level operator skill)

Lives in `.claude/skills/gta6-clipper/` so I (Claude) can operate and improve the platform on command:
- `SKILL.md` – how the system works, commands, safety rules, autopilot policy.
- `references/clip-rules.md` – the full rulebook (CLIP_RULES.md).
- `references/hooks-and-titles.md` – proven hook/title formulas + examples of winners (updated from analytics).
- `references/qa-checklist.md`, `references/streamers.md`, `references/troubleshooting.md`.
- `scripts/` – `status.py` (system state), `clip_url.py` (clip any URL/time range), `review.py` (review queue), `tune.py` (adjust weights from analytics), `add_streamer.py`.

Commands you'll use: *"status"*, *"what are the agents planning"*, *"clip this stream now"*, *"review the queue"*, *"why did clip X fail QA"*, *"what's performing best today and tune the rules"*.

---

## 10. Legal / channel safety (non-negotiable rules)
- Clip only streamers with `permission_status` = granted or a verified open clipping policy. A big streamer *tolerating* clips ≠ YouTube's rules: YouTube's **reused content** policy is enforced by YouTube itself and can demonetize the whole channel. (MrBeast isn't a comparison — he doesn't live-stream GTA, and his videos are edited uploads, not streams.)
- Every clip is **transformed** (captions, hook, edit, framing) and **credits** the streamer (on-screen + description link to the original stream timestamp).
- Avoid licensed music (GTA radio) and story spoilers in titles — see CLIP_RULES.md.

## 11. Rough running cost (approx.)
**LLM: $0** (Claude Bridge) · **Transcription: $0** (local faster-whisper) · Render ~$25–100/mo depending on worker size ·
Supabase free tier to start (clips deleted from Storage 7 days after publishing to stay under 1 GB) · static ISP proxy only if needed ~$2–5/mo.

---

## 12. Claude Bridge — Claude Code is the brain (no paid LLM API)

Agents never call an AI API. When they need intelligence they **leave a request in a mailbox** (`llm_requests`)
and keep working; Claude picks requests up on a schedule, does the work, and writes the answer back; the agent picks it up.

```
Cutter ──insert──► llm_requests (pending: clip_brief + transcript + 6-frame contact sheet)
                          │
     every 2 min:  Claude Code ── /gta6-clipper process-queue ──►  bridge.py pull  (claims a batch, downloads images)
                          │        Claude reads transcript + looks at frames, applies CLIP_RULES.md
                          │        bridge.py submit results.json
                          ▼
             llm_requests (done: result) ──realtime──► Cutter/Editor/Writer/QA continue
```

### Request kinds (kept to 2 per clip so we stay inside Claude plan usage limits)
| Kind | Asked by | Claude returns |
|---|---|---|
| `clip_brief` | Cutter (before render) | exact in/out, category, llm_score, layout, hook text, title, description, hashtags, reject reasons |
| `clip_qa` | QA (after render) | rubric score per R6, issues, verdict, fixes (new title, trim) |
| `plan` | Supervisor (every heartbeat) | which streams get slots, posting schedule for next 2 h, risks |
| `task` | Assistant (your dashboard tasks) | whatever the task needs |

Batching: one wake handles up to ~8 requests, payloads are compact (transcript text + one contact-sheet image).

### Never stalls — fallbacks if Claude hasn't answered by `deadline_at` (default 10 min)
- `clip_brief` → rule-based cut (sentence-snapped), keyword category, **template title** from R5 formulas.
- `clip_qa` → automatic technical checks only, then the clip goes to **your review queue** (never auto-published).
- `plan` → rule-based plan (top streams by viewers × streamer weight).

### Event-driven wake-up (no polling — Claude sleeps until there is real work)
Claude is **only woken when an agent actually needs it** — a moment passed the detector's threshold, a clip needs QA,
or you assigned a task. Idle time costs zero Claude usage.

```
Detector finds spike (rule-based, no AI) ──► llm_requests row (pending)
        │
   Dispatcher (inside the Render worker)
        ├─ debounce 60 s (collect other requests that arrive together → one wake handles a batch)
        ├─ skip if Claude is already awake (it drains the whole queue before sleeping)
        └─ WAKE ──► Claude processes ALL pending requests ──► queue empty ──► sleeps again
```

**Wake channels** (the Dispatcher picks automatically):
| Channel | How it wakes Claude | Speed | Needs |
|---|---|---|---|
| **W1. Cloud routine via GitHub event** (primary) | Dispatcher creates a GitHub event in the repo (free) → a webhook trigger fires a Claude Code **cloud routine** → it drains the queue and exits | ~1–2 min start-up | Nothing on your Mac; repo + Supabase key stored as routine secrets |
| **W2. Open Claude Code session** (optional fast lane) | A tiny listener script waits on Supabase; the moment a request lands it wakes the session | seconds | Mac awake with Claude Code open (no video work on the Mac) |

If W2 is online it's used (fastest); otherwise W1. Priority requests (your dashboard tasks) skip the debounce.
Urgency rule: during launch week a wake happens at most every ~5 min unless a request scores ≥ 85 (then immediately).

The 10-min Render heartbeat only checks health (stuck requests → re-wake or fallback); the dashboard shows
**"Claude: sleeping / working / last wake X min ago / queue N"**.

Note: this runs on your Claude subscription's usage limits — a higher plan means more clips per hour. The Analyst tracks requests per hour so we can tune batch size.
