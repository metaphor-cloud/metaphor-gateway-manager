#!/usr/bin/env bash
# Builds, packages and publishes this app to the AWS Serverless Application
# Repository. Run by buildspec-publish.yml.
#
# Idempotent on the SemanticVersion in template.yaml: if
# that version is already published, the publish is a no-op, so re-running on an
# unchanged branch is safe. Bump SemanticVersion to cut a release.
#
# Env: SAM_PACKAGE_BUCKET (SAR-readable bucket), APP_NAME, MAKE_PUBLIC
# (true|false), AWS_REGION.
set -euo pipefail

: "${SAM_PACKAGE_BUCKET:?}" "${APP_NAME:?}" "${AWS_REGION:?}"
cd "$(dirname "$0")/.."

cfn-lint template.yaml
sam build
sam package --s3-bucket "$SAM_PACKAGE_BUCKET" --output-template-file packaged.yaml

set +e
output=$(sam publish --template packaged.yaml --region "$AWS_REGION" 2>&1)
status=$?
set -e
echo "$output"
if [ "$status" -ne 0 ]; then
  if echo "$output" | grep -qiE "already exists|already published|same.*version|version.*exists"; then
    echo "SemanticVersion already published; treating as no-op."
  else
    exit "$status"
  fi
fi

if [ "${MAKE_PUBLIC:-false}" = "true" ]; then
  app_id=$(aws serverlessrepo list-applications \
    --query "Applications[?Name=='$APP_NAME'].ApplicationId | [0]" --output text)
  if [ -z "$app_id" ] || [ "$app_id" = "None" ]; then
    echo "Could not resolve the ApplicationId for $APP_NAME; not granting public Deploy." >&2
    exit 1
  fi
  aws serverlessrepo put-application-policy \
    --application-id "$app_id" \
    --statements Principals='*',Actions='Deploy'
  echo "Granted public Deploy on $app_id"
fi
