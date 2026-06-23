#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONFIG_FILE="${CONFIG_PATH:-/workspace/configuration.yaml}"
export CONFIG_FILE


# GPU preflight benchmark
if python -m modules.metrics.preflight; then
    echo "=== PRE-FLIGHT OK ==="
else
    echo "=== PRE-FLIGHT FAILED — starting FastAPI in REPLACE mode, skipping vLLM ==="
    exec python serve.py
fi


# FastAPI
echo "=== STAGE 2: FastAPI ==="
python serve.py &
SERVE_PID=$!


# Dedicated GLM vLLM env (GLM-4.6V needs a newer transformers than the coder).
# Idempotent: only builds when the binary is missing. Non-fatal: on failure the
# judge/critic client is skipped and the coder still comes up.
GLM_VLLM_BIN="${GLM_VLLM_BIN:-/opt/vllm-glm-env/bin/vllm}"
if [ ! -x "$GLM_VLLM_BIN" ]; then
    echo "=== STAGE 2.5: building GLM vLLM env ($GLM_VLLM_BIN missing) ==="
    bash "$SCRIPT_DIR/scripts/setup_glm_vllm_env.sh" \
        || echo "[run.sh] GLM env setup failed — judge/critic vLLM skipped, coder continues" >&2
else
    echo "[run.sh] GLM vLLM env present at $GLM_VLLM_BIN — skipping build"
fi


# vLLM spawn
echo "=== STAGE 3: vLLM spawn ==="
python -m llm.spawn || echo "[run.sh] vllm spawn returned non-zero — FastAPI continues for diagnostics" >&2

wait $SERVE_PID
