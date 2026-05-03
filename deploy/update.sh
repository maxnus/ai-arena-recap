#!/usr/bin/env bash
# Pulls the latest main, reinstalls deps if locked changed, restarts the service.
# Run as root on the VPS.
set -euo pipefail

# Belt-and-suspenders: tell git both users may safely operate on this repo,
# even if some files end up owned by the "wrong" user (e.g. a stray root pull).
sudo -u aiarena git config --global --add safe.directory /opt/ai-arena-recap || true

# Run the actual update as the aiarena user. Using `sudo -u` (no -i) keeps
# error propagation clean — `set -e` reliably aborts on a failing command.
sudo -u aiarena bash -e <<'INNER'
set -uo pipefail
export PATH="/home/aiarena/.local/bin:$PATH"
cd /opt/ai-arena-recap
git fetch origin main
git reset --hard origin/main
uv sync --frozen
echo "Deployed: $(git rev-parse --short HEAD)"
INNER

systemctl restart ai-arena-recap
systemctl is-active --quiet ai-arena-recap || {
  echo "Service failed to start; check journalctl -u ai-arena-recap" >&2
  exit 1
}

echo "Service restarted."
