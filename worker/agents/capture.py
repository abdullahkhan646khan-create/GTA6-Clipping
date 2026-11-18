"""Capture agent: records live streams into a rolling buffer, listens to chat, detects moments LIVE.

One CaptureManager claims `record_stream` jobs (up to max_parallel_streams) and runs a StreamSession thread
per stream. Each session:
  yt-dlp (live HLS) ─pipe─► ffmpeg -f segment (6 s .ts files named by epoch)   ← never stops while live
  chat thread: messages/sec + hype-keyword hits per second
  detector: per finished segment → loudness/sec → score window → moment → cut window → `cut` job
"""
import re
import shutil
import statistics
import subprocess
import threading
import time
from collections import defaultdict
from pathlib import Path

import requests

from ..core import config, db, media, rules, ytdl
from ..core.agent import Agent

SEG_RE = re.compile(r"seg_(\d+)\.ts$")


class CaptureManager(Agent):
    type = "capture"
    poll_s = 5

    def __init__(self):
        super().__init__()
        self.sessions: dict[str, "StreamSession"] = {}

    def loop(self):
        # drop finished sessions
        for vid, s in list(self.sessions.items()):
            if not s.thread.is_alive():
                self.sessions.pop(vid)
        slots = int(db.setting("max_parallel_streams", 2) or 2)
        live = [f"{s.streamer_name} ({s.state})" for s in self.sessions.values()]
        if self.paused():
            for s in self.sessions.values():
                s.stop("kill switch")
            self.status("paused (kill switch)", "stopped")
            time.sleep(10)
            return
        if len(self.sessions) < slots:
            jobs = db.rpc("claim_job", {"p_types": ["record_stream"], "p_worker": self.id})
            if jobs:
                job = jobs[0]
                stream = db.get("streams", id=job["payload"]["stream_id"])
                if not stream or stream["video_id"] in self.sessions:
                    db.finish_job(job["id"], True, {"skipped": "already recording or missing"})
                else:
                    s = StreamSession(self, stream, job)
                    self.sessions[stream["video_id"]] = s
                    s.start()
                    self.log(f"recording started: {s.streamer_name} — {stream.get('title', '')[:80]}", "success")
        self.status(f"recording {len(self.sessions)}/{slots}: " + (", ".join(live) or "none"),
                    "working" if self.sessions else "idle",
                    next_steps=["watch for moments", "free slot → claim next stream"] if self.sessions else
                    ["wait for Supervisor to assign a live stream"])
        time.sleep(self.poll_s)


