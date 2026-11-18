"""Rules = worker/rules/clip_rules.json defaults, deep-merged with the active row in the `rules` table."""
import json
import time
from pathlib import Path

from . import db

_DEFAULTS = json.loads((Path(__file__).resolve().parents[1] / "rules" / "clip_rules.json").read_text())
_cache: dict = {"at": 0.0, "rules": _DEFAULTS}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def get() -> dict:
    """Cached for 60 s so dashboard edits apply without a redeploy."""
    if time.time() - _cache["at"] > 60:
        try:
            rows = db.sb().table("rules").select("rules").eq("active", True) \
                .order("version", desc=True).limit(1).execute().data
            _cache["rules"] = _merge(_DEFAULTS, rows[0]["rules"] if rows else {})
        except Exception:
            _cache["rules"] = _DEFAULTS
        _cache["at"] = time.time()
    return _cache["rules"]
