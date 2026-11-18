"""yt-dlp wrapper with the capture escalation ladder (C1 → C2 → C3) for cloud IPs.

C1: latest yt-dlp + JS runtime (deno) + PO-token provider plugin (bgutil script)  — free
C2: C1 + cookies of a burner Google account (YTDLP_COOKIES_B64)                    — free
C3: C2 through a static ISP proxy (YTDLP_PROXY)                                    — ~$2–5/mo
"""
import base64
import json
import os
import subprocess
from pathlib import Path

from . import config

BLOCK_MARKERS = ("confirm you're not a bot", "confirm you’re not a bot", "sign in to confirm",
                 "http error 429", "this helps protect our community", "po token")
_working_level: str | None = None


def available_levels() -> list[str]:
    lv = ["C1"]
    if config.YTDLP_COOKIES_B64:
        lv.append("C2")
    if config.YTDLP_PROXY:
        lv.append("C3")
    if config.CAPTURE_LEVEL in ("C1", "C2", "C3"):
        return [config.CAPTURE_LEVEL]
    return lv


def _cookies_file() -> Path | None:
    if not config.YTDLP_COOKIES_B64:
        return None
    p = config.DATA_DIR / "cookies.txt"
    if not p.exists():
        p.write_bytes(base64.b64decode(config.YTDLP_COOKIES_B64))
        os.chmod(p, 0o600)
    return p


def base_args(level: str) -> list[str]:
    args = ["yt-dlp", "--no-warnings", "--no-progress", "--retries", "5", "--socket-timeout", "20"]
    if Path(config.POT_SCRIPT).exists():
        args += ["--extractor-args", f"youtubepot-bgutilscript:script_path={config.POT_SCRIPT}"]
    if level in ("C2", "C3") and (c := _cookies_file()):
        args += ["--cookies", str(c)]
    if level == "C3" and config.YTDLP_PROXY:
        args += ["--proxy", config.YTDLP_PROXY]
    return args


def is_blocked(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in BLOCK_MARKERS)


def ladder() -> list[str]:
    lv = available_levels()
    if _working_level in lv:  # start from what worked last time
        lv = lv[lv.index(_working_level):]
    return lv


def mark_working(level: str):
    global _working_level
    _working_level = level


def probe(url: str, timeout: int = 60) -> tuple[dict | None, str, str]:
    """Return (info, level_used, error). info has is_live, id, title, concurrent_view_count …"""
    last_err = ""
    for level in ladder():
        cmd = base_args(level) + ["-J", "--skip-download", url]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last_err = "timeout"
            continue
        if p.returncode == 0 and p.stdout.strip():
            mark_working(level)
            return json.loads(p.stdout), level, ""
        last_err = (p.stderr or "")[-600:]
        if not is_blocked(last_err):  # a real error (e.g. "not currently live") — no point escalating
            return None, level, last_err
    return None, "", last_err


def record_cmd(url: str, level: str) -> list[str]:
    fmt = f"best[height<={config.REC_MAX_HEIGHT}][vcodec!=none][acodec!=none]/best"
    return base_args(level) + ["-f", fmt, "--hls-use-mpegts", "--no-part", "-o", "-", url]


def download_section(url: str, start_s: float, end_s: float, out: Path) -> tuple[bool, str]:
    """VOD clipping (dashboard task 'clip this URL from mm:ss to mm:ss')."""
    last = ""
    for level in ladder():
        cmd = base_args(level) + ["-f", f"bv*[height<={config.REC_MAX_HEIGHT}]+ba/b",
                                  "--download-sections", f"*{start_s:.2f}-{end_s:.2f}",
                                  "--force-keyframes-at-cuts", "--merge-output-format", "mp4",
                                  "-o", str(out), url]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if p.returncode == 0 and out.exists():
            mark_working(level)
            return True, level
        last = p.stderr[-600:]
        if not is_blocked(last):
            break
    return False, last
