# syntax=docker/dockerfile:1.7
FROM pytorch/pytorch:2.8.0-cuda12.8-cudnn9-devel

# Environment
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TORCH_CUDA_ARCH_LIST="8.0;8.6;8.9;9.0;10.0;12.0" \
    CUDA_HOME=/usr/local/cuda-12.8 \
    PATH=/usr/local/cuda-12.8/bin:/opt/vllm-env/bin:$PATH \
    LD_LIBRARY_PATH=/usr/local/cuda-12.8/lib64:/usr/lib64:$LD_LIBRARY_PATH \
    PUPPETEER_CACHE_DIR=/opt/puppeteer-cache \
    NODE_MAJOR=20 \
    RENDER_POOL_SIZE=32

# System deps + Node.js 20 + Chromium runtime libs
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl wget git build-essential bash gnupg \
        # headless Chromium runtime
        fonts-liberation fonts-noto-color-emoji \
        libnss3 libatk-bridge2.0-0 libx11-xcb1 libxcomposite1 \
        libxdamage1 libxrandr2 libgbm1 libasound2 \
        libpangocairo-1.0-0 libgtk-3-0 \
        libegl1 libgl1-mesa-dri libgles2 \
        xvfb; \
    # Node.js 20 LTS via NodeSource
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -; \
    apt-get install -y --no-install-recommends nodejs; \
    rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y \
    speedtest-cli 

# Python deps
COPY docker/requirements.vllm.txt /tmp/requirements.vllm.txt
COPY docker/requirements.txt      /tmp/requirements.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    python3 -m venv /opt/vllm-env && \
    /opt/vllm-env/bin/pip install --upgrade pip && \
    /opt/vllm-env/bin/pip install --no-build-isolation -r /tmp/requirements.vllm.txt && \
    pip install -r /tmp/requirements.txt

# Node deps 
WORKDIR /workspace
COPY docker/package.json docker/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund

# Add the pipeline service
COPY pipeline_service     /workspace
COPY configuration.yaml   /workspace/configuration.yaml
RUN chmod +x /workspace/run.sh

EXPOSE 10006 8001 8002
CMD ["/workspace/run.sh"]