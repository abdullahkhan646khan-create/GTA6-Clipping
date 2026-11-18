"""Bridge Watcher + Dispatcher.
- Applies Claude's answers (clip_brief → edit, clip_qa → approve/fix/reject).
- Expired requests (Claude didn't answer by the deadline) → rule-based fallback, never stalls.
- Dispatcher: wakes Claude ONLY when work is waiting (debounced, batched, urgent requests skip the wait)."""
import time

from ..core import bridge, config, db, rules, wake
from ..core.agent import Agent
from .qa import CRITICAL


class BridgeWatcher(Agent):
    type = "bridge"
    poll_s = 5

    def loop(self):
        self._consume()
        self._expire()
        self._dispatch()
        time.sleep(self.poll_s)

    # ───── results ─────
    def _consume(self):
        rows = db.sb().table("llm_requests").select("*").in_("status", ["done", "expired", "failed"]) \
            .eq("consumed", False).order("created_at").limit(20).execute().data
        for r in rows:
            try:
                by_claude = r["status"] == "done"
                if r["kind"] == "clip_brief":
                    apply_brief(r, by_claude, self)
                elif r["kind"] == "clip_qa":
                    apply_qa(r, by_claude, self)
                elif r["kind"] == "plan" and by_claude:
                    db.insert("plans", {"plan": r["result"], "rationale": (r["result"] or {}).get("rationale")})
                elif r["kind"] == "task" and not by_claude:
                    db.update("tasks", {"id": r["task_id"]}, {"status": "expired",
                                                              "result": "Claude did not pick this up in time."})
            except Exception as e:
                self.log(f"apply {r['kind']} failed: {e}", "error", request_id=r["id"])
            db.update("llm_requests", {"id": r["id"]}, {"consumed": True})

    def _expire(self):
        rows = db.sb().table("llm_requests").select("id,kind").in_("status", ["pending", "claimed"]) \
            .lt("deadline_at", db.iso()).execute().data
        for r in rows:
            db.update("llm_requests", {"id": r["id"]}, {"status": "expired"})
            self.log(f"{r['kind']} request expired → using fallback", "warn", request_id=r["id"])

    # ───── wake-up dispatcher ─────
    def _dispatch(self):
        st = bridge.pending_stats()
        b = db.setting("bridge", {}) or {}
        if not st["pending"]:
            self.status(f"Claude {b.get('state', 'sleeping')} · queue empty", "idle",
                        next_steps=["wake Claude when an agent needs it"])
            return
        age = (db.now() - st["oldest"]).total_seconds() if st["oldest"] else 0
        seen = db.ts(b.get("last_seen_at"))
        listener = db.ts(b.get("listener_seen_at"))
        awake = (b.get("state") == "working" and seen and (db.now() - seen).total_seconds() < 120) or \
                (listener and (db.now() - listener).total_seconds() < 45)  # draining now, or local listener on
        last_wake = db.ts(b.get("last_wake_at"))
        since_wake = (db.now() - last_wake).total_seconds() if last_wake else 1e9
        urgent = st["max_priority"] >= 85
        self.status(f"{st['pending']} waiting for Claude (oldest {age:.0f}s)", "working",
                    next_steps=["Claude awake — waiting" if awake else
                                f"wake in ≤{max(0, config.WAKE_DEBOUNCE_S - age):.0f}s"])
        if awake:
            return
        ready = urgent or age >= config.WAKE_DEBOUNCE_S
        min_gap = 90 if urgent else config.WAKE_MIN_GAP_S
        if ready and since_wake >= min_gap:
            ok = wake.wake("urgent" if urgent else "batch", st["pending"])
            self.log(f"woke Claude ({st['pending']} pending, {'urgent' if urgent else 'batched'})" if ok
                     else "wake attempt failed (check WAKE_METHOD / GitHub token)", "info" if ok else "warn")


# ───────────────────────── applying results ─────────────────────────
def _final_score(clip: dict, llm_score: float | None, category: str | None, streamer: dict | None, R: dict):
    s = R["scoring"]
    sig = float(clip.get("signal_score") or 50)
    llm = float(llm_score if llm_score is not None else sig)
    cw = s["category_weights"].get(category or "other", 0.8)
    sw = float((streamer or {}).get("weight") or 1.0)
    return round(min(100.0, s["signal_weight"] * sig + s["llm_weight"] * llm * cw * sw), 1)


