"""CVV microservice: thin wrapper over AWS Payment Cryptography (data plane).

Routes (API Gateway HTTP API, payload format 2.0):
  POST /cvv/verify    {"pan", "expiry", "cvv", ["type", "serviceCode"]} -> {"valid": bool}
  POST /cvv/generate  {"pan", "expiry", ["type", "serviceCode"]}        -> {"cvv": str}
                      (disabled unless ENABLE_GENERATE=true)

"type" is "cvv2" (default, the printed security code) or "cvv1" (magstripe,
needs a 3-digit "serviceCode"). "expiry" is the 4-digit expiry date exactly as
the CVK was used when the card was issued (commonly MMYY).

PAN and CVV values are never logged.
"""

import base64
import json
import logging
import os
import re

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

KEY_ARN = os.environ["CVK_KEY_ARN"]
ENABLE_GENERATE = os.environ.get("ENABLE_GENERATE", "false").lower() == "true"

client = boto3.client("payment-cryptography-data")

PAN_RE = re.compile(r"^\d{12,19}$")
FOUR_DIGITS_RE = re.compile(r"^\d{4}$")
THREE_DIGITS_RE = re.compile(r"^\d{3}$")


class BadRequest(Exception):
    pass


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body),
    }


def _parse_body(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        body = json.loads(raw)
    except ValueError:
        raise BadRequest("body must be JSON")
    if not isinstance(body, dict):
        raise BadRequest("body must be a JSON object")
    return body


def _luhn_ok(pan):
    total = 0
    for i, ch in enumerate(reversed(pan)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _card_attributes(body):
    """Validate PAN/expiry/type and build the Payment Cryptography attributes."""
    pan = str(body.get("pan", "")).replace(" ", "")
    expiry = str(body.get("expiry", ""))
    cvv_type = str(body.get("type", "cvv2")).lower()

    if not PAN_RE.match(pan) or not _luhn_ok(pan):
        raise BadRequest("pan must be 12-19 digits and pass the Luhn check")
    if not FOUR_DIGITS_RE.match(expiry):
        raise BadRequest("expiry must be 4 digits")

    if cvv_type == "cvv2":
        attrs = {"CardVerificationValue2": {"CardExpiryDate": expiry}}
    elif cvv_type == "cvv1":
        service_code = str(body.get("serviceCode", ""))
        if not THREE_DIGITS_RE.match(service_code):
            raise BadRequest("serviceCode must be 3 digits for cvv1")
        attrs = {
            "CardVerificationValue1": {
                "CardExpiryDate": expiry,
                "ServiceCode": service_code,
            }
        }
    else:
        raise BadRequest("type must be cvv1 or cvv2")
    return pan, attrs


def verify(body):
    pan, attrs = _card_attributes(body)
    cvv = str(body.get("cvv", ""))
    if not THREE_DIGITS_RE.match(cvv):
        raise BadRequest("cvv must be 3 digits")
    try:
        client.verify_card_validation_data(
            KeyIdentifier=KEY_ARN,
            PrimaryAccountNumber=pan,
            VerificationAttributes=attrs,
            ValidationData=cvv,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "VerificationFailedException":
            return _response(200, {"valid": False})
        raise
    return _response(200, {"valid": True})


def generate(body):
    pan, attrs = _card_attributes(body)
    result = client.generate_card_validation_data(
        KeyIdentifier=KEY_ARN,
        PrimaryAccountNumber=pan,
        GenerationAttributes=attrs,
        ValidationDataLength=3,
    )
    return _response(200, {"cvv": result["ValidationData"]})


def lambda_handler(event, context):
    route = event.get("routeKey", "")
    try:
        if route == "POST /cvv/verify":
            return verify(_parse_body(event))
        if route == "POST /cvv/generate":
            if not ENABLE_GENERATE:
                return _response(403, {"error": "generate is disabled"})
            return generate(_parse_body(event))
        return _response(404, {"error": "not found"})
    except BadRequest as e:
        return _response(400, {"error": str(e)})
    except ClientError as e:
        # Log only the AWS error code, never the request (it contains the PAN).
        logger.error("payment-cryptography error on %s: %s", route, e.response["Error"]["Code"])
        return _response(502, {"error": "upstream cryptography error"})
