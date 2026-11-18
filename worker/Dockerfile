# ClipForge engine image (used by the Render worker AND the heartbeat cron job).
# Build context = repo root (see render.yaml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/tmp/clipforge \
    DENO_INSTALL=/usr/local HF_HOME=/opt/hf

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg fonts-dejavu-core fonts-montserrat fontconfig curl unzip git ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Deno: JS runtime yt-dlp uses to solve YouTube's player challenges
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y || echo "deno install failed (yt-dlp may still work)"

# PO-token provider (script mode) for capture level C1 — best effort, build continues if it fails
RUN git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && cd /opt/bgutil/server && npm ci && npx tsc || echo "bgutil build failed (C1 runs without PO tokens)"

WORKDIR /app
COPY worker/requirements.txt worker/requirements.txt
RUN pip install -r worker/requirements.txt && pip install -U --pre "yt-dlp[default]"

# pre-download the Whisper model so the first clip isn't slow
ARG WHISPER_MODEL=base.en
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"

COPY worker worker
COPY CLIP_RULES.md PLAN.md ./

CMD ["python", "-m", "worker.main"]
