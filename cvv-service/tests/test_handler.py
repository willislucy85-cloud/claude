import importlib
import json
import os
import sys
import unittest

from botocore.stub import Stubber

KEY_ARN = "arn:aws:payment-cryptography:us-east-2:111122223333:key/r52o3wbqxyf6qlqr"
PAN = "4111111111111111"

os.environ.update(CVK_KEY_ARN=KEY_ARN, AWS_DEFAULT_REGION="us-east-2",
                  AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def event(route, body):
    return {"routeKey": route, "body": json.dumps(body), "isBase64Encoded": False}


class HandlerTest(unittest.TestCase):
    def setUp(self):
        os.environ["ENABLE_GENERATE"] = "false"
        self.h = importlib.reload(importlib.import_module("handler"))
        self.stub = Stubber(self.h.client)
        self.stub.activate()

    def tearDown(self):
        self.stub.deactivate()

    def call(self, route, body):
        r = self.h.lambda_handler(event(route, body), None)
        return r["statusCode"], json.loads(r["body"])

    def test_verify_valid(self):
        self.stub.add_response(
            "verify_card_validation_data",
            {"KeyArn": KEY_ARN, "KeyCheckValue": "ABC123"},
            {"KeyIdentifier": KEY_ARN, "PrimaryAccountNumber": PAN,
             "VerificationAttributes": {"CardVerificationValue2": {"CardExpiryDate": "1229"}},
             "ValidationData": "123"},
        )
        self.assertEqual(self.call("POST /cvv/verify", {"pan": PAN, "expiry": "1229", "cvv": "123"}),
                         (200, {"valid": True}))

    def test_verify_mismatch(self):
        self.stub.add_client_error("verify_card_validation_data", "VerificationFailedException")
        self.assertEqual(self.call("POST /cvv/verify", {"pan": PAN, "expiry": "1229", "cvv": "999"}),
                         (200, {"valid": False}))

    def test_cvv1_uses_service_code(self):
        self.stub.add_response(
            "verify_card_validation_data",
            {"KeyArn": KEY_ARN, "KeyCheckValue": "ABC123"},
            {"KeyIdentifier": KEY_ARN, "PrimaryAccountNumber": PAN,
             "VerificationAttributes": {"CardVerificationValue1": {"CardExpiryDate": "1229", "ServiceCode": "101"}},
             "ValidationData": "123"},
        )
        status, _ = self.call("POST /cvv/verify",
                              {"pan": PAN, "expiry": "1229", "cvv": "123", "type": "cvv1", "serviceCode": "101"})
        self.assertEqual(status, 200)

    def test_bad_input(self):
        for body in ({"pan": "4111111111111112", "expiry": "1229", "cvv": "123"},
                     {"pan": PAN, "expiry": "12/29", "cvv": "123"},
                     {"pan": PAN, "expiry": "1229", "cvv": "12"},
                     {"pan": PAN, "expiry": "1229", "cvv": "123", "type": "cvv1"}):
            self.assertEqual(self.call("POST /cvv/verify", body)[0], 400)

    def test_upstream_error_is_502(self):
        self.stub.add_client_error("verify_card_validation_data", "AccessDeniedException")
        self.assertEqual(self.call("POST /cvv/verify", {"pan": PAN, "expiry": "1229", "cvv": "123"})[0], 502)

    def test_generate_disabled_by_default(self):
        self.assertEqual(self.call("POST /cvv/generate", {"pan": PAN, "expiry": "1229"})[0], 403)

    def test_generate_enabled(self):
        os.environ["ENABLE_GENERATE"] = "true"
        self.stub.deactivate()
        self.h = importlib.reload(self.h)
        self.stub = Stubber(self.h.client)
        self.stub.activate()
        self.stub.add_response(
            "generate_card_validation_data",
            {"KeyArn": KEY_ARN, "KeyCheckValue": "ABC123", "ValidationData": "456"},
            {"KeyIdentifier": KEY_ARN, "PrimaryAccountNumber": PAN,
             "GenerationAttributes": {"CardVerificationValue2": {"CardExpiryDate": "1229"}},
             "ValidationDataLength": 3},
        )
        self.assertEqual(self.call("POST /cvv/generate", {"pan": PAN, "expiry": "1229"}),
                         (200, {"cvv": "456"}))

    def test_unknown_route(self):
        self.assertEqual(self.call("GET /", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
