#!/usr/bin/env python3
"""Create and populate your private ATS data GitHub repository (owner/repo via --repo or ATS_DATA_REMOTE).

Run this on a machine holding a .ats clone with historical data.
Needs authenticated GitHub CLI (gh), Git and a clean target folder.
Never makes the original Agent-Team-Starter repository public.
"""
import argparse
import os
import shutil
import subprocess
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'data'
DEFAULT_TARGET = Path.home() / '.ats-data'


def run(*args, cwd=None, check=True):
    return subprocess.run(args, cwd=cwd, check=check, text=True,
                          capture_output=True)


def bootstrap(destination=DEFAULT_TARGET, source=SOURCE, repo=None):
    OWNER_REPO = (repo or os.environ.get('ATS_DATA_REMOTE', '')).strip()
    if not OWNER_REPO or OWNER_REPO.count('/') != 1:
        raise RuntimeError('Pass --repo OWNER/REPO or set ATS_DATA_REMOTE.')
    destination = Path(destination).expanduser().resolve()
    source = Path(source).expanduser().resolve()
    if not shutil.which('gh') or not shutil.which('git'):
        raise RuntimeError('Install and authenticate GitHub CLI (gh) and Git first.')
    view = run('gh', 'repo', 'view', OWNER_REPO, '--json', 'visibility',
               '--jq', '.visibility', check=False)
    if view.returncode == 0:
        if view.stdout.strip().upper() != 'PRIVATE':
            raise RuntimeError(OWNER_REPO + ' already exists but is NOT private; refusing migration.')
        if destination.exists():
            raise RuntimeError('Target folder exists; use a fresh path or sync manually.')
        run('gh', 'repo', 'clone', OWNER_REPO, str(destination))
    else:
        if destination.exists():
            raise RuntimeError('Target folder exists; select a fresh path.')
        destination.mkdir(parents=True)
        run('git', 'init', '-b', 'main', cwd=destination)
        (destination / '.gitignore').write_text(
            '.env\n.env.*\n*.pem\n*.key\n*.log\n', encoding='utf-8')
        (destination / 'README.md').write_text(
            '# ATS-Data (private)\n\nPrivate, versioned per-project ATS task events. '
            'Keep this repository PRIVATE and do not publish its history.\n',
            encoding='utf-8')

    copied = 0
    if source.exists():
        for event in source.glob('*/events/*.json'):
            if event.is_symlink() or not event.is_file():
                continue
            relative = event.relative_to(source)
            dest = destination / 'data' / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                if dest.read_bytes() != event.read_bytes():
                    raise RuntimeError('Conflict: ' + str(dest))
                continue
            shutil.copyfile(event, dest)
            copied += 1

    run('git', 'add', '--', '.', cwd=destination)
    if run('git', 'diff', '--cached', '--quiet', cwd=destination, check=False).returncode != 0:
        run('git', 'commit', '-m', 'Initialize private ATS task events', cwd=destination)

    if view.returncode == 0:
        run('git', 'pull', '--rebase', 'origin', 'main', cwd=destination)
        run('git', 'push', 'origin', 'main', cwd=destination)
    else:
        run('gh', 'repo', 'create', OWNER_REPO, '--private',
            '--source', str(destination), '--remote', 'origin', '--push')
    visibility = run('gh', 'repo', 'view', OWNER_REPO, '--json', 'visibility',
                     '--jq', '.visibility').stdout.strip()
    if visibility.upper() != 'PRIVATE':
        raise RuntimeError('GitHub did not confirm private visibility.')
    print(f'ATS-Data ready: {OWNER_REPO}; new historical events copied: {copied}')
    print(f'Local checkout: {destination}')
    print('Use .ats data-sync to exchange new events between projects/devices.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', default=str(DEFAULT_TARGET))
    parser.add_argument('--source', default=str(SOURCE))
    parser.add_argument('--repo', help='OWNER/REPO of the private data repository (default: $ATS_DATA_REMOTE)')
    args = parser.parse_args()
    bootstrap(args.destination, args.source, args.repo)
