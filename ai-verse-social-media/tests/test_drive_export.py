import hashlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from social_video_ops.drive import Drive
from social_video_ops.util import UserError, file_hash


class FakeHttpError(Exception):
    def __init__(self, status):
        self.resp = types.SimpleNamespace(status=status)


class FakeRequest:
    def __init__(self, callback):
        self.callback = callback
    def execute(self):
        return self.callback()


class FakeUploadRequest:
    def __init__(self, service, body, path):
        self.service, self.body, self.path = service, body, path
    def next_chunk(self, num_retries):
        self.service.create_calls += 1
        md5 = hashlib.md5(self.path.read_bytes()).hexdigest()
        result = {**self.body, "size": str(self.path.stat().st_size), "md5Checksum": md5}
        self.service.files_by_id[self.body["id"]] = result
        if self.service.lose_response:
            self.service.lose_response = False
            raise TimeoutError("synthetic response loss")
        return None, result


class FakeFiles:
    def __init__(self, service):
        self.service = service
    def generateIds(self, **kwargs):
        self.service.generated_args = kwargs
        return FakeRequest(lambda: {"ids": ["reserved-drive-id"]})
    def get(self, fileId, **kwargs):
        def read():
            if fileId not in self.service.files_by_id:
                raise FakeHttpError(404)
            return self.service.files_by_id[fileId]
        return FakeRequest(read)
    def create(self, body, media_body, **kwargs):
        return FakeUploadRequest(self.service, body, Path(media_body.path))


class FakeService:
    def __init__(self):
        self.files_by_id = {}
        self.create_calls = 0
        self.lose_response = True
        self.generated_args = None
    def files(self):
        return FakeFiles(self)


class DriveExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "clip.mp4"
        self.path.write_bytes(b"synthetic video bytes")
        self.drive = Drive.__new__(Drive)
        self.drive.service = FakeService()
        self.drive.retry_attempts = 1
        self.modules = {
            "googleapiclient": types.ModuleType("googleapiclient"),
            "googleapiclient.http": types.ModuleType("googleapiclient.http"),
        }
        self.modules["googleapiclient"].__path__ = []
        class MediaFileUpload:
            def __init__(self, path, **kwargs):
                self.path = path
                self.kwargs = kwargs
        self.modules["googleapiclient.http"].MediaFileUpload = MediaFileUpload
        self.module_patch = patch.dict(sys.modules, self.modules)
        self.module_patch.start()

    def tearDown(self):
        self.module_patch.stop()
        self.temp.cleanup()

    def test_lost_upload_response_recovers_by_reserved_id_without_duplicate_file(self):
        file_id = self.drive.generate_file_id()
        result = self.drive.ensure_uploaded(self.path, "posted-folder", file_id, "asset-1", file_hash(self.path))
        self.assertEqual(result["id"], file_id)
        self.assertEqual(result["parents"], ["posted-folder"])
        self.assertEqual(self.drive.service.create_calls, 1)
        # A repeated sync finds the same preallocated ID and does not create again.
        self.drive.ensure_uploaded(self.path, "posted-folder", file_id, "asset-1", file_hash(self.path))
        self.assertEqual(self.drive.service.create_calls, 1)
        self.assertEqual(self.drive.service.generated_args, {"count": 1, "space": "drive"})

    def test_reserved_file_id_with_wrong_content_is_never_overwritten(self):
        file_id = self.drive.generate_file_id()
        self.drive.service.files_by_id[file_id] = {"id": file_id, "size": "1", "parents": ["posted-folder"],
            "appProperties": {"aiVerseAssetId": "different", "aiVerseSha256": "different"}}
        with self.assertRaisesRegex(UserError, "different content"):
            self.drive.ensure_uploaded(self.path, "posted-folder", file_id, "asset-1", file_hash(self.path))
        self.assertEqual(self.drive.service.create_calls, 0)


if __name__ == "__main__":
    unittest.main()
