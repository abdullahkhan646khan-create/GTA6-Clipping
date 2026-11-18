"""Local live clipper — runs on the Mac (home IP, no YouTube block), reuses the engine's editor/caption code.
  record  URL DIR                 record a live stream into 6 s segments (run in background)
  scan    DIR [--top N]           loudness spikes → candidate moments (epoch, score)
  cut     DIR PEAK_EPOCH NAME     build window + transcript + contact sheet for Claude's brief
  render  CLIPDIR                 render the Short from CLIPDIR/brief.json (+ QA contact sheet)
  publish CLIPDIR                 upload to Supabase Storage → Buffer → YouTube Shorts
"""
import json
import os
import re
import statistics
import subprocess
import sys
import types
from pathlib import Path

os.environ["LIGHT_MODE"] = "0"                       # full 1080×1920 on the Mac
os.environ.setdefault("FONT_FILE", "/System/Library/Fonts/Supplemental/Arial Bold.ttf")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.modules.setdefault("av", types.ModuleType("av"))  # faster-whisper import without PyAV

from worker.core import config, media  # noqa: E402

YTDLP = str(Path(sys.executable).with_name("yt-dlp"))
SEG = re.compile(r"seg_(\d+)\.ts$")


def record(url: str, out: Path, seconds: int | None = None):
    """streamlink muxes YouTube's separate live video+audio tracks reliably → ffmpeg cuts 6 s segments.
    seconds=None records until stopped; otherwise stops after `seconds` (used by the scheduled runs)."""
    out.mkdir(parents=True, exist_ok=True)
    sl_args = [str(Path(sys.executable).with_name("streamlink")), "--stdout", "--hls-live-edge", "3",
               "--stream-segment-threads", "2"]
    if os.environ.get("YTDLP_COOKIES_B64"):  # same burner-account cookies as yt-dlp (cloud IPs)
        import base64
        ck = out / "cookies.txt"
        ck.write_bytes(base64.b64decode(os.environ["YTDLP_COOKIES_B64"]))
        for line in ck.read_text().splitlines():
            f = line.split("\t")
            if len(f) == 7 and not line.startswith("#"):
                sl_args += ["--http-cookie", f"{f[5]}={f[6]}"]
    sl = subprocess.Popen(sl_args + [url, "1080p60,1080p,720p60,best"], stdout=subprocess.PIPE)
    ff = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-c", "copy",
                           *(["-t", str(seconds)] if seconds else []), "-f",
                           "segment", "-segment_time", "6", "-reset_timestamps", "1", "-strftime", "1",
                           str(out / "seg_%s.ts")], stdin=sl.stdout)
    sl.stdout.close()
    ff.wait()
    sl.terminate()


