import copy
import json
import tempfile
import socket
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from social_video_ops.config import Workspace
from social_video_ops.audit import audit
from social_video_ops.config import validate as validate_config
from social_video_ops.caption_integrity import require_caption
from social_video_ops.drive import Drive
from social_video_ops.cli import parser,dispatch
from social_video_ops.engine import Engine
from social_video_ops.providers import ProviderError
from social_video_ops.scheduling import next_times
from social_video_ops.util import UserError, file_hash


class FakeProvider:
    def __init__(self):
        self.created, self.posts = [], {}
        self.cancel_calls=[]
        self.uploads=0
        self.fail_response_once = False
        self.health_calls = []
        self.health_all_calls = []
        self.health_result = {"status": "healthy", "permissions": {"canPost": True, "missingRequired": []}}
        self.timeout = 60
        self.observed_timeouts = []

    def accounts(self, profiles):
        self.observed_timeouts.append(self.timeout)
        return [{"_id": "ig", "platform": "instagram", "isActive": True,"profileId":"customer-profile","platformUserId":"native-ig"}]

    def health_all(self, profiles):
        self.health_all_calls.append(profiles)
        self.observed_timeouts.append(self.timeout)
        return [{"accountId": "ig", "status": self.health_result["status"],
                 "canPost": self.health_result.get("permissions", {}).get("canPost"),
                 "issues": self.health_result.get("issues", [])}]

    def health(self, identity):
        self.health_calls.append(identity)
        self.observed_timeouts.append(self.timeout)
        return self.health_result

    def upload(self, path):
        self.uploads+=1
        return "https://media.example.com/video.mp4"

    def validate(self, body):
        return {"valid": True}

    def create(self, payload, key):
        for prior_key, identity in self.created:
            if prior_key == key:
                return {"post": self.posts[identity]}
        identity = "post-" + str(len(self.created))
        target = payload["platforms"][0]
        post = {"_id": identity, "content": payload["content"], "status": "published",
                "platforms": [{"platform": target["platform"], "accountId": target["accountId"],
                               "status": "published", "platformPostId": "native-"+identity,
                               "platformPostUrl": "https://example.com/"+identity}]}
        self.created.append((key, identity)); self.posts[identity] = post
        if self.fail_response_once:
            self.fail_response_once = False
            raise ProviderError("lost response", uncertain=True)
        return {"post": post}

    def get(self, identity):
        return {"post": self.posts[identity]}

    def cancel(self, identity):
        self.cancel_calls.append(identity)
        self.posts[identity]["status"]="cancelled"
        for platform in self.posts[identity].get("platforms",[]):
            platform["status"]="cancelled"
        return {"post":self.posts[identity]}


