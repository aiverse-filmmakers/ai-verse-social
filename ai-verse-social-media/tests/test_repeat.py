import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from social_video_ops.config import Workspace
from social_video_ops.engine import Engine
from social_video_ops.store import Store, SCHEMA
from social_video_ops.repeat import phash, distance, ALGORITHM
from social_video_ops.util import UserError, file_hash
from test_engine import FakeProvider

@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'Requires FFmpeg')
class RepeatVideos(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.workspace=Workspace(self.root/'customer');self.workspace.init()
        config=self.workspace.config;config['profiles']=['customer-profile'];self.workspace.save(config)
        self.provider=FakeProvider();self.engine=Engine(self.workspace,self.provider);self.addCleanup(self.engine.close)
        self.engine.store.sync_accounts([{'_id':'ig','platform':'instagram','profileId':'customer-profile','platformUserId':'native-ig','isActive':True}],['customer-profile'],auto_enroll=True)
        self.destination=self.engine.store.rows('SELECT id FROM destinations')[0]['id']
        self.engine.store.set_destination(self.destination,capability={'video':True,'evidence':'synthetic fixture','proof':'public_url'})
        self.source=self.root/'motion.mp4'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc2=size=160x120:rate=12:duration=3','-c:v','libx264','-y',str(self.source)],check=True)
        self.asset=self.engine.ingest(self.source,library=True)['asset']['id']

    def prepare(self,asset=None,accounts='all'):
        asset=asset or self.asset
        return self.engine.prepare({'asset_id':asset,'mode':'now','accounts':accounts,'captions':{'instagram':{'text':'Fixture video'}}})

    def publish(self,asset=None):
        prepared=self.prepare(asset)
        self.assertIn('payload_hash',prepared,prepared)
        authorized=self.engine.authorize(prepared['request_id'],prepared['payload_hash'],'User fixture publication')
        job=authorized['jobs'][0]['job_id'];result=self.engine.execute(job)
        self.assertEqual(result['state'],'verified',result)
        return job

    def reencoded(self):
        target=self.root/'copy.mp4'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(self.source),'-c:v','libx264','-crf','30','-y',str(target)],check=True)
        return self.engine.ingest(target)['asset']['id']

    def test_identical_upload_has_one_asset_and_no_second_create(self):
        self.publish()
        renamed=self.root/'renamed.mp4';shutil.copy2(self.source,renamed)
        result=self.engine.ingest(renamed)
        self.assertTrue(result['duplicate']);self.assertEqual(result['asset']['id'],self.asset)
        no_op=self.prepare();self.assertEqual(no_op['status'],'already_covered_or_reserved')
        self.assertEqual(len(self.provider.created),1)

    def test_real_reencode_triggers_review_and_can_share_coverage(self):
        self.publish();alias=self.reencoded()
        result=self.prepare(alias)
        self.assertEqual(result['status'],'needs_review',result)
        self.assertEqual(result['repeat']['matches'][0]['work_id'],self.asset)
        self.assertEqual(len(self.provider.created),1)
        self.engine.repeat.review(alias,'same',self.asset,'User confirmed the same footage after inspecting it')
        self.assertEqual(self.prepare(alias)['status'],'already_covered_or_reserved')
        # Linking neither copies old receipts nor enrolls the new upload.
        self.assertEqual(self.engine.store.rows('SELECT * FROM jobs WHERE asset_id=?',(alias,)),[])
        self.assertFalse(self.engine.store.require_asset(alias)['library'])
        self.assertEqual(self.engine.repeat.panel(alias)['unique_works'],1)

    def test_prepared_alias_cannot_bypass_pending_job(self):
        prepared=self.prepare();self.engine.authorize(prepared['request_id'],prepared['payload_hash'],'Explicit fixture scope')
        alias=self.reencoded();result=self.prepare(alias)
        self.assertEqual(result['status'],'needs_review');self.assertEqual(len(self.provider.created),0)
        with self.assertRaises(UserError):self.engine.repeat.review(alias,'same',self.asset,'Same work')

    def test_flat_material_needs_explicit_review(self):
        flat=self.root/'black.mp4'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=black:size=160x120:duration=1','-c:v','libx264','-y',str(flat)],check=True)
        asset=self.engine.ingest(flat)['asset']['id']
        self.assertEqual(self.prepare(asset)['status'],'needs_review')
        self.engine.repeat.review(asset,'distinct','','User confirmed this intentional static clip is valid')
        self.assertIn('payload_hash',self.prepare(asset))

    def test_cache_missing_frame_rebuilds_without_full_decode(self):
        data=self.engine.repeat.fingerprint(self.asset)
        frame=self.workspace.path/data['frames'][0]['path'];frame.unlink()
        with patch('social_video_ops.repeat.probe',wraps=__import__('social_video_ops.media',fromlist=['probe']).probe) as probe:
            self.engine.repeat.fingerprint(self.asset)
            self.assertFalse(probe.call_args.kwargs['decode'])
        self.assertTrue(frame.exists())

    def test_extraction_failure_holds_without_external_create(self):
        with patch('social_video_ops.repeat.subprocess.run',side_effect=FileNotFoundError):
            result=self.prepare()
        self.assertEqual(result['status'],'check_pending');self.assertEqual(self.provider.uploads,0)

    def test_new_account_can_receive_confirmed_work_without_reposting_old(self):
        self.publish();alias=self.reencoded();self.engine.repeat.check(alias)
        self.engine.repeat.review(alias,'same',self.asset,'Confirmed same footage')
        self.engine.store.sync_accounts([
            {'_id':'ig','platform':'instagram','profileId':'customer-profile','platformUserId':'native-ig','isActive':True},
            {'_id':'new','platform':'instagram','profileId':'customer-profile','platformUserId':'native-new','isActive':True}],['customer-profile'],auto_enroll=True)
        new=self.engine.store.one("SELECT id FROM destinations WHERE provider_id='new'")['id']
        self.engine.store.set_destination(new,capability={'video':True,'evidence':'fixture','proof':'public_url'})
        request=self.prepare(alias)
        self.assertEqual(list(request['preview']['targets']),[new])
        jobs=self.engine.authorize(request['request_id'],request['payload_hash'],'User requested all selected accounts')['jobs']
        self.assertEqual(self.engine.execute(jobs[0]['job_id'])['state'],'verified')
        self.assertEqual(len(self.provider.created),2)
        self.assertEqual(self.engine.repeat.panel()['unique_works'],1)
        self.assertFalse(self.engine.store.require_asset(alias)['library'])

    def test_two_encodings_authorized_concurrently_create_at_most_one_post(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        alias=self.reencoded()
        first=self.prepare(self.asset);second=self.prepare(alias)
        self.assertIn('payload_hash',first);self.assertIn('payload_hash',second)
        barrier=threading.Barrier(2)
        def run(request):
            engine=Engine(self.workspace,self.provider)
            try:
                barrier.wait(timeout=5)
                auth=engine.authorize(request['request_id'],request['payload_hash'],'Explicit fixture scope')
                if auth.get('jobs'): return engine.execute(auth['jobs'][0]['job_id'])
                return auth
            finally:engine.close()
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(run,(first,second)))
        self.assertLessEqual(len(self.provider.created),1,results)
        self.assertTrue(any(r.get('status')=='needs_review' or r.get('state')=='repeat_hold' for r in results),results)

    def test_single_shared_sample_is_not_a_repeat(self):
        self.publish();alias=self.reencoded()
        data=self.engine.repeat.fingerprint(alias)
        for index in (1,2):data['frames'][index]['hash']=f"{int(data['frames'][index]['hash'],16)^0x7fffffffffffffff:016x}"
        self.engine.store.db.execute('UPDATE repeat_fingerprints SET data=? WHERE asset_id=?',(json.dumps(data),alias))
        result=self.engine.repeat.check(alias)
        self.assertEqual(result['status'],'clear',result)
        self.assertEqual(result['matches'],[])

    def test_pair_specific_distinct_does_not_clear_a_new_match(self):
        self.publish();alias=self.reencoded()
        self.engine.repeat.check(alias)
        self.engine.repeat.review(alias,'distinct',self.asset,'Same imagery but different story; intentionally distinct')
        self.assertEqual(self.engine.repeat.check(alias)['status'],'clear')
        config=self.workspace.config;config['repeat_guard']['hamming_bits']=5;self.workspace.save(config)
        self.assertEqual(self.engine.repeat.check(alias)['status'],'needs_review')

    def test_source_change_and_malformed_cache_do_not_pass(self):
        self.engine.repeat.fingerprint(self.asset)
        self.engine.store.db.execute("UPDATE repeat_fingerprints SET data='[]' WHERE asset_id=?",(self.asset,))
        self.assertEqual(self.engine.repeat.check(self.asset)['status'],'clear') # Rebuilt from real source.
        Path(self.engine.store.require_asset(self.asset)['path']).write_bytes(b'changed')
        self.assertEqual(self.engine.repeat.check(self.asset)['status'],'check_pending')

    def test_held_job_does_not_spin_and_can_be_cancelled(self):
        request=self.prepare();job=self.engine.authorize(request['request_id'],request['payload_hash'],'Scope')['jobs'][0]['job_id']
        self.engine.store.db.execute("UPDATE repeat_fingerprints SET status='pending' WHERE asset_id=?",(self.asset,))
        with patch.object(self.engine.repeat,'fingerprint',side_effect=UserError('Unavailable')):
            result=self.engine.execute(job)
        self.assertEqual(result['state'],'repeat_hold')
        self.assertEqual(self.engine.tick()['results'],[])
        self.assertEqual(self.engine.cancel_job(job)['state'],'cancelled')
        self.assertIsNone(self.engine.store.work_job(self.asset,self.destination))

    def test_backup_restore_retains_work_cache_and_review_decisions(self):
        from social_video_ops.backup import create_backup,restore_backup
        self.publish();alias=self.reencoded();self.engine.repeat.check(alias)
        self.engine.repeat.review(alias,'same',self.asset,'Confirmed same')
        backup=self.root/'backup.zip';create_backup(self.engine,backup)
        restored=self.root/'restored';restore_backup(backup,restored)
        engine=Engine(Workspace(restored),self.provider)
        try:
            self.assertEqual(engine.store.work_id(alias),self.asset)
            self.assertTrue(engine.repeat.cached_data(engine.store.one('SELECT * FROM repeat_fingerprints WHERE asset_id=?',(alias,)),engine.store.require_asset(alias)))
            self.assertEqual(engine.store.work_job(alias,self.destination)['state'],'verified')
            self.assertEqual(engine.store.meta('restore_reconciliation_required'),'1')
        finally:engine.close()

    def test_real_shared_intro_and_same_style_distinct_footage_pass(self):
        self.publish()
        distinct=self.root/'different.mp4'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(self.source),'-vf','hflip,hue=h=100','-c:v','libx264','-y',str(distinct)],check=True)
        other=self.engine.ingest(distinct)['asset']['id']
        self.assertEqual(self.engine.repeat.check(other)['status'],'clear')
        intro=self.root/'intro.mp4'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(self.source),'-i',str(distinct),'-filter_complex','[0:v]trim=duration=1,setpts=PTS-STARTPTS[a];[1:v]trim=start=1,setpts=PTS-STARTPTS[b];[a][b]concat=n=2:v=1:a=0[out]','-map','[out]','-c:v','libx264','-y',str(intro)],check=True)
        shared=self.engine.ingest(intro)['asset']['id']
        result=self.engine.repeat.check(shared)
        self.assertEqual(result['status'],'clear',result)

    def test_final_create_gate_catches_change_during_upload(self):
        request=self.prepare();job=self.engine.authorize(request['request_id'],request['payload_hash'],'Scope')['jobs'][0]['job_id']
        upload=self.provider.upload
        def change(path):
            url=upload(path)
            self.engine.store.db.execute("UPDATE repeat_fingerprints SET status='pending' WHERE asset_id=?",(self.asset,))
            return url
        with patch.object(self.provider,'upload',side_effect=change): result=self.engine.execute(job)
        self.assertEqual(result['state'],'repeat_hold');self.assertEqual(self.provider.created,[])
        self.assertIsNone(self.engine.store.one('SELECT submitted FROM jobs WHERE id=?',(job,))['submitted'])

    def test_pending_unknown_attempt_reconciles_despite_new_repeat_hold(self):
        request=self.prepare();job=self.engine.authorize(request['request_id'],request['payload_hash'],'Scope')['jobs'][0]['job_id']
        self.provider.fail_response_once=True
        self.assertEqual(self.engine.execute(job)['state'],'outcome_unknown')
        self.engine.store.db.execute("INSERT INTO repeat_holds VALUES(?, 'needs_review','{}','now')",(self.asset,))
        self.assertIsNotNone(self.engine.store.work_job(self.asset,self.destination))
        self.assertEqual(self.engine.execute(job)['state'],'verified')
        self.assertEqual(len(self.provider.created),1)

    def test_panel_is_bounded_and_embeds_matches_outside_nine(self):
        from social_video_ops.util import now
        self.publish();alias=self.reencoded();self.engine.repeat.check(alias)
        # Nine newer distinct work identities displace the actual match from the grid.
        original=self.engine.store.one('SELECT * FROM jobs WHERE asset_id=?',(self.asset,))
        for i in range(9):
            identity='fixture-'+str(i)
            asset=self.engine.store.require_asset(self.asset)
            self.engine.store.db.execute('INSERT INTO assets(id,hash,title,path,created) VALUES(?,?,?,?,?)',(identity,'fixturehash'+str(i),'Tile '+str(i),asset['path'],now()))
            self.engine.store.work_id(identity)
            columns=list(original);values=[original[c] for c in columns]
            for key,value in {'id':'job-'+identity,'asset_id':identity,'idempotency_key':'key-'+identity,'updated':'2099-01-01T00:00:00+00:00'}.items():values[columns.index(key)]=value
            self.engine.store.db.execute('INSERT INTO jobs('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',values)
        with patch.object(self.engine.repeat,'index',return_value={'results':[]}) as index,patch.object(self.engine.repeat,'fingerprint',wraps=self.engine.repeat.fingerprint) as extract:
            panel=self.engine.repeat.panel(alias)
        self.assertEqual(index.call_count,1);self.assertEqual(extract.call_count,1)
        self.assertEqual(panel['unique_works'],9);self.assertEqual(panel['status'],'check_pending')
        legend=__import__('json').loads(Path(panel['legend']).read_text())
        self.assertNotIn(self.asset,[t['work_id'] for t in legend['tiles']])
        # Incomplete unrelated history is deliberately held. Verify match evidence
        # rendering separately with the authoritative comparison result available.
        check={'matches':[{'work_id':self.asset,'asset_id':self.asset,'source_hash':asset['hash']}],'status':'needs_review'}
        with patch.object(self.engine.repeat,'check',return_value=check): panel=self.engine.repeat.panel(alias)
        legend=__import__('json').loads(Path(panel['legend']).read_text())
        self.assertIn(self.asset,legend['matched_evidence'])
        svg=Path(panel['panel']).read_text();self.assertEqual(svg.count('Match '+self.asset[:12]),3)

    def test_unrecorded_visual_opt_out_does_not_bypass_guard(self):
        config=self.workspace.config;config['repeat_guard']['enabled']=False;self.workspace.save(config)
        result=self.prepare()
        self.assertEqual(result['status'],'check_pending')
        self.assertEqual(result['repeat']['reason'],'record_visual_check_choice')
        self.assertEqual(self.provider.created,[])

    def test_readiness_and_recorded_disable_policy(self):
        self.publish();self.engine.store.db.execute("UPDATE repeat_fingerprints SET status='pending'")
        self.assertEqual(self.engine.repeat.readiness()['status'],'check_pending')
        with self.assertRaises(UserError):self.engine.repeat.set_policy(False,'')
        self.engine.repeat.set_policy(False,'Customer chose exact-byte protection only')
        self.assertTrue(self.engine.repeat.readiness()['recorded_choice'])
        self.assertEqual(self.prepare()['status'],'already_covered_or_reserved')

    def test_active_claims_survive_reopen_and_confirmed_failure_releases(self):
        request=self.prepare();job=self.engine.authorize(request['request_id'],request['payload_hash'],'Scope')['jobs'][0]['job_id']
        for state in ('prepared','scheduled','provider_draft','outcome_unknown','verification_pending','cancel_pending'):
            self.engine.store.db.execute('UPDATE jobs SET state=? WHERE id=?',(state,job))
            reopened=Store(self.workspace.path/'state.sqlite3')
            try:self.assertEqual(reopened.work_job(self.asset,self.destination)['id'],job)
            finally:reopened.close()
        self.engine.store.db.execute("UPDATE jobs SET state='prepared' WHERE id=?",(job,))
        create=self.provider.create
        def failed(body,key):
            result=create(body,key);post=result['post'];post['status']='failed'
            post['platforms'][0]={'platform':'instagram','accountId':'ig','status':'failed'}
            return result
        with patch.object(self.provider,'create',side_effect=failed):
            self.assertEqual(self.engine.execute(job)['state'],'failed')
        self.assertIsNone(self.engine.store.work_job(self.asset,self.destination))
        reopened=Store(self.workspace.path/'state.sqlite3')
        try:self.assertIsNone(reopened.work_job(self.asset,self.destination))
        finally:reopened.close()

    def test_uncertain_failed_receipt_retains_claim(self):
        request=self.prepare();job=self.engine.authorize(request['request_id'],request['payload_hash'],'Scope')['jobs'][0]['job_id']
        create=self.provider.create
        def ambiguous(body,key):
            result=create(body,key);post=result['post'];post['status']='failed';post['platforms'][0]['status']='failed'
            return result
        with patch.object(self.provider,'create',side_effect=ambiguous):
            self.assertEqual(self.engine.execute(job)['state'],'failed')
        self.assertIsNotNone(self.engine.store.work_job(self.asset,self.destination))
        alias=self.reencoded();self.engine.repeat.check(alias)
        with self.assertRaises(UserError):self.engine.repeat.review(alias,'same',self.asset,'Review cannot erase uncertain publication evidence')

    def test_unrelated_work_progresses_while_another_job_is_held(self):
        first=self.prepare();held=self.engine.authorize(first['request_id'],first['payload_hash'],'Scope')['jobs'][0]['job_id']
        self.engine.store.db.execute("UPDATE jobs SET state='repeat_hold' WHERE id=?",(held,))
        other=self.root/'unrelated.mp4'
        subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(self.source),'-vf','hflip,hue=h=100','-c:v','libx264','-y',str(other)],check=True)
        asset=self.engine.ingest(other)['asset']['id'];request=self.prepare(asset)
        job=self.engine.authorize(request['request_id'],request['payload_hash'],'Scope')['jobs'][0]['job_id']
        result=self.engine.tick()
        self.assertTrue(any(r['job_id']==job and r['state']=='verified' for r in result['results']),result)
        self.assertEqual(self.engine.store.one('SELECT state FROM jobs WHERE id=?',(held,))['state'],'repeat_hold')

    def test_exact_history_survives_visual_window(self):
        self.publish();self.engine.store.db.execute("UPDATE jobs SET updated='2000-01-01T00:00:00+00:00'")
        renamed=self.root/'old-copy.mp4';shutil.copy2(self.source,renamed)
        self.assertTrue(self.engine.ingest(renamed)['duplicate'])
        self.assertEqual(self.prepare()['status'],'already_covered_or_reserved')
        self.assertEqual(len(self.provider.created),1)

    def test_multiple_history_assets_index_in_resumable_small_batches(self):
        self.engine.repeat.set_policy(False,'Build pending fixture library without visual processing')
        for crf in (18,22,26,30):
            target=self.root/('encoding-'+str(crf)+'.mp4')
            subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(self.source),'-c:v','libx264','-crf',str(crf),'-y',str(target)],check=True)
            asset=self.engine.ingest(target)['asset']['id'];request=self.prepare(asset)
            self.engine.authorize(request['request_id'],request['payload_hash'],'Scope')
        self.engine.repeat.set_policy(True,'Enable repeat comparison after fixture preparation')
        self.assertEqual(len(self.engine.repeat.index(limit=2)['results']),2)
        self.assertEqual(len(self.engine.repeat.index(limit=2)['results']),2)
        self.assertEqual(self.engine.repeat.index(limit=2)['results'],[])
        self.assertEqual(self.provider.created,[])

    def test_schema_one_migration_preserves_actual_job_and_receipt(self):
        job=self.publish();before=self.engine.store.one('SELECT * FROM jobs WHERE id=?',(job,))
        with self.engine.store.transaction():
            for table in ('repeat_source_actions','repeat_holds','repeat_decisions','repeat_fingerprints','work_destination_claims','work_members','works'):
                self.engine.store.db.execute('DROP TABLE '+table)
            self.engine.store.set_meta('schema','1')
        migrated=Store(self.workspace.path/'state.sqlite3')
        try:
            self.assertEqual(migrated.one('SELECT * FROM jobs WHERE id=?',(job,)),before)
            self.assertEqual(migrated.work_job(self.asset,self.destination)['receipt'],before['receipt'])
            self.assertTrue(migrated.coverage(self.asset)['complete'])
        finally:migrated.close()

    def duplicate_source(self):
        self.publish()
        config=self.workspace.config;config['drive']['enabled']=True;config['drive']['folders']={'ready':'ready','repeat_review':'review','posted':'posted'};self.workspace.save(config)
        self.engine.store.db.execute("UPDATE assets SET drive_id='canonical',drive_parent='posted' WHERE id=?",(self.asset,))
        self.engine.store.db.execute("INSERT INTO asset_sources VALUES(?,'gdrive','canonical','posted','1','now')",(self.asset,))
        self.engine.store.db.execute("INSERT INTO asset_sources VALUES(?,'gdrive','extra','ready','1','now')",(self.asset,))
        checksum=file_hash(Path(self.engine.store.require_asset(self.asset)['path']),'md5')
        class FakeDrive:
            def __init__(self):self.parent='ready';self.moves=[];self.fail=False;self.modified=False
            def metadata(self,identity):
                return {'id':identity,'parents':[self.parent],'version':'2' if self.modified else '1','md5Checksum':checksum,'trashed':False}
            def move(self,identity,parent,target):
                self.moves.append((identity,parent,target));self.parent=target
                if self.fail:self.fail=False;raise UserError('Uncertain network outcome')
                return self.metadata(identity)
        return FakeDrive()

    def test_drive_uncertain_move_reconciles_and_restore_keeps_receipts(self):
        drive=self.duplicate_source();drive.fail=True
        with patch('social_video_ops.drive.Drive',return_value=drive):
            self.assertEqual(self.engine.repeat.sync_sources()['results'][0]['state'],'filing_pending')
            self.assertEqual(self.engine.repeat.sync_sources()['results'][0]['state'],'filed')
            self.assertEqual(len(drive.moves),1)
            self.assertEqual(self.engine.repeat.sync_sources(restore='extra')['results'][0]['state'],'restored')
        self.assertEqual(drive.parent,'ready');self.assertEqual(len(self.provider.created),1)
        self.assertEqual(self.engine.store.work_job(self.asset,self.destination)['state'],'verified')
        self.assertEqual([x[0] for x in drive.moves],['extra','extra'])

    def test_interrupted_restore_resumes_automatically_without_second_move(self):
        drive=self.duplicate_source()
        with patch('social_video_ops.drive.Drive',return_value=drive):
            self.engine.repeat.sync_sources();drive.fail=True
            self.assertEqual(self.engine.repeat.sync_sources(restore='extra')['results'][0]['state'],'filing_pending')
            self.assertEqual(self.engine.repeat.sync_sources()['results'][0]['state'],'restored')
        self.assertEqual(len(drive.moves),2);self.assertEqual(drive.parent,'ready')

    def test_review_filing_waits_for_missing_authorized_account(self):
        drive=self.duplicate_source()
        self.engine.store.sync_accounts([{'_id':'new','platform':'instagram','profileId':'customer-profile','platformUserId':'native-new','isActive':True}],['customer-profile'],auto_enroll=True)
        with patch('social_video_ops.drive.Drive',return_value=drive):result=self.engine.repeat.sync_sources()
        self.assertEqual(result['results'][0]['state'],'waiting_for_authorized_accounts');self.assertEqual(drive.moves,[])

    def test_drive_changed_checksum_is_not_moved(self):
        drive=self.duplicate_source();metadata=drive.metadata
        with patch.object(drive,'metadata',side_effect=lambda identity:{**metadata(identity),'md5Checksum':'changed'}),patch('social_video_ops.drive.Drive',return_value=drive):
            self.assertEqual(self.engine.repeat.sync_sources()['results'][0]['state'],'filing_pending')
        self.assertEqual(drive.moves,[])

    def test_canonical_published_source_in_ready_is_protected(self):
        drive=self.duplicate_source()
        self.engine.store.db.execute("UPDATE asset_sources SET parent='ready' WHERE source_id='canonical'")
        self.engine.repeat.queue_sources()
        self.assertEqual([r['source_id'] for r in self.engine.store.rows('SELECT source_id FROM repeat_source_actions')],['extra'])

    def test_drive_changed_version_and_missing_folder_do_not_move(self):
        drive=self.duplicate_source();drive.modified=True
        with patch('social_video_ops.drive.Drive',return_value=drive):
            self.assertEqual(self.engine.repeat.sync_sources()['results'][0]['state'],'filing_pending')
        self.assertEqual(drive.moves,[])
        config=self.workspace.config;del config['drive']['folders']['repeat_review'];self.workspace.save(config)
        self.assertEqual(self.engine.repeat.sync_sources()['state'],'needs_repeat_review_folder')

    def test_normal_archive_sync_does_not_move_review_source(self):
        drive=self.duplicate_source()
        self.engine.repeat.queue_sources()
        with patch('social_video_ops.drive.Drive',return_value=drive):
            result=self.engine.sync_drive()
        self.assertEqual(drive.moves,[])
        self.assertTrue(any(r['state']=='repeat_review_filing' for r in result['results']))

    def test_local_input_has_no_repeat_source_actions(self):
        self.publish()
        self.engine.repeat.queue_sources()
        self.assertEqual(self.engine.store.rows('SELECT * FROM repeat_source_actions'),[])

    def test_index_batch_is_bounded_and_resumes(self):
        self.publish()
        self.engine.store.db.execute("UPDATE repeat_fingerprints SET status='pending'")
        first=self.engine.repeat.index(limit=1)
        self.assertEqual(len(first['results']),1)
        self.assertEqual(self.engine.repeat.index(limit=1)['results'],[])


