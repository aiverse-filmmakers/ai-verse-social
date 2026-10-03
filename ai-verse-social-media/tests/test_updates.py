import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/check_updates.py'
spec = importlib.util.spec_from_file_location('check_updates', SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)

class UpdateChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.skill = self.root/'skill'
        self.skill.mkdir()
        (self.skill/'LOCAL-SETUP.json').write_text(json.dumps({'revision': 'a'*40}))
        self.state = self.root/'private/update-check.json'
        self.remote = 'a'*40
        self.calls = []

    def runner(self, command, **kwargs):
        self.calls.append(command)
        self.assertLessEqual(kwargs['timeout'], 30)
        self.assertGreater(kwargs['timeout'], 0)
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(kwargs['env']['GIT_TERMINAL_PROMPT'], '0')
        self.assertEqual(kwargs['env']['GH_PROMPT_DISABLED'], '1')
        return subprocess.CompletedProcess(command, 0, self.remote+'\trefs/heads/main\n')

    def check(self, runner=None, executable=None):
        return checker.check(self.skill, self.state, runner or self.runner, executable or (lambda _: None))

    def ack(self, events):
        checker.acknowledge(self.state, [x['event_id'] for x in events], 'host-message:123')

    def test_current_is_silent(self):
        self.assertEqual(self.check(), [])
        self.assertEqual(json.loads(self.state.read_text())['status'], 'current')

    def test_undelivered_notice_retries_until_confirmed(self):
        self.remote = 'b'*40
        events = self.check()
        self.assertEqual(events[0]['status'], 'update_available')
        self.assertEqual(self.check(), events)
        self.ack(events)
        self.assertEqual(self.check(), [])
        self.remote = 'c'*40
        self.assertEqual(self.check()[0]['available'], self.remote)

    def test_ack_is_idempotent_and_has_no_network(self):
        self.remote = 'b'*40
        events = self.check()
        calls = len(self.calls)
        self.ack(events); self.ack(events)
        self.assertEqual(len(self.calls), calls)
        state = json.loads(self.state.read_text())
        self.assertEqual(state['pending'], [])
        self.assertEqual(len(state['acknowledged']), 1)

    def test_ack_requires_known_event_and_delivery_reference(self):
        self.check()
        with self.assertRaises(checker.CheckError):
            checker.acknowledge(self.state,['made-up'],'receipt')
        with self.assertRaises(checker.CheckError):
            checker.acknowledge(self.state,[],None)

    def failed(self, command, **kwargs):
        return subprocess.CompletedProcess(command, 128, '', 'secret-sensitive-provider-error')

    def test_failure_is_reported_and_retried_until_ack(self):
        events = self.check(self.failed)
        self.assertEqual(events[0]['status'], 'needs_attention')
        self.assertEqual(self.check(self.failed), events)
        self.ack(events)
        self.assertEqual(self.check(self.failed), [])
        self.assertNotIn('secret-sensitive-provider-error', self.state.read_text())

    def test_recovery_does_not_repeat_acknowledged_update(self):
        self.remote = 'b'*40
        self.ack(self.check())
        self.ack(self.check(self.failed))
        self.assertEqual(self.check(), [])

    def test_new_outage_can_alert_after_healthy_check(self):
        events = self.check(self.failed)
        self.ack(events)
        self.check()
        new = self.check(self.failed)
        self.assertNotEqual(events[0]['event_id'], new[0]['event_id'])

    def test_new_revision_retires_obsolete_pending_revision(self):
        self.remote = 'b'*40; self.check()
        self.remote = 'c'*40
        events = self.check()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['available'], self.remote)

    def test_updated_install_retires_update_notice(self):
        self.remote = 'b'*40; self.check()
        (self.skill/'LOCAL-SETUP.json').write_text(json.dumps({'revision': self.remote}))
        self.assertEqual(self.check(), [])

    def test_malformed_setup_is_sanitised_blocker(self):
        for raw in ('[]', '{broken', '{"revision": ["private-data"]}', '{"revision":"not-a-sha"}'):
            with self.subTest(raw=raw):
                (self.skill/'LOCAL-SETUP.json').write_text(raw)
                events = self.check()
                self.assertEqual(events[0]['status'], 'needs_attention')
                self.assertNotIn('private-data', self.state.read_text())
                self.assertEqual(self.calls, [])

    def test_corrupt_status_is_preserved_and_reported(self):
        self.state.parent.mkdir()
        self.state.write_text('{damaged')
        events = self.check()
        preserved = list(self.state.parent.glob('*.corrupt-*'))
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_text(), '{damaged')
        self.assertEqual(events[0]['code'], 'state_recovered')
        self.ack(events)
        self.assertEqual(self.check(), [])

    def test_invalid_status_shape_is_preserved(self):
        self.state.parent.mkdir()
        self.state.write_text('{"schema":2,"pending":"wrong","outage":0,"acknowledged":[]}')
        self.assertEqual(self.check()[0]['code'], 'state_recovered')

    def test_future_schema_is_not_overwritten(self):
        self.state.parent.mkdir()
        content = '{"schema":999}'
        self.state.write_text(content)
        with self.assertRaises(checker.CheckError): self.check()
        self.assertEqual(self.state.read_text(), content)

    def test_old_observation_state_is_not_assumed_delivered(self):
        self.state.parent.mkdir()
        self.state.write_text('{"notification_key":"update:old"}')
        self.remote = 'b'*40
        self.assertEqual(self.check()[0]['status'], 'update_available')

    def test_concurrent_run_skips_without_network(self):
        with checker.state_lock(self.state) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.check(), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.check(), [])
        self.assertEqual(len(self.calls), 1)

    def test_symlink_state_is_not_overwritten(self):
        self.state.parent.mkdir()
        other = self.root/'important'
        other.write_text('keep'); self.state.symlink_to(other)
        with self.assertRaises(checker.CheckError): self.check()
        self.assertEqual(other.read_text(), 'keep')

    def test_private_gh_api_works_without_git_helper(self):
        self.remote = 'b'*40
        def gh(command, **kwargs):
            self.assertEqual(command[0], 'gh')
            self.assertIn('github.com', command)
            return subprocess.CompletedProcess(command, 0, self.remote+'\n')
        self.assertEqual(self.check(gh,lambda _: '/installed/gh')[0]['available'], self.remote)

    def test_invalid_gh_auth_falls_back_to_public_git(self):
        commands = []
        def fallback(command, **kwargs):
            commands.append(command[0])
            return self.failed(command,**kwargs) if command[0]=='gh' else self.runner(command,**kwargs)
        self.assertEqual(self.check(fallback,lambda _: '/installed/gh'), [])
        self.assertEqual(commands, ['gh','git'])

    def test_timeout_and_invalid_remote_never_claim_current(self):
        def timeout(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs['timeout'], stderr='secret')
        def bad_output(command, **kwargs):
            return subprocess.CompletedProcess(command,0,'garbage\n')
        for runner in (timeout,bad_output):
            with self.subTest(runner=runner):
                self.assertEqual(self.check(runner)[0]['status'],'needs_attention')
        self.assertNotIn('secret', self.state.read_text())

    def test_cli_gate_skips_ai_and_ack_runs_without_checking(self):
        with patch.object(checker, 'check', return_value=[]), patch.object(sys, 'argv', ['check','--state',str(self.state),'--hermes-gate']), patch('sys.stdout') as stdout:
            self.assertEqual(checker.main(),0)
            output = ''.join(x.args[0] for x in stdout.write.call_args_list)
            self.assertFalse(json.loads(output)['wakeAgent'])
        with patch.object(checker,'acknowledge') as ack, patch.object(checker,'check') as check, patch.object(sys,'argv',['check','--state',str(self.state),'--ack','event','--delivery-receipt','receipt']):
            self.assertEqual(checker.main(),0);ack.assert_called_once();check.assert_not_called()

    def test_unwritable_status_has_sanitised_cli_error(self):
        with patch.object(checker,'check',side_effect=PermissionError('private-secret-path')), patch.object(sys,'argv',['check','--state',str(self.state)]), patch('sys.stdout') as stdout:
            self.assertEqual(checker.main(),2)
            output = ''.join(x.args[0] for x in stdout.write.call_args_list)
            self.assertNotIn('private-secret-path',output)
            self.assertFalse(json.loads(output)['acknowledgeable'])

    def test_network_fallback_shares_one_total_time_budget(self):
        calls=[]
        def timeout(command, **kwargs):
            calls.append(command[0])
            raise subprocess.TimeoutExpired(command,kwargs['timeout'])
        with patch('time.monotonic',side_effect=[100,100,131]):
            with self.assertRaises(checker.CheckError):
                checker.network_revision(timeout,lambda _: '/installed/gh')
        self.assertEqual(calls,['gh'])

    @unittest.skipIf(os.name == 'nt','POSIX fake executable for CLI smoke')
    def test_real_cli_stdout_silence_gate_and_ack(self):
        import shutil
        scripts=self.skill/'scripts';scripts.mkdir()
        copied=scripts/'check_updates.py';shutil.copy2(SCRIPT,copied)
        bins=self.root/'bin';bins.mkdir()
        git=bins/'git';git.write_text('#!/bin/sh\necho "'+self.remote+' refs/heads/main"\n');git.chmod(0o700)
        env=dict(os.environ,PATH=str(bins))
        def run(*args):
            return subprocess.run([sys.executable,str(copied),'--state',str(self.state),*args],capture_output=True,text=True,env=env)
        result=run();self.assertEqual(result.returncode,0);self.assertEqual(result.stdout,'');self.assertEqual(result.stderr,'')
        result=run('--hermes-gate');self.assertFalse(json.loads(result.stdout)['wakeAgent'])
        git.write_text('#!/bin/sh\necho "'+'b'*40+' refs/heads/main"\n')
        result=run();event=json.loads(result.stdout);self.assertEqual(event['status'],'update_available')
        result=run('--ack',event['event_id'],'--delivery-receipt','offline-delivery:1')
        self.assertEqual(result.returncode,0);self.assertEqual(result.stdout,'')
        self.assertEqual(run().stdout,'')

    def test_state_permissions_and_ack_history_are_bounded(self):
        self.remote = 'b'*40
        events = self.check(); self.ack(events)
        if os.name!='nt': self.assertEqual(self.state.stat().st_mode & 0o777,0o600)
        state = json.loads(self.state.read_text())
        state['acknowledged'] = ['update:'+str(i) for i in range(128)]
        state['pending'] = events
        checker.save_state(self.state,state)
        self.ack(events)
        self.assertEqual(len(json.loads(self.state.read_text())['acknowledged']),128)

if __name__ == '__main__':
    unittest.main()
