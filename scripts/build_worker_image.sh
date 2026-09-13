#!/usr/bin/env bash
# Build and push the clhear-workers fleet image without a local Docker daemon.
# Zips the build context to the deploy bucket, runs the CodeBuild project from
# infra/codebuild.tf, and waits for it. Prereqs: aws cli, zip, terraform applied.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="clhear-deploy-${ACCOUNT}"
PROJECT="${CODEBUILD_PROJECT:-clhear-workers-image}"
KEY="build/workers-source.zip"
TAG="${IMAGE_TAG:-$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short HEAD 2>/dev/null || echo nogit)}"
ZIP="$(mktemp -d)/workers-source.zip"

echo "== packaging build context =="
zip -qr "$ZIP" Dockerfile requirements.txt app migrations -x '*/__pycache__/*' '*.pyc'
echo "context: $(du -h "$ZIP" | cut -f1)"

echo "== uploading to s3://${BUCKET}/${KEY} =="
aws s3 cp "$ZIP" "s3://${BUCKET}/${KEY}" --region "$REGION" --only-show-errors

echo "== starting CodeBuild ${PROJECT} (tag ${TAG}) =="
BUILD_ID=$(aws codebuild start-build --region "$REGION" --project-name "$PROJECT" \
  --environment-variables-override "name=IMAGE_TAG,value=${TAG},type=PLAINTEXT" \
  --query 'build.id' --output text)
echo "build: $BUILD_ID"

while :; do
  STATUS=$(aws codebuild batch-get-builds --region "$REGION" --ids "$BUILD_ID" --query 'builds[0].buildStatus' --output text)
  case "$STATUS" in
    IN_PROGRESS) sleep 15 ;;
    SUCCEEDED) break ;;
    *) echo "build $STATUS"; aws codebuild batch-get-builds --region "$REGION" --ids "$BUILD_ID" \
         --query 'builds[0].phases[?phaseStatus==`FAILED`].[phaseType,contexts[0].message]' --output text; exit 1 ;;
  esac
done

echo "== pushed =="
aws ecr describe-images --region "$REGION" --repository-name clhear-workers --image-ids imageTag=latest \
  --query 'imageDetails[0].[imagePushedAt,imageDigest,imageTags]' --output text
