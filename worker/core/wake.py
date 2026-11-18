"""Wake Claude only when there is work. Channels:
  github_issue    – comment on a "doorbell" issue → GitHub event fires the Claude Code routine
  github_dispatch – repository_dispatch event (if your routine trigger listens for it)
  webhook         – POST to any URL you configure
  none            – don't wake (a local listener session drains the queue)
"""
import requests

from . import config, db


def wake(reason: str, pending: int) -> bool:
    method = config.WAKE_METHOD
    ok = False
    try:
        if method == "github_issue" and config.GITHUB_TOKEN and config.GITHUB_REPO and config.GITHUB_WAKE_ISSUE:
            r = requests.post(
                f"https://api.github.com/repos/{config.GITHUB_REPO}/issues/{config.GITHUB_WAKE_ISSUE}/comments",
                headers=_gh(), json={"body": f"clipforge-wake: {pending} request(s) pending — {reason}"}, timeout=15)
            ok = r.status_code in (200, 201)
        elif method == "github_dispatch" and config.GITHUB_TOKEN and config.GITHUB_REPO:
            r = requests.post(f"https://api.github.com/repos/{config.GITHUB_REPO}/dispatches", headers=_gh(),
                              json={"event_type": "clipforge-wake",
                                    "client_payload": {"pending": pending, "reason": reason}}, timeout=15)
            ok = r.status_code == 204
        elif method == "webhook" and config.WAKE_WEBHOOK_URL:
            r = requests.post(config.WAKE_WEBHOOK_URL, json={"pending": pending, "reason": reason}, timeout=15)
            ok = r.ok
        elif method == "none":
            return False
    except Exception as e:
        db.event("dispatcher", f"wake failed: {e}", "error")
        return False

    bridge = db.setting("bridge", {}) or {}
    bridge.update({"last_wake_at": db.iso(), "last_wake_ok": ok, "last_wake_reason": reason,
                   "wakes_today": int(bridge.get("wakes_today", 0)) + 1})
    db.set_setting("bridge", bridge)
    return ok


def _gh() -> dict:
    return {"Authorization": f"Bearer {config.GITHUB_TOKEN}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}
