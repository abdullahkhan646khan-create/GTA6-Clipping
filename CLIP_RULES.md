# ClipForge Rulebook — How we pick, cut, build, write and publish a clip

These are the rules every agent follows. They are seeded into the `rules` table and editable from the dashboard.
Numbers are starting values; the Analyst tunes them weekly from real performance.

---

## R1. Moment detection (Detector)

### Signals (computed every second, scored over a sliding 10 s window)
| Signal | How | Weight |
|---|---|---|
| **Chat spike** | messages/sec vs. rolling 5-min baseline (z-score). Hype messages count ×2 | 35 |
| **Hype keywords** | `LMAO, LOL, 😂, 💀, KEKW, OMEGALUL, NO WAY, BRO, W, L, CLIP, CLIP IT, WTF, HOLY, 😭, ??? , !!!` | (inside chat) |
| **Audio spike** | short-term loudness ≥ baseline + 8 dB for ≥ 0.5 s (yell, scream, laugh) | 25 |
| **Viewer jump** | concurrent viewers +3% in 2 min | 10 |
| **Super Chats / members burst** | ≥ 3 in 30 s | 5 |
| **Streamer says clip-words** | "clip that", "no way", "did you see that" (from live transcript of the window) | 25 |

`signal_score = Σ weight × normalized(signal)` → 0–100.

### Triggers
- `signal_score ≥ 60` → **candidate moment**.
- **Chat delay correction:** peak = chat peak − `chat_delay_s` (default 6 s; calibrated per stream by lining up audio spikes with chat spikes).
- **Cooldown:** max 1 moment per stream per 90 s (merge overlapping spikes into one moment).
- Wait **15 s post-roll** after the peak before cutting.

### Final score (Cutter, after Claude watches the transcript + frames)
`final_score = 0.4 × signal_score + 0.6 × llm_score × category_weight × streamer_weight`
- `final_score ≥ 70` → edit it. `< 70` → archive (kept for analytics, not edited).
- Freshness: launch week, moments older than 6 h lose 20%.

---

## R2. What makes the cut (categories)

| Tier | Category | Examples | Starting weight |
|---|---|---|---|
| **S** | First reactions | Streamer's first look at Vice City / Leonida, graphics shock, "this is insane" (launch week only) | 1.3 |
| **S** | Glitch / physics chaos | NPC flies away, car stuck in a wall, ragdoll madness | 1.3 |
| **S** | Rage / scream / jump scare | Streamer loses it, alligator surprise, crash at the end of a long drive | 1.25 |
| **S** | Absurd NPC interaction | NPC says something wild, unexpected AI behaviour | 1.2 |
| **A** | Police chase chaos | Wanted-level madness with a funny or dramatic ending | 1.1 |
| **A** | Epic fail | Missed jump, own-goal explosion, walks into traffic | 1.1 |
| **A** | Clutch / insane skill | Impossible escape, perfect stunt | 1.0 |
| **A** | Easter egg / secret found | Hidden reference, secret location | 1.0 |
| **B** | Funny banter / wholesome | Good joke with chat, co-streamer moment | 0.9 |
| **B** | Info ("how to get money fast") | Better as a separate guide format | 0.7 |

