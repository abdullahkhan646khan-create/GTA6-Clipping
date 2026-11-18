"""ffmpeg helpers: segment windows, loudness, contact sheets, probing, technical QA checks."""
import json
import re
import subprocess
from pathlib import Path

from . import config


def run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {p.stderr[-800:]}")
    return p


def probe(path: Path) -> dict:
    p = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    return json.loads(p.stdout)


def duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def concat_segments(segs: list[Path], out: Path) -> Path:
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{s.resolve()}'\n" for s in segs))
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c", "copy", str(out)])
    lst.unlink(missing_ok=True)
    return out


def trim(src: Path, start: float, end: float, out: Path) -> Path:
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(src),
         "-t", f"{end - start:.3f}", "-c", "copy", str(out)])
    return out


def extract_audio(src: Path, out: Path) -> Path:
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1",
         "-ar", "16000", str(out)])
    return out


_M = re.compile(r"t:\s*([\d.]+).*?M:\s*(-?[\d.]+|-inf)")


def momentary_loudness(src: Path) -> list[tuple[float, float]]:
    """[(t_seconds, LUFS)] every 0.1 s using ebur128 (works on a 6 s segment in ~0.2 s)."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(src), "-vn",
                        "-af", "ebur128=framelog=info", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=120)
    out = []
    for line in p.stderr.splitlines():
        m = _M.search(line)
        if m and m.group(2) != "-inf":
            out.append((float(m.group(1)), float(m.group(2))))
    return out


def integrated_loudness(src: Path) -> float | None:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(src), "-vn",
                        "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    m = re.findall(r"I:\s*(-?[\d.]+)\s*LUFS", p.stderr)
    return float(m[-1]) if m else None


def black_frames(src: Path, min_d: float = 0.5) -> list[tuple[float, float]]:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(src), "-vf",
                        f"blackdetect=d={min_d}:pix_th=0.10", "-an", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=300)
    return [(float(a), float(b)) for a, b in
            re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", p.stderr)]


def contact_sheet(src: Path, out: Path, times: list[float], cols: int = 3, tile_w: int = 480) -> Path:
    """One JPEG with frames at `times`, each labelled with its timestamp — what Claude looks at."""
    frames = []
    for i, t in enumerate(times):
        f = out.parent / f"{out.stem}_f{i}.jpg"
        draw = (f"drawtext=fontfile={config.FONT_FILE}:text='t={t:.1f}s':x=10:y=10:fontsize=28:"
                f"fontcolor=yellow:box=1:boxcolor=black@0.6")
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(t, 0):.2f}",
                        "-i", str(src), "-frames:v", "1", "-vf", f"scale={tile_w}:-2,{draw}", str(f)],
                       capture_output=True, timeout=60)
        if f.exists():
            frames.append(f)
    if not frames:
        raise RuntimeError("no frames for contact sheet")
    inputs = sum([["-i", str(f)] for f in frames], [])
    n = len(frames)
    pad = "".join(f"[{i}:v]" for i in range(n))
    # xstack grid layout: x = (col × w0), y = (row × h0); all tiles share the same size
    layout = "|".join(
        ("+".join(["w0"] * (i % cols)) or "0") + "_" + ("+".join(["h0"] * (i // cols)) or "0") for i in range(n))
    fill = f"xstack=inputs={n}:layout={layout}:fill=black" if n > 1 else "null"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs, "-filter_complex",
         f"{pad}{fill}", "-frames:v", "1", "-q:v", "4", str(out)])
    for f in frames:
        f.unlink(missing_ok=True)
    return out
