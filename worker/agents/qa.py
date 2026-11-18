"""QA: automatic technical checks, then asks Claude to review the rendered Short (contact sheet + captions + copy)."""
from pathlib import Path

from ..core import bridge, db, media, rules
from ..core.agent import Agent


class QA(Agent):
    type = "qa"
    job_types = ["qa"]

    def handle(self, job):
        clip = db.get("clips", id=job["payload"]["clip_id"])
        R = rules.get()
        short = Path(clip["raw_path"]).parent / "short.mp4"
        self.status("technical checks", next_steps=["loudness", "black frames", "ask Claude to review"])
        checks = tech_checks(short, R)
        passed = sum(1 for c in checks.values() if c["ok"])
        tech_score = round(100 * passed / len(checks))
        db.insert("qa_reports", {"clip_id": clip["id"], "score": tech_score, "checks": checks,
                                 "issues": [k for k, c in checks.items() if not c["ok"]], "verdict": "tech",
                                 "by_claude": False})
        brief = (clip.get("brief") or {}).get("final") or {}
        captions = " ".join(w["w"] for w in (clip.get("transcript") or [])
                            if clip["start_s"] - 0.05 <= w["s"] <= clip["end_s"])
        payload = {"title": clip.get("title"), "hook_text": clip.get("hook_text"),
                   "description": (clip.get("description") or "")[:400], "category": clip.get("category"),
                   "layout": clip.get("layout"), "duration_s": round(clip["end_s"] - clip["start_s"], 1),
                   "captions": captions[:1500], "tech_checks": {k: c["ok"] for k, c in checks.items()},
                   "tech_notes": {k: c.get("value") for k, c in checks.items()},
                   "fix_round": clip.get("qa_attempts", 0)}
        db.update("clips", {"id": clip["id"]}, {"status": "awaiting_qa", "updated_at": db.iso()})
        bridge.request("clip_qa", payload, self.id, clip_id=clip["id"], attachments=[clip["sheet_path"]],
                       priority=int(clip.get("final_score") or 50))
        self.log(f"QA tech {tech_score}/100 → asked Claude for review", "info", clip_id=clip["id"])
        return {"tech_score": tech_score}


def tech_checks(short: Path, R: dict) -> dict:
    info = media.probe(short)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), {})
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    dur = float(info["format"]["duration"])
    size_mb = short.stat().st_size / 1e6
    lufs = media.integrated_loudness(short)
    blacks = media.black_frames(short)
    c, b = R["cut"], R["build"]
    return {
        "duration": {"ok": c["min_s"] - 1 <= dur <= c["max_s"] + 1, "value": round(dur, 1)},
        "resolution": {"ok": v.get("width") == b["width"] and v.get("height") == b["height"],
                       "value": f"{v.get('width')}x{v.get('height')}"},
        "audio": {"ok": a is not None, "value": bool(a)},
        "loudness": {"ok": lufs is not None and abs(lufs - b["lufs"]) <= 1.5, "value": lufs},
        "black_frames": {"ok": not blacks, "value": blacks[:3]},
        "file_size": {"ok": size_mb <= R["qa"]["max_file_mb"], "value": round(size_mb, 1)},
    }


CRITICAL = ("duration", "resolution", "audio", "file_size")
