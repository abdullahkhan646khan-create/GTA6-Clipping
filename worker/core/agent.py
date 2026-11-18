"""Base class for every agent: registration, heartbeat, live status for the dashboard, job loop."""
import threading
import time
import traceback

from . import config, db


class Agent:
    type: str = "agent"
    job_types: list[str] = []   # job types this agent claims; empty = custom loop()
    poll_s: float = 3.0

    def __init__(self, suffix: str = ""):
        self.id = f"{self.type}{suffix}@{config.NODE}"
        self._stop = threading.Event()
        self.current_job = None
        self.stats = {"done": 0, "failed": 0}
        db.retry(lambda: db.sb().table("agents").upsert({
            "id": self.id, "type": self.type, "node": config.NODE, "status": "idle",
            "current_action": "starting", "current_job_id": None, "next_steps": [],
            "last_heartbeat": db.iso(), "started_at": db.iso()}).execute())
        threading.Thread(target=self._heartbeat, daemon=True, name=f"hb-{self.id}").start()

    # ───── status reporting (what the dashboard shows) ─────
    def status(self, action: str, status: str = "working", next_steps: list[str] | None = None):
        vals = {"current_action": action[:300], "status": status, "last_heartbeat": db.iso(),
                "current_job_id": self.current_job["id"] if self.current_job else None, "stats": self.stats}
        if next_steps is not None:
            vals["next_steps"] = next_steps
        try:
            db.update("agents", {"id": self.id}, vals)
        except Exception as e:
            print(f"[status-fail] {self.id}: {e}")

    def log(self, message: str, level: str = "info", **data):
        db.event(self.id, message, level, data or None)

    def _heartbeat(self):
        while not self._stop.is_set():
            try:
                db.update("agents", {"id": self.id}, {"last_heartbeat": db.iso()})
            except Exception:
                pass
            self._stop.wait(15)

    # ───── main loop ─────
    def run(self):
        self.status("waiting for work", "idle")
        while not self._stop.is_set():
            try:
                if self.job_types:
                    if self.paused():
                        self.status("paused (kill switch)", "stopped")
                        time.sleep(10)
                        continue
                    jobs = db.rpc("claim_job", {"p_types": self.job_types, "p_worker": self.id})
                    if not jobs:
                        time.sleep(self.poll_s)
                        continue
                    self._run_job(jobs[0])
                else:
                    self.loop()
            except Exception as e:
                self.log(f"loop error: {e}", "error", trace=traceback.format_exc()[-1500:])
                self.status(f"error: {e}", "error")
                time.sleep(10)

    def _run_job(self, job):
        self.current_job = job
        self.status(f"{job['type']} started", "working")
        try:
            result = self.handle(job) or {}
            db.finish_job(job["id"], True, result)
            self.stats["done"] += 1
        except Exception as e:
            self.stats["failed"] += 1
            self.log(f"{job['type']} failed: {e}", "error", job_id=job["id"],
                     trace=traceback.format_exc()[-1500:])
            db.finish_job(job["id"], False, error=str(e)[:1000])
        finally:
            self.current_job = None
            self.status("waiting for work", "idle")

    def paused(self) -> bool:
        ks = db.setting("kill_switch", {}) or {}
        return bool(ks.get("all"))

    def handle(self, job: dict) -> dict | None:  # override
        raise NotImplementedError

    def loop(self):  # override for non-job agents
        time.sleep(self.poll_s)

    def stop(self):
        self._stop.set()
        try:
            db.update("agents", {"id": self.id}, {"status": "stopped", "current_action": "stopped"})
        except Exception:
            pass
