#!/usr/bin/env python3
"""Install the clean skill without overwriting customer settings or edited skills."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

HOSTS = ('hermes', 'claude-code', 'codex', 'openclaw')
NAME = 'ai-verse-social-media'
RECEIPT = '.ai-verse-install.json'


def host_root(host):
    home = Path.home()
    return {
        'hermes': Path(os.environ.get('HERMES_HOME', str(home / '.hermes'))) / 'skills',
        'claude-code': home / '.claude/skills',
        'codex': home / '.agents/skills',
        'openclaw': Path(os.environ.get('OPENCLAW_STATE_DIR', str(home / '.openclaw'))) / 'skills',
    }[host]


def inventory(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError(f'Symlink refused: {path}')
        if '__pycache__' in path.relative_to(root).parts or path.suffix == '.pyc':
            continue
        if path.is_file() and path.name != RECEIPT:
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def alias(name):
    return f'''---
name: {name}
description: Set up AI-Verse Social and check what is missing, one step at a time.
---

Find the installed `{NAME}` skill beside this skill. Read its SKILL.md and references/onboarding.md and follow that setup flow. Accept natural-language setup requests. Preserve existing accounts, settings and publication history. An audit does not authorize posting or creating schedules. If the main skill is absent, report the missing installation.
'''


def install(source, root, workspace, python, update=False):
    inventory(source)
    if not (source / 'SKILL.md').is_file() or not (source / 'scripts/social.py').is_file():
        raise ValueError('The complete AI-Verse skill package is missing.')
    root = root.expanduser().absolute()
    workspace = workspace.expanduser().absolute()
    if workspace == root or root in workspace.parents:
        raise ValueError('Keep the private workspace outside the skills directory.')
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ai-verse-stage-', dir=root) as staging:
        stage = Path(staging)
        shutil.copytree(source, stage / NAME)
        note = {'workspace': str(workspace), 'python': str(Path(python).absolute()),
                'entrypoint': str(root / NAME / 'scripts/social.py')}
        (stage / NAME / 'LOCAL-SETUP.json').write_text(json.dumps(note, indent=2) + '\n')
        names = [NAME, 'ai-verse-social-onboard', 'ai-verse-social-media-onboard', 'ai-verse-social-media-oboard']
        for name in names[1:]:
            (stage / name).mkdir()
            (stage / name / 'SKILL.md').write_text(alias(name))
        changes = []
        for name in names:
            target = root / name
            desired = inventory(stage / name)
            if target.is_symlink():
                raise ValueError(f'Existing symlink refused: {target}')
            if target.exists():
                receipt = target / RECEIPT
                if not receipt.is_file():
                    raise ValueError(f'Existing skill is not installer-owned: {target}. Choose another skills directory.')
                previous = json.loads(receipt.read_text())
                actual = inventory(target)
                if actual != previous.get('files'):
                    raise ValueError(f'Local changes found in {target}. Back up and resolve them before updating.')
                if actual == desired:
                    continue
                if not update:
                    raise ValueError('A different version is already installed. Run with --update to keep a backup and replace it.')
            (stage / name / RECEIPT).write_text(json.dumps({'files': desired}, indent=2) + '\n')
            changes.append(name)
        backups = root / '.ai-verse-backups'
        saved = {}
        committed = []
        try:
            for name in changes:
                target = root / name
                if target.exists():
                    backups.mkdir(exist_ok=True)
                    backup = Path(tempfile.mkdtemp(prefix=name + '-', dir=backups)) / name
                    target.rename(backup)
                    saved[name] = backup
                (stage / name).rename(target)
                committed.append(name)
        except Exception:
            for name in reversed(committed):
                shutil.rmtree(root / name)
            for name, backup in saved.items():
                backup.rename(root / name)
            raise
    return root / NAME


def main():
    parser = argparse.ArgumentParser(description='Install AI-Verse Social for your AI agent.')
    parser.add_argument('--host', choices=HOSTS)
    parser.add_argument('--skills-dir', type=Path)
    parser.add_argument('--workspace', type=Path, default=Path.home() / '.ai-verse-social/workspace')
    parser.add_argument('--update', action='store_true')
    args = parser.parse_args()
    if not args.host and not args.skills_dir:
        try:
            with open('/dev/tty', 'r+') as terminal:
                terminal.write('Where should AI-Verse Social be installed?\n1. Hermes\n2. Claude Code\n3. Codex\n4. OpenClaw\nChoose 1–4: ')
                terminal.flush()
                choice = terminal.readline().strip()
            if choice not in ('1', '2', '3', '4'):
                parser.error('Choose 1–4, or run again with --host hermes|claude-code|codex|openclaw.')
            args.host = HOSTS[int(choice) - 1]
        except OSError:
            parser.error('No interactive terminal. Supply --host or --skills-dir.')
    source = Path(__file__).resolve().parents[1] / NAME
    target = install(source, args.skills_dir or host_root(args.host), args.workspace, sys.executable, args.update)
    print(f'Installed AI-Verse Social: {target}')
    print('Reload your agent, then say: Set up AI-Verse Social.')
    print('Or use /ai-verse-social-onboard where your agent supports slash skills.')
    print('Setup checks Drive, Zernio, media tools and scheduling. No posts or jobs were started.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print(f'Installation stopped: {error}', file=sys.stderr)
        raise SystemExit(1)
