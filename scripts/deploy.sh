#!/usr/bin/env bash
set -euo pipefail

cd /opt/homelab

git fetch origin
git checkout main
git reset --hard origin/main

# AI Stack
docker compose -f stacks/ai/docker-compose.yml pull
docker compose -f stacks/ai/docker-compose.yml up -d --remove-orphans
