import tempfile
import unittest
import zipfile
import hashlib
import json
from pathlib import Path

from social_video_ops.backup import create_backup, restore_backup
from social_video_ops.audit import audit
from social_video_ops.config import Workspace
from social_video_ops.cli import dispatch, parser
from social_video_ops.engine import Engine
from social_video_ops.util import UserError


class WorkspaceBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root / "live")
        self.workspace.init()
        self.engine = Engine(self.workspace)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def test_backup_restores_customer_data_paused_in_new_private_workspace(self):
        media = self.workspace.path / "media" / "originals" / "clip.mp4"
        media.write_bytes(b"private video fixture")
        log = self.workspace.path / "logs" / "status.json"
        log.write_text('{"state":"verified"}')
        token = self.workspace.path / "token.json"
        token.write_text('{"refresh_token":"must not be included"}')
        target = self.root / "backup.zip"
        create_backup(self.engine, target)
        with zipfile.ZipFile(target) as archive:
            self.assertNotIn("token.json", archive.namelist())
            self.assertIn("state.sqlite3", archive.namelist())
        restored = self.root / "restored"
        result = restore_backup(target, restored)
        self.assertTrue(result["paused"])
        self.assertTrue(Workspace(restored).config["paused"])
        self.assertEqual((restored / "media" / "originals" / "clip.mp4").read_bytes(), b"private video fixture")
        self.assertEqual((restored / "logs" / "status.json").read_text(), '{"state":"verified"}')
        if __import__("os").name == "posix":
            self.assertEqual(restored.stat().st_mode & 0o777, 0o700)

    def test_restore_refuses_existing_workspace_without_changing_it(self):
        target = self.root / "backup.zip"
        create_backup(self.engine, target)
        existing = self.root / "existing"
        existing.mkdir()
        marker = existing / "keep.txt"
        marker.write_text("preserve")
        with self.assertRaisesRegex(UserError, "new workspace"):
            restore_backup(target, existing)
        self.assertEqual(marker.read_text(), "preserve")

    def test_restore_rejects_checksum_tampering(self):
        target = self.root / "backup.zip"
        create_backup(self.engine, target)
        changed = self.root / "changed.zip"
        with zipfile.ZipFile(target) as original, zipfile.ZipFile(changed, "w") as altered:
            for item in original.infolist():
                data = original.read(item.filename)
                if item.filename == "settings.json":
                    data += b" "
                altered.writestr(item.filename, data)
        with self.assertRaisesRegex(UserError, "checksum"):
            restore_backup(changed, self.root / "bad-restore")
        self.assertFalse((self.root / "bad-restore").exists())

    def test_restore_rejects_path_traversal_entries(self):
        original = self.root / "safe.zip"
        create_backup(self.engine, original)
        malicious = self.root / "malicious.zip"
        payload = b"outside the restore folder"
        with zipfile.ZipFile(original) as source:
            entries = {info.filename: source.read(info.filename) for info in source.infolist() if info.filename != "backup-manifest.json"}
            manifest = json.loads(source.read("backup-manifest.json"))
        entries["../escaped.txt"] = payload
        manifest["files"]["../escaped.txt"] = hashlib.sha256(payload).hexdigest()
        with zipfile.ZipFile(malicious, "w") as target:
            for name, data in entries.items():
                target.writestr(name, data)
            target.writestr("backup-manifest.json", json.dumps(manifest))
        with self.assertRaisesRegex(UserError, "unsafe path"):
            restore_backup(malicious, self.root / "bad-path-restore")
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_restore_command_does_not_require_an_unrelated_live_workspace(self):
        args = parser().parse_args(["restore", "customer.zip", "--to", "/private/tmp/new-customer"])
        self.assertIsNone(args.workspace)

    def test_backup_command_does_not_require_a_provider_connection(self):
        import os
        from unittest.mock import patch
        from social_video_ops.cli import main
        target = self.root / "offline.zip"
        with patch.dict(os.environ, {"ZERNIO_API_KEY": ""}):
            self.assertEqual(main(["--workspace", str(self.workspace.path), "backup", str(target)]), 0)
        self.assertTrue(target.exists())

    def test_restore_quarantine_blocks_publish_and_resume_until_reconciliation(self):
        backup = self.root / "customer.zip"
        create_backup(self.engine, backup)
        restored_path = self.root / "restored"
        restore_backup(backup, restored_path)
        restored_workspace = Workspace(restored_path)
        config = restored_workspace.config
        config["paused"] = False
        restored_workspace.save(config)
        restored = Engine(restored_workspace, provider=object())
        try:
            self.assertEqual(audit(restored)["modes"]["live_publishing"], "restore_reconciliation_required")
            self.assertEqual(restored.execute("unknown-job")["state"], "restore_reconciliation_required")
            with self.assertRaisesRegex(UserError, "restore-reconcile"):
                dispatch(parser().parse_args(["--workspace", str(restored_path), "pause", "--resume"]), restored)
            result = restored.acknowledge_restore_reconciliation("Reviewed account histories and outstanding provider schedules.")
            self.assertEqual(result["state"], "reconciled")
            self.assertEqual(restored.store.meta("restore_reconciliation_required"), "0")
        finally:
            restored.close()

    def test_restore_reconciliation_requires_saved_accounts_to_match_provider_inventory(self):
        config = self.workspace.config
        config["profiles"] = ["profile-1"]
        self.workspace.save(config)
        self.engine.store.sync_accounts([{"_id": "account-1", "platform": "instagram",
            "profileId": {"_id": "profile-1"}, "platformUserId": "native-1", "isActive": True}],
            ["profile-1"], auto_enroll=True)
        backup = self.root / "customer-with-account.zip"
        create_backup(self.engine, backup)
        restored_path = self.root / "restored-account"
        restore_backup(backup, restored_path)

        class Provider:
            inventory = []
            def accounts(self, profiles):
                return self.inventory

        provider = Provider()
        restored = Engine(Workspace(restored_path), provider=provider)
        try:
            with self.assertRaisesRegex(UserError, "missing from the connected profile inventory"):
                restored.acknowledge_restore_reconciliation("Reviewed the provider post history.")
            provider.inventory = [{"_id": "account-1", "platform": "instagram", "isActive": True,"profileId":"profile-1","platformUserId":"native-1"}]
            self.assertEqual(restored.acknowledge_restore_reconciliation("Reviewed the provider post history.")["state"], "reconciled")
        finally:
            restored.close()


if __name__ == "__main__":
    unittest.main()
