#!/usr/bin/env bash
# Build build/function.zip for Lambda deployment.
#
# Pure uv pipeline:
#   1. uv export       — pinned deps from uv.lock (no re-resolution)
#   2. uv pip install  — download Linux wheels to a flat target directory
#                        (uses --python-platform for cross-platform wheels)
#   3. uv pip install  — add the project's own modules via hatchling
#   4. python3 zip     — pack everything into function.zip
set -euo pipefail

cd "$(dirname "$0")/.."

BUILD_DIR="build"
PKG_DIR="${BUILD_DIR}/package"
ZIP_PATH="${BUILD_DIR}/function.zip"

echo "==> Cleaning ${BUILD_DIR}/"
rm -rf "${BUILD_DIR}"
mkdir -p "${PKG_DIR}"

echo "==> Exporting pinned deps from uv.lock"
uv export --no-dev --no-emit-project --format requirements-txt \
  > "${BUILD_DIR}/requirements.txt"

# VIRTUAL_ENV="" keeps uv from trying to resolve against the active venv —
# we want a standalone cross-platform resolve.
echo "==> Installing runtime deps (x86_64-manylinux_2_28, py3.12)"
# Lambda Python 3.12 runs on Amazon Linux 2023 (glibc 2.34), so manylinux_2_28
# wheels are safe. Some newer packages (contourpy, numpy) no longer ship the
# older manylinux2014 tag.
VIRTUAL_ENV="" uv pip install \
  --target "${PKG_DIR}" \
  --python-platform x86_64-manylinux_2_28 \
  --python-version 3.12 \
  --no-build \
  -r "${BUILD_DIR}/requirements.txt"

echo "==> Copying project modules"
cp cost_reporter.py lambda_handler.py "${PKG_DIR}/"

# Strip things Lambda never needs.
echo "==> Trimming package"
find "${PKG_DIR}" -type d \( -name "__pycache__" -o -name "tests" -o -name "test" \) \
  -exec rm -rf {} + 2>/dev/null || true
find "${PKG_DIR}" -type f \( -name "*.pyc" -o -name "*.pyi" \) \
  -delete 2>/dev/null || true

echo "==> Writing ${ZIP_PATH}"
(cd "${PKG_DIR}" && zip -qr - .) > "${ZIP_PATH}"
printf "  zip size: %s (unpacked: %s)\n" \
  "$(du -h "${ZIP_PATH}" | cut -f1)" \
  "$(du -sh "${PKG_DIR}" | cut -f1)"
