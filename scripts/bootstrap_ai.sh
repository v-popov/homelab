#!/usr/bin/env bash
set -euo pipefail

set -a
source /opt/homelab/config/ai.cfg
set +a

docker exec -it ollama ollama pull "${LLM_MODEL}"