class StreamSession:
    def __init__(self, mgr: CaptureManager, stream: dict, job: dict):
        self.mgr, self.stream, self.job = mgr, stream, job
        self.vid = stream["video_id"]
        streamer = db.get("streamers", id=stream["streamer_id"]) if stream.get("streamer_id") else None
        self.streamer = streamer or {}
        self.streamer_name = self.streamer.get("name") or "streamer"
        self.dir = config.DATA_DIR / "streams" / self.vid
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state = "starting"
        self._stop = threading.Event()
        self.proc: list[subprocess.Popen] = []
        self.level = None
        # signals keyed by epoch second
        self.chat = defaultdict(int)
        self.hype = defaultdict(int)
        self.loud: dict[int, float] = {}
        self.viewers: list[tuple[float, int]] = []
        self.chat_alive = False
        self.processed: set[str] = set()
        self.last_trigger = 0.0
        self.pending: list[dict] = []
        self.started_epoch = None
        self.thread = threading.Thread(target=self._main, daemon=True, name=f"rec-{self.vid}")

    def start(self):
        self.thread.start()

    def stop(self, why: str):
        if not self._stop.is_set():
            self.mgr.log(f"stopping {self.streamer_name}: {why}")
        self._stop.set()
        for p in self.proc:
            try:
                p.terminate()
            except Exception:
                pass

    # ───────────────────────── main ─────────────────────────
    def _main(self):
        try:
            info, level, err = ytdl.probe(self.stream["url"])
            if not info or not info.get("is_live"):
                raise RuntimeError(f"not live / blocked: {err[:200]}")
            self.started_epoch = info.get("release_timestamp") or info.get("timestamp")
            db.update("streams", {"id": self.stream["id"]}, {
                "status": "recording", "recording_node": config.NODE, "capture_level": level,
                "started_at": db.iso(db.datetime.fromtimestamp(self.started_epoch, db.timezone.utc))
                if self.started_epoch else None})
            threading.Thread(target=self._chat_loop, daemon=True).start()
            if config.YOUTUBE_API_KEY:
                threading.Thread(target=self._viewers_loop, daemon=True).start()
            self._record_loop(level)
            db.finish_job(self.job["id"], True, {"ended": True})
        except Exception as e:
            self.mgr.log(f"{self.streamer_name}: capture error: {e}", "error")
            db.update("streams", {"id": self.stream["id"]}, {"status": "error"})
            db.finish_job(self.job["id"], False, error=str(e)[:500], retry_delay_s=120)
        finally:
            self.stop("session ended")
            shutil.rmtree(self.dir, ignore_errors=True)

    def _record_loop(self, level: str):
        failures = 0
        levels = ytdl.ladder()
        idx = levels.index(level) if level in levels else 0
        while not self._stop.is_set():
            lvl = levels[idx]
            self.state = f"recording {lvl}"
            ytp = subprocess.Popen(ytdl.record_cmd(self.stream["url"], lvl), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
            ffp = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-c", "copy",
                                    "-f", "segment", "-segment_time", str(config.SEGMENT_S),
                                    "-reset_timestamps", "1", "-strftime", "1", str(self.dir / "seg_%s.ts")],
                                   stdin=ytp.stdout, stderr=subprocess.PIPE)
            ytp.stdout.close()
            self.proc = [ytp, ffp]
            t0 = time.time()
            while ffp.poll() is None and not self._stop.is_set():
                self._tick()
                time.sleep(2)
            for p in self.proc:
                if p.poll() is None:
                    p.terminate()
            if self._stop.is_set():
                break
            err = (ytp.stderr.read() or b"").decode(errors="ignore")[-600:]
            if ytdl.is_blocked(err) and idx + 1 < len(levels):
                idx += 1
                self.mgr.log(f"{self.streamer_name}: blocked at {lvl} → escalating to {levels[idx]}", "warn")
                continue
            # did the stream end?
            row = db.get("streams", id=self.stream["id"])
            if row and row["status"] == "stop_requested":
                break
            info, _, _ = ytdl.probe(self.stream["url"])
            if not info or not info.get("is_live"):
                db.update("streams", {"id": self.stream["id"]}, {"status": "ended"})
                self.mgr.log(f"{self.streamer_name}: stream ended", "info")
                break
            failures = failures + 1 if time.time() - t0 < 60 else 0
            if failures >= 5:
                raise RuntimeError(f"recorder keeps failing: {err[:200]}")
            self.mgr.log(f"{self.streamer_name}: recorder restarted ({err[:120]})", "warn")
            time.sleep(3)

    # ───────────────────────── chat & viewers ─────────────────────────
    def _chat_loop(self):
        kws = [k.lower() for k in rules.get()["detector"]["hype_keywords"]]
        try:
            from chat_downloader import ChatDownloader
            chat = ChatDownloader().get_chat(self.stream["url"], message_types=["text_message", "paid_message"])
            self.chat_alive = True
            for msg in chat:
                if self._stop.is_set():
                    break
                sec = int(time.time())
                self.chat[sec] += 1
                text = (msg.get("message") or "").lower()
                words = set(re.findall(r"[\w']+|[^\w\s]+", text))
                if any((k in words) if len(k) <= 3 else (k in text) for k in kws):
                    self.hype[sec] += 1
                if msg.get("message_type") == "paid_message":
                    self.hype[sec] += 3
        except Exception as e:
            self.mgr.log(f"{self.streamer_name}: chat unavailable ({str(e)[:120]}) — audio-only detection", "warn")
        self.chat_alive = False

    def _viewers_loop(self):
        while not self._stop.is_set():
            try:
                r = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=15, params={
                    "part": "liveStreamingDetails", "id": self.vid, "key": config.YOUTUBE_API_KEY}).json()
                v = int(r["items"][0]["liveStreamingDetails"].get("concurrentViewers", 0))
                self.viewers.append((time.time(), v))
                self.viewers = self.viewers[-30:]
                db.update("streams", {"id": self.stream["id"]}, {"concurrent_viewers": v,
                                                                 "last_seen_live": db.iso()})
            except Exception:
                pass
            self._stop.wait(120)

    # ───────────────────────── detection ─────────────────────────
    def _segments(self) -> list[tuple[int, Path]]:
        segs = []
        for p in self.dir.glob("seg_*.ts"):
            m = SEG_RE.search(p.name)
            if m:
                segs.append((int(m.group(1)), p))
        return sorted(segs)

    def _tick(self):
        segs = self._segments()
        if len(segs) < 2:
            return
        # finished = all but the newest (still being written)
        for epoch, p in segs[:-1]:
            if p.name in self.processed:
                continue
            self.processed.add(p.name)
            for t, lufs in media.momentary_loudness(p):
                sec = epoch + int(t)
                self.loud[sec] = max(self.loud.get(sec, -99.0), lufs)
            self._evaluate(epoch + config.SEGMENT_S)
        self._flush_pending(segs)
        self._cleanup(segs)

    def _evaluate(self, now_s: int):
        r = rules.get()["detector"]
        W, B = r["window_s"], r["baseline_s"]
        win = range(now_s - W, now_s)
        base = range(now_s - B, now_s - W)
        have_base = sum(1 for s in base if s in self.loud)
        if have_base < 60:  # warm-up: need ≥ 60 s of baseline
            return
        scores, weights, reasons = {}, r["weights"], {}

        # audio
        base_l = [self.loud[s] for s in base if s in self.loud]
        win_l = [(s, self.loud[s]) for s in win if s in self.loud]
        if win_l and base_l:
            peak_s, peak_v = max(win_l, key=lambda x: x[1])
            delta = peak_v - statistics.median(base_l)
            scores["audio"] = min(1.0, max(0.0, (delta - 3) / max(1.0, r["audio_spike_db"] - 3)))
            reasons["audio_db"] = round(delta, 1)
        else:
            peak_s = now_s - W // 2

        # chat + hype
        if self.chat_alive or any(self.chat.get(s) for s in base):
            for key, series in (("chat", self.chat), ("hype", self.hype)):
                b = [series.get(s, 0) for s in base]
                x = statistics.mean(series.get(s, 0) for s in win)
                mu, sd = statistics.mean(b), statistics.pstdev(b)
                z = (x - mu) / max(sd, 0.5)
                scores[key] = min(1.0, max(0.0, (z - 1) / 3))
                reasons[f"{key}_z"] = round(z, 2)

        # viewers
        if len(self.viewers) >= 2:
            (t0, v0), (t1, v1) = self.viewers[-2], self.viewers[-1]
            growth = (v1 - v0) / max(v0, 1)
            scores["viewers"] = min(1.0, max(0.0, growth / 0.03))
            reasons["viewer_growth"] = round(growth, 3)

        if not scores:
            return
        wsum = sum(weights[k] for k in scores)
        score = 100 * sum(weights[k] * v for k, v in scores.items()) / wsum
        db.insert("stream_signals", {"stream_id": self.stream["id"], "ts": db.iso(db.datetime.fromtimestamp(
            now_s, db.timezone.utc)), "chat_rate": sum(self.chat.get(s, 0) for s in win) / W,
            "hype_rate": sum(self.hype.get(s, 0) for s in win) / W,
            "audio_lufs": max((v for _, v in win_l), default=None), "score": round(score, 1)})

        if score < r["candidate_threshold"] or time.time() - self.last_trigger < r["cooldown_s"]:
            return
        # peak: loudest second if audio drove it, else chat peak minus chat delay
        if scores.get("audio", 0) < 0.5 and self.chat:
            chat_peak = max(win, key=lambda s: self.chat.get(s, 0))
            peak_s = int(chat_peak - (self.stream.get("chat_delay_s") or r["default_chat_delay_s"]))
        self.last_trigger = time.time()
        self.pending.append({"peak": peak_s, "score": round(score, 1), "reasons": reasons,
                             "due": peak_s + r["post_roll_s"]})
        self.mgr.log(f"🔥 {self.streamer_name}: moment detected (score {score:.0f}) — waiting post-roll",
                     "success", **reasons)

    def _flush_pending(self, segs):
        if not self.pending or not segs:
            return
        newest_done = segs[-2][0] + config.SEGMENT_S if len(segs) > 1 else 0
        r = rules.get()["detector"]
        for mom in list(self.pending):
            if newest_done < mom["due"]:
                continue
            self.pending.remove(mom)
            w0, w1 = mom["peak"] - r["pre_roll_s"], mom["due"]
            chosen = [p for e, p in segs[:-1] if e + config.SEGMENT_S > w0 and e < w1]
            if not chosen:
                continue
            epoch0 = int(SEG_RE.search(chosen[0].name).group(1))
            clip = db.insert("clips", {
                "stream_id": self.stream["id"], "streamer_id": self.stream.get("streamer_id"),
                "signal_score": mom["score"], "status": "cutting",
                "window_offset_s": (epoch0 - self.started_epoch) if self.started_epoch else None})[0]
            out_dir = config.DATA_DIR / "clips" / clip["id"]
            out_dir.mkdir(parents=True, exist_ok=True)
            window = media.concat_segments(chosen, out_dir / "window.mp4")
            moment = db.insert("moments", {
                "stream_id": self.stream["id"], "signal_score": mom["score"], "reasons": mom["reasons"],
                "peak_ts": db.iso(db.datetime.fromtimestamp(mom["peak"], db.timezone.utc)),
                "window_start": db.iso(db.datetime.fromtimestamp(epoch0, db.timezone.utc)),
                "window_end": db.iso(db.datetime.fromtimestamp(w1, db.timezone.utc)), "status": "cut"})[0]
            db.update("clips", {"id": clip["id"]}, {"moment_id": moment["id"], "raw_path": str(window)})
            db.enqueue("cut", {"clip_id": clip["id"], "peak_offset": mom["peak"] - epoch0},
                       priority=int(mom["score"]))

    def _cleanup(self, segs):
        keep_after = time.time() - config.RING_MINUTES * 60
        for e, p in segs[:-1]:
            if e < keep_after:
                p.unlink(missing_ok=True)
                self.processed.discard(p.name)
        cutoff = int(keep_after)
        for d in (self.chat, self.hype, self.loud):
            for k in [k for k in d if k < cutoff]:
                d.pop(k, None)
