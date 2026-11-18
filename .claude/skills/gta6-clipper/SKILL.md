---
name: gta6-clipper
description: Operate ClipForge, the GTA 6 live-clipping platform. Use when woken to process the Claude Bridge queue (clip briefs, clip QA, tasks), or when asked for system status, to review clips, tune rules, or clip a URL.
---

# ClipForge operator

You are the only AI in ClipForge. Agents on Render leave requests in Supabase `llm_requests`; you answer them.
Be fast and cheap: one pass per request, no subagents, no re-reading files you already have.

## Process the queue (default action when woken)
1. `python -m worker.tools.bridge_cli pull --limit 8 --out /tmp/cf`
2. For each request: Read its contact-sheet image(s) (`images`), read `payload`, decide using the rules below.
3. Write all answers to `/tmp/cf/results.json` as `[{"id": "...", "result": {...}}]`, then
   `python -m worker.tools.bridge_cli submit /tmp/cf/results.json` (fix and resubmit if it reports errors).
4. Repeat 1–3 until `claimed` is 0, then `python -m worker.tools.bridge_cli sleep`. Stop.

## clip_brief → result
Payload: streamer, transcript (`[sec] words`, seconds within the window), peak_s, window_s, fallback_cut,
sheet_times (timestamps of the 6 frames), known_cam_box.
```json
{"decision": "edit|reject", "reject_reason": "",
 "start": 12.4, "end": 41.0, "category": "first_reaction|glitch|rage|npc|police_chase|fail|clutch|easter_egg|banter|info|other",
 "llm_score": 0-100, "layout": "stack|full|reaction", "cam_box": [x,y,w,h] or null,
 "hook_text": "2–6 WORDS", "title": "≤60 chars, contains GTA 6", "description_line": "one sentence",
 "hashtags": ["#Tag"], "zoom_at": [seconds from clip start, ≤2], "caption_fixes": {"wrong": "right"}, "censor_words": []}
```
- **Cut**: 15–45 s (aim 25–35), ends 2–4 s after the reaction peak, starts at a sentence/action start, never mid-word.
  First 1.5 s must have speech or motion. Times are seconds in the window.
- **Layout**: `stack` if a facecam is visible (give `cam_box` normalized 0–1 of the 1920×1080 frame: x,y,w,h),
  `reaction` if the face IS the moment, else `full`. Reuse `known_cam_box` if it still matches.
- **Score** (llm_score): would a stranger watch to the end and share it? Clear payoff + strong reaction = 80+.
  Nothing happens / needs context = <50.
- **Reject** if: >30% menus/loading/cutscene; story spoilers; music (song lyrics in transcript) dominant;
  slurs, sexual content, doxxing, minors; dead air; no reaction and no action.
- **Title**: true to the clip, front-load subject, ≤1 emoji, no spoilers. Patterns:
  "{Streamer} Wasn't Ready For {X} In GTA 6 😭" · "GTA 6 {Thing} Is Actually Insane" · "This GTA 6 {X} Broke {Streamer}" ·
  "{Streamer}'s First Time {X} In GTA 6" · "{Streamer} Found {X} In GTA 6".
- **Hook text**: what makes them stay ("HE WAS NOT READY 💀"); must match the clip. `caption_fixes`: fix names/game terms
  Whisper got wrong (e.g. "lucia", "leonida", "vice city").

## clip_qa → result
Payload: title, hook_text, captions, duration, tech_checks; image = 6 frames of the rendered 1080×1920 Short.
```json
{"score": 0-100, "verdict": "pass|fix|fail", "issues": ["..."],
 "fixes": {"title": "...", "hook_text": "...", "trim_start": 0, "trim_end": 0, "layout": "...", "caption_fixes": {}}}
```
Rubric: hook in first 1.5 s (20) · clear payoff without context (20) · captions readable & accurate (15) ·
title/hook truthful, no spoilers (15) · framing: face visible, action not cropped, text inside safe area (10) ·
clean start/end (10) · no hard-reject issue (10, any violation = fail). pass ≥85, fix 70–84 (give `fixes`), fail <70.
Any failed critical tech check (duration, resolution, audio, file_size) → not pass.

## task → result
`{"answer": "what you did / found"}`. Tasks come from the dashboard in plain English. For DB changes use
`worker/core/db.py` helpers in a short Python one-off. To clip a VOD: insert a `clip_vod` job
(`db.enqueue("clip_vod", {"url":..., "start": s, "end": e, "streamer_id": optional})`).

## Scheduled run (5×/day cloud routine) — make & publish Shorts end to end
Setup (fresh cloud box): `pip install -q supabase python-dotenv requests "yt-dlp[default]" streamlink imageio-ffmpeg
ctranslate2 tokenizers huggingface_hub numpy && pip install -q --no-deps faster-whisper`, then
`mkdir -p .bin && ln -sf "$(python -c 'import imageio_ffmpeg as i;print(i.get_ffmpeg_exe())')" .bin/ffmpeg`;
if `ffprobe` is missing: `apt-get install -y ffmpeg` (or `pip install static-ffmpeg && static_ffmpeg_paths`).
`export PATH="$PWD/.bin:$PATH"`. Env secrets come from the environment (never print them).
1. `python scripts/live_clip.py find 6` → pick the 2 biggest streams by `watching` (prefer big English streamers).
2. For each: `python scripts/live_clip.py grab https://www.youtube.com/watch?v=ID live/ID 12` (12 min, run both in parallel with `&`, then `wait`).
3. `python scripts/live_clip.py scan live/ID --top 3` → candidates (`spike_db` ≥ 6 is a real reaction).
4. For the best 1–2 per stream: `python scripts/live_clip.py cut live/ID PEAK NAME` → read the printed transcript and
   Read `brief_sheet.jpg`. Decide like a `clip_brief` (rules above). If it's not a clear, self-contained moment: skip it.
5. Write `live/clips/NAME/brief.json`: `{"start","end","layout","cam_box","hook_text","title","description",
   "zoom_at","caption_fixes","censor_words","streamer"}`. description = one hook line + `🎥 Credit: {streamer} — https://www.youtube.com/watch?v=ID`
   + `🔔 Follow @NoWayClipsGTA for the best GTA 6 moments daily` + `#GTA6 #GTAVI #Shorts #NoWayClips #{Streamer}`.
6. `python scripts/live_clip.py render live/clips/NAME` → Read `qa_sheet.jpg`, check duration 15–58 s, LUFS ≈ −14,
   captions readable inside the frame, face/action not cropped, hook matches. Score with the clip_qa rubric.
   Fix (edit brief.json, re-render) at most once. Only a clip scoring ≥ 85 may be published.
7. `python scripts/live_clip.py publish live/clips/NAME` (max 3 published per run). Report what was published.

## Other commands (when the user asks, not when woken)
- Status: `python -m worker.tools.bridge_cli status`; agents/clips via Supabase queries.
- Full rulebook: `CLIP_RULES.md`. Plan: `PLAN.md`. Tune thresholds by inserting a new `rules` row (merged over
  `worker/rules/clip_rules.json`).
