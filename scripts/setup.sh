#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
selected_python=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3,11))' 2>/dev/null; then
    selected_python="$candidate"
    break
  fi
done
if [ -z "$selected_python" ]; then
  echo "Python 3.11+ is required. Install it from python.org or Homebrew."
  exit 1
fi
"$selected_python" -m venv .venv
.venv/bin/python -m pip install -c constraints.txt '.[documents]'
echo "Drivestigate is ready. Start: ./Start\ Drivestigate.command"