def find(top: int = 5):
    """Biggest live GTA 6 streams right now (YouTube search, live filter — no API key needed)."""
    import urllib.request
    req = urllib.request.Request("https://www.youtube.com/results?search_query=gta+6&sp=EgJAAQ%253D%253D",
                                 headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"})
    h = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
    data = json.loads(re.search(r"var ytInitialData = (\{.*?\});</script>", h).group(1))
    out = []

    def walk(o):
        if isinstance(o, dict):
            if "videoRenderer" in o:
                v = o["videoRenderer"]
                title = "".join(r.get("text", "") for r in v.get("title", {}).get("runs", []))
                vc = "".join(r.get("text", "") for r in v.get("viewCountText", {}).get("runs", []))
                n = int(re.sub(r"\D", "", vc) or 0)
                ch = "".join(r.get("text", "") for r in v.get("ownerText", {}).get("runs", []))
                if re.search(r"gta\s*(6|vi)|grand theft auto", title, re.I) and n:
                    out.append({"video_id": v["videoId"], "channel": ch, "watching": n, "title": title[:90]})
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(data)
    out = sorted({o["video_id"]: o for o in out}.values(), key=lambda o: -o["watching"])[:top]
    print(json.dumps(out, ensure_ascii=False, indent=1))


def segments(d: Path):
    s = sorted((int(SEG.search(p.name).group(1)), p) for p in d.glob("seg_*.ts") if SEG.search(p.name))
    return s[:-1]  # newest one is still being written


def loudness(d: Path) -> dict[int, float]:
    cache = d / "loud.json"
    loud = {int(k): v for k, v in json.loads(cache.read_text()).items()} if cache.exists() else {}
    done = set(json.loads((d / "done.json").read_text())) if (d / "done.json").exists() else set()
    for e, p in segments(d):
        if p.name in done:
            continue
        for t, l in media.momentary_loudness(p):
            k = e + int(t)
            loud[k] = max(loud.get(k, -99.0), l)
        done.add(p.name)
    cache.write_text(json.dumps(loud))
    (d / "done.json").write_text(json.dumps(sorted(done)))
    return loud


def scan(d: Path, top: int = 5):
    import time
    for e, p in segments(d):  # keep disk bounded: drop segments older than 20 min (1080p60 ≈ 40 MB/min)
        if e < time.time() - 1200:
            p.unlink(missing_ok=True)
    loud = loudness(d)
    used = set(json.loads((d / "used.json").read_text())) if (d / "used.json").exists() else set()
    keys = sorted(loud)
    cands = []
    for i, k in enumerate(keys):
        base = [loud[x] for x in keys[max(0, i - 300):max(0, i - 10)]]
        if len(base) < 60:
            continue
        delta = loud[k] - statistics.median(base)
        cands.append((delta, k))
    cands.sort(reverse=True)
    picked = []
    for delta, k in cands:
        if any(abs(k - p) < 60 for _, p in picked) or any(abs(k - u) < 60 for u in used):
            continue
        if keys[-1] - k < 15:  # need post-roll recorded
            continue
        picked.append((delta, k))
        if len(picked) >= top:
            break
    span = (keys[-1] - keys[0]) / 60 if keys else 0
    print(json.dumps({"recorded_min": round(span, 1), "candidates": [
        {"peak_epoch": k, "spike_db": round(dl, 1), "age_s": keys[-1] - k} for dl, k in picked]}))


def words_for(audio_src: Path) -> list[dict]:
    import numpy as np
    from faster_whisper import WhisperModel
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(audio_src), "-f", "f32le", "-ac", "1",
                          "-ar", "16000", "-"], capture_output=True).stdout
    audio = np.frombuffer(raw, np.float32)
    model = WhisperModel(os.environ.get("WHISPER_LOCAL", "base.en"), device="cpu", compute_type="int8")
    segs, _ = model.transcribe(audio, word_timestamps=True, vad_filter=False, beam_size=1)
    return [{"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)} for s in segs for w in (s.words or [])]


def cut(d: Path, peak: int, name: str):
    from worker.core.transcribe import compact
    segs = segments(d)
    w0, w1 = peak - 40, peak + 15
    chosen = [p for e, p in segs if e + 6 > w0 and e < w1]
    epoch0 = int(SEG.search(chosen[0].name).group(1))
    cd = d.parent / "clips" / name
    cd.mkdir(parents=True, exist_ok=True)
    win = media.concat_segments(chosen, cd / "window.mp4")
    words = words_for(win)
    (cd / "words.json").write_text(json.dumps(words))
    dur = media.duration(win)
    pk = peak - epoch0
    times = [round(max(0, pk - 25) + i * (min(dur - 0.5, pk + 10) - max(0, pk - 25)) / 5, 1) for i in range(6)]
    media.contact_sheet(win, cd / "brief_sheet.jpg", times)
    used = set(json.loads((d / "used.json").read_text())) if (d / "used.json").exists() else set()
    used.add(peak)
    (d / "used.json").write_text(json.dumps(sorted(used)))
    (cd / "meta.json").write_text(json.dumps({"peak_s": pk, "window_s": dur, "epoch0": epoch0}))
    print(json.dumps({"clip_dir": str(cd), "window_s": round(dur, 1), "peak_s": pk, "sheet_times": times,
                      "sheet": str(cd / "brief_sheet.jpg")}))
    print(compact(words))


def render(cd: Path):
    from worker.agents.editor import build_ass, graph
    from worker.core import rules
    brief = json.loads((cd / "brief.json").read_text())
    b = dict(rules._DEFAULTS["build"])
    b["caption_font"] = "Impact"   # installed on macOS; matches the No Way Clips brand
    words = json.loads((cd / "words.json").read_text())
    s, e = float(brief["start"]), float(brief["end"])
    ws = [{**w, "s": max(0.0, w["s"] - s), "e": w["e"] - s} for w in words if w["s"] >= s - 0.05 and w["e"] <= e + 0.05]
    # collapse Whisper repetition loops ("what what what what …" → max 3 in a row)
    clean, run = [], 0
    for w in ws:
        same = clean and w["w"].lower().strip(".,!?") == clean[-1]["w"].lower().strip(".,!?")
        run = run + 1 if same else 1
        if run <= 3:
            clean.append(w)
    ws = [w for w in clean if w["s"] <= float(brief.get("caption_until", 1e9))
          and not any(a <= w["s"] <= b for a, b in brief.get("caption_drop", []))]  # drop misheard stretches
    (cd / "captions.ass").write_text(build_ass(ws, brief, brief.get("streamer", ""), brief["layout"], b), encoding="utf-8")
    vf = graph(brief["layout"], brief.get("cam_box"), brief.get("zoom_at") or [], b, e - s)
    p = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{s:.3f}", "-i", "window.mp4",
                        "-t", f"{e - s:.3f}", "-filter_complex", vf, "-map", "[out]", "-map", "0:a?",
                        "-af", "loudnorm=I=-14:TP=-1:LRA=11", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                        "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                        "-movflags", "+faststart", "short.mp4"], cwd=cd, capture_output=True, text=True)
    if p.returncode:
        sys.exit(p.stderr[-1500:])
    dur = media.duration(cd / "short.mp4")
    media.contact_sheet(cd / "short.mp4", cd / "qa_sheet.jpg", [round(0.3 + i * (dur - 0.6) / 5, 1) for i in range(6)],
                        cols=3, tile_w=270)
    lufs = media.integrated_loudness(cd / "short.mp4")
    print(json.dumps({"short": str(cd / "short.mp4"), "duration": round(dur, 1), "lufs": lufs,
                      "size_mb": round((cd / "short.mp4").stat().st_size / 1e6, 1), "qa_sheet": str(cd / "qa_sheet.jpg")}))


def publish(cd: Path):
    from worker.core import buffer, db
    brief = json.loads((cd / "brief.json").read_text())
    path = db.upload(cd / "short.mp4", f"clips/local_{cd.name}.mp4", "video/mp4")
    url = db.signed_url(path, 7 * 86400)
    post = buffer.create_video_post(config.BUFFER_CHANNEL_ID, brief["title"], brief["description"], url)
    (cd / "published.json").write_text(json.dumps({"buffer_post_id": post["id"], "storage": path}))
    db.event("claude", f"📤 published to Buffer: “{brief['title']}”", "success")
    print(json.dumps({"published": True, "buffer_post_id": post["id"]}))


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "record":
        record(args[0], Path(args[1]))
    elif cmd == "grab":  # grab URL DIR MINUTES
        record(args[0], Path(args[1]), int(float(args[2]) * 60))
    elif cmd == "find":
        find(int(args[0]) if args else 5)
    elif cmd == "scan":
        scan(Path(args[0]), int(args[2]) if len(args) > 2 else 5)
    elif cmd == "cut":
        cut(Path(args[0]), int(args[1]), args[2])
    elif cmd == "render":
        render(Path(args[0]))
    elif cmd == "publish":
        publish(Path(args[0]))
