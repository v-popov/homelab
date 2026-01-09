#!/usr/bin/env bash
set -euo pipefail

docker exec -it ollama ollama pull qwen2.5:7b-instruct

