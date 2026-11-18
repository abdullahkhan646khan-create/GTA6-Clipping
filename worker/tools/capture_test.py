"""Capture test — run FIRST on Render to learn which capture level (C1/C2/C3) works from Render's IP.
Usage: ROLE=capture_test TEST_URL=https://www.youtube.com/@SomeLiveChannel/live python -m worker.main
       (or: python -m worker.tools.capture_test <url>)
Records ~30 s at each available level, reports results to stdout and to the dashboard event feed."""
import os
import subprocess
import sys
import time

from ..core import config, ytdl


def _event(msg, level="info"):
    print(f"[{level}] {msg}", flush=True)
    try:
        from ..core import db
        db.event(f"capture-test@{config.NODE}", msg, level)
    except Exception:
        pass


def main(url: str | None = None):
    url = url or (sys.argv[1] if len(sys.argv) > 1 else None) or os.environ.get("TEST_URL")
    if not url:
        _event("set TEST_URL to a live YouTube stream or channel /live URL", "error")
        return
    out_dir = config.DATA_DIR / "capture_test"
    out_dir.mkdir(parents=True, exist_ok=True)
    _event(f"capture test on {url} — levels available: {ytdl.available_levels()}")
    for level in ytdl.available_levels():
        t0 = time.time()
        p = subprocess.run(ytdl.base_args(level) + ["-J", "--skip-download", url],
                           capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            _event(f"{level}: metadata FAILED ({'BOT CHECK' if ytdl.is_blocked(p.stderr) else 'error'}): "
                   f"{p.stderr[-200:]}", "warn")
            continue
        out = out_dir / f"{level}.ts"
        yt = subprocess.Popen(ytdl.record_cmd(url, level), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ff = subprocess.Popen(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-t", "30",
                               "-c", "copy", str(out)], stdin=yt.stdout)
        yt.stdout.close()
        try:
            ff.wait(timeout=120)
        except subprocess.TimeoutExpired:
            ff.kill()
        yt.kill()
        size = out.stat().st_size / 1e6 if out.exists() else 0
        if size > 1:
            _event(f"✅ {level} WORKS — metadata + 30 s recording ({size:.1f} MB in {time.time() - t0:.0f}s). "
                   f"Use CAPTURE_LEVEL=auto (it starts at {level}).", "success")
            return
        err = (yt.stderr.read() or b"").decode(errors="ignore")[-300:]
        _event(f"{level}: recording FAILED: {err}", "warn")
    _event("❌ no capture level worked from this IP. Next: add YTDLP_COOKIES_B64 (C2) or YTDLP_PROXY (C3).", "error")


if __name__ == "__main__":
    main()
