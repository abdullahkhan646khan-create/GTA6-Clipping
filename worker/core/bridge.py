"""Engine side of the Claude Bridge: agents leave requests here; Claude answers them when woken."""
from datetime import timedelta

from . import db


def request(kind: str, payload: dict, requested_by: str, clip_id: str | None = None,
            attachments: list[str] | None = None, priority: int = 50, deadline_min: int = 10) -> dict:
    row = {"kind": kind, "payload": payload, "requested_by": requested_by, "clip_id": clip_id,
           "attachments": attachments or [], "priority": max(0, min(100, int(priority))),
           "deadline_at": db.iso(db.now() + timedelta(minutes=deadline_min))}
    return db.insert("llm_requests", row)[0]


def pending_stats() -> dict:
    rows = db.sb().table("llm_requests").select("id,priority,created_at,status,claimed_at") \
        .in_("status", ["pending", "claimed"]).execute().data
    pend = [r for r in rows if r["status"] == "pending"]
    return {
        "pending": len(pend),
        "claimed": len(rows) - len(pend),
        "max_priority": max((r["priority"] for r in pend), default=0),
        "oldest": min((db.ts(r["created_at"]) for r in pend), default=None),
    }
