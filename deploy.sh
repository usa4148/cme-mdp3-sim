#!/usr/bin/env bash
# Deploy the toolkit to the capture VM.
#
#   ./deploy.sh                 # copy code + create venv (flask, boto3)
#   ./deploy.sh --service       # also install & (re)start the systemd --user capture service
#
# HOST=user@host is required (put it in your shell profile, or a local, untracked
# deploy.env: HOST=user@host). DIR=dirname overrides the target directory. Requires key-based SSH
# (run `ssh-copy-id "$HOST"` once) and Python 3.14+ on the VM.
#
# config.env (S3 credentials) is NOT copied — create it on the VM from
# config.env.example and chmod 600. capture.env (PRODUCTS=…, IFACE=…) is not
# copied either; it is per-host.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SRC_DIR/deploy.env" ] && . "$SRC_DIR/deploy.env"
: "${HOST:?set HOST=user@host (or put it in deploy.env next to this script)}"
DIR="${DIR:-cme-mdp3-sim}"
SRC="$(cd "$(dirname "$0")" && pwd)/"

echo ">> copy code -> $HOST:~/$DIR (tar over ssh; the VM has no rsync)"
ssh "$HOST" "mkdir -p ~/$DIR"
# COPYFILE_DISABLE stops macOS tar from adding ._* AppleDouble files
COPYFILE_DISABLE=1 tar czf - --no-xattrs -C "$SRC" --exclude '._*' --exclude 'deploy.env' \
  --exclude '.venv' --exclude '__pycache__' --exclude 'config.env' --exclude 'capture.env' \
  --exclude 'pcaps' --exclude 'logs' --exclude '.git' --exclude '.prior_close_cache.json' \
  --exclude 'session.json' --exclude 'dashboard.html' --exclude '.DS_Store' . \
  | ssh "$HOST" "tar xzf - -C ~/$DIR"

echo ">> create venv + install deps"
ssh "$HOST" bash -lc "'
  set -e
  cd ~/$DIR
  # python3.14 on PATH, else one uv installed (uv python install 3.14), else python3
  PY=\$(command -v python3.14 || ~/.local/bin/uv python find 3.14 2>/dev/null || command -v python3)
  if ! \$PY -c \"import sys; sys.exit(sys.version_info < (3, 14))\"; then
    echo \"!! \$PY is \$(\$PY --version 2>&1); this toolkit needs Python 3.14+ on the VM\" >&2
    exit 1
  fi
  [ -x .venv/bin/python ] && .venv/bin/python -c \"import sys; sys.exit(sys.version_info < (3, 14))\" || \$PY -m venv --clear .venv
  .venv/bin/pip -q install --upgrade pip
  .venv/bin/pip -q install -r requirements.txt
  echo venv ready: \$(.venv/bin/python --version)
'"

if [ "${1:-}" = "--service" ]; then
  echo ">> install systemd --user capture service (survives logout via linger)"
  ssh "$HOST" bash -lc "'
    set -e
    cd ~/$DIR
    mkdir -p ~/.config/systemd/user
    cp deploy/mdp3-capture.service ~/.config/systemd/user/mdp3-capture.service
    loginctl enable-linger \$USER || true
    systemctl --user daemon-reload
    systemctl --user enable mdp3-capture.service
    systemctl --user restart mdp3-capture.service
    sleep 2
    systemctl --user --no-pager status mdp3-capture.service | head -14
  '"
fi

echo ">> done. Live ladder:  ssh -t $HOST 'cd ~/$DIR && ./mdp3 ladder --product ES --iface 0.0.0.0'"
