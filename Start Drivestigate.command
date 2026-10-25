#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
if [ ! -x .venv/bin/python ]; then
  bash scripts/setup.sh
fi
exec .venv/bin/python -m drivestigate.server "$@"
