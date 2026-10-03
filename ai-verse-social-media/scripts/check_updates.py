#!/usr/bin/env python3
"""Bounded read-only GitHub revision check. Silent when unchanged; never installs."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile

REPO = 'https://github.com/aiverse-filmmakers/ai-verse-social.git'


def check(skill, state_path, runner=subprocess.run):
    try:
        setup = json.loads((skill / 'LOCAL-SETUP.json').read_text())
        installed = setup.get('revision')
        if not installed:
            raise ValueError('Installed revision is unknown. Reinstall with the official installer before enabling update checks.')
        env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
        result = runner(['git', 'ls-remote', '--exit-code', REPO, 'refs/heads/main'],
                        capture_output=True, text=True, timeout=30, env=env)
        if result.returncode:
            raise ValueError('Cannot check GitHub. Verify internet access and repository authentication for the scheduled worker.')
        lines = result.stdout.strip().splitlines()
        if len(lines) != 1:
            raise ValueError('GitHub returned an unexpected branch response.')
        revision, ref = lines[0].split()
        if ref != 'refs/heads/main' or len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
            raise ValueError('GitHub returned an invalid branch revision.')
        event = {'status': 'current' if revision == installed else 'update_available',
                 'installed': installed, 'available': revision}
        key = None if event['status'] == 'current' else 'update:' + revision
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        message = str(error) if isinstance(error, ValueError) else 'Update check could not run. Verify Git, installation metadata and scheduler access.'
        event = {'status': 'needs_attention', 'message': message}
        key = 'error:' + message
    previous = {}
    if state_path.exists():
        previous = json.loads(state_path.read_text())
    state = {'checked_at': datetime.now(timezone.utc).isoformat(), 'notification_key': key, **event}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=state_path.parent, delete=False) as handle:
        json.dump(state, handle, indent=2)
        temporary = handle.name
    os.replace(temporary, state_path)
    if key and key != previous.get('notification_key'):
        return event
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True, help='Private workspace update-check.json path')
    args = parser.parse_args()
    event = check(Path(__file__).resolve().parents[1], args.state.expanduser())
    if event:
        print(json.dumps(event))


if __name__ == '__main__':
    main()
