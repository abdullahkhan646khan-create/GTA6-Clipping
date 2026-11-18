"""Cutter: transcribes the moment window, prepares a rule-based fallback cut + a contact sheet,
then asks Claude (via the Bridge) for the clip brief: exact cut, score, layout, hook, title, description."""
from pathlib import Path

from ..core import bridge, config, db, media, rules, transcribe, ytdl
from ..core.agent import Agent


class Cutter(Agent):
    type = "cutter"
    job_types = ["cut", "clip_vod"]

    def handle(self, job):
        if job["type"] == "clip_vod":
            return self._vod(job)
        p = job["payload"]
        clip = db.get("clips", id=p["clip_id"])
        return self._prepare(clip, Path(clip["raw_path"]), float(p.get("peak_offset", 0)))

    # dashboard task: "clip this URL from a to b"
    def _vod(self, job):
        p = job["payload"]
        streamer_id = p.get("streamer_id")
        clip = db.insert("clips", {"source": "vod", "streamer_id": streamer_id, "signal_score": 70,
                                   "window_offset_s": float(p["start"]), "status": "cutting"})[0]
        out_dir = config.DATA_DIR / "clips" / clip["id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        self.status(f"downloading VOD section {p['start']}–{p['end']}s")
        ok, info = ytdl.download_section(p["url"], float(p["start"]), float(p["end"]), out_dir / "window.mp4")
        if not ok:
            db.update("clips", {"id": clip["id"]}, {"status": "error", "status_reason": info[:300]})
            raise RuntimeError(f"VOD download failed: {info[:200]}")
        vid = p["url"].split("v=")[-1].split("&")[0]
        stream = db.sb().table("streams").upsert({"video_id": vid, "url": p["url"], "status": "ended",
                                                  "streamer_id": streamer_id}, on_conflict="video_id").execute().data[0]
        db.update("clips", {"id": clip["id"]}, {"raw_path": str(out_dir / "window.mp4"), "stream_id": stream["id"]})
        clip = db.get("clips", id=clip["id"])
        dur = media.duration(out_dir / "window.mp4")
        return self._prepare(clip, out_dir / "window.mp4", dur * 0.6)

    def _prepare(self, clip, window: Path, peak: float):
        R = rules.get()
        dur = media.duration(window)
        streamer = db.get("streamers", id=clip["streamer_id"]) if clip.get("streamer_id") else {}
        stream = db.get("streams", id=clip["stream_id"]) if clip.get("stream_id") else {}
        name = (streamer or {}).get("name", "streamer")

        self.status(f"transcribing {name} moment ({dur:.0f}s)", next_steps=["fallback cut", "contact sheet",
                                                                          "ask Claude for brief"])
        audio = media.extract_audio(window, window.with_name("audio.wav"))
        words = transcribe.words(audio)
        fallback = default_cut(words, peak, dur, R)

        self.status("building contact sheet for Claude")
        lo, hi = max(0.0, peak - 25), min(dur - 0.5, peak + 10)
        times = [round(lo + i * (hi - lo) / 5, 1) for i in range(6)]
        sheet_local = media.contact_sheet(window, window.with_name("brief_sheet.jpg"), times)
        sheet = db.upload(sheet_local, f"sheets/{clip['id']}_brief.jpg", "image/jpeg")

        has_cam = bool((streamer or {}).get("layout_preset"))
        payload = {
            "streamer": name, "stream_title": (stream or {}).get("title"),
            "window_s": round(dur, 1), "peak_s": round(peak, 1),
            "signal_score": clip.get("signal_score"),
            "transcript": transcribe.compact(words),
            "fallback_cut": fallback,
            "known_cam_box": (streamer or {}).get("layout_preset", {}).get("cam") if has_cam else None,
            "sheet_times": times,
        }
        db.update("clips", {"id": clip["id"]}, {"transcript": words, "brief": {"fallback": fallback},
                                                "start_s": fallback["start"], "end_s": fallback["end"],
                                                "status": "awaiting_brief", "updated_at": db.iso()})
        req = bridge.request("clip_brief", payload, self.id, clip_id=clip["id"], attachments=[sheet],
                             priority=int(clip.get("signal_score") or 50))
        self.log(f"{name}: asked Claude for clip brief", "info", clip_id=clip["id"], request_id=req["id"])
        return {"clip_id": clip["id"], "request_id": req["id"]}


def default_cut(words: list[dict], peak: float, dur: float, R: dict) -> dict:
    """Rule-based cut (used if Claude doesn't answer): end shortly after the peak at a word boundary,
    start ~25–35 s earlier at a sentence start."""
    c = R["cut"]
    end = min(dur, peak + 4 + c["tail_s"])
    after = [w for w in words if w["s"] >= peak]
    if after:
        # end after the last word within 4 s of the peak + tail
        near = [w for w in after if w["e"] <= peak + 4]
        if near:
            end = min(dur, near[-1]["e"] + c["tail_s"])
    target_start = max(0.0, end - (c["target_min_s"] + c["target_max_s"]) / 2)
    start = target_start
    # snap to a sentence start: first word after a ≥0.6 s gap at/after target_start
    for i, w in enumerate(words):
        if w["s"] < target_start:
            continue
        prev = words[i - 1] if i else None
        if not prev or w["s"] - prev["e"] >= 0.6 or prev["w"][-1:] in ".?!":
            start = max(0.0, w["s"] - c["word_pad_s"])
            break
    if end - start < c["min_s"]:
        start = max(0.0, end - c["min_s"])
    if end - start > c["max_s"]:
        start = end - c["max_s"]
    return {"start": round(start, 2), "end": round(end, 2)}
