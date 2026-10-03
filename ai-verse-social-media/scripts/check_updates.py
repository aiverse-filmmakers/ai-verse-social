#!/usr/bin/env python3
"""Read-only weekly revision check with a durable notification outbox."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

REPO_NAME = 'aiverse-filmmakers/ai-verse-social'
REPO = f'https://github.com/{REPO_NAME}.git'
SHA = re.compile(r'^[0-9a-f]{40}$')
MAX_JSON_BYTES = 1024 * 1024


class CheckError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def read_object(path):
    if path.is_symlink():
        raise CheckError('invalid_file', 'Update metadata must be a regular private file, not a symlink.')
    try:
        with path.open('rb') as handle:
            raw = handle.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise CheckError('invalid_json', 'Update metadata is damaged or invalid; repair the installation or private status file.') from None


@contextmanager
def state_lock(path):
    """OS releases this nonblocking lock even if the checker crashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + '.lock')
    if lock.is_symlink():
        raise CheckError('invalid_file', 'The update-check lock cannot be a symlink.')
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'r+b') as handle:
        acquired = False
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b'\0'); handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    pass
            yield acquired
        finally:
            if acquired:
                if os.name == 'nt':
                    handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def blank_state():
    return {'schema': 2, 'acknowledged': [], 'pending': [], 'outage': 0}


def load_state(path):
    if not path.exists():
        return blank_state(), False
    state = read_object(path)
    if 'schema' not in state and 'notification_key' in state:
        # Old checker tracked observation, not delivery: do not infer acknowledgement.
        return blank_state(), False
    if state.get('schema') != 2:
        raise CheckError('state_version', 'The update status format is unsupported; use a compatible checker.')
    if (not isinstance(state.get('pending'), list) or len(state['pending']) > 16 or
            not isinstance(state.get('acknowledged'), list) or len(state['acknowledged']) > 128 or
            any(not isinstance(x, str) or len(x) > 100 for x in state['acknowledged']) or
            type(state.get('outage')) is not int or not 0 <= state['outage'] < 10**9):
        raise CheckError('invalid_json', 'The update status data is damaged.')
    for event in state['pending']:
        if (not isinstance(event, dict) or not isinstance(event.get('event_id'), str) or
                len(event['event_id']) > 100 or event.get('status') not in ('update_available', 'needs_attention')):
            raise CheckError('invalid_json', 'The update notification data is damaged.')
    return state, False


def save_state(path, state):
    if path.is_symlink():
        raise CheckError('invalid_file', 'The private update status cannot be a symlink.')
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as handle:
            name = handle.name
            json.dump(state, handle, indent=2)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def network_revision(runner, executable=shutil.which):
    """One total 30-second network budget across gh and Git fallback."""
    import time
    deadline = time.monotonic() + 30
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never',
               GH_PROMPT_DISABLED='1', GIT_ASKPASS='', SSH_ASKPASS='')
    commands = []
    if executable('gh'):
        commands.append(['gh', 'api', '--hostname', 'github.com',
                         f'repos/{REPO_NAME}/git/ref/heads/main', '--jq', '.object.sha'])
    commands.append(['git', '-c', 'credential.interactive=false', 'ls-remote', '--exit-code', REPO, 'refs/heads/main'])
    for command in commands:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            result = runner(command, capture_output=True, text=True, timeout=remaining,
                            stdin=subprocess.DEVNULL, env=env)
            if result.returncode:
                continue
            if command[0] == 'gh':
                revision = result.stdout.strip()
            else:
                pieces = result.stdout.strip().split()
                if len(pieces) != 2 or pieces[1] != 'refs/heads/main':
                    continue
                revision = pieces[0]
            if SHA.fullmatch(revision):
                return revision
        except (OSError, subprocess.SubprocessError):
            continue
    raise CheckError('repository_access', 'Cannot check GitHub. Verify network access and private-repo authentication in the scheduled worker.')


def add_pending(state, event):
    if event['event_id'] in state['acknowledged'] or any(x['event_id'] == event['event_id'] for x in state['pending']):
        return
    state['pending'].append(event)


