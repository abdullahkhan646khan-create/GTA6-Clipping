"""Editor: renders the vertical Short (1080×1920) from the brief — layout, karaoke captions, hook text,
streamer credit, zoom punches, loudness −14 LUFS — then hands it to QA."""
import re
import subprocess
from pathlib import Path

from ..core import config, db, media, rules
from ..core.agent import Agent

W, H = 1080, 1920
BASE = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1"
STRONG = re.compile(r"\b(fuck\w*|shit\w*|bitch\w*|cunt\w*|motherfuck\w*)\b", re.I)


class Editor(Agent):
    type = "editor"
    job_types = ["edit"]

    def handle(self, job):
        clip = db.get("clips", id=job["payload"]["clip_id"])
        brief = (clip.get("brief") or {}).get("final") or {}
        R = rules.get()
        b = R["build"]
        window = Path(clip["raw_path"])
        work = window.parent
        start, end = float(brief.get("start", clip["start_s"])), float(brief.get("end", clip["end_s"]))
        layout = brief.get("layout") or "full"
        cam = brief.get("cam_box")
        if layout in ("stack", "reaction") and not cam:
            layout = "full"
        streamer = db.get("streamers", id=clip["streamer_id"]) if clip.get("streamer_id") else {}
        name = (streamer or {}).get("name", "")

        self.status(f"rendering {name} clip ({end - start:.0f}s, layout {layout})",
                    next_steps=["captions", "encode", "upload", "send to QA"])
        words = [w for w in (clip.get("transcript") or []) if w["s"] >= start - 0.05 and w["e"] <= end + 0.05]
        words = [{**w, "s": max(0.0, w["s"] - start), "e": w["e"] - start} for w in words]
        ass = build_ass(words, brief, name, layout, b)
        (work / "captions.ass").write_text(ass, encoding="utf-8")

        vf = graph(layout, cam, brief.get("zoom_at") or [], b, end - start)
        out = work / "short.mp4"
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(window),
               "-t", f"{end - start:.3f}", "-filter_complex", vf, "-map", "[out]", "-map", "0:a?",
               "-af", f"loudnorm=I={b['lufs']}:TP={b['true_peak']}:LRA=11",
               "-c:v", "libx264", "-preset", config.X264_PRESET, "-crf", str(b["crf"]), "-maxrate", b["maxrate"],
               "-bufsize", "12M", "-pix_fmt", "yuv420p", "-r", str(b["fps"]),
               "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", "short.mp4"]
        p = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=1200)
        if p.returncode != 0:
            raise RuntimeError(f"render failed: {p.stderr[-600:]}")

        dur = media.duration(out)
        times = [round(0.3 + i * (dur - 0.6) / 5, 1) for i in range(6)]
        sheet_local = media.contact_sheet(out, work / "qa_sheet.jpg", times, cols=3, tile_w=270)
        video_path = db.upload(out, f"clips/{clip['id']}.mp4", "video/mp4")
        sheet_path = db.upload(sheet_local, f"sheets/{clip['id']}_qa.jpg", "image/jpeg")
        db.update("clips", {"id": clip["id"]}, {"video_path": video_path, "sheet_path": sheet_path,
                                                "layout": layout, "start_s": start, "end_s": end,
                                                "status": "awaiting_qa", "updated_at": db.iso()})
        db.enqueue("qa", {"clip_id": clip["id"]}, priority=int(clip.get("final_score") or 50))
        self.log(f"{name}: rendered {dur:.1f}s Short → QA", "success", clip_id=clip["id"])
        return {"video_path": video_path}


