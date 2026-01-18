#!/usr/bin/env bash
set -euo pipefail

cd /opt/homelab

git fetch origin
git checkout main
git reset --hard origin/main

# Ensure shared docker network exists (for cross-stack communication)
docker network inspect homelab >/dev/null 2>&1 || docker network create homelab

# Portainer
docker compose -f stacks/portainer/docker-compose.yml pull
docker compose -f stacks/portainer/docker-compose.yml up -d --remove-orphans

# Postgres (shared DB)
docker compose -f stacks/shared/postgres/docker-compose.yml pull
docker compose -f stacks/shared/postgres/docker-compose.yml up -d --remove-orphans

# AI Stack
docker compose --env-file config/ai.cfg -f stacks/ai/docker-compose.yml pull
docker compose --env-file config/ai.cfg -f stacks/ai/docker-compose.yml up -d --remove-orphans

# HomeLab Bot
docker compose -f stacks/homelab_bot/docker-compose.yml pull
docker compose -f stacks/homelab_bot/docker-compose.yml up -d --remove-orphans
