#!/usr/bin/env bash
set -euo pipefail

set -a
source /opt/homelab/config/ai.cfg
set +a

if [[ -n "${LLM_MODELS:-}" ]]; then
  for model in ${LLM_MODELS}; do
    docker exec -it ollama ollama pull "${model}"
  done
else
  docker exec -it ollama ollama pull "${LLM_MODEL}"
fi
