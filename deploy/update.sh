#!/usr/bin/env bash
# Pulls the latest main, reinstalls deps if locked changed, restarts the service.
# Run as root on the VPS.
set -euo pipefail

# Everything lives in main() because this script replaces itself while it runs:
# the `git reset --hard` below rewrites deploy/update.sh, the very file bash is
# reading. Bash reads a script incrementally, remembering a byte offset, so with
# a flat script it resumes the *new* file at the *old* offset once the pull has
# shifted things — and silently skips whatever it lands past. That is a deploy
# that reports success without ever restarting the service. Wrapped in a
# function, bash parses the whole body into memory before running any of it, and
# `main "$@"; exit $?` is parsed as one list, so nothing is read from the file
# after main returns.
main() {
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
# Snapshot the DB before the new code gets to run against it (the service is
# still on the old revision until the restart below). It holds every finished
# season the site serves at /s/<slug>/, and re-importing one from the API takes
# hours. Online backup, so the running service can keep writing. Keeps 3.
uv run --frozen python scripts/backup_db.py || echo "WARNING: DB backup failed; continuing" >&2
echo "Deployed: $(git rev-parse --short HEAD)"
INNER

  systemctl restart ai-arena-recap
  systemctl is-active --quiet ai-arena-recap || {
    echo "Service failed to start; check journalctl -u ai-arena-recap" >&2
    exit 1
  }

  echo "Service restarted."
}

main "$@"; exit $?
