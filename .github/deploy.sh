#!/bin/bash
# =============================================================================
# twitch_consumer — deploy the Docker image from Docker Hub.
#
# Runs ON the server, invoked by .github/workflows/deploy.yml over SSH with
# DEPLOY_TAG=<commit short SHA> exported. The image is built and pushed in CI
# (public repo gunlinuxloki/twitch_consumer — no docker login needed here);
# this script installs the systemd unit with the tag baked in (the unit pulls
# the image and runs it), and restarts the service. Idempotent: safe to re-run.
# =============================================================================
set -euo pipefail

REPO_DIR="/home/loki/twitch_consumer"
BRANCH="main"
IMAGE="gunlinuxloki/twitch_consumer"
NEW_UNIT="twitch_consumer"

cd "$REPO_DIR" || exit 1

TAG="${DEPLOY_TAG:?DEPLOY_TAG is required (commit short SHA)}"

# The server's git remote historically pointed at the wrong repo; enforce the
# real one so `git fetch` always pulls this repo.
git remote set-url origin git@github.com:gunlinux/twitch_consumer.git

# --- 1. Pull the deployed commit ----------------------------------------------
git fetch --all
git reset --hard "origin/$BRANCH"

# docker --env-file does NOT strip quotes (unlike systemd EnvironmentFile);
# normalize any remaining KEY="value" lines so the container gets clean env.
sed -i -E 's/^([A-Z_]+)="(.*)"$/\1=\2/' .env || true

# --- 2. Install the unit with the commit-hash tag baked in --------------------
sed -e "s|@IMAGE_TAG@|$TAG|g" deploy/twitch_consumer.service \
    | sudo tee "/etc/systemd/system/$NEW_UNIT.service" >/dev/null
sudo systemctl daemon-reload

# --- 3. Start/update the service ---------------------------------------------
sudo systemctl enable --now "$NEW_UNIT"

# `enable --now` does not restart an already-active unit; restart explicitly
# when the running image differs from the deployed tag (idempotent no-op when
# the tag is unchanged).
CURRENT="$(docker inspect --format '{{.Config.Image}}' "$NEW_UNIT" 2>/dev/null || true)"
if [ "$CURRENT" != "$IMAGE:$TAG" ]; then
    sudo systemctl restart "$NEW_UNIT"
fi

echo "Deployment completed: $IMAGE:$TAG"
