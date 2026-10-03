import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from social_video_ops.drive import Drive
from social_video_ops.util import UserError


class DriveAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.token = Path(self.temp.name) / "token.json"
        self.token.write_text('{"synthetic":true}')
        self.auth_calls = []
        self.credential = None
        modules = {}
        for name in ("google", "google.oauth2", "google.auth", "google.auth.transport", "googleapiclient"):
            module = types.ModuleType(name)
            module.__path__ = []
            modules[name] = module

        class Credentials:
            @staticmethod
            def from_authorized_user_file(path):
                return self.credential

        credentials = types.ModuleType("google.oauth2.credentials")
        credentials.Credentials = Credentials
        requests = types.ModuleType("google.auth.transport.requests")
        requests.Request = object
        discovery = types.ModuleType("googleapiclient.discovery")
        discovery.build = lambda *args, **kwargs: {"service": "fake"}
        httplib2 = types.ModuleType("httplib2")
        httplib2.Http = lambda timeout: {"timeout": timeout}
        google_auth_httplib2 = types.ModuleType("google_auth_httplib2")
        google_auth_httplib2.AuthorizedHttp = lambda creds, **kwargs: self.auth_calls.append(kwargs) or {"authorized": True}
        modules.update({
            "google.oauth2.credentials": credentials,
            "google.auth.transport.requests": requests,
            "googleapiclient.discovery": discovery,
            "httplib2": httplib2,
            "google_auth_httplib2": google_auth_httplib2,
        })
        self.modules = modules
        self.module_patch = patch.dict(sys.modules, modules)
        self.module_patch.start()

    def tearDown(self):
        self.module_patch.stop()
        self.temp.cleanup()

    def config(self):
        return {"credentials_file": str(self.token)}

    def test_read_only_audit_neither_refreshes_expired_tokens_nor_refreshes_after_401(self):
        class Expired:
            expired = True
            refresh_token = "synthetic"
            valid = False
            def refresh(self, request):
                raise AssertionError("audit must not refresh the token")
        self.credential = Expired()
        original = self.token.read_bytes()
        with self.assertRaisesRegex(UserError, "will not refresh credentials"):
            Drive(self.config(), refresh_credentials=False)
        self.assertEqual(self.token.read_bytes(), original)

        class Valid:
            expired = False
            refresh_token = "synthetic"
            valid = True
        self.credential = Valid()
        Drive(self.config(), timeout=3, refresh_credentials=False, retry_attempts=1)
        self.assertEqual(self.auth_calls[-1]["refresh_status_codes"], ())
        self.assertEqual(self.auth_calls[-1]["http"]["timeout"], 3)


if __name__ == "__main__":
    unittest.main()
