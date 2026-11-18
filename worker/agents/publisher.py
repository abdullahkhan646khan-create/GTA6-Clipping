"""Publisher: sends approved Shorts to YouTube through Buffer, respecting cadence, caps, permission and kill switch."""
import time
from datetime import timedelta

from ..core import buffer, config, db, rules
from ..core.agent import Agent


class Publisher(Agent):
    type = "publisher"
    poll_s = 30

    def loop(self):
        ks = db.setting("kill_switch", {}) or {}
        if ks.get("publishing") or ks.get("all"):
            self.status("publishing paused (kill switch)", "stopped")
            return time.sleep(self.poll_s)
        if not (config.BUFFER_API_KEY and config.BUFFER_CHANNEL_ID):
            self.status("Buffer not configured (BUFFER_API_KEY / BUFFER_CHANNEL_ID)", "error")
            return time.sleep(120)
        R = rules.get()["publish"]
        cap = int(db.setting("daily_post_cap", 10) or 10)
        gap = int(db.setting("min_post_gap_minutes", 30) or 30)
        since = db.iso(db.now() - timedelta(hours=24))
        recent = db.sb().table("publications").select("clip_id,scheduled_for,created_at") \
            .gte("created_at", since).in_("status", ["scheduled", "published"]) \
            .order("created_at", desc=True).execute().data
        approved = db.sb().table("clips").select("*").eq("status", "approved") \
            .order("final_score", desc=True).limit(20).execute().data
        if not approved:
            self.status(f"nothing approved · {len(recent)}/{cap} posted in 24h", "idle",
                        next_steps=["approve clips in the dashboard (or raise autopilot level)"])
            return time.sleep(self.poll_s)
        if len(recent) >= cap:
            self.status(f"daily cap reached ({cap})", "idle")
            return time.sleep(self.poll_s)
        if recent:
            last = db.ts(recent[0]["scheduled_for"] or recent[0]["created_at"])
            wait = (last + timedelta(minutes=gap) - db.now()).total_seconds()
            if wait > 0:
                self.status(f"next post in {wait / 60:.0f} min · {len(approved)} approved waiting", "idle",
                            next_steps=[f"publish “{approved[0]['title']}”"])
                return time.sleep(min(self.poll_s, wait))

        # streamer rotation: no more than N in a row from the same streamer
        last_ids = [r["clip_id"] for r in recent[: R["max_same_streamer_in_row"]]]
        last_streamers = {c["streamer_id"] for c in db.sb().table("clips").select("streamer_id")
                          .in_("id", last_ids).execute().data} if last_ids else set()
        same_run = len(last_ids) == R["max_same_streamer_in_row"] and len(last_streamers) == 1
        for clip in approved:
            streamer = db.get("streamers", id=clip["streamer_id"]) if clip.get("streamer_id") else {}
            if R["require_permission"] and (streamer or {}).get("permission_status") not in ("granted", "open_policy"):
                if clip.get("status_reason") != "blocked: no permission":
                    db.update("clips", {"id": clip["id"]}, {"status_reason": "blocked: no permission"})
                    self.log(f"blocked “{clip['title']}”: {(streamer or {}).get('name')} has not granted permission",
                             "warn", clip_id=clip["id"])
                continue
            if same_run and clip["streamer_id"] in last_streamers:
                continue
            self._publish(clip)
            return
        self.status("approved clips are waiting on permission/rotation rules", "idle")
        time.sleep(self.poll_s)

    def _publish(self, clip):
        self.status(f"publishing “{clip['title']}”")
        url = db.signed_url(clip["video_path"], 7 * 86400)
        pub = db.insert("publications", {"clip_id": clip["id"], "channel": "buffer", "status": "queued",
                                         "scheduled_for": db.iso()})[0]
        try:
            # shareNow: ClipForge already enforces cadence/caps, so Buffer posts immediately
            post = buffer.create_video_post(config.BUFFER_CHANNEL_ID, clip["title"], clip["description"], url)
            db.update("publications", {"id": pub["id"]}, {"status": "scheduled", "buffer_post_id": post["id"]})
            db.update("clips", {"id": clip["id"]}, {"status": "scheduled", "updated_at": db.iso()})
            self.log(f"📤 scheduled on Buffer: “{clip['title']}”", "success", clip_id=clip["id"])
        except Exception as e:
            db.update("publications", {"id": pub["id"]}, {"status": "failed", "error": str(e)[:500]})
            db.update("clips", {"id": clip["id"]}, {"status": "review", "status_reason": f"Buffer error: {e}"[:300]})
            self.log(f"Buffer publish failed: {e}", "error", clip_id=clip["id"])
