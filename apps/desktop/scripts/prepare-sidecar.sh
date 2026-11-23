#!/bin/sh
# Make the workspace buildable and testable:
#  - src-tauri/binaries/metis-backend/: the REAL packaged backend, bundled as a
#    Tauri resource. Rebuilt (slow, needs uv + network for PyInstaller) when it
#    is missing or any Python source is newer, so an app never ships stale code.
#  - target/debug/metis-health-sidecar: test-only fake backend (never bundled).
set -eu
cd "$(dirname "$0")/.."
export PATH="$HOME/.cargo/bin:$PATH"
case "$(rustc -vV | sed -n 's/^host: //p')" in *windows*) ext=".exe";; *) ext="";; esac
exe="src-tauri/binaries/metis-backend/metis-backend$ext"
if [ ! -f "$exe" ] || [ -n "$(find ../../services ../../packages/contracts/src python-backend \
  -name '*.py' -not -path '*/.build/*' -newer "$exe" -print -quit)" ]; then
  sh python-backend/build.sh
fi
cargo build -p metis-health-sidecar
