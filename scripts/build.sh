#!/usr/bin/env bash
# Build build/function.zip for Lambda deployment.
#
# Uses uv export to get exact pinned versions from uv.lock (no re-resolution),
# then installs manylinux wheels for Lambda. The project itself is installed
# via the hatchling build system — no manual file copying.
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

echo "==> Installing runtime deps (manylinux2014_x86_64, py3.12, no-compile)"
uv pip install \
  --no-compile \
  --target "${PKG_DIR}" \
  --platform manylinux2014_x86_64 \
  --python-version 3.12 \
  --implementation cp \
  --only-binary=:all: \
  -r "${BUILD_DIR}/requirements.txt"

echo "==> Installing project modules via build system"
uv pip install --no-compile --no-deps --target "${PKG_DIR}" .

echo "==> Writing ${ZIP_PATH}"
python3 - <<PY
import os, zipfile
pkg = "${PKG_DIR}"
out = "${ZIP_PATH}"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, _, files in os.walk(pkg):
        for f in files:
            full = os.path.join(root, f)
            arc = os.path.relpath(full, pkg)
            zf.write(full, arc)
print(f"  zip size: {os.path.getsize(out) / (1024*1024):.1f} MiB")
PY
