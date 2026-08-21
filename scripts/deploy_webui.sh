#!/usr/bin/env bash
# Deploy the public Sources Explorer (Lambda Function URL).
# Prereqs: aws cli + terraform + a built corpus at deploy/clhear.db
# (scripts/build_corpus.py). Run from the repo root.
set -euo pipefail

PYTHON="${PYTHON:-python3}"
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="clhear-deploy-${ACCOUNT}"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BUILD_DIR="deploy/build"
ZIP="deploy/webui-${STAMP}.zip"

echo "== packaging lambda zip =="
rm -rf "$BUILD_DIR" && mkdir -p "$BUILD_DIR"
# boto3 ships with the Lambda runtime; uvicorn/pytest are dev-only.
$PYTHON -m pip install -q --target "$BUILD_DIR" \
    fastapi sqlalchemy "pydantic>=2.7" pydantic-settings httpx PyYAML beautifulsoup4 mangum
cp -r app migrations "$BUILD_DIR/"
find "$BUILD_DIR" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
(cd "$BUILD_DIR" && zip -qr "../../$ZIP" .)
echo "zip: $ZIP ($(du -h "$ZIP" | cut -f1))"

echo "== ensuring deploy bucket exists (terraform) =="
terraform -chdir=infra apply -input=false -auto-approve \
    -target=aws_s3_bucket.deploy \
    -target=aws_s3_bucket_versioning.deploy \
    -target=aws_s3_bucket_public_access_block.deploy >/dev/null

echo "== uploading artifacts =="
ZIP_KEY="webui/$(basename "$ZIP")"
DB_KEY="webui/clhear-${STAMP}.db"
aws s3 cp "$ZIP" "s3://${BUCKET}/${ZIP_KEY}" --region "$REGION"
aws s3 cp deploy/clhear.db "s3://${BUCKET}/${DB_KEY}" --region "$REGION"
ZIP_SHA=$(openssl dgst -sha256 -binary "$ZIP" | openssl base64)

echo "== terraform apply (lambda + function url) =="
terraform -chdir=infra apply -input=false -auto-approve \
    -var "webui_zip_key=${ZIP_KEY}" \
    -var "webui_zip_sha256=${ZIP_SHA}" \
    -var "webui_db_key=${DB_KEY}"

terraform -chdir=infra output -raw webui_url
