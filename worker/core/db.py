"""Supabase access (thread-local clients), settings, events and storage helpers."""
import json
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from supabase import create_client

from . import config

_local = threading.local()


def sb():
    if not getattr(_local, "client", None):
        if not (config.SUPABASE_URL and config.SUPABASE_KEY):
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
        _local.client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    return _local.client


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat()


def ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def retry(fn, tries=4, delay=1.5):
    for i in range(tries):
        try:
            return fn()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(delay * (i + 1))


# ───── generic table helpers ─────
def insert(table: str, row: dict | list) -> list:
    return retry(lambda: sb().table(table).insert(row).execute()).data


def update(table: str, match: dict, values: dict) -> list:
    def go():
        q = sb().table(table).update(values)
        for k, v in match.items():
            q = q.eq(k, v)
        return q.execute()
    return retry(go).data


def get(table: str, **match) -> dict | None:
    def go():
        q = sb().table(table).select("*")
        for k, v in match.items():
            q = q.eq(k, v)
        return q.limit(1).execute()
    rows = retry(go).data
    return rows[0] if rows else None


def rpc(name: str, params: dict) -> list:
    return retry(lambda: sb().rpc(name, params).execute()).data or []


# ───── settings ─────
def setting(key: str, default=None):
    row = get("settings", key=key)
    return row["value"] if row else default


def set_setting(key: str, value) -> None:
    retry(lambda: sb().table("settings").upsert({"key": key, "value": value, "updated_at": iso()}).execute())


# ───── events (dashboard live feed) ─────
def event(agent_id: str, message: str, level: str = "info", data: dict | None = None) -> None:
    try:
        insert("agent_events", {"agent_id": agent_id, "message": message[:500], "level": level, "data": data})
    except Exception as e:  # never let logging kill an agent
        print(f"[event-fail] {agent_id}: {message} ({e})")
    print(f"[{level}] {agent_id}: {message}", flush=True)


# ───── jobs ─────
def enqueue(job_type: str, payload: dict, priority: int = 50, delay_s: int = 0) -> dict:
    row = {"type": job_type, "payload": payload, "priority": priority,
           "run_after": iso(now() + timedelta(seconds=delay_s))}
    return insert("jobs", row)[0]


def finish_job(job_id: str, ok: bool = True, result: dict | None = None, error: str | None = None,
               retry_delay_s: int = 30) -> None:
    job = get("jobs", id=job_id)
    if not ok and job and job["attempts"] < job["max_attempts"]:
        update("jobs", {"id": job_id}, {"status": "queued", "error": error, "locked_by": None,
                                        "run_after": iso(now() + timedelta(seconds=retry_delay_s)),
                                        "updated_at": iso()})
        return
    update("jobs", {"id": job_id}, {"status": "done" if ok else "failed", "result": result,
                                    "error": error, "updated_at": iso()})


# ───── storage ─────
def upload(local: Path, dest: str, content_type: str) -> str:
    data = Path(local).read_bytes()
    retry(lambda: sb().storage.from_(config.BUCKET).upload(
        dest, data, {"content-type": content_type, "upsert": "true"}))
    return dest


def signed_url(path: str, seconds: int = 86400) -> str:
    res = retry(lambda: sb().storage.from_(config.BUCKET).create_signed_url(path, seconds))
    return res.get("signedURL") or res.get("signedUrl") or res.get("signed_url")


def download(path: str, local: Path) -> Path:
    data = retry(lambda: sb().storage.from_(config.BUCKET).download(path))
    Path(local).parent.mkdir(parents=True, exist_ok=True)
    Path(local).write_bytes(data)
    return Path(local)


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False, default=str)