def check(skill, state_path, runner=subprocess.run, executable=shutil.which):
    with state_lock(state_path) as acquired:
        if not acquired:
            return []
        try:
            state, _ = load_state(state_path)
        except CheckError as error:
            if error.code != 'invalid_json':
                raise
            # Preserve corrupt bytes; never overwrite the only evidence of damage.
            state_path.rename(state_path.with_name(state_path.name + '.corrupt-' + uuid.uuid4().hex))
            state = blank_state()
            add_pending(state, {'event_id': 'error:state_recovered', 'status': 'needs_attention',
                                'code': 'state_recovered', 'message': 'Damaged update status was preserved and reset. Earlier notification acknowledgements may need review.'})
        try:
            setup = read_object(skill / 'LOCAL-SETUP.json')
            installed = setup.get('revision')
            if not isinstance(installed, str) or not SHA.fullmatch(installed):
                raise CheckError('unknown_revision', 'Installed revision is unknown. Reinstall from the official clean repository before enabling update checks.')
            revision = network_revision(runner, executable)
            state.update(status='current' if revision == installed else 'update_available',
                         installed=installed, available=revision)
            # Retire obsolete updates and resolved connection/setup errors.
            state['pending'] = [x for x in state['pending'] if x.get('code') == 'state_recovered' or
                                (x['status'] == 'update_available' and x.get('available') == revision and revision != installed)]
            if revision != installed:
                add_pending(state, {'event_id': 'update:' + revision, 'status': 'update_available',
                                    'installed': installed, 'available': revision})
        except (CheckError, OSError) as error:
            if state.get('status') != 'needs_attention':
                state['outage'] += 1
            code = error.code if isinstance(error, CheckError) else 'installation_access'
            message = error.message if isinstance(error, CheckError) else 'Cannot read installation metadata. Verify the installed skill path and scheduler permissions.'
            state['status'] = 'needs_attention'
            state['pending'] = [x for x in state['pending'] if x['status'] != 'needs_attention' or x.get('code') == 'state_recovered']
            add_pending(state, {'event_id': f'error:{state["outage"]}:{code}', 'status': 'needs_attention',
                                'code': code, 'message': message})
        state['checked_at'] = datetime.now(timezone.utc).isoformat()
        save_state(state_path, state)
        return list(state['pending'])


def acknowledge(state_path, event_ids, receipt):
    if not receipt or len(receipt) > 256 or '\n' in receipt or '\r' in receipt:
        raise CheckError('delivery_receipt', 'Acknowledgement needs a nonsecret delivery receipt or message ID.')
    with state_lock(state_path) as acquired:
        if not acquired:
            raise CheckError('busy', 'Another update check is active; try acknowledgement again later.')
        state, _ = load_state(state_path)
        known = set(state['acknowledged']) | {x['event_id'] for x in state['pending']}
        if any(event not in known for event in event_ids):
            raise CheckError('unknown_event', 'Cannot acknowledge an unknown update notification.')
        for event in event_ids:
            if event not in state['acknowledged']:
                state['acknowledged'].append(event)
        state['acknowledged'] = state['acknowledged'][-128:]
        state['pending'] = [x for x in state['pending'] if x['event_id'] not in event_ids]
        state['last_delivery'] = {'at': datetime.now(timezone.utc).isoformat(), 'receipt': receipt, 'events': event_ids}
        save_state(state_path, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True, help='Private workspace update-check.json path')
    parser.add_argument('--ack', action='append', default=[], metavar='EVENT_ID')
    parser.add_argument('--delivery-receipt', help='Nonsecret confirmed delivery reference; used with --ack')
    parser.add_argument('--hermes-gate', action='store_true', help='Pre-run gate: skip inference when no pending event exists')
    args = parser.parse_args()
    skill = Path(__file__).resolve().parents[1]
    state_path = args.state.expanduser().absolute()
    if state_path == skill or skill in state_path.parents:
        parser.error('Keep update status outside the installed skill, in the private workspace.')
    try:
        if args.ack:
            acknowledge(state_path, args.ack, args.delivery_receipt)
            return 0
        if args.delivery_receipt:
            parser.error('--delivery-receipt requires --ack')
        events = check(skill, state_path)
        if args.hermes_gate:
            print(json.dumps({'wakeAgent': bool(events), 'context': {'update_events': events}}))
        else:
            for event in events:
                print(json.dumps(event))
        return 0
    except (CheckError, OSError):
        # Filesystem/schema failures cannot safely be deduplicated without durable state.
        error = {'status': 'needs_attention', 'code': 'state_unavailable', 'acknowledgeable': False,
                 'message': 'Update status could not be safely read or saved. Repair private-file permissions or its format before resuming this job.'}
        print(json.dumps({'wakeAgent': True, 'context': {'update_events': [error]}}) if args.hermes_gate else json.dumps(error))
        return 2


if __name__ == '__main__':
    sys.exit(main())
