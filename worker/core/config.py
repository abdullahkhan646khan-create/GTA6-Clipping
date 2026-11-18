"""All configuration comes from environment variables (Render dashboard / .env locally)."""
import os
import socket
from pathlib import Path

try:  # local dev convenience
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:
    pass


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


SUPABASE_URL = env("SUPABASE_URL")
SUPABASE_KEY = env("SUPABASE_SERVICE_ROLE_KEY")
BUCKET = env("STORAGE_BUCKET", "clips")

NODE = env("NODE_NAME", socket.gethostname()[:24])
ROLE = env("ROLE", "engine")  # engine | capture_test | heartbeat

DATA_DIR = Path(env("DATA_DIR", "/tmp/clipforge"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Capture
REC_MAX_HEIGHT = int(env("REC_MAX_HEIGHT", "1080"))
RING_MINUTES = int(env("RING_MINUTES", "12"))
SEGMENT_S = int(env("SEGMENT_S", "6"))
YTDLP_COOKIES_B64 = env("YTDLP_COOKIES_B64")       # base64 of a Netscape cookies.txt (burner account)
YTDLP_PROXY = env("YTDLP_PROXY")                   # http://user:pass@host:port (C3 only)
CAPTURE_LEVEL = env("CAPTURE_LEVEL", "auto")       # auto | C1 | C2 | C3
POT_SCRIPT = env("BGUTIL_SCRIPT", "/opt/bgutil/server/build/generate_once.js")

# APIs (all optional except Buffer for publishing)
YOUTUBE_API_KEY = env("YOUTUBE_API_KEY")
BUFFER_API_KEY = env("BUFFER_API_KEY")
BUFFER_CHANNEL_ID = env("BUFFER_CHANNEL_ID")       # found with: python -m worker.tools.buffer_setup

# Transcription (local, free)
WHISPER_MODEL = env("WHISPER_MODEL", "base.en")

# Claude wake-up
WAKE_METHOD = env("WAKE_METHOD", "github_issue")   # github_issue | github_dispatch | webhook | none
GITHUB_TOKEN = env("GITHUB_TOKEN")
GITHUB_REPO = env("GITHUB_REPO")                   # owner/repo
GITHUB_WAKE_ISSUE = env("GITHUB_WAKE_ISSUE")       # issue number used as the wake "doorbell"
WAKE_WEBHOOK_URL = env("WAKE_WEBHOOK_URL")
WAKE_DEBOUNCE_S = int(env("WAKE_DEBOUNCE_S", "60"))
WAKE_MIN_GAP_S = int(env("WAKE_MIN_GAP_S", "300"))

FONT_FILE = env("FONT_FILE", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
