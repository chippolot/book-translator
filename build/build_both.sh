#!/bin/bash
# Build both the Apple Silicon and Intel DMGs in sequence.
# Run after `./build/setup_build_venvs.sh` (one-time per machine).

set -euo pipefail

cd "$(dirname "$0")/.."

TARGET_ARCH=arm64  ./build/build_mac.sh
TARGET_ARCH=x86_64 ./build/build_mac.sh

echo
echo "Both DMGs ready:"
ls -lh dist/*.dmg
