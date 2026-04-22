#!/usr/bin/env bash
# Convenience wrapper: tofu init (once) + tofu apply.
# The zip is built by the null_resource in tofu/lambda.tf whenever source
# files change — there is no separate build step here.
set -euo pipefail

cd "$(dirname "$0")/../tofu"

echo "==> Running tofu init"
tofu init -upgrade

echo "==> Running tofu apply"
tofu apply -auto-approve