### Hard rejects (never publish)
- Clip is > 30% cutscene, menu, loading screen or map screen.
- **Story spoilers** (missions' endings, major plot reveals, character deaths) in title, hook text or the first 3 s.
- **Licensed music dominant** — GTA in-game radio or streamer's music is audible for > 40% of the clip (Content ID risk). Detector flag: Whisper transcribes song lyrics.
- Hate speech, slurs, sexual content, real violence, doxxing/personal info, anything involving minors.
- Dead air: > 1.5 s of no speech AND no action.
- No visible reaction **and** no notable gameplay action (nothing happens).
- Streamer `permission_status` not in (granted, open_policy).
- Duplicate: same event from the same streamer within 60 s, or the same event from multiple POVs (keep the highest score).

---

## R3. Cutting rules (Cutter)

- **Length:** 15–45 s, target **25–35 s**, hard max 58 s.
- **Structure:** `HOOK (0–1.5 s) → SETUP (≤ 15 s) → PAYOFF → REACTION TAIL (2–4 s) → END`.
- **First 1.5 s must contain speech or motion.** If the setup is slow (> 12 s), use a **cold open**: show 1–1.5 s of the payoff first, then cut back to the setup with hook text.
- **Start** on the start of a sentence or the start of an action — never mid-word (snap to word timestamps, pad 0.15 s).
- **End** within 1.5 s after the reaction peak fades. Prefer an ending that loops back into the start (replays boost Shorts).
- **Jump-cut silences** > 0.8 s only in talking parts — never during action.
- Window to pull from the buffer: `[peak − 40 s, peak + 15 s]`; Claude picks the exact in/out points inside it.

---

## R4. Build rules (Editor)

### Format
- 1080×1920 (9:16), 30 fps (60 fps if the source is 60 fps and action-heavy), H.264 High, CRF 20, AAC 192 kbps.
- Loudness **−14 LUFS**, true peak −1 dBTP.

### Layouts (chosen automatically; per-streamer facecam box saved in `streamers.layout_preset`)
| Layout | When | Composition |
|---|---|---|
| **L1 Stack** (default) | Streamer has a facecam and reaction matters | Facecam top 40% (1080×768) · gameplay bottom 60% (1080×1152, centre crop with slight zoom) |
| **L2 Full Game** | No facecam, or the action is the content | 9:16 crop that follows the action; blurred fill if needed; optional small facecam bubble |
| **L3 Reaction** | The face/reaction *is* the moment | Facecam full frame, small gameplay picture-in-picture |

### Captions
- Word-by-word, groups of **1–3 words**, bold font (Montserrat ExtraBold / similar open font), ~85 px, white with 8 px black outline.
- **Active word highlighted** (yellow `#FFE600`); emphasis words (names, "NO WAY", numbers) bigger or green.
- Position: on the seam between facecam and gameplay (L1) or ~65% height (L2/L3).
- **Safe zone:** keep all text inside x 60–960 and y 220–1480 (YouTube's buttons are on the right and the title/description at the bottom).
- Strong profanity masked in captions (`f***`). Slurs → hard reject.
- Caption accuracy ≥ 95% (QA checks against the transcript confidence).

### Hook text
- 2–6 words, ALL CAPS allowed, top area, visible 0–2.5 s. Must match what happens (e.g. "HE WAS NOT READY 💀", "GTA 6 PHYSICS ARE BROKEN").

### Effects (less is more)
- Zoom punch 1.0 → 1.12 in 0.25 s on the payoff. Max **3 effects per clip**.
- SFX from our own/royalty-free library only, ≤ 2 per clip (whoosh on the cold-open cut, one hit on the payoff). No background music (Content ID risk).

### Branding / credit
- "🎥 @StreamerName" small badge top-left for the first 3 s (always).
- Small channel watermark (40% opacity), outside the caption zone.

---

## R5. Writing rules (Writer)

### Title
- ≤ 60 characters (aim 35–50). Always contains **"GTA 6"** during launch month. Front-load the subject.
- Must be **true to the clip** — no fake clickbait (it kills retention and trust).
- Max 1 emoji; not all caps; no spoilers.
- Formulas (Writer picks and rotates; the Analyst ranks them by performance):
  - `{Streamer} Wasn't Ready For {X} In GTA 6 😭`
  - `GTA 6 {Thing} Is Actually Insane`
  - `This GTA 6 {Glitch/NPC} Broke {Streamer}`
  - `{Streamer}'s First Time {Doing X} In GTA 6`
  - `Nobody Expected This In GTA 6…`
  - `{Streamer} Found {Secret} In GTA 6`

### Description
```
{one-line hook sentence}
🎥 Credit: {Streamer} — {channel_url}
▶ Full stream: {stream_url}&t={seconds}s
#GTA6 #GTAVI #Shorts #{StreamerTag} #{CategoryTag}
```
- 3–5 hashtags, ≤ 10 tags, English first (localized channels later).

---

## R6. QA rules (QA agent) — must score ≥ 85 to pass

**Automatic technical checks (each pass/fail):** duration in range · resolution/fps correct · loudness −14 ±1 LUFS · no black/frozen frames > 0.5 s · audio present and not clipped · captions inside safe zone · credit badge present · file < 100 MB.

**Claude review (scored):**
| Check | Points |
|---|---|
| Hook grabs attention in the first 1.5 s | 20 |
| Clear payoff; clip makes sense without context | 20 |
| Captions accurate and in sync | 15 |
| Title/hook text truthful, no spoilers, follows R5 | 15 |
| Good framing (face visible, action not cropped) | 10 |
| Clean start/end (no mid-word cuts) | 10 |
| No hard-reject issues (R2) | 10 (any violation = automatic fail) |

Verdict: **pass** (≥ 85) → publish queue · **fix** (70–84) → auto re-render/rewrite once, then human review · **fail** (< 70 or any hard reject) → rejected with reasons.

---

## R7. Publishing rules (Publisher)
- Autopilot level decides auto vs. manual approval (see PLAN.md §2).
- **Cadence:** start at **10 Shorts/day per channel**, at least 30 min apart; launch week allows up to 1 every 30 min during peak hours.
- No more than **2 clips in a row** from the same streamer.
- Freshness first: during launch week the newest high-score clip jumps the queue.
- Peak windows (ET): 12:00–15:00 and 18:00–23:00; launch week publishes around the clock.
- Daily cap and kill switch are always respected.

---

## R8. Learning rules (Analyst)
- Pull metrics at **1 h, 3 h, 24 h** after publishing: views, average view %, likes, comments, subs gained.
- A clip is a **winner** if it's in the top 20% of views at 24 h **and** has average view % ≥ 70%.
- Weekly (and daily during launch week): nudge `category_weight`, `streamer_weight`, title formula ranking and hook styles up for winners / down for losers (max ±10% per update).
- Store the top 20 winners' hooks/titles in the skill's `hooks-and-titles.md` as examples for the Writer.
