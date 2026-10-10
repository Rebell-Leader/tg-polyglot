#!/bin/bash
# Build and (re)start the bot. Data lives in ./data, so rebuilding keeps users and settings.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "Create .env first: cp .env.example .env"; exit 1; }
mkdir -p data   # created by you (not by docker as root), so the container user can write to it
docker compose up -d --build
docker compose logs --tail 20 bot
