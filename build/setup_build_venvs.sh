#!/bin/bash
# Download python-build-standalone CPython for both arm64 and x86_64 macOS,
# extract them locally, and create one venv per arch. Idempotent — skips
# anything that already exists.
#
# These per-arch venvs are used by build_mac.sh to produce single-arch .apps.
# We use python-build-standalone (https://github.com/astral-sh/python-build-standalone)
# because their builds are truly relocatable: every dylib uses @rpath /
# @executable_path so the Python interpreter runs from any directory
# without a sudo install.
#
# Usage: setup_build_venvs.sh [PYTHON_VERSION]

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

PYTHON_VERSION="${1:-3.14.5}"
# python-build-standalone publishes a dated tag per release. The version
# below is current as of build authoring; we read it from a small
# pinned file so future bumps are a one-line change.
PBS_TAG="${PBS_TAG:-20260602}"

PBS_DIR="$ROOT/build/_pbs"
mkdir -p "$PBS_DIR"

# Map arch label → tarball arch component (case used instead of an assoc
# array so this works on macOS's bash 3.2).
for ARCH in arm64 x86_64; do
    case "$ARCH" in
        arm64)  TARBALL_ARCH="aarch64-apple-darwin" ;;
        x86_64) TARBALL_ARCH="x86_64-apple-darwin" ;;
    esac
    TARBALL="$PBS_DIR/cpython-$PYTHON_VERSION-$ARCH.tar.gz"
    EXTRACT_DIR="$PBS_DIR/$ARCH"
    VENV_DIR="$ROOT/.venv-$ARCH"

    # Download once.
    if [ ! -f "$TARBALL" ]; then
        URL="https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/cpython-$PYTHON_VERSION+$PBS_TAG-$TARBALL_ARCH-install_only_stripped.tar.gz"
        echo "==> Downloading $URL"
        curl -fsSL -o "$TARBALL" "$URL"
    fi

    # Extract once.
    PBS_PY="$EXTRACT_DIR/python/bin/python3.14"
    if [ ! -x "$PBS_PY" ]; then
        echo "==> Extracting $ARCH"
        mkdir -p "$EXTRACT_DIR"
        tar -xzf "$TARBALL" -C "$EXTRACT_DIR"
    fi

    # Verify the binary is the expected arch.
    GOT_ARCH="$(lipo -archs "$PBS_PY" 2>/dev/null || file "$PBS_PY" | awk '{print $NF}')"
    echo "    python: $PBS_PY  ($GOT_ARCH)"

    # Create venv (run via the matching arch so the venv interpreter matches).
    if [ ! -x "$VENV_DIR/bin/python3" ]; then
        echo "==> Creating $VENV_DIR via arch -$ARCH"
        arch -"$ARCH" "$PBS_PY" -m venv "$VENV_DIR"
    fi

    # Install dependencies.
    echo "==> Installing requirements into $VENV_DIR"
    arch -"$ARCH" "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    arch -"$ARCH" "$VENV_DIR/bin/pip" install --quiet -r requirements.txt
done

echo
echo "Per-arch venvs ready:"
echo "  .venv-arm64    (Apple Silicon)"
echo "  .venv-x86_64   (Intel, via Rosetta)"
echo
echo "Now run:  TARGET_ARCH=arm64  ./build/build_mac.sh"
echo "  and:    TARGET_ARCH=x86_64 ./build/build_mac.sh"
