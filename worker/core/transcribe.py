"""Free local transcription with word timestamps (faster-whisper, int8 on CPU)."""
import threading
from pathlib import Path

from . import config

_model = None
_lock = threading.Lock()


def _get():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model


def words(audio: Path) -> list[dict]:
    """[{"w": "word", "s": start, "e": end, "p": prob}] — relative to the audio file start."""
    with _lock:  # one transcription at a time keeps RAM bounded
        segments, _info = _get().transcribe(str(audio), word_timestamps=True, vad_filter=True, beam_size=1)
        out = []
        for seg in segments:
            for w in seg.words or []:
                out.append({"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2),
                            "p": round(w.probability, 2)})
        return out


def compact(ws: list[dict], gap: float = 0.6) -> str:
    """Transcript as short timestamped lines — compact so Claude reads it cheaply."""
    lines, cur, start = [], [], None
    for i, w in enumerate(ws):
        if start is None:
            start = w["s"]
        cur.append(w["w"])
        nxt = ws[i + 1] if i + 1 < len(ws) else None
        if not nxt or nxt["s"] - w["e"] > gap or w["w"][-1:] in ".?!" or len(cur) >= 14:
            lines.append(f"[{start:.1f}] {' '.join(cur)}")
            cur, start = [], None
    return "\n".join(lines)
