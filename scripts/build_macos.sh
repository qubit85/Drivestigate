#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$(uname -s)" != Darwin ]; then
  echo "The desktop bundle must be built on macOS."
  exit 1
fi
export PYINSTALLER_CONFIG_DIR="$PWD/.test-data/pyinstaller"
.venv/bin/python -m PyInstaller --noconfirm --clean --windowed --name Drivestigate \
  --osx-bundle-identifier app.drivestigate.desktop \
  --collect-data drivestigate --hidden-import drivestigate.extract \
  --hidden-import pypdf --hidden-import PIL.Image scripts/app_entry.py
echo "Desktop application: dist/Drivestigate.app"
echo "This local build is ad-hoc signed, not notarized for public distribution."
