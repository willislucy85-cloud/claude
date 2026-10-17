# CVV microservice

A small service for checking and creating CVV codes:

- **AWS Payment Cryptography** holds the card verification key (CVK) and does the CVV math. The key never leaves the HSM.
- **Lambda (Python 3.12)** checks the input and calls Payment Cryptography (`src/handler.py`).
- **API Gateway HTTP API** is the entry point. It uses IAM (SigV4) auth, so it is not open to the public.

## Prerequisites

- An AWS account with Payment Cryptography enabled in `us-east-2`
- A CVK key that already exists (you need its `KeyArn`)
- The AWS CLI, set up with rights to create IAM roles, Lambda functions and API Gateway APIs

## Deploy

```bash
CVK_KEY_ARN=arn:aws:payment-cryptography:us-east-2:111122223333:key/r52o3wbqxyf6qlqr ./deploy.sh
```

You can run it again safely. On a re-run it updates the role policy, the function code and the environment variables.

The script:

1. Creates the IAM role `cvv-service-lambda-role`, trusted by Lambda (`iam/lambda-trust-policy.json`). It attaches `AWSLambdaBasicExecutionRole` and adds an inline policy that allows only `GenerateCardValidationData` and `VerifyCardValidationData` on your CVK (`iam/payment-crypto-policy.json`).
2. Creates or updates the `cvv-service` Lambda function.
3. Creates an HTTP API with `POST /cvv/verify` and `POST /cvv/generate`. Both routes use `AWS_IAM` auth and send requests to the Lambda function. The script also grants API Gateway permission to call the function.

> **IAM action prefix:** the actions are `payment-cryptography:…`, not `payment-cryptography-data:…`. The data-plane endpoint is called `dataplane.payment-cryptography`, but IAM uses the `payment-cryptography` prefix for both the control plane and the data plane. With the `-data` prefix, every call is denied with `AccessDeniedException`.

## API

Requests are JSON. `type` is `cvv2` by default; that's the printed security code. Use `cvv1` for the magstripe CVV, which also needs `serviceCode`.

| Route | Body | Response |
|---|---|---|
| `POST /cvv/verify` | `{"pan", "expiry", "cvv", ["type", "serviceCode"]}` | `{"valid": true \| false}` |
| `POST /cvv/generate` | `{"pan", "expiry", ["type", "serviceCode"]}` | `{"cvv": "123"}` |

- `expiry` must be 4 digits. Pass the same format that was used when the card was issued (often `MMYY`). A different format gives a different CVV.
- `/cvv/generate` returns **403 until you deploy with `ENABLE_GENERATE=true`**. Only an issuing or personalization flow should get CVVs back. A verify-only service is a much smaller target.
- Status codes: `400` for bad input, `502` when Payment Cryptography returns an error (for example a missing permission). The log records only the AWS error code. PANs and CVVs are never logged.

Example call (SigV4-signed with your AWS credentials):

```bash
curl --aws-sigv4 "aws:amz:us-east-2:execute-api" \
  --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" \
  -H "x-amz-security-token: $AWS_SESSION_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"pan":"4111111111111111","expiry":"1229","cvv":"123"}' \
  https://<api-id>.execute-api.us-east-2.amazonaws.com/cvv/verify
```

Callers need `execute-api:Invoke` on `arn:aws:execute-api:us-east-2:<account>:<api-id>/*/POST/cvv/verify`.

## Tests

```bash
pip install boto3
python3 -m unittest discover -s tests -v
```

The tests replace Payment Cryptography with botocore's `Stubber`, so they need no AWS account.

## PCI notes

PANs pass through Lambda and API Gateway, so both are in PCI DSS scope. Keep API Gateway access logging free of request bodies. Don't add PAN or CVV values to log lines. Limit `execute-api:Invoke` to the services that need it.
