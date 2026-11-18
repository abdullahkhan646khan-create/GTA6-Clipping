"""10-minute heartbeat (Render Cron Job): Supervisor + Scout.
- Health: mark dead agents offline, re-queue jobs held by dead agents.
- Scout: which watchlist streamers are live with GTA 6 right now (+ YouTube search every 30 min if a key is set).
- Plan: pick which streams get recording slots, write the plan the dashboard shows.
- Safety: if requests wait and Claude hasn't been seen, wake it; prune old data.
Run: python -m worker.heartbeat
"""
import re
import time
from datetime import timedelta

import requests

from .core import config, db, wake, ytdl

GTA = re.compile(r"gta\s*-?\s*(6|vi)\b|grand\s+theft\s+auto\s*(6|vi)\b|vice\s*city|leonida", re.I)
ME = f"supervisor@{config.NODE}"


def health() -> dict:
    cutoff = db.now() - timedelta(minutes=2)
    agents = db.sb().table("agents").select("*").execute().data
    dead = [a for a in agents if a["status"] != "offline" and db.ts(a["last_heartbeat"]) < cutoff]
    for a in dead:
        db.update("agents", {"id": a["id"]}, {"status": "offline", "current_action": "no heartbeat"})
    dead_ids = [a["id"] for a in dead] + [a["id"] for a in agents if a["status"] == "offline"]
    requeued = 0
    if dead_ids:
        stuck = db.sb().table("jobs").select("id,type").eq("status", "running").in_("locked_by", dead_ids).execute().data
        for j in stuck:
            db.update("jobs", {"id": j["id"]}, {"status": "queued", "locked_by": None, "updated_at": db.iso()})
            requeued += 1
        if stuck:
            db.event(ME, f"re-queued {len(stuck)} job(s) from dead agents", "warn")
    alive = [a for a in agents if a not in dead and a["status"] != "offline"]
    return {"agents_alive": len(alive), "agents_dead": len(dead), "requeued": requeued}


def scout() -> list[dict]:
    found = []
    streamers = db.sb().table("streamers").select("*").eq("watch", True).order("priority", desc=True).execute().data
    for s in streamers:
        info, level, err = ytdl.probe(s["channel_url"].rstrip("/") + "/live", timeout=45)
        if not info:
            if ytdl.is_blocked(err):
                db.event(ME, f"YouTube blocked the scout ({level or 'all levels'}) — check capture ladder", "error")
                break
            continue
        if not info.get("is_live"):
            continue
        title = info.get("title") or ""
        if not GTA.search(title + " " + " ".join(info.get("tags") or [])):
            continue
        row = {"video_id": info["id"], "url": f"https://www.youtube.com/watch?v={info['id']}", "title": title,
               "streamer_id": s["id"], "concurrent_viewers": info.get("concurrent_view_count"),
               "last_seen_live": db.iso()}
        existing = db.get("streams", video_id=info["id"])
        if existing:
            db.update("streams", {"id": existing["id"]}, {k: v for k, v in row.items() if k != "video_id"})
            row = {**existing, **row}
        else:
            row = db.insert("streams", {**row, "status": "live"})[0]
            db.event(ME, f"🔴 {s['name']} is LIVE with GTA 6: {title[:80]}", "success")
        found.append({**row, "_priority": s["priority"], "_name": s["name"]})
    return found + search_api()


def search_api() -> list[dict]:
    """YouTube Data API search (100 units) — at most every 30 min to stay within the free 10k/day quota."""
    if not config.YOUTUBE_API_KEY:
        return []
    last = db.setting("last_api_search")
    if last and db.now() - db.ts(last) < timedelta(minutes=30):
        return []
    db.set_setting("last_api_search", db.iso())
    try:
        s = requests.get("https://www.googleapis.com/youtube/v3/search", timeout=20, params={
            "part": "snippet", "q": "GTA 6", "eventType": "live", "type": "video", "order": "viewCount",
            "maxResults": 15, "relevanceLanguage": "en", "key": config.YOUTUBE_API_KEY}).json()
        ids = [i["id"]["videoId"] for i in s.get("items", [])]
        if not ids:
            return []
        v = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=20, params={
            "part": "snippet,liveStreamingDetails", "id": ",".join(ids), "key": config.YOUTUBE_API_KEY}).json()
    except Exception as e:
        db.event(ME, f"YouTube API search failed: {e}", "warn")
        return []
    out = []
    for it in v.get("items", []):
        sn, ld = it["snippet"], it.get("liveStreamingDetails", {})
        viewers = int(ld.get("concurrentViewers", 0) or 0)
        if viewers < 2000:
            continue
        ch_url = f"https://www.youtube.com/channel/{sn['channelId']}"
        st = db.get("streamers", channel_url=ch_url)
        if not st:
            st = db.insert("streamers", {"name": sn["channelTitle"], "channel_url": ch_url, "priority": 40,
                                         "notes": "auto-discovered by Scout"})[0]
            db.event(ME, f"discovered new GTA 6 streamer: {sn['channelTitle']} ({viewers:,} watching)", "info")
        row = {"video_id": it["id"], "url": f"https://www.youtube.com/watch?v={it['id']}", "title": sn["title"],
               "streamer_id": st["id"], "concurrent_viewers": viewers, "last_seen_live": db.iso()}
        ex = db.get("streams", video_id=it["id"])
        row = db.update("streams", {"id": ex["id"]}, row)[0] if ex else db.insert("streams", {**row, "status": "live"})[0]
        out.append({**row, "_priority": st["priority"], "_name": st["name"]})
    return out


