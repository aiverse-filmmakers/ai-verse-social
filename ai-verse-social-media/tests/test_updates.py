import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('check_updates', Path(__file__).resolve().parents[1] / 'scripts/check_updates.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)

class UpdateChecks(unittest.TestCase):
    def test_silent_current_and_notify_once_per_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'LOCAL-SETUP.json').write_text(json.dumps({'revision': 'a'*40}))
            state = root/'private/status.json'
            def runner(*args, **kwargs):
                self.assertEqual(kwargs['timeout'], 30)
                self.assertEqual(kwargs['env']['GIT_TERMINAL_PROMPT'], '0')
                return subprocess.CompletedProcess(args, 0, 'a'*40+'\trefs/heads/main\n')
            self.assertIsNone(checker.check(root,state,runner))
            def changed(*args, **kwargs):
                return subprocess.CompletedProcess(args, 0, 'b'*40+'\trefs/heads/main\n')
            self.assertEqual(checker.check(root,state,changed)['status'], 'update_available')
            self.assertIsNone(checker.check(root,state,changed))
            self.assertEqual(json.loads(state.read_text())['available'], 'b'*40)
    def test_auth_failure_is_not_no_update_and_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'LOCAL-SETUP.json').write_text(json.dumps({'revision':'a'*40}))
            def failed(*args,**kwargs):
                return subprocess.CompletedProcess(args,128,'','secret-sensitive-error')
            state=root/'state.json'
            self.assertEqual(checker.check(root,state,failed)['status'],'needs_attention')
            self.assertIsNone(checker.check(root,state,failed))
            self.assertNotIn('secret-sensitive-error',state.read_text())
    def test_unknown_install_does_not_claim_current(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'LOCAL-SETUP.json').write_text('{}')
            self.assertEqual(checker.check(root,root/'state.json')['status'],'needs_attention')

if __name__ == '__main__':
    unittest.main()