class RepeatMigration(unittest.TestCase):
    def test_schema_one_migrates_without_losing_assets(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'state.sqlite3';db=sqlite3.connect(path)
            db.executescript(SCHEMA);db.execute("INSERT INTO meta VALUES('schema','1')")
            db.execute("INSERT INTO assets(id,hash,title,path,created) VALUES('a','hash','title','original','2026-10-03')");db.commit();db.close()
            store=Store(path)
            try:
                self.assertEqual(store.meta('schema'),'2');self.assertEqual(store.work_id('a'),'a')
                self.assertEqual(store.require_asset('a')['path'],'original')
            finally:store.close()
    def test_migration_failure_rolls_back_version_and_new_tables(self):
        import social_video_ops.store as module
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'state.sqlite3';db=sqlite3.connect(path);db.executescript(SCHEMA)
            db.execute("INSERT INTO meta VALUES('schema','1')");db.commit();db.close()
            with patch.object(module,'REPEAT_SCHEMA',module.REPEAT_SCHEMA+'SELECT missing_column;'):
                with self.assertRaises(sqlite3.OperationalError):Store(path)
            db=sqlite3.connect(path)
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0],'1')
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='work_members'").fetchone())
            db.close()

    def test_unknown_schema_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'state.sqlite3';db=sqlite3.connect(path);db.executescript(SCHEMA)
            db.execute("INSERT INTO meta VALUES('schema','999')");db.commit();db.close()
            with self.assertRaises(UserError):Store(path)
            db=sqlite3.connect(path);self.assertEqual(db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0],'999');db.close()
    def test_phash_rejects_invalid_frames(self):
        with self.assertRaises(UserError):phash(b'bad')
        self.assertEqual(distance('0000000000000000','0000000000000001'),1)

if __name__=='__main__':unittest.main()