def apply_brief(req: dict, by_claude: bool, agent: Agent):
    clip = db.get("clips", id=req["clip_id"])
    if not clip:
        return
    R = rules.get()
    streamer = db.get("streamers", id=clip["streamer_id"]) if clip.get("streamer_id") else None
    stream = db.get("streams", id=clip["stream_id"]) if clip.get("stream_id") else None
    fb = (clip.get("brief") or {}).get("fallback") or {"start": clip["start_s"], "end": clip["end_s"]}
    res = (req.get("result") or {}) if by_claude else {}

    if by_claude and res.get("decision") == "reject":
        db.update("clips", {"id": clip["id"]}, {"status": "rejected", "status_reason": res.get("reject_reason"),
                                                "llm_score": res.get("llm_score"), "updated_at": db.iso()})
        agent.log(f"Claude rejected clip: {res.get('reject_reason')}", "info", clip_id=clip["id"])
        return

    name = (streamer or {}).get("name", "Streamer")
    category = res.get("category") or "other"
    final = {
        "start": float(res.get("start", fb["start"])), "end": float(res.get("end", fb["end"])),
        "layout": res.get("layout") or ("stack" if (streamer or {}).get("layout_preset") else "full"),
        "cam_box": res.get("cam_box") or ((streamer or {}).get("layout_preset") or {}).get("cam"),
        "hook_text": res.get("hook_text") or ("WAIT FOR IT…" if not by_claude else ""),
        "zoom_at": res.get("zoom_at") or [],
        "caption_fixes": res.get("caption_fixes") or {}, "censor_words": res.get("censor_words") or [],
        "by_claude": by_claude,
    }
    # learn the facecam box for this streamer the first time Claude finds it
    if by_claude and res.get("cam_box") and streamer and not streamer.get("layout_preset"):
        db.update("streamers", {"id": streamer["id"]}, {"layout_preset": {"cam": res["cam_box"]}})

    title = res.get("title") or R["title_formulas"][4]
    hashtags = list(dict.fromkeys(R["publish"]["hashtags_base"] + (res.get("hashtags") or [])
                                  + [f"#{name.replace(' ', '').replace('-', '')}"]))[:7]
    offset = int((clip.get("window_offset_s") or 0) + final["start"])
    url = (stream or {}).get("url", "")
    desc = (f"{res.get('description_line') or title}\n\n🎥 Credit: {name} — {(streamer or {}).get('channel_url', '')}\n"
            f"▶ Full stream: {url}{'&' if '?' in url else '?'}t={max(0, offset)}s\n"
            f"🔔 Follow {R['publish'].get('channel_handle', '')} for the best GTA 6 moments daily\n\n{' '.join(hashtags)}")
    fs = _final_score(clip, res.get("llm_score"), category, streamer, R)
    status = "editing"
    reason = None
    if fs < R["scoring"]["edit_threshold"] and by_claude:
        status, reason = "rejected", f"final score {fs} < {R['scoring']['edit_threshold']}"
    db.update("clips", {"id": clip["id"]}, {
        "brief": {**(clip.get("brief") or {}), "final": final, "claude": res if by_claude else None},
        "category": category, "llm_score": res.get("llm_score"), "final_score": fs,
        "title": title[:100], "description": desc, "hashtags": hashtags, "hook_text": final["hook_text"],
        "start_s": final["start"], "end_s": final["end"], "status": status, "status_reason": reason,
        "updated_at": db.iso()})
    if status == "editing":
        db.enqueue("edit", {"clip_id": clip["id"]}, priority=int(fs))
        agent.log(f"brief {'from Claude' if by_claude else '(fallback)'}: “{title}” score {fs} → Editor",
                  "success" if by_claude else "warn", clip_id=clip["id"])
    else:
        agent.log(f"clip archived: {reason}", "info", clip_id=clip["id"])


def apply_qa(req: dict, by_claude: bool, agent: Agent):
    clip = db.get("clips", id=req["clip_id"])
    if not clip:
        return
    R = rules.get()
    q = R["qa"]
    tech = (req.get("payload") or {}).get("tech_checks", {})
    critical_fail = any(not tech.get(k, True) for k in CRITICAL)
    if not by_claude:
        db.update("clips", {"id": clip["id"]}, {"status": "review", "updated_at": db.iso(),
                                                "status_reason": "Claude didn't review in time — needs your eyes"})
        return
    res = req.get("result") or {}
    score = float(res.get("score", 0))
    if critical_fail:
        score = min(score, q["fix"] - 1)
    verdict = res.get("verdict") or ("pass" if score >= q["pass"] else "fix" if score >= q["fix"] else "fail")
    db.insert("qa_reports", {"clip_id": clip["id"], "score": score, "checks": tech, "issues": res.get("issues"),
                             "verdict": verdict, "by_claude": True})
    vals = {"qa_score": score, "updated_at": db.iso()}
    fixes = res.get("fixes") or {}
    if verdict == "fix" and clip["qa_attempts"] < q["max_fix_rounds"] and fixes:
        brief = clip.get("brief") or {}
        final = {**brief.get("final", {})}
        for k in ("hook_text", "zoom_at", "caption_fixes", "layout"):
            if k in fixes:
                final[k] = fixes[k]
        final["start"] = float(final.get("start", clip["start_s"])) + float(fixes.get("trim_start", 0))
        final["end"] = float(final.get("end", clip["end_s"])) - float(fixes.get("trim_end", 0))
        if fixes.get("title"):
            vals["title"] = fixes["title"][:100]
        vals.update({"brief": {**brief, "final": final}, "qa_attempts": clip["qa_attempts"] + 1,
                     "status": "editing", "hook_text": final.get("hook_text")})
        db.update("clips", {"id": clip["id"]}, vals)
        db.enqueue("edit", {"clip_id": clip["id"]}, priority=80)
        agent.log(f"QA {score:.0f}: Claude requested fixes → re-render", "warn", clip_id=clip["id"], issues=res.get("issues"))
        return
    if verdict == "fail":
        vals.update({"status": "rejected", "status_reason": "; ".join(res.get("issues") or [])[:300]})
    else:
        level = int(db.setting("autopilot_level", 0) or 0)
        p = R["publish"]
        auto = (level >= 2 and verdict == "pass") or (
            level == 1 and verdict == "pass" and (clip.get("final_score") or 0) >= p["auto_publish_min_final"]
            and score >= p["auto_publish_min_qa"])
        vals["status"] = "approved" if auto else "review"
        if fixes.get("title"):
            vals["title"] = fixes["title"][:100]
    db.update("clips", {"id": clip["id"]}, vals)
    agent.log(f"QA {score:.0f} ({verdict}) → {vals['status']}", "success" if verdict == "pass" else "info",
              clip_id=clip["id"])
