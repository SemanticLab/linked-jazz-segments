#!/usr/bin/env bash
# Deploy / update the search Lambda (us-east-1). First run creates the function
# (reusing archives-search's execution role) and a public Function URL; later
# runs just push new code. Env: GOOGLE_AI must be set in the shell.
#   search/deploy.sh            create or update code
#   search/deploy.sh env        also (re)write the environment variables
set -euo pipefail
cd "$(dirname "$0")"
FN=linked-jazz-segments-search
REGION=us-east-1
ROLE=$(aws lambda get-function-configuration --function-name archives-search --region $REGION --query Role --output text)
ENV="Variables={GOOGLE_AI=$GOOGLE_AI,VBUCKET=linked-jazz-segments,RINDEX=retrieval-v1,CINDEX=clustering-v2,TABLE=${TABLE:-archives-ratelimit},RATE_PER_MIN=${RATE_PER_MIN:-10},DAY_CAP=${DAY_CAP:-2000}}"

rm -f function.zip && zip -q function.zip lambda_function.py
if aws lambda get-function --function-name $FN --region $REGION >/dev/null 2>&1; then
  aws lambda update-function-code --function-name $FN --region $REGION --zip-file fileb://function.zip --query LastModified --output text
  aws lambda wait function-updated --function-name $FN --region $REGION
  if [ "${1:-}" = "env" ]; then
    aws lambda update-function-configuration --function-name $FN --region $REGION --environment "$ENV" --query LastModified --output text
  fi
else
  aws lambda create-function --function-name $FN --region $REGION --runtime python3.13 \
    --role "$ROLE" --handler lambda_function.handler --zip-file fileb://function.zip \
    --timeout 60 --memory-size 512 --environment "$ENV" --query FunctionArn --output text
  aws lambda wait function-active --function-name $FN --region $REGION
  aws lambda put-function-concurrency --function-name $FN --region $REGION --reserved-concurrent-executions 5 >/dev/null
  aws lambda create-function-url-config --function-name $FN --region $REGION --auth-type NONE --query FunctionUrl --output text
  aws lambda add-permission --function-name $FN --region $REGION --statement-id public-url \
    --action lambda:InvokeFunctionUrl --principal '*' --function-url-auth-type NONE >/dev/null
fi
aws lambda get-function-url-config --function-name $FN --region $REGION --query FunctionUrl --output text
