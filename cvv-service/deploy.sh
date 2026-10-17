#!/usr/bin/env bash
# Deploy the CVV microservice: IAM role -> Lambda -> API Gateway HTTP API.
#
# Usage:
#   CVK_KEY_ARN=arn:aws:payment-cryptography:us-east-2:111122223333:key/xxxx ./deploy.sh
#
# Optional env: AWS_REGION (default us-east-2), NAME (default cvv-service),
#               ENABLE_GENERATE (default false).
# Safe to re-run: existing role/function/API are reused and updated.
set -euo pipefail

: "${CVK_KEY_ARN:?set CVK_KEY_ARN to your CVK key ARN}"
export AWS_REGION="${AWS_REGION:-us-east-2}"
NAME="${NAME:-cvv-service}"
ENABLE_GENERATE="${ENABLE_GENERATE:-false}"
ROLE_NAME="${NAME}-lambda-role"
HERE="$(cd "$(dirname "$0")" && pwd)"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

echo "==> IAM role ${ROLE_NAME}"
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document "file://${HERE}/iam/lambda-trust-policy.json" >/dev/null
  NEW_ROLE=1
fi
aws iam attach-role-policy --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
# Inline policy scoped to this CVK only (the key ARN in the JSON file is replaced).
POLICY="$(sed "s#\"Resource\": \".*\"#\"Resource\": \"${CVK_KEY_ARN}\"#" "${HERE}/iam/payment-crypto-policy.json")"
aws iam put-role-policy --role-name "$ROLE_NAME" \
  --policy-name payment-cryptography-cvv --policy-document "$POLICY"
ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)"
# New roles take a few seconds before Lambda can assume them.
[ -n "${NEW_ROLE:-}" ] && sleep 10

echo "==> Lambda ${NAME}"
ZIP="$(mktemp -d)/function.zip"
(cd "${HERE}/src" && zip -q "$ZIP" handler.py)
ENV="Variables={CVK_KEY_ARN=${CVK_KEY_ARN},ENABLE_GENERATE=${ENABLE_GENERATE}}"
if aws lambda get-function --function-name "$NAME" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$NAME" --zip-file "fileb://${ZIP}" >/dev/null
  aws lambda wait function-updated --function-name "$NAME"
  aws lambda update-function-configuration --function-name "$NAME" --environment "$ENV" >/dev/null
else
  aws lambda create-function --function-name "$NAME" \
    --runtime python3.12 --handler handler.lambda_handler \
    --role "$ROLE_ARN" --zip-file "fileb://${ZIP}" \
    --timeout 10 --memory-size 256 --environment "$ENV" >/dev/null
fi
aws lambda wait function-updated --function-name "$NAME"
FUNCTION_ARN="$(aws lambda get-function --function-name "$NAME" --query Configuration.FunctionArn --output text)"

echo "==> HTTP API ${NAME}"
API_ID="$(aws apigatewayv2 get-apis --query "Items[?Name=='${NAME}'].ApiId | [0]" --output text)"
if [ "$API_ID" = "None" ]; then
  API_ID="$(aws apigatewayv2 create-api --name "$NAME" --protocol-type HTTP --query ApiId --output text)"
  INTEGRATION_ID="$(aws apigatewayv2 create-integration --api-id "$API_ID" \
    --integration-type AWS_PROXY --integration-uri "$FUNCTION_ARN" \
    --payload-format-version 2.0 --query IntegrationId --output text)"
  # AWS_IAM auth: callers must SigV4-sign requests and hold execute-api:Invoke.
  for ROUTE in "POST /cvv/verify" "POST /cvv/generate"; do
    aws apigatewayv2 create-route --api-id "$API_ID" --route-key "$ROUTE" \
      --target "integrations/${INTEGRATION_ID}" --authorization-type AWS_IAM >/dev/null
  done
  aws apigatewayv2 create-stage --api-id "$API_ID" --stage-name '$default' --auto-deploy >/dev/null
  aws lambda add-permission --function-name "$NAME" \
    --statement-id apigateway-invoke --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*/*/cvv/*" >/dev/null
fi

echo
echo "Endpoint: https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com"
