#!/usr/bin/env bash
# Pulls the latest main, reinstalls deps if locked changed, restarts the service.
# Run as root on the VPS.
set -euo pipefail

sudo -iu aiarena bash -c '
  set -euo pipefail
  cd /opt/ai-arena-recap
  git fetch origin main
  git reset --hard origin/main
  ~/.local/bin/uv sync --frozen
  echo "Deployed: $(git rev-parse --short HEAD)"
'

systemctl restart ai-arena-recap
systemctl is-active --quiet ai-arena-recap || {
  echo "Service failed to start; check journalctl -u ai-arena-recap" >&2
  exit 1
}

echo "Service restarted."
