# ClipForge — go-live checklist (≈45 min)

## 1. Supabase (free)
1. supabase.com → New project.
2. SQL Editor → paste & run `supabase/migrations/001_init.sql`, then `supabase/seed.sql`.
3. Authentication → Users → **Add user** (your email + password) — this is your dashboard login.
4. Project Settings → API → copy **URL**, **anon key**, **service_role key**.

## 2. GitHub
1. Create a **private** repo and push this folder (ask Claude: "commit and push to <repo url>").
2. Open an issue titled **"ClipForge doorbell"** — note its number (usually `1`). Agents comment on it to wake Claude.
3. Settings → Developer settings → Fine-grained token → this repo only → **Issues: Read & write** → copy.

## 3. Render
1. New → **Blueprint** → pick the repo → it creates `clipforge-engine`, `clipforge-heartbeat`, `clipforge-dashboard`.
2. Fill the **clipforge-secrets** group: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `GITHUB_TOKEN`,
   `GITHUB_REPO` (owner/repo), `GITHUB_WAKE_ISSUE`, `YOUTUBE_API_KEY` (optional), `BUFFER_API_KEY`.
   Dashboard service: `SUPABASE_URL`, `SUPABASE_ANON_KEY`.
3. **Capture test first** — engine → Shell:
   `python -m worker.tools.capture_test https://www.youtube.com/watch?v=<a LIVE GTA 6 stream>`
   - ✅ C1 works → done.
   - ❌ bot check → export cookies from a **burner** Google account (cookies.txt extension), `base64 -i cookies.txt`,
     put it in `YTDLP_COOKIES_B64`, redeploy, test again (C2).
   - Still ❌ → add `YTDLP_PROXY` (static ISP proxy) → C3.
4. Buffer channel id — engine → Shell: `python -m worker.tools.buffer_setup` → put the YouTube id in `BUFFER_CHANNEL_ID`.

## 4. Wake Claude (event-driven)
Tell Claude: **"set up the ClipForge routine"**. It creates a Claude Code cloud routine fired by comments on the
doorbell issue, with prompt "use the gta6-clipper skill and process the queue". Needs `SUPABASE_URL` and
`SUPABASE_SERVICE_ROLE_KEY` as routine secrets.
Optional fast lane while you're at the Mac: tell Claude **"listen for ClipForge work"** — it runs
`python -m worker.tools.bridge_cli wait` in the background and wakes the moment a request lands.

## 5. Go
- Open the dashboard URL → sign in.
- **Streamers** page: set `permission_status = granted` for streamers who said yes (publishing is blocked otherwise).
- Autopilot stays **L0** (you approve every clip) until you trust it.
- Paste a live stream in **Streams → Record** to test immediately, or wait for the 10-min heartbeat to pick streams.