def plan(live: list[dict]) -> dict:
    slots = int(db.setting("max_parallel_streams", 2) or 2)
    # rank: priority × log-ish viewers
    def rank(s):
        return s["_priority"] * (1 + min((s.get("concurrent_viewers") or 0) / 50000, 2))
    ranked = sorted({s["video_id"]: s for s in live}.values(), key=rank, reverse=True)
    chosen = ranked[:slots]
    recording = db.sb().table("streams").select("video_id").eq("status", "recording").execute().data
    rec_ids = {r["video_id"] for r in recording}
    active_jobs = db.sb().table("jobs").select("payload").eq("type", "record_stream") \
        .in_("status", ["queued", "running"]).execute().data
    job_stream_ids = {j["payload"].get("stream_id") for j in active_jobs}
    assigned = []
    for s in chosen:
        if s["video_id"] in rec_ids or s["id"] in job_stream_ids:
            continue
        if len(rec_ids) + len(assigned) >= slots:
            break
        db.enqueue("record_stream", {"stream_id": s["id"]}, priority=80)
        assigned.append(s["_name"])
        db.event(ME, f"assigned recording slot → {s['_name']}", "success")
    p = {"record": [f"{s['_name']} ({s.get('concurrent_viewers') or '?'} viewers)" for s in chosen],
         "newly_assigned": assigned, "live_found": len(ranked), "slots": slots,
         "queue": queue_counts()}
    rationale = (f"{len(ranked)} GTA 6 streams live; recording the top {len(chosen)} by priority × viewers. "
                 + (f"Started: {', '.join(assigned)}." if assigned else "No changes needed."))
    db.insert("plans", {"plan": p, "rationale": rationale})
    return p


def queue_counts() -> dict:
    out = {}
    for st in ("cutting", "awaiting_brief", "editing", "awaiting_qa", "review", "approved", "scheduled"):
        out[st] = db.sb().table("clips").select("id", count="exact").eq("status", st).execute().count
    return out


def bridge_safety():
    pend = db.sb().table("llm_requests").select("id,created_at").eq("status", "pending").execute().data
    if not pend:
        return
    b = db.setting("bridge", {}) or {}
    seen, woke = db.ts(b.get("last_seen_at")), db.ts(b.get("last_wake_at"))
    oldest = min(db.ts(p["created_at"]) for p in pend)
    if (db.now() - oldest).total_seconds() > 300 and (not woke or (db.now() - woke).total_seconds() > 300) \
            and (not seen or (db.now() - seen).total_seconds() > 300):
        ok = wake.wake("heartbeat safety", len(pend))
        db.event(ME, f"safety wake for {len(pend)} waiting request(s): {'ok' if ok else 'failed'}", "warn")


def prune():
    db.sb().table("stream_signals").delete().lt("ts", db.iso(db.now() - timedelta(hours=24))).execute()
    db.sb().table("agent_events").delete().lt("created_at", db.iso(db.now() - timedelta(days=3))).execute()
    b = db.setting("bridge", {}) or {}
    day = db.now().strftime("%Y-%m-%d")
    if b.get("day") != day:
        b.update({"day": day, "wakes_today": 0})
        db.set_setting("bridge", b)


def main():
    t0 = time.time()
    db.event(ME, "heartbeat: checking agents, scouting streams, planning", "info")
    h = health()
    live = []
    try:
        live = scout()
    except Exception as e:
        db.event(ME, f"scout error: {e}", "error")
    p = plan(live)
    bridge_safety()
    prune()
    summary = {**h, "live_streams": len(live), "plan": p, "took_s": round(time.time() - t0, 1)}
    db.insert("heartbeats", {"summary": summary})
    db.event(ME, f"heartbeat done: {h['agents_alive']} agents alive, {len(live)} GTA 6 streams live", "success")


if __name__ == "__main__":
    main()