def fake_probe(path, decode=True):
    return {"sha256": file_hash(path), "bytes": path.stat().st_size, "duration": 1,
            "width": 32, "height": 32, "has_audio": False}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.workspace = Workspace(self.path / "customer")
        self.workspace.init()
        config = self.workspace.config
        config["profiles"] = ["customer-profile"]
        config["repeat_guard"]["enabled"] = False
        self.workspace.save(config)
        self.provider = FakeProvider()
        self.engine = Engine(self.workspace, self.provider)
        self.engine.store.set_meta("repeat_policy_note","Synthetic fake-byte provider fixtures intentionally test only byte/work protection")
        self.patcher = patch("social_video_ops.engine.probe", fake_probe)
        self.patcher.start()
        self.source = self.path / "video.mp4"
        self.source.write_bytes(b"synthetic-video")

    def tearDown(self):
        self.patcher.stop(); self.engine.close(); self.temp.cleanup()

    def accounts(self, names):
        values = [{"_id": identity, "platform": platform, "profileId": {"_id": "customer-profile"},
                   "platformUserId": native, "isActive": True, "displayName": identity}
                  for identity, platform, native in names]
        self.engine.store.sync_accounts(values, ["customer-profile"], auto_enroll=True)
        rows = self.engine.store.rows("SELECT * FROM destinations")
        for row in rows:
            self.engine.store.set_destination(row["id"], capability={"video": True, "evidence": "synthetic contract fixture"})
        return rows

    def ingest(self, library=True):
        return self.engine.ingest(self.source, library=library)["asset"]["id"]

    def test_offline_audit_never_claims_live_publishing_is_ready(self):
        self.assertEqual(audit(self.engine)["modes"]["live_publishing"], "live_check_required")

    def test_live_audit_checks_saved_account_health_without_publishing(self):
        self.accounts([("ig", "instagram", "native-ig")])
        with patch.dict("os.environ", {"ZERNIO_API_KEY": "test-secret"}):
            result = audit(self.engine, live=True, budget=20)
        states = {row["component"]: row["status"] for row in result["checks"]}
        self.assertEqual(states["provider connection"], "ready")
        self.assertIn("account health:", next(key for key in states if key.startswith("account health:")))
        self.assertEqual(next(row["status"] for row in result["checks"] if row["component"].startswith("account health:")), "ready")
        self.assertEqual(self.provider.health_calls, [])
        self.assertEqual(self.provider.health_all_calls, [["customer-profile"]])
        self.assertLess(max(self.provider.observed_timeouts), 60)
        self.assertEqual(self.provider.created, [])

    def test_live_audit_blocks_healthy_account_without_publish_permission(self):
        self.accounts([("ig", "instagram", "native-ig")])
        self.provider.health_result = {"status": "healthy", "permissions": {
            "canPost": False, "missingRequired": ["instagram_content_publish"]}}
        with patch.dict("os.environ", {"ZERNIO_API_KEY": "test-secret"}):
            result = audit(self.engine, live=True)
        health = next(row for row in result["checks"] if row["component"].startswith("account health:"))
        self.assertEqual(health["status"], "needs_setup")
        self.assertFalse(health["detail"]["can_post"])
        self.assertEqual(result["modes"]["live_publishing"], "needs_setup")
        self.assertEqual(self.provider.created, [])

    def test_publish_path_rechecks_can_post_before_upload_or_create(self):
        self.accounts([("ig", "instagram", "native-ig")])
        asset = self.ingest()
        request = self.prepare(asset)
        self.provider.health_result = {"status": "healthy", "permissions": {"canPost": False, "missingRequired": ["posting"]}}
        result = self.engine.execute(request["jobs"][0]["job_id"])
        self.assertEqual(result["state"], "prepared")
        self.assertIn("permissions", result["error"])
        self.assertEqual(self.provider.uploads,0)
        self.assertEqual(self.provider.created, [])
        self.assertEqual(self.provider.posts, {})

    def test_live_drive_audit_does_not_refresh_expired_credentials(self):
        config = self.workspace.config
        config["drive"] = {"enabled": True, "credentials_file": "token.json", "client_secrets_file": "",
            "folders": {"ready": "folder-ready", "posted": "folder-posted"}}
        self.workspace.save(config)
        self.engine.config = self.workspace.config
        observed = {}
        class ReadOnlyDrive:
            def __init__(self, config, timeout, refresh_credentials, retry_attempts):
                observed.update(timeout=timeout, refresh_credentials=refresh_credentials,retry_attempts=retry_attempts)
            def identity(self):
                return {"user": {"emailAddress": "private@example.test"}}
            def metadata(self, identity):
                return {"mimeType": "application/vnd.google-apps.folder", "trashed": False}
        with patch("social_video_ops.drive.Drive", ReadOnlyDrive), patch.dict("os.environ", {"ZERNIO_API_KEY": "test-secret"}):
            audit(self.engine, live=True, budget=20)
        self.assertFalse(observed["refresh_credentials"])
        self.assertEqual(observed["retry_attempts"],1)
        self.assertLessEqual(observed["timeout"], 20/3)
        self.assertEqual(self.provider.timeout, 60)

    def prepare(self, asset, selector="all", mode="now", **extra):
        targets = self.engine.targets(selector)
        spec = {"asset_id": asset, "accounts": selector, "mode": mode,
                "captions": {row["id"]: {"text": f"A real caption for {row['platform']}.\nSecond line.",
                    "platform_data": ({"tiktokSettings":{"privacy_level":"PUBLIC_TO_EVERYONE","content_preview_confirmed":True,
                    "express_consent_given":True,"allow_comment":True,"allow_duet":True,"allow_stitch":True}} if row["platform"]=="tiktok" else {})}
                    for row in targets}}
        spec.update(extra)
        request = self.engine.prepare(spec)
        return self.engine.authorize(request["request_id"], request["payload_hash"], "User asked to post this video.")

    def test_dynamic_new_account_backfills_posted_without_reposting_old(self):
        rows = self.accounts([("ig", "instagram", "native-ig"), ("tt", "tiktok", "native-tt")])
        asset = self.ingest()
        request = self.prepare(asset)
        for job in request["jobs"]:
            self.assertEqual(self.engine.execute(job["job_id"])["state"], "verified")
        self.assertTrue(self.engine.store.coverage(asset)["complete"])
        self.assertEqual(self.engine.store.require_asset(asset)["state"], "posted")
        self.accounts([("ig", "instagram", "native-ig"), ("tt", "tiktok", "native-tt"), ("li", "linkedin", "native-li")])
        self.assertFalse(self.engine.store.coverage(asset)["complete"])
        self.assertEqual(self.engine.store.require_asset(asset)["state"], "posted")
        request = self.prepare(asset, "linkedin")
        self.engine.execute(request["jobs"][0]["job_id"])
        self.assertEqual(len(self.provider.created), 3)
        self.assertTrue(self.engine.store.coverage(asset)["complete"])

    def test_second_account_same_platform_is_independent_and_ambiguous_by_name(self):
        self.accounts([("ig1", "instagram", "native-one"), ("ig2", "instagram", "native-two")])
        with self.assertRaisesRegex(UserError, "ambiguous"):
            self.engine.targets("instagram")
        self.assertEqual(len(self.engine.targets("all")), 2)

    def test_same_account_reconnected_with_new_provider_id_preserves_history(self):
        rows = self.accounts([("ig", "instagram", "same-native")])
        asset = self.ingest()
        self.engine.execute(self.prepare(asset)["jobs"][0]["job_id"])
        reconnected = self.accounts([("ig-new", "instagram", "same-native")])
        self.assertEqual(rows[0]["id"], reconnected[0]["id"])
        self.assertTrue(self.engine.store.coverage(asset)["complete"])

    def test_missing_status_and_url_cannot_satisfy_completion(self):
        self.accounts([("ig", "instagram", "native")])
        asset = self.ingest()
        self.provider.get = lambda identity: {"post": {"_id": identity, "status": "published", "content": "caption", "platforms": []}}
        result = self.engine.execute(self.prepare(asset)["jobs"][0]["job_id"])
        self.assertEqual(result["state"], "verification_pending")
        self.assertFalse(self.engine.store.coverage(asset)["complete"])

    def test_repeat_request_reuses_job_even_after_success(self):
        self.accounts([("ig", "instagram", "native")]); asset = self.ingest()
        first = self.prepare(asset)
        self.engine.execute(first["jobs"][0]["job_id"])
        second = self.engine.prepare({'asset_id':asset,'accounts':'all'})
        self.assertEqual(second['status'],'already_covered_or_reserved')
        self.assertIsNone(second['request_id'])
        self.assertEqual(second['repeat']['covered'][0]['job']['id'],first['jobs'][0]['job_id'])
        self.assertEqual(len(self.provider.created), 1)

    def test_cancel_unsent_job_locally_without_provider_call(self):
        self.accounts([("ig", "instagram", "native")]); asset=self.ingest()
        job=self.prepare(asset)["jobs"][0]["job_id"]
        result=self.engine.cancel_job(job)
        self.assertEqual(result,{"job_id":job,"state":"cancelled","external_action":False})
        self.assertEqual(self.engine.store.one("SELECT state FROM jobs WHERE id=?",(job,))["state"],"cancelled")
        self.assertEqual(self.provider.cancel_calls,[])

    def test_cancel_provider_scheduled_post_and_verify_remote_cancellation(self):
        self.accounts([("ig", "instagram", "native")]); asset=self.ingest()
        due=(datetime.now(timezone.utc)+timedelta(days=1)).replace(hour=10,minute=0,second=0,microsecond=0).isoformat()
        job=self.prepare(asset,mode="schedule",scheduled_at=due)["jobs"][0]["job_id"]
        post_id="scheduled-post"
        self.engine.store.db.execute("UPDATE jobs SET state='scheduled',provider_id=? WHERE id=?",(post_id,job))
        self.provider.posts[post_id]={"_id":post_id,"status":"scheduled","platforms":[
            {"platform":"instagram","accountId":"ig","status":"scheduled"}]}
        result=self.engine.cancel_job(job)
        self.assertEqual(result["state"],"cancelled")
        self.assertEqual(self.provider.cancel_calls,[post_id])
        self.assertEqual(self.engine.store.one("SELECT state FROM jobs WHERE id=?",(job,))["state"],"cancelled")

    def test_cancel_never_deletes_a_post_that_is_already_published(self):
        self.accounts([("ig", "instagram", "native")]); asset=self.ingest()
        due=(datetime.now(timezone.utc)+timedelta(days=1)).replace(hour=10,minute=0,second=0,microsecond=0).isoformat()
        job=self.prepare(asset,mode="schedule",scheduled_at=due)["jobs"][0]["job_id"]
        post_id="already-published"
        self.engine.store.db.execute("UPDATE jobs SET state='scheduled',provider_id=? WHERE id=?",(post_id,job))
        self.provider.posts[post_id]={"_id":post_id,"status":"published","content":"A real caption for instagram.\nSecond line.",
            "platforms":[{"platform":"instagram","accountId":"ig","status":"published",
                "platformPostId":"native-post","platformPostUrl":"https://example.com/native-post"}]}
        result=self.engine.cancel_job(job)
        self.assertEqual(result["state"],"not_cancellable")
        self.assertEqual(self.provider.cancel_calls,[])
        self.assertEqual(self.engine.store.one("SELECT state FROM jobs WHERE id=?",(job,))["state"],"verified")

    def test_cancel_refuses_an_unknown_create_outcome_without_provider_id(self):
        self.accounts([("ig", "instagram", "native")]); asset=self.ingest()
        job=self.prepare(asset)["jobs"][0]["job_id"]
        self.provider.fail_response_once=True
        self.assertEqual(self.engine.execute(job)["state"],"outcome_unknown")
        with self.assertRaisesRegex(UserError,"unknown provider outcome"):
            self.engine.cancel_job(job)
        self.assertEqual(self.provider.cancel_calls,[])

    def test_unknown_response_reuses_real_idempotency_key_after_engine_restart(self):
        self.accounts([("ig", "instagram", "native")]); asset = self.ingest()
        job = self.prepare(asset)["jobs"][0]["job_id"]
        self.provider.fail_response_once = True
        self.assertEqual(self.engine.execute(job)["state"], "outcome_unknown")
        original_key = self.engine.store.one("SELECT idempotency_key FROM jobs WHERE id=?", (job,))["idempotency_key"]
        self.engine.close()
        self.engine = Engine(self.workspace, self.provider)
        self.assertEqual(self.engine.execute(job)["state"], "verified")
        self.assertEqual(len(self.provider.created), 1)
        self.assertEqual(self.provider.created[0][0], original_key)

    def test_database_failure_before_create_keeps_social_post_unsubmitted(self):
        self.accounts([("ig", "instagram", "native")]); asset=self.ingest()
        job=self.prepare(asset)["jobs"][0]["job_id"]
        original=self.engine._update
        def fail_submit(job_id,owner,**fields):
            if fields.get("state")=="submitting":
                raise OSError("simulated disk full")
            return original(job_id,owner,**fields)
        self.engine._update=fail_submit
        result=self.engine.execute(job)
        self.engine._update=original
        self.assertEqual(result["state"],"prepared")
        self.assertEqual(self.provider.created,[])
        self.assertEqual(self.engine.store.one("SELECT state FROM jobs WHERE id=?",(job,))["state"],"prepared")

    def test_database_failure_after_provider_acceptance_recovers_without_duplicate(self):
        self.accounts([("ig", "instagram", "native")]); asset=self.ingest()
        job=self.prepare(asset)["jobs"][0]["job_id"]
        original=self.engine._update
        failed=[False]
        def fail_provider_id_once(job_id,owner,**fields):
            if fields.get("provider_id") and not failed[0]:
                failed[0]=True
                raise OSError("simulated disk full after provider acceptance")
            return original(job_id,owner,**fields)
        self.engine._update=fail_provider_id_once
        result=self.engine.execute(job)
        self.assertEqual(result["state"],"outcome_unknown")
        self.engine._update=original
        self.engine.close()
        self.engine=Engine(self.workspace,self.provider)
        self.assertEqual(self.engine.execute(job)["state"],"verified")
        self.assertEqual(len(self.provider.created),1)

    def test_definitive_rejection_can_be_revised_with_new_authorization(self):
        self.accounts([("ig", "instagram", "native")]); asset=self.ingest()
        first=self.prepare(asset)["jobs"][0]
        create=self.provider.create
        self.provider.create=lambda payload,key: (_ for _ in ()).throw(ProviderError("Zernio returned HTTP 400.",status=400))
        result=self.engine.execute(first["job_id"])
        self.assertEqual(result["state"],"failed")
        self.provider.create=create
        revised=self.engine.prepare({"asset_id":asset,"mode":"now","captions":{"instagram":{"text":"Corrected caption"}}})
        job=self.engine.authorize(revised["request_id"],revised["payload_hash"],"User approved the corrected caption.")["jobs"][0]
        self.assertTrue(job["revised"])
        self.assertEqual(self.engine.execute(job["job_id"])["state"],"verified")
        self.assertEqual(len(self.provider.created),1)

    def test_unknown_response_after_window_blocks_new_create(self):
        self.accounts([("ig", "instagram", "native")]); asset = self.ingest()
        job = self.prepare(asset)["jobs"][0]["job_id"]
        self.provider.fail_response_once = True; self.engine.execute(job)
        self.engine.store.db.execute("UPDATE jobs SET submitted=? WHERE id=?", ((datetime.now(timezone.utc)-timedelta(days=2)).isoformat(),job))
        result = self.engine.execute(job)
        self.assertIn("outside", result["error"])
        self.assertEqual(len(self.provider.created), 1)

    def test_provider_id_can_be_reconciled_after_idempotency_window(self):
        self.accounts([("ig", "instagram", "native")]); asset = self.ingest()
        job = self.prepare(asset)["jobs"][0]["job_id"]
        self.engine.execute(job)
        self.engine.store.db.execute("UPDATE jobs SET state='outcome_unknown',submitted=? WHERE id=?",
                                     ((datetime.now(timezone.utc)-timedelta(days=2)).isoformat(),job))
        self.assertEqual(self.engine.execute(job)["state"], "verified")
        self.assertEqual(len(self.provider.created), 1)

    def test_claim_blocks_second_engine(self):
        self.accounts([("ig", "instagram", "native")]); asset = self.ingest()
        job = self.prepare(asset)["jobs"][0]["job_id"]
        other = Engine(self.workspace, self.provider)
        try:
            self.assertIsNotNone(self.engine._claim(job, "worker-one"))
            self.assertIsNone(other._claim(job, "worker-two"))
        finally:
            other.close()

    def test_no_accounts_does_not_archive_or_publish(self):
        asset = self.ingest()
        self.engine.close_asset(asset)
        self.assertEqual(self.engine.store.require_asset(asset)["state"], "ready")
        with self.assertRaises(UserError):
            self.prepare(asset)

    def test_only_request_does_not_enroll_elsewhere(self):
        self.accounts([("ig", "instagram", "native-ig"), ("li", "linkedin", "native-li")])
        asset = self.ingest(library=False)
        self.engine.execute(self.prepare(asset, "instagram")["jobs"][0]["job_id"])
        coverage = self.engine.store.coverage(asset)
        self.assertTrue(coverage["complete"])
        self.assertEqual(len(coverage["destinations"]),1)
        self.accounts([("ig", "instagram", "native-ig"), ("li", "linkedin", "native-li"), ("yt", "youtube", "native-yt")])
        self.assertEqual(len(self.engine.store.coverage(asset)["destinations"]),1)

    def test_request_scoped_archive_stays_distinct_until_library_enrollment_completes(self):
        self.accounts([("ig", "instagram", "native-ig"),("li", "linkedin", "native-li")])
        asset=self.ingest(library=False)
        self.engine.execute(self.prepare(asset,"instagram")["jobs"][0]["job_id"])
        request_path=self.workspace.path/"media"/"posted"/"request-scoped"/Path(self.engine.store.require_asset(asset)["path"]).name
        library_path=self.workspace.path/"media"/"posted"/"library"/Path(self.engine.store.require_asset(asset)["path"]).name
        self.assertTrue(request_path.is_file())
        self.assertFalse(library_path.exists())
        self.engine.enroll(asset,True)
        job=self.prepare(asset,"linkedin")["jobs"][0]["job_id"]
        self.engine.execute(job)
        self.assertTrue(library_path.is_file())
        self.assertTrue(request_path.is_file())

    def test_custom_mode_requires_current_video_input(self):
        self.accounts([("ig", "instagram", "native")]); asset = self.ingest()
        self.engine.config["captions"]["mode"] = "custom"
        self.workspace.save(self.engine.config)
        with self.assertRaisesRegex(UserError,"custom wording"):
            self.prepare(asset)
        self.engine.captions.add("My custom wording.","custom",asset_id=asset)
        self.assertTrue(self.prepare(asset)["jobs"])

    def test_feedback_journal_preserves_revision_and_filters_rejected_proposals(self):
        first = self.engine.captions.add("My original wording.","custom")
        revision = self.engine.captions.add("My improved wording.","revision",parent_id=first,note="Make it conversational.")
        suggested = self.engine.captions.add("A generic AI draft.","suggestion")
        self.engine.captions.add("A generic AI draft.","rejected",parent_id=suggested)
        context = self.engine.captions.context(self.engine.config)
        self.assertEqual([row["id"] for row in context["user_examples"]],[revision])
        journal=(self.workspace.path/"captions/caption-journal.md").read_text()
        self.assertIn("My original wording",journal); self.assertIn("My improved wording",journal)
        self.assertIn("rejected",journal)

    def test_caption_validation_preserves_typographic_unicode_spacing_and_direction(self):
        caption="Bonjour\u00a0! שלום\u200fمرحبا"
        self.assertEqual(require_caption(caption), caption)
        with self.assertRaises(ValueError):
            require_caption("line\x00break")

    def test_config_refuses_recurring_without_explicit_authorization(self):
        config=copy.deepcopy(self.engine.config)
        config["schedule"]["enabled"]=True
        with self.assertRaisesRegex(UserError,"requires recorded scope approval"):
            validate_config(config)

    def test_workspace_stays_outside_skill_and_is_owner_only_on_posix(self):
        skill=Path(__file__).resolve().parents[1]
        with self.assertRaisesRegex(UserError,"separate from the installed skill"):
            Workspace(skill/"customer-state")
        if __import__("os").name=="posix":
            self.assertEqual(self.workspace.path.stat().st_mode & 0o777,0o700)

    def test_bad_tick_limits_and_caption_shapes_fail_on_save(self):
        config=copy.deepcopy(self.engine.config)
        config["limits"]["tick_seconds"]=99999
        with self.assertRaisesRegex(UserError,"tick_seconds"):
            validate_config(config)
        config=copy.deepcopy(self.engine.config)
        config["captions"]["avoid"]="spam"
        with self.assertRaisesRegex(UserError,"Caption avoid"):
            validate_config(config)

    def test_drive_sync_waits_for_coverage_then_moves_original(self):
        rows=self.accounts([("ig", "instagram", "native")])
        asset=self.ingest(library=True)
        self.engine.store.db.execute("UPDATE assets SET drive_id='drive-file',drive_parent='ready' WHERE id=?",(asset,))
        config=self.engine.config
        config["drive"]={"enabled":True,"folders":{"posted":"posted"}}
        self.engine.workspace.save(config)
        self.engine.config=self.engine.workspace.config
        with patch("social_video_ops.drive.Drive") as drive_class:
            result=self.engine.sync_drive(asset)
            self.assertEqual(result["results"][0]["state"],"waiting_for_accounts")
            drive_class.assert_not_called()
            self.engine.execute(self.prepare(asset)["jobs"][0]["job_id"])
            drive=drive_class.return_value
            drive.metadata.return_value={"md5Checksum":file_hash(Path(self.engine.store.require_asset(asset)["path"]),"md5")}
            drive.move.return_value={"id":"drive-file","parents":["posted"]}
            result=self.engine.sync_drive(asset)
            self.assertEqual(result["results"][0]["state"],"filed")
            drive.move.assert_called_once_with("drive-file","ready","posted")
            self.assertEqual(self.engine.store.require_asset(asset)["drive_parent"],"posted")

    def test_drive_sync_reconciles_a_folder_mapping_change_during_the_remote_move(self):
        self.accounts([("ig", "instagram", "native")])
        asset=self.engine.ingest(self.source,library=True,drive_id="drive-file",drive_parent="ready")["asset"]["id"]
        config=self.engine.config
        config["drive"]={"enabled":True,"folders":{"ready":"ready","posted":"posted"}}
        self.workspace.save(config)
        self.engine.config=self.workspace.config
        self.engine.execute(self.prepare(asset)["jobs"][0]["job_id"])
        moves=[]
        workspace=self.workspace
        checksum=file_hash(Path(self.engine.store.require_asset(asset)["path"]),"md5")
        class FakeDrive:
            def __init__(self,config): pass
            def metadata(self,identity): return {"md5Checksum":checksum}
            def move(self,identity,expected,target):
                moves.append((identity,expected,target))
                if len(moves)==1:
                    changed=workspace.config
                    changed["drive"]["folders"]["posted"]="new-posted"
                    workspace.save(changed)
                return {"id":identity,"parents":[target]}
        with patch("social_video_ops.drive.Drive",FakeDrive):
            first=self.engine.sync_drive(asset)
            self.assertEqual(first["results"][0]["state"],"scope_changed_after_move")
            self.assertEqual(self.engine.store.require_asset(asset)["drive_parent"],"posted")
            second=self.engine.sync_drive(asset)
        self.assertEqual(second["results"][0]["state"],"filed")
        self.assertEqual(moves,[ ("drive-file","ready","posted"), ("drive-file","posted","new-posted") ])

    def test_duplicate_chat_video_attaches_drive_source_for_archive_filing(self):
        self.accounts([("ig", "instagram", "native")])
        asset=self.ingest(library=True)
        duplicate=self.engine.ingest(self.source,library=True,drive_id="drive-copy",drive_parent="ready",drive_version="2")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["asset"]["id"],asset)
        self.engine.execute(self.prepare(asset)["jobs"][0]["job_id"])
        config=self.engine.config; config["drive"]={"enabled":True,"folders":{"posted":"posted"}}
        self.engine.workspace.save(config); self.engine.config=self.engine.workspace.config
        with patch("social_video_ops.drive.Drive") as drive_class:
            drive_class.return_value.metadata.return_value={"version":"2","trashed":False}
            drive_class.return_value.move.return_value={"id":"drive-copy","parents":["posted"]}
            result=self.engine.sync_drive(asset)
        self.assertEqual(result["results"][0]["state"],"filed")
        drive_class.return_value.move.assert_called_once_with("drive-copy","ready","posted")

    def test_chat_video_is_copied_to_drive_only_after_verified_and_explicitly_enabled(self):
        self.accounts([("ig", "instagram", "native")])
        asset=self.ingest(library=True)
        config=self.workspace.config
        config["drive"]={"enabled":True,"archive_local_uploads":True,"folders":{"posted":"posted"}}
        self.workspace.save(config)
        self.engine.config=self.workspace.config
        calls=[]
        class FakeDrive:
            def __init__(self,config): pass
            def metadata(self,identity): return {"id":identity,"version":"1","trashed":False}
            def generate_file_id(self): return "reserved-drive-id"
            def ensure_uploaded(self,path,parent,file_id,asset_id,sha256):
                calls.append((str(path),parent,file_id,asset_id,sha256))
                return {"id":file_id,"parents":[parent],"version":"1"}
        with patch("social_video_ops.drive.Drive",FakeDrive):
            waiting=self.engine.sync_drive(asset)
            self.assertEqual(waiting["results"][0]["state"],"waiting_for_accounts")
            self.assertEqual(calls,[])
            self.engine.execute(self.prepare(asset)["jobs"][0]["job_id"])
            result=self.engine.sync_drive(asset)
            again=self.engine.sync_drive(asset)
        self.assertEqual(result["results"][0]["state"],"uploaded_and_filed")
        self.assertEqual(again["results"][0]["state"],"already_filed")
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][1:4],("posted","reserved-drive-id",asset))
        export=self.engine.store.one("SELECT * FROM drive_exports WHERE asset_id=?",(asset,))
        self.assertEqual(export["state"],"filed")

    def test_uncertain_drive_upload_reuses_the_same_reserved_file_id(self):
        self.accounts([("ig", "instagram", "native")])
        asset=self.ingest(library=True)
        self.engine.execute(self.prepare(asset)["jobs"][0]["job_id"])
        config=self.workspace.config
        config["drive"]={"enabled":True,"archive_local_uploads":True,"folders":{"posted":"posted"}}
        self.workspace.save(config)
        self.engine.config=self.workspace.config
        calls=[]
        class FakeDrive:
            def __init__(self,config): pass
            def generate_file_id(self): calls.append(("generate",)); return "single-reserved-id"
            def ensure_uploaded(self,path,parent,file_id,asset_id,sha256):
                calls.append(("upload",file_id))
                if len([call for call in calls if call[0]=="upload"])==1:
                    raise UserError("Drive upload outcome is uncertain; retry filing with the same reserved file ID.")
                return {"id":file_id,"parents":[parent],"version":"2"}
        with patch("social_video_ops.drive.Drive",FakeDrive):
            first=self.engine.sync_drive(asset)
            self.engine.close()
            self.engine=Engine(self.workspace,self.provider)
            second=self.engine.sync_drive(asset)
        self.assertEqual(first["results"][0]["state"],"sync_pending")
        self.assertEqual(second["results"][0]["state"],"uploaded_and_filed")
        self.assertEqual(calls,[ ("generate",),("upload","single-reserved-id"),("upload","single-reserved-id") ])

    def test_drive_archive_policy_command_updates_setting_without_remote_io(self):
        result=dispatch(parser().parse_args(["--workspace",str(self.workspace.path),"drive-archive-policy","--enabled","yes"]),self.engine)
        self.assertTrue(result["archive_local_uploads"])
        self.assertTrue(self.workspace.config["drive"]["archive_local_uploads"])

    def test_drive_export_tracking_is_added_to_an_existing_database_without_reset(self):
        self.engine.store.set_meta("retained-history-marker","keep")
        self.engine.close()
        import sqlite3
        with sqlite3.connect(self.workspace.path/"state.sqlite3") as db:
            db.execute("DROP TABLE drive_exports")
        self.engine=Engine(self.workspace,self.provider)
        self.assertEqual(self.engine.store.meta("retained-history-marker"),"keep")
        self.assertIsNotNone(self.engine.store.one("SELECT name FROM sqlite_master WHERE type='table' AND name='drive_exports'"))

    def test_drive_folder_mapping_change_during_local_upload_is_reconciled(self):
        self.accounts([("ig", "instagram", "native")])
        asset=self.ingest(library=True)
        self.engine.execute(self.prepare(asset)["jobs"][0]["job_id"])
        config=self.workspace.config
        config["drive"]={"enabled":True,"archive_local_uploads":True,"folders":{"posted":"posted"}}
        self.workspace.save(config)
        self.engine.config=self.workspace.config
        calls=[]
        workspace=self.workspace
        class FakeDrive:
            def __init__(self,config): pass
            def metadata(self,identity): return {"id":identity,"version":"1","trashed":False}
            def generate_file_id(self): return "reserved-drive-id"
            def ensure_uploaded(self,path,parent,file_id,asset_id,sha256):
                calls.append(("upload",parent))
                changed=workspace.config
                changed["drive"]["folders"]["posted"]="posted-v2"
                workspace.save(changed)
                return {"id":file_id,"parents":[parent],"version":"1"}
            def move(self,file_id,expected,target):
                calls.append(("move",expected,target))
                return {"id":file_id,"parents":[target]}
        with patch("social_video_ops.drive.Drive",FakeDrive):
            first=self.engine.sync_drive(asset)
            second=self.engine.sync_drive(asset)
        self.assertEqual(first["results"][0]["state"],"scope_changed_after_upload")
        self.assertEqual(second["results"][0]["state"],"filed")
        self.assertEqual(calls,[("upload","posted"),("move","posted","posted-v2")])

    def test_request_only_drive_source_uses_request_scoped_archive(self):
        self.accounts([("ig", "instagram", "native")])
        asset=self.engine.ingest(self.source,library=False,drive_id="scoped-copy",drive_parent="ready",drive_version="1")["asset"]["id"]
        self.engine.execute(self.prepare(asset,"instagram")["jobs"][0]["job_id"])
        config=self.engine.config
        config["drive"]={"enabled":True,"folders":{"posted":"posted","request_scoped":"request-only"}}
        self.engine.workspace.save(config); self.engine.config=self.engine.workspace.config
        with patch("social_video_ops.drive.Drive") as drive_class:
            drive_class.return_value.metadata.return_value={"version":"1","trashed":False}
            drive_class.return_value.move.return_value={"id":"scoped-copy","parents":["request-only"]}
            result=self.engine.sync_drive(asset)
        self.assertEqual(result["results"][0]["state"],"filed")
        drive_class.return_value.move.assert_called_once_with("scoped-copy","ready","request-only")

    def test_tiktok_requires_platform_consent_fields_before_provider_create(self):
        self.accounts([("tt", "tiktok", "native")]); asset=self.ingest()
        invalid=self.engine.prepare({"asset_id":asset,"mode":"now","captions":{"tiktok":{"text":"Caption"}}})
        blocked=self.engine.authorize(invalid["request_id"],invalid["payload_hash"],"User requested a TikTok post.")["jobs"][0]
        result=self.engine.execute(blocked["job_id"])
        self.assertIn("TikTok needs",result["error"])
        self.assertEqual(len(self.provider.created),0)
        destination=self.engine.store.one("SELECT id FROM destinations WHERE platform='tiktok'")["id"]
        prepared=self.engine.prepare({"asset_id":asset,"mode":"now","captions":{destination:{
            "text":"Caption","platform_data":{"tiktokSettings":{"privacy_level":"PUBLIC_TO_EVERYONE",
            "content_preview_confirmed":True,"express_consent_given":True,"allow_comment":True,
            "allow_duet":True,"allow_stitch":True}}}}})
        job=self.engine.authorize(prepared["request_id"],prepared["payload_hash"],"User requested a TikTok post.")["jobs"][0]
        self.assertEqual(job["job_id"],blocked["job_id"])
        self.assertTrue(job["revised"])
        self.assertEqual(self.engine.execute(job["job_id"])["state"],"verified")

    def test_drive_intake_paginates_saves_watermark_and_deduplicates_sources(self):
        config=self.engine.config
        config["drive"]={"enabled":True,"folders":{"ready":"ready-folder"}}
        self.engine.workspace.save(config); self.engine.config=self.engine.workspace.config
        class FakeDrive:
            pages=[([{"id":"file-one","name":"one.mp4","mimeType":"video/mp4","version":"1","size":"6","modifiedTime":"2026-10-01T00:00:00Z"}],"page-2"),
                   ([{"id":"file-two","name":"two.mp4","mimeType":"video/mp4","version":"1","size":"6","modifiedTime":"2026-10-02T00:00:00Z"}],None),([],None)]
            calls=[]
            def __init__(self,config): pass
            def inventory_page(self,folder,token,limit,modified_after=None):
                self.calls.append((token,modified_after)); return self.pages.pop(0)
            def metadata(self,identity): return {"id":identity,"name":identity+".mp4","version":"1","parents":["ready-folder"],"mimeType":"video/mp4"}
            def download(self,identity,target): target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes((identity+"-bytes").encode())
        with patch("social_video_ops.drive.Drive",FakeDrive):
            args=parser().parse_args(["--workspace",str(self.workspace.path),"drive-intake","--limit","1"])
            first=dispatch(args,self.engine)
            self.assertEqual(first["processed"][0]["file_id"],"file-one")
            second=dispatch(args,self.engine)
            self.assertEqual(second["processed"][0]["file_id"],"file-two")
            third=dispatch(args,self.engine)
        self.assertEqual(FakeDrive.calls[1][0],"page-2")
        self.assertIsNotNone(FakeDrive.calls[2][1])
        self.assertEqual(self.engine.store.one("SELECT COUNT(*) AS n FROM assets")["n"],2)
    
    def test_transcript_mode_requires_actual_transcript(self):
        self.accounts([("ig", "instagram", "native")]); asset = self.ingest()
        self.engine.config["captions"]["mode"]="transcript"
        self.workspace.save(self.engine.config)
        with self.assertRaisesRegex(UserError,"actual transcript"):
            self.prepare(asset)

    def test_past_schedule_does_not_turn_into_immediate_post(self):
        self.accounts([("ig", "instagram", "native")]); asset = self.ingest()
        with self.assertRaisesRegex(UserError,"future"):
            self.prepare(asset, mode="schedule", scheduled_at="2020-01-01T00:00:00Z")

    def test_explicit_schedule_must_fit_customer_time_window(self):
        self.accounts([("ig", "instagram", "native")]); asset=self.ingest()
        self.engine.config["timezone"]="UTC"
        self.engine.config["schedule"]["windows"]=["12:00"]
        due=(datetime.now(timezone.utc)+timedelta(days=2)).replace(hour=12,minute=30,second=0,microsecond=0).isoformat()
        with self.assertRaisesRegex(UserError,"outside the customer's approved posting times"):
            self.prepare(asset,mode="schedule",scheduled_at=due)

    def test_authorized_schedules_obey_daily_limit_and_minimum_spacing(self):
        self.accounts([("ig", "instagram", "native")])
        self.engine.config["timezone"]="UTC"
        self.engine.config["schedule"]["windows"]=["12:00","13:00"]
        self.engine.config["schedule"]["max_posts_per_day"]=4
        self.engine.config["schedule"]["min_gap_minutes"]=120
        self.workspace.save(self.engine.config)
        day=(datetime.now(timezone.utc)+timedelta(days=2)).date()
        first_asset=self.ingest()
        first=self.prepare(first_asset,mode="schedule",scheduled_at=datetime(day.year,day.month,day.day,12,tzinfo=timezone.utc).isoformat())
        self.assertEqual(first["jobs"][0]["state"],"prepared")
        self.source.write_bytes(b"next-video")
        second_asset=self.ingest()
        with self.assertRaisesRegex(UserError,"too close"):
            self.prepare(second_asset,mode="schedule",scheduled_at=datetime(day.year,day.month,day.day,13,tzinfo=timezone.utc).isoformat())
        self.engine.config["schedule"]["max_posts_per_day"]=1
        self.workspace.save(self.engine.config)
        with self.assertRaisesRegex(UserError,"daily post limit"):
            self.prepare(second_asset,mode="schedule",scheduled_at=datetime(day.year,day.month,day.day,12,tzinfo=timezone.utc).isoformat())

    def test_edited_payload_invalidates_authorization(self):
        self.accounts([("ig", "instagram", "native")]); asset=self.ingest()
        prepared=self.engine.prepare({"asset_id":asset,"mode":"now","captions":{"instagram":{"text":"Original"}}})
        self.engine.store.db.execute("UPDATE requests SET payload='{}' WHERE id=?",(prepared["request_id"],))
        with self.assertRaises(UserError):
            self.engine.authorize(prepared["request_id"],prepared["payload_hash"],"User approval")

    def test_schedule_skips_dst_missing_hour(self):
        config=copy.deepcopy(self.engine.config); config["timezone"]="Europe/Bucharest"
        config["schedule"]["windows"]=["03:30"]
        result=next_times(config,[],start=datetime(2026,3,28,23,0,tzinfo=timezone.utc))
        self.assertTrue(result["local_time"].startswith("2026-03-30"))

    def test_zernio_utc_analytics_schema_ranks_slots_with_sample_threshold(self):
        config=copy.deepcopy(self.engine.config)
        config["schedule"]["windows"]=["11:00","12:00"]
        config["schedule"]["minimum_best_time_samples"]=10
        start=datetime(2026,1,4,8,0,tzinfo=timezone.utc)
        result=next_times(config,[],start=start,analytics=[
            {"day_of_week":0,"hour":11,"post_count":4,"avg_engagement":1.0},
            {"day_of_week":0,"hour":12,"post_count":10,"avg_engagement":9.0},
        ])
        self.assertTrue(result["scheduled_at"].startswith("2026-01-04T12:00"))
        self.assertEqual(result["confidence"],"historical estimate")

    def test_duplicate_ingest_does_not_reset_distribution(self):
        asset=self.ingest(library=False)
        repeat=self.engine.ingest(self.source,library=True)
        self.assertTrue(repeat["duplicate"])
        self.assertEqual(repeat["asset"]["id"],asset)
        self.assertFalse(repeat["asset"]["library"])


class DriveMoveTests(unittest.TestCase):
    def test_move_reconciles_a_lost_update_response_before_retrying(self):
        drive=object.__new__(Drive)
        class FileRecord:
            def __init__(self):
                self.parents=["ready"]
                self.failed_once=False
            def execute(self):
                self.parents=["posted"]
                if not self.failed_once:
                    self.failed_once=True
                    raise socket.timeout()
                return {"id":"video","parents":self.parents}
        class Files:
            def __init__(self, record): self.record=record
            def update(self, **kwargs): return self.record
        record=FileRecord()
        drive.service=type("Service",(),{"files":lambda self:Files(record)})()
        calls=[]
        def metadata(identity):
            calls.append(identity)
            return {"id":identity,"parents":record.parents,"trashed":False}
        drive.metadata=metadata
        result=drive.move("video","ready","posted")
        self.assertEqual(result["parents"],["posted"])
        self.assertEqual(len(calls),2)
        self.assertEqual(record.parents,["posted"])

if __name__ == "__main__":
    unittest.main()
