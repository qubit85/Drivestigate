#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "$(uname -s)" != Darwin ]; then
  echo "The desktop bundle must be built on macOS."
  exit 1
fi
if pgrep -x Drivestigate >/dev/null; then
  echo "Quit Drivestigate before rebuilding its desktop bundle. Saved progress will be preserved."
  exit 1
fi
export PYINSTALLER_CONFIG_DIR="$PWD/.test-data/pyinstaller"
.venv/bin/python -m PyInstaller --noconfirm --clean --windowed --name Drivestigate \
  --osx-bundle-identifier app.drivestigate.desktop \
  --collect-data drivestigate --hidden-import drivestigate.extract --hidden-import drivestigate.discovery \
  --hidden-import pypdf --hidden-import PIL.Image scripts/app_entry.py
.venv/bin/python - <<'PY'
import plistlib
from pathlib import Path
from drivestigate import __version__

path = Path('dist/Drivestigate.app/Contents/Info.plist')
data = plistlib.loads(path.read_bytes())
data.update(CFBundleShortVersionString=__version__, CFBundleVersion=__version__)
path.write_bytes(plistlib.dumps(data))
PY
codesign --force --deep --sign - dist/Drivestigate.app
echo "Desktop application: dist/Drivestigate.app"
echo "This local build is ad-hoc signed, not notarized for public distribution."
