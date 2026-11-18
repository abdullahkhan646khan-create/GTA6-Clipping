"""Claude's side of the Bridge.
  python -m worker.tools.bridge_cli pull [--limit 8] [--out DIR]   claim requests, download contact sheets
  python -m worker.tools.bridge_cli submit results.json            write answers back (list of {id, result})
  python -m worker.tools.bridge_cli status                         queue overview
  python -m worker.tools.bridge_cli sleep                          mark Claude as sleeping
  python -m worker.tools.bridge_cli wait [--every 10]              block until work arrives (local listener)
"""
import argparse
import json
import sys
import time
from pathlib import Path

from ..core import db

REQUIRED = {
    "clip_brief": {"decision"},
    "clip_qa": {"score", "verdict"},
    "task": {"answer"},
    "plan": {"rationale"},
}


def _seen(state="working"):
    b = db.setting("bridge", {}) or {}
    b.update({"state": state, "last_seen_at": db.iso()})
    db.set_setting("bridge", b)


def pull(limit: int, out: Path):
    _seen()
    rows = db.rpc("claim_llm_requests", {"p_limit": limit, "p_claimer": "claude"})
    out.mkdir(parents=True, exist_ok=True)
    items = []
    for r in rows:
        imgs = []
        for a in r.get("attachments") or []:
            local = out / Path(a).name
            try:
                db.download(a, local)
                imgs.append(str(local))
            except Exception as e:
                imgs.append(f"(download failed: {e})")
        items.append({"id": r["id"], "kind": r["kind"], "priority": r["priority"], "payload": r["payload"],
                      "images": imgs})
    (out / "requests.json").write_text(json.dumps(items, ensure_ascii=False, indent=1))
    print(json.dumps({"claimed": len(items), "file": str(out / "requests.json"),
                      "kinds": [i["kind"] for i in items]}))
    for i in items:
        print("\n" + json.dumps(i, ensure_ascii=False))


def submit(path: Path):
    _seen()
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        data = [{"id": k, "result": v} for k, v in data.items()]
    ok, bad = 0, []
    for item in data:
        req = db.get("llm_requests", id=item["id"])
        if not req:
            bad.append(f"{item['id']}: not found")
            continue
        res = item["result"]
        missing = REQUIRED.get(req["kind"], set()) - set(res)
        if missing:
            bad.append(f"{item['id']} ({req['kind']}): missing {sorted(missing)}")
            continue
        db.update("llm_requests", {"id": req["id"]}, {"status": "done", "result": res, "done_at": db.iso()})
        if req["kind"] == "task" and req.get("task_id"):
            db.update("tasks", {"id": req["task_id"]}, {"status": "done", "result": str(res.get("answer"))[:5000],
                                                         "updated_at": db.iso()})
        ok += 1
    print(json.dumps({"submitted": ok, "errors": bad}))
    if bad:
        sys.exit(1)


def status():
    rows = db.sb().table("llm_requests").select("kind,status").in_("status", ["pending", "claimed"]).execute().data
    by = {}
    for r in rows:
        by[f"{r['kind']}:{r['status']}"] = by.get(f"{r['kind']}:{r['status']}", 0) + 1
    print(json.dumps({"open": len(rows), "by": by, "bridge": db.setting("bridge")}))


def wait(every: int):
    while True:
        n = db.sb().table("llm_requests").select("id", count="exact").eq("status", "pending").execute().count
        if n:
            print(f"WORK: {n} pending request(s)")
            return
        b = db.setting("bridge", {}) or {}
        b.update({"listener_seen_at": db.iso()})
        db.set_setting("bridge", b)
        time.sleep(every)


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("pull"); p.add_argument("--limit", type=int, default=8); p.add_argument("--out", default="/tmp/clipforge-bridge")
    s = sp.add_parser("submit"); s.add_argument("file")
    sp.add_parser("status"); sp.add_parser("sleep")
    w = sp.add_parser("wait"); w.add_argument("--every", type=int, default=10)
    a = ap.parse_args()
    if a.cmd == "pull":
        pull(a.limit, Path(a.out))
    elif a.cmd == "submit":
        submit(Path(a.file))
    elif a.cmd == "status":
        status()
    elif a.cmd == "sleep":
        _seen("sleeping")
        print("sleeping")
    elif a.cmd == "wait":
        wait(a.every)


if __name__ == "__main__":
    main()
