# syntax=docker/dockerfile:1.7
#
# leanctx "Layer 8" serving sidecar for ClawRouter — CPU image.
#
# Unlike the repo-root ./Dockerfile (a base image that just pre-installs leanctx
# and exits), this image RUNS the HTTP service (leanctx-serve) with the ~1.2 GB
# LLMLingua-2 model baked in, ready for ClawRouter to call over POST /compress.
# It builds leanctx from the in-repo source, so it needs no PyPI release.
#
# Build from the repo root:
#   docker build -f integrations/clawrouter/Dockerfile -t leanctx-layer8:cpu .
# Or via compose:
#   docker compose -f integrations/clawrouter/docker-compose.yml up -d --build
# Smoke:
#   ./integrations/clawrouter/scripts/smoke.sh
#
# For production / RTX-class GPUs, use Dockerfile.gpu.

FROM python:3.12-slim

ARG LINGUA_MODEL=microsoft/llmlingua-2-xlm-roberta-large-meetingbank

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models

# CPU-only torch in its own layer (the default PyPI wheel bundles CUDA and is
# ~2 GB larger; a separate layer also stays cached across source edits).
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch

# Build leanctx from the in-repo source — same cache-friendly copy order as the
# repo-root Dockerfile. The [server] extra (FastAPI/uvicorn + leanctx-serve) +
# [lingua] (LLMLingua-2) + [tokens] (real tiktoken counts, not chars/4).
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY leanctx ./leanctx
RUN pip install ".[server,lingua,tokens]"

# Bake the LLMLingua-2 weights into the image (offline, deterministic first boot).
RUN python -c "from llmlingua import PromptCompressor; \
PromptCompressor(model_name='${LINGUA_MODEL}', use_llmlingua2=True, device_map='cpu')"
ENV HF_HUB_OFFLINE=1

# --- ClawRouter Layer-8 defaults (override via .env / compose / -e) ---
#   threshold 1500  -> never compress short system/instruction turns
#   dedup off       -> ClawRouter's Layer 1 already owns deduplication
ENV LEANCTX_SERVER_MODE=on \
    LEANCTX_SERVER_THRESHOLD=1500 \
    LEANCTX_SERVER_LINGUA_RATIO=0.5 \
    LEANCTX_SERVER_LINGUA_DEVICE=cpu \
    LEANCTX_SERVER_DEDUP=off

EXPOSE 8459

# Generous start-period: the startup warmup loads the model before serving.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8459/health').getcode()==200 else 1)"

# 0.0.0.0 so the container is reachable; a single worker keeps one warm model.
CMD ["leanctx-serve", "--host", "0.0.0.0", "--port", "8459", "--workers", "1"]
