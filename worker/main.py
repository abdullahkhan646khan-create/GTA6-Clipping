"""Engine entrypoint. ROLE=engine runs every agent as a thread in one Render Background Worker.
Run: python -m worker.main
"""
import signal
import sys
import threading
import time

from .core import config, db


def run_engine():
    from .agents.bridge_watcher import BridgeWatcher
    from .agents.capture import CaptureManager
    from .agents.cutter import Cutter
    from .agents.editor import Editor
    from .agents.publisher import Publisher
    from .agents.qa import QA

    agents = [CaptureManager(), Cutter(), Editor(), QA(), Publisher(), BridgeWatcher()]
    for a in agents:
        threading.Thread(target=a.run, daemon=True, name=a.id).start()
    db.event(f"engine@{config.NODE}", f"engine started with {len(agents)} agents", "success")

    def shutdown(*_):
        for a in agents:
            a.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    while True:
        time.sleep(60)


if __name__ == "__main__":
    if config.ROLE == "capture_test":
        from .tools.capture_test import main
        main()
    elif config.ROLE == "heartbeat":
        from .heartbeat import main
        main()
    else:
        run_engine()
