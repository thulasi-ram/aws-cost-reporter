#!/usr/bin/env bash
# Build the Lambda container image, push it to ECR, and update the function.
#
# Prerequisites:
#   - Docker running locally
#   - AWS credentials with ECR push + Lambda update permissions
#   - ECR repo already created via `tofu apply -target=aws_ecr_repository.this`
#
# Flow:
#   1. Read ECR URL from tofu output
#   2. docker buildx build --platform linux/amd64 (Lambda runs on amd64)
#   3. ECR login, push
#   4. Bounce the Lambda to pick up the new image (no-op on first deploy)
set -euo pipefail

cd "$(dirname "$0")/.."

TAG="${1:-latest}"
TOFU_DIR="tofu"

pushd "$TOFU_DIR" >/dev/null
ECR_URL=$(tofu output -raw ecr_repository_url)
REGION=$(tofu output -raw -json 2>/dev/null | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('region',{}).get('value','us-east-1'))" 2>/dev/null || echo "us-east-1")
FUNCTION_NAME=$(tofu output -raw lambda_function_name 2>/dev/null || echo "")
popd >/dev/null

REGISTRY="${ECR_URL%%/*}"

echo "==> Building image for linux/amd64 ..."
docker buildx build --platform linux/amd64 --provenance=false -t "${ECR_URL}:${TAG}" --load .

echo "==> Logging in to ECR: ${REGISTRY}"
aws ecr get-login-password --region "${REGION}" | \
  docker login --username AWS --password-stdin "${REGISTRY}"

echo "==> Pushing ${ECR_URL}:${TAG}"
docker push "${ECR_URL}:${TAG}"

if [[ -n "${FUNCTION_NAME}" ]]; then
  echo "==> Updating Lambda ${FUNCTION_NAME} to new image"
  aws lambda update-function-code \
    --function-name "${FUNCTION_NAME}" \
    --image-uri "${ECR_URL}:${TAG}" \
    --region "${REGION}" \
    >/dev/null
  echo "==> Waiting for Lambda to become active"
  aws lambda wait function-updated --function-name "${FUNCTION_NAME}" --region "${REGION}"
  echo "==> Done"
else
  echo "==> Lambda function not yet created; run \`tofu apply\` next."
fi
