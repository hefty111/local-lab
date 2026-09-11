"""Run inside the image to verify the premium override without a license."""

import os
import unittest
from unittest.mock import patch

from litellm.proxy.auth.litellm_license import LicenseCheck


class PremiumOverrideTest(unittest.TestCase):
    def test_is_premium_without_license_or_verification(self):
        with patch.dict(os.environ, {}, clear=True):
            license_check = LicenseCheck()
            self.assertIsNone(license_check.license_str)
            with (
                patch.object(license_check, "_verify", side_effect=AssertionError("Remote verification")),
                patch.object(
                    license_check,
                    "verify_license_without_api_request",
                    side_effect=AssertionError("Local verification"),
                ),
            ):
                self.assertIs(license_check.is_premium(), True)


if __name__ == "__main__":
    unittest.main()