def graph(layout: str, cam, zoom_at: list, b: dict, dur: float) -> str:
    fps = b["fps"]
    if cam:
        x, y, w, h = [float(v) for v in cam]
        cx, cy, cw, ch = int(x * 1920), int(y * 1080), max(64, int(w * 1920)), max(64, int(h * 1080))
    if layout == "stack":
        g = (f"[0:v]{BASE},split=2[a][b];"
             f"[a]crop={cw}:{ch}:{cx}:{cy},scale=1080:768:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:768,unsharp=5:5:0.7[cam];"
             f"[b]crop=1012:1080:454:0,scale=1080:1152:flags=lanczos[game];[cam][game]vstack=2[v0]")
    elif layout == "reaction":
        g = (f"[0:v]{BASE},crop={cw}:{ch}:{cx}:{cy},"
             f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},unsharp=5:5:0.5[v0]")
    else:  # full game: 4:5 centre crop over blurred fill
        g = (f"[0:v]{BASE},split=2[a][b];"
             f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:2,eq=brightness=-0.08[bg];"
             f"[b]crop=864:1080:528:0,scale=1080:1350:flags=lanczos[fg];[bg][fg]overlay=0:285[v0]")
    g += f";[v0]fps={fps},setsar=1[v1]"
    zs = [float(z) for z in zoom_at[: b["max_zooms"]] if 0 <= float(z) < dur]
    if zs:
        cond = "+".join(f"between(in,{int(z * fps)},{int((z + 1.2) * fps)})" for z in zs)
        g += (f";[v1]zoompan=z='if({cond},{b['zoom']},1)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":d=1:s={W}x{H}:fps={fps}[v2]")
        last = "v2"
    else:
        last = "v1"
    # compose at 1080×1920 (caption coordinates), then downscale in light mode — encoding is the CPU-heavy part
    final_scale = f",scale={config.OUT_W}:{config.OUT_H}:flags=fast_bilinear" if config.OUT_H != H else ""
    g += f";[{last}]subtitles=captions.ass{final_scale}[out]"
    return g


def _t(s: float) -> str:
    s = max(0.0, s)
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"


def _esc(t: str) -> str:
    return t.replace("\\", "").replace("{", "(").replace("}", ")")


def build_ass(words, brief, name, layout, b) -> str:
    fixes = {k.lower(): v for k, v in (brief.get("caption_fixes") or {}).items()}
    censor = {w.lower() for w in (brief.get("censor_words") or [])}
    toks = []
    for w in words:
        t = fixes.get(w["w"].lower().strip(".,!?"), w["w"])
        if t.lower().strip(".,!?") in censor or STRONG.search(t):
            t = t[0] + "*" * max(1, len(t) - 1)
        toks.append({**w, "t": _esc(t.upper())})
    # group into captions of ≤ N words, breaking on pauses / punctuation
    groups, cur = [], []
    for i, w in enumerate(toks):
        cur.append(w)
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if not nxt or len(cur) >= b["max_words_per_caption"] or nxt["s"] - w["e"] > 0.4 or w["t"][-1:] in ".?!,":
            groups.append(cur)
            cur = []
    y = {"stack": 768, "reaction": 1300, "full": 1360}.get(layout, 1360)
    hl, base = b["caption_highlight"][-6:], b["caption_color"][-6:]  # override tags use &HBBGGRR&
    lines = []
    for g in groups:
        for j, w in enumerate(g):
            s = w["s"]
            e = g[j + 1]["s"] if j + 1 < len(g) else max(w["e"], s + 0.25)
            text = " ".join((f"{{\\c&H{hl}&\\fscx110\\fscy110}}{x['t']}{{\\c&H{base}&\\fscx100\\fscy100}}"
                             if k == j else x["t"]) for k, x in enumerate(g))
            lines.append(f"Dialogue: 1,{_t(s)},{_t(e)},Cap,,0,0,0,,{{\\pos(540,{y})}}{text}")
    hook = _esc((brief.get("hook_text") or "").upper())
    if hook:
        lines.append(f"Dialogue: 2,{_t(0)},{_t(b['hook_seconds'])},Hook,,0,0,0,,{{\\pos(540,330)\\fad(80,200)}}{hook}")
    if name:
        lines.append(f"Dialogue: 2,{_t(0)},{_t(b['credit_seconds'])},Credit,,0,0,0,,"
                     f"{{\\pos(70,230)\\fad(80,300)}}▶ @{_esc(name)}")
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{b['caption_font']},{b['caption_size']},{b['caption_color']},&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,{b['caption_outline']},2,5,60,60,0,1
Style: Hook,{b['caption_font']},92,&H0000E6FF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,9,3,5,60,60,0,1
Style: Credit,{b['caption_font']},40,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,3,4,0,7,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    return head + "\n".join(lines) + "\n"
