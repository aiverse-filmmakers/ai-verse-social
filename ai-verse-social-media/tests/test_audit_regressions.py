"""Independent regression evidence for the 2026-10-03 audit findings."""
import copy
import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import test_engine as fixtures
from social_video_ops.backup import create_backup, restore_backup
from social_video_ops.config import Workspace, validate, validate_capability
from social_video_ops.engine import Engine
from social_video_ops.media import render, probe
from social_video_ops.providers import ProviderError, Zernio
from social_video_ops.util import UserError, canonical, digest, file_hash, write_json

class AuditRegressions(unittest.TestCase):
    setUp=fixtures.WorkflowTests.setUp
    tearDown=fixtures.WorkflowTests.tearDown
    accounts=fixtures.WorkflowTests.accounts
    ingest=fixtures.WorkflowTests.ingest
    prepare=fixtures.WorkflowTests.prepare

    def job(self):
        self.accounts([('ig','instagram','native')])
        asset=self.ingest()
        return asset,self.prepare(asset)['jobs'][0]['job_id']

    def test_native_identity_swap_rolls_back_inventory(self):
        self.accounts([('ig','instagram','native')])
        with self.assertRaisesRegex(UserError,'native identity'):
            self.accounts([('ig','instagram','someone-else')])
        self.assertTrue(self.engine.store.rows('SELECT native_key FROM destinations')[0]['native_key'].endswith(':native'))

    def test_duplicate_or_missing_profile_inventory_is_rejected(self):
        self.accounts([('ig','instagram','native')])
        value={'_id':'other','platform':'instagram','profileId':'customer-profile','isActive':True}
        for rows in ([value,value],[dict(value,profileId=None)]):
            with self.assertRaises(UserError): self.engine.store.sync_accounts(rows,['customer-profile'])
        self.assertEqual(len(self.engine.store.rows('SELECT * FROM destinations')),1)

    def test_removed_profile_is_not_selected_and_cannot_send(self):
        asset,job=self.job()
        config=self.workspace.config; config['profiles']=['another']; self.workspace.save(config)
        with self.assertRaises(UserError): self.engine.targets('all')
        self.assertIn('Account is disconnected',self.engine.execute(job)['error'])
        self.assertEqual(self.provider.created,[])

    def test_active_lease_cannot_be_revised_by_live_request(self):
        asset,job=self.job(); self.engine._claim(job,'worker')
        preview=self.engine.prepare({'asset_id':asset,'mode':'now','captions':{'instagram':{'text':'Changed'}}})
        with self.assertRaisesRegex(UserError,'processed'):
            self.engine.authorize(preview['request_id'],preview['payload_hash'],'User changed wording')
        self.assertNotEqual(self.engine.store.one('SELECT request_id FROM jobs WHERE id=?',(job,))['request_id'],preview['request_id'])

    def test_job_target_tamper_never_reaches_provider(self):
        _,job=self.job()
        raw=json.loads(self.engine.store.one('SELECT payload FROM jobs WHERE id=?',(job,))['payload']); raw['text']='Unapproved'
        self.engine.store.db.execute('UPDATE jobs SET payload=? WHERE id=?',(canonical(raw),job))
        self.assertIn('authorized request',self.engine.execute(job)['error'])
        self.assertEqual(self.provider.created,[])

    def test_cached_provider_body_tamper_is_rejected(self):
        _,job=self.job()
        raw=json.loads(self.engine.store.one('SELECT payload FROM jobs WHERE id=?',(job,))['payload'])
        raw['api_payload']={'content':'Unapproved','platforms':[]}
        self.engine.store.db.execute('UPDATE jobs SET payload=? WHERE id=?',(canonical(raw),job))
        self.assertIn('Cached provider payload',self.engine.execute(job)['error'])
        self.assertEqual(self.provider.created,[])

    def test_receipt_and_obligation_are_one_transaction(self):
        asset,job=self.job()
        self.engine.store.db.execute("CREATE TRIGGER reject_proof BEFORE UPDATE ON obligations WHEN NEW.status='verified' BEGIN SELECT RAISE(ABORT,'simulated disk write failure'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.engine.execute(job)
        saved=self.engine.store.one('SELECT * FROM jobs WHERE id=?',(job,))
        self.assertNotEqual(saved['state'],'verified')
        self.assertFalse(self.engine.store.coverage(asset)['complete'])
        self.engine.store.db.execute('DROP TRIGGER reject_proof')
        self.assertEqual(self.engine.execute(job)['state'],'verified')
        self.assertEqual(len(self.provider.created),1)

    def test_wrong_provider_record_cannot_verify(self):
        _,job=self.job(); self.provider.fail_response_once=True
        self.engine.execute(job); self.engine.execute(job)
        saved=self.engine.store.one('SELECT * FROM jobs WHERE id=?',(job,))
        self.engine.store.db.execute("UPDATE jobs SET state='verification_pending' WHERE id=?",(job,))
        original=self.provider.get
        self.provider.get=lambda identity: {'post':dict(original(identity)['post'],_id='wrong')}
        self.assertIn('different post',self.engine.execute(job)['error'])

    def test_deleted_scheduled_record_is_confirmed_cancelled(self):
        _,job=self.job(); self.engine.execute(job)
        saved=self.engine.store.one('SELECT * FROM jobs WHERE id=?',(job,)); identity=saved['provider_id']
        self.engine.store.db.execute("UPDATE jobs SET state='scheduled' WHERE id=?",(job,))
        self.provider.posts[identity]['status']='scheduled'
        def cancel(identity): self.provider.posts.pop(identity); return {'message':'Post deleted successfully'}
        def get(identity):
            if identity not in self.provider.posts: raise ProviderError('missing',status=404)
            return {'post':self.provider.posts[identity]}
        self.provider.cancel=cancel; self.provider.get=get
        self.assertEqual(self.engine.cancel_job(job)['state'],'cancelled')
        self.assertEqual(len(self.provider.created),1)

    def test_paused_workspace_reconciles_existing_post(self):
        asset,job=self.job(); self.engine.execute(job)
        self.engine.store.db.execute("UPDATE jobs SET state='verification_pending' WHERE id=?",(job,))
        config=self.workspace.config; config['paused']=True; self.workspace.save(config)
        self.assertEqual(self.engine.execute(job)['state'],'verified')
        self.assertEqual(len(self.provider.created),1)

    def test_archive_failure_retries_without_reposting(self):
        asset,job=self.job()
        original_replace=__import__('os').replace
        def fail_archive(source,destination):
            if '/media/posted/' in str(destination): raise OSError('disk unavailable')
            return original_replace(source,destination)
        with patch('social_video_ops.engine.os.replace',side_effect=fail_archive):
            self.assertEqual(self.engine.execute(job)['state'],'verified')
        self.assertEqual(self.engine.store.one("SELECT state FROM actions WHERE asset_id=?",(asset,))['state'],'pending')
        self.engine.tick()
        self.assertEqual(self.engine.store.one("SELECT state FROM actions WHERE asset_id=?",(asset,))['state'],'done')
        self.assertEqual(len(self.provider.created),1)

    def test_qc_file_must_prove_source_and_output(self):
        self.accounts([('ig','instagram','native')]); asset=self.ingest()
        media=self.workspace.path/'media/ready/unrelated.mp4'; media.write_bytes(b'unrelated')
        write_json(media.with_suffix('.qc.json'),{'sha256':file_hash(media),'source_sha256':'wrong','recipe_hash':'a'})
        with self.assertRaisesRegex(UserError,'QC does not match'):
            self.engine.prepare({'asset_id':asset,'media':str(media),'captions':{'instagram':{'text':'Caption'}}})

    def test_account_specific_rendition_is_uploaded(self):
        self.accounts([('ig','instagram','native')]); asset=self.ingest()
        media=self.workspace.path/'media/ready/edited.mp4'; media.write_bytes(b'edited')
        write_json(media.with_suffix('.qc.json'),{'sha256':file_hash(media),'source_sha256':self.engine.store.require_asset(asset)['hash'],'recipe_hash':'a'})
        destination=self.engine.targets('all')[0]['id']
        prepared=self.engine.prepare({'asset_id':asset,'mode':'now','media_by_account':{destination:str(media)},'captions':{'instagram':{'text':'Caption'}}})
        job=self.engine.authorize(prepared['request_id'],prepared['payload_hash'],'User approves edited rendition')['jobs'][0]['job_id']
        uploaded=[]; self.provider.upload=lambda path: uploaded.append(path) or 'https://media.example.com/video.mp4'
        self.assertEqual(self.engine.execute(job)['state'],'verified'); self.assertEqual(uploaded,[media])

    def test_restore_relocates_assets_and_approved_requests(self):
        asset,job=self.job(); backup=self.path/'workspace.zip'; create_backup(self.engine,backup)
        restored=(self.path/'restored').resolve(); restore_backup(backup,restored)
        recovered=Engine(Workspace(restored),self.provider)
        try:
            row=recovered.store.require_asset(asset)
            self.assertTrue(Path(row['path']).is_relative_to(restored)); self.assertEqual(file_hash(Path(row['path'])),row['hash'])
            saved=recovered.store.one('SELECT * FROM jobs WHERE id=?',(job,))
            request=recovered.store.one('SELECT * FROM requests WHERE id=?',(saved['request_id'],))
            payload=json.loads(request['payload']); self.assertEqual(digest(payload),request['payload_hash'])
            self.assertTrue(Path(payload['media']).is_relative_to(restored))
            self.engine.close(); shutil.rmtree(self.workspace.path)
            # Reopen an empty old DB solely so the fixture teardown remains valid.
            self.engine.close=lambda: None
            self.assertEqual(file_hash(Path(payload['media'])),payload['media_hash'])
            self.assertEqual(json.loads(saved['payload']),payload['targets'][saved['destination_id']])
        finally: recovered.close()

    def test_failed_existing_post_recovery_never_creates_another(self):
        _,job=self.job(); self.engine.execute(job)
        saved=self.engine.store.one('SELECT * FROM jobs WHERE id=?',(job,)); post=self.provider.posts[saved['provider_id']]
        post['status']='failed'; post['platforms'][0]['status']='failed'
        self.engine.store.db.execute("UPDATE jobs SET state='failed' WHERE id=?",(job,))
        calls=[]
        def retry(identity): calls.append(identity); post['status']='published'; post['platforms'][0]['status']='published'
        self.provider.retry=retry
        self.assertEqual(self.engine.recover(job,'retry-failed','User authorizes retry')['state'],'verified')
        self.engine.recover(job,'retry-failed','User repeated message')
        self.assertEqual(len(calls),1); self.assertEqual(len(self.provider.created),1)

    def test_settings_reject_string_booleans_and_invalid_shapes(self):
        for section,key,value in [('drive','enabled','false'),('schedule','max_posts_per_day',True),('captions','mode',[]),('editing','recipe',[])]:
            config=self.workspace.config; config[section][key]=value
            with self.assertRaises(UserError): validate(config)
        with self.assertRaises(UserError): validate_capability({'video':True,'evidence':'contract','proof':[]})

    def test_authorize_refreshes_pause_even_after_preview(self):
        self.accounts([('ig','instagram','native')]); asset=self.ingest()
        preview=self.engine.prepare({'asset_id':asset,'captions':{'instagram':{'text':'Caption'}}})
        config=self.workspace.config; config['paused']=True; self.workspace.save(config)
        with self.assertRaisesRegex(UserError,'paused'): self.engine.authorize(preview['request_id'],preview['payload_hash'],'User approves')

    def test_lease_filter_does_not_starve_new_live_job(self):
        asset,first=self.job(); self.engine._claim(first,'other-worker')
        self.source.write_bytes(b'second'); second=self.ingest()
        job=self.prepare(second)['jobs'][0]['job_id']
        config=self.workspace.config; config['limits']['max_jobs_per_tick']=1; self.workspace.save(config)
        result=self.engine.tick()
        self.assertEqual(result['results'][0]['job_id'],job); self.assertEqual(result['results'][0]['state'],'verified')

    def test_cancel_response_loss_reconciles_absent_record_once(self):
        _,job=self.job(); self.engine.execute(job)
        identity=self.engine.store.one('SELECT provider_id FROM jobs WHERE id=?',(job,))['provider_id']
        self.provider.posts[identity]['status']='scheduled'
        self.engine.store.db.execute("UPDATE jobs SET state='scheduled' WHERE id=?",(job,))
        deletes=[]
        def cancel(identity): deletes.append(identity); self.provider.posts.pop(identity); raise ProviderError('response lost',uncertain=True)
        def get(identity):
            if identity not in self.provider.posts: raise ProviderError('missing',status=404)
            return {'post':self.provider.posts[identity]}
        self.provider.cancel=cancel; self.provider.get=get
        self.assertEqual(self.engine.cancel_job(job)['state'],'cancel_pending')
        self.assertEqual(self.engine.tick()['results'][0]['state'],'cancelled'); self.assertEqual(len(deletes),1)

    def test_draft_promotion_uses_existing_post_and_new_approved_caption(self):
        asset,job=self.job(); self.engine.execute(job)
        identity=self.engine.store.one('SELECT provider_id FROM jobs WHERE id=?',(job,))['provider_id']
        post=self.provider.posts[identity]; post['status']='draft'; post['platforms'][0]['status']='pending'
        self.engine.store.db.execute("UPDATE jobs SET state='provider_draft' WHERE id=?",(job,))
        preview=self.engine.prepare({'asset_id':asset,'mode':'now','captions':{'instagram':{'text':'New approved caption'}}})
        updates=[]
        def update(identity,body): updates.append((identity,body)); post.update(content=body['content'],status='published'); post['platforms'][0]['status']='published'
        self.provider.update=update
        result=self.engine.recover(job,'promote-draft','User approved this preview',request_id=preview['request_id'],payload_hash=preview['payload_hash'])
        self.assertEqual(result['state'],'verified'); self.assertEqual(len(updates),1); self.assertEqual(len(self.provider.created),1)
        self.assertEqual(updates[0][1]['isDraft'],False); self.assertEqual(post['content'],'New approved caption')

    def test_uncertain_failed_retry_is_not_resent(self):
        _,job=self.job(); self.engine.execute(job)
        identity=self.engine.store.one('SELECT provider_id FROM jobs WHERE id=?',(job,))['provider_id']
        post=self.provider.posts[identity]; post['status']='failed'; post['platforms'][0]['status']='failed'
        self.engine.store.db.execute("UPDATE jobs SET state='failed' WHERE id=?",(job,))
        calls=[]
        def retry(identity): calls.append(identity); raise ProviderError('uncertain retry',uncertain=True)
        self.provider.retry=retry
        self.assertEqual(self.engine.recover(job,'retry-failed','User approves retry')['state'],'recovery_pending')
        self.engine.recover(job,'retry-failed','User repeated request')
        self.assertEqual(len(calls),1); self.assertEqual(len(self.provider.created),1)

    def test_unknown_outcome_links_only_proven_job_metadata(self):
        asset,job=self.job(); self.provider.fail_response_once=True; self.engine.execute(job)
        identity=self.provider.created[0][1]
        result=self.engine.recover(job,'reconcile','User investigated provider record',provider_id=identity)
        self.assertIn('metadata',result['error'])
        self.provider.posts[identity]['metadata']={'aiVerseJob':job,'asset':asset}
        self.assertEqual(self.engine.recover(job,'reconcile','Verified provider metadata',provider_id=identity)['state'],'verified')
        self.assertEqual(len(self.provider.created),1)

    def test_source_revision_change_blocks_drive_move(self):
        self.accounts([('ig','instagram','native')])
        asset=self.engine.ingest(self.source,library=True,drive_id='drive-file',drive_parent='ready',drive_version='1')['asset']['id']
        self.engine.execute(self.prepare(asset)['jobs'][0]['job_id'])
        config=self.workspace.config; config['drive']={'enabled':True,'folders':{'posted':'posted'}}; self.workspace.save(config)
        with patch('social_video_ops.drive.Drive') as drive:
            drive.return_value.metadata.return_value={'version':'2','trashed':False}
            result=self.engine.sync_drive(asset)
            self.assertEqual(result['results'][0]['state'],'sync_pending'); drive.return_value.move.assert_not_called()

    def test_tiktok_truthy_string_is_not_consent(self):
        self.accounts([('tt','tiktok','native')]); asset=self.ingest()
        job=self.prepare(asset,captions={'tiktok':{'text':'Caption','platform_data':{'tiktokSettings':{'privacy_level':'PUBLIC_TO_EVERYONE','content_preview_confirmed':'false','express_consent_given':True}}}})['jobs'][0]['job_id']
        self.assertIn('TikTok needs',self.engine.execute(job)['error']); self.assertEqual(self.provider.created,[])

    def test_upload_rejects_private_presigned_network(self):
        provider=Zernio.__new__(Zernio); provider.timeout=1
        provider.request=lambda *args,**kw: {'uploadUrl':'https://upload.example.com/upload','publicUrl':'https://media.example.com/file'}
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaisesRegex(UserError,'nonpublic'): provider.upload(self.source)

    def test_log_export_contains_final_url_and_request_coverage(self):
        asset,job=self.job(); self.engine.execute(job)
        log=next((self.workspace.path/'logs/accounts').glob('*.csv')).read_text()
        self.assertIn('platform_post_url',log); self.assertIn('https://example.com/post-0',log)
        request=self.engine.store.one('SELECT request_id FROM jobs WHERE id=?',(job,))['request_id']
        self.assertTrue(self.engine.request_coverage(request)['published_complete'])

    def test_manual_account_enable_respects_disabled_backfill(self):
        self.accounts([('ig','instagram','native')]); asset=self.ingest()
        self.engine.store.sync_accounts([{'_id':'ig','platform':'instagram','profileId':'customer-profile','platformUserId':'native','isActive':True},{'_id':'li','platform':'linkedin','profileId':'customer-profile','platformUserId':'new','isActive':True}],['customer-profile'],auto_enroll=False,future_backfill=False)
        destination=self.engine.store.one("SELECT id FROM destinations WHERE provider_id='li'")['id']
        self.engine.store.set_destination(destination,enabled=True,future_backfill=False)
        self.assertIsNone(self.engine.store.one('SELECT * FROM obligations WHERE asset_id=? AND destination_id=?',(asset,destination)))
        self.source.write_bytes(b'new-library-video'); future=self.ingest()
        self.assertIsNotNone(self.engine.store.one('SELECT * FROM obligations WHERE asset_id=? AND destination_id=?',(future,destination)))

    def test_cached_upload_url_change_blocks_unknown_attempt_retry(self):
        _,job=self.job(); self.provider.fail_response_once=True; self.engine.execute(job)
        raw=json.loads(self.engine.store.one('SELECT payload FROM jobs WHERE id=?',(job,))['payload'])
        raw['api_payload']['mediaItems'][0]['url']='https://media.example.com/different.mp4'
        self.engine.store.db.execute('UPDATE jobs SET payload=? WHERE id=?',(canonical(raw),job))
        self.assertIn('checksum changed',self.engine.execute(job)['error']); self.assertEqual(len(self.provider.created),1)

    def test_paused_tick_skips_unsent_backlog_to_reconcile_receipts(self):
        asset,job=self.job(); self.engine.execute(job)
        self.engine.store.db.execute("UPDATE jobs SET state='verification_pending' WHERE id=?",(job,))
        self.source.write_bytes(b'unsent-backlog'); waiting=self.ingest(); self.prepare(waiting)
        config=self.workspace.config; config['paused']=True; config['limits']['max_jobs_per_tick']=1; self.workspace.save(config)
        result=self.engine.tick()
        self.assertEqual(result['results'][0]['job_id'],job); self.assertEqual(result['results'][0]['state'],'verified')
        self.assertEqual(len(self.provider.created),1)

class RealMediaTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'Requires real FFmpeg')
    def test_real_passthrough_cache_and_standard_render_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'source.mov'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc2=size=64x48:rate=10','-t','0.5','-c:v','libx264','-pix_fmt','yuv420p',str(source)],check=True)
            first=render(source,root,{'recipe':'passthrough'})
            second=render(source,root,{'recipe':'passthrough'})
            self.assertTrue(second['cached']); self.assertTrue(first['path'].endswith('.mov'))
            edited=render(source,root,{'recipe':'standard','options':{'width':48,'height':64}})
            qc=probe(Path(edited['path']))
            self.assertEqual((qc['width'],qc['height']),(48,64)); self.assertEqual(edited['qc']['source_sha256'],file_hash(source))
