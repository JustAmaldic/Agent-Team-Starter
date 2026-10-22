#!/usr/bin/env python3
"""Export a deliberately allowlisted, history-free ATS source snapshot."""
import argparse
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_FILES = (
    '.gitignore', 'AGENTS.md', 'CLAUDE.md', 'LICENSE', 'POLICY.md', 'SECURITY.md', 'ats.py',
    'config.example.json', 'project-config.example.json', 'requirements.txt',
    'setup-shell.sh', 'setup.ps1',
    '.github/workflows/ci.yml',
)
PUBLIC_DIRS = {
    'shell': {'.zsh'},
    'tests': {'.py'},
    'scripts': {'.py'},
}
DENIED_PARTS = {'.git', 'data', '.ats-state', '__pycache__', '.venv', '.env'}
DENIED_EXTENSIONS = {'.pem', '.key', '.p12', '.pfx'}


def copy_checked(source, target):
    relative = source.relative_to(ROOT)
    if source.is_symlink() or not source.is_file():
        raise RuntimeError(f'Unsafe public export file: {relative}')
    if any(part in DENIED_PARTS for part in relative.parts):
        raise RuntimeError(f'Private path in public export: {relative}')
    if source.suffix.lower() in DENIED_EXTENSIONS:
        raise RuntimeError(f'Credential file in public export: {relative}')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def export(destination):
    destination = Path(destination).expanduser().resolve()
    if destination == ROOT or ROOT in destination.parents or destination in ROOT.parents:
        raise RuntimeError('Export into a new sibling or independent folder, never inside source.')
    if destination.exists():
        raise RuntimeError('Output folder already exists. Choose an empty new path.')
    destination.mkdir(parents=True)
    for name in PUBLIC_FILES:
        copy_checked(ROOT / name, destination / name)
    readme = ROOT / 'PUBLIC_README.md'
    copy_checked(readme if readme.exists() else ROOT / 'README.md', destination / 'README.md')
    for dirname, suffixes in PUBLIC_DIRS.items():
        for source in sorted((ROOT / dirname).rglob('*')):
            if source.is_file() and source.suffix in suffixes:
                copy_checked(source, destination / source.relative_to(ROOT))
    (destination / 'PUBLICATION_REVIEW_REQUIRED.txt').write_text(
        'Review all files for confidential content, secrets, and licensing before '
        'publishing. This folder contains NO Git history. Create a NEW repository '
        'instead of changing visibility on the original private repository.\n',
        encoding='utf-8')
    return destination


NOREPLY_SUFFIX = '@users.noreply.github.com'


def github_noreply_identity():
    """(login, id+login@users.noreply.github.com) of the gh-authenticated user, or None."""
    gh = shutil.which('gh')
    if not gh:
        return None
    result = subprocess.run([gh, 'api', 'user', '--jq', '.login, .id'],
                            text=True, capture_output=True, check=False)
    lines = result.stdout.split()
    if result.returncode or len(lines) != 2:
        return None
    login, user_id = lines
    return login, f'{user_id}+{login}{NOREPLY_SUFFIX}'


def init_git(destination, name=None, email=None, allow_any_email=False):
    """Start a fresh history whose commits use the GitHub no-reply address."""
    if not (name and email):
        identity = github_noreply_identity()
        if identity is None:
            raise RuntimeError('Pass --git-name and --git-email, or authenticate gh '
                               'so the GitHub no-reply address can be looked up.')
        name, email = name or identity[0], email or identity[1]
    if not allow_any_email and not email.endswith(NOREPLY_SUFFIX):
        raise RuntimeError('Refusing a non-no-reply commit e-mail for a public repository: ' + email)
    for args in (['init', '-q', '-b', 'main'], ['config', 'user.name', name],
                 ['config', 'user.email', email]):
        subprocess.run(['git', *args], cwd=destination, check=True)
    return name, email


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='New destination folder outside ATS repository')
    parser.add_argument('--init-git', action='store_true',
                        help='Also run git init with a repository-local GitHub no-reply identity')
    parser.add_argument('--git-name', help='Commit name (default: your gh login)')
    parser.add_argument('--git-email', help='Commit e-mail (default: your GitHub no-reply address)')
    args = parser.parse_args()
    destination = export(args.output)
    print(f'Public export created: {destination}')
    if args.init_git:
        name, email = init_git(destination, args.git_name, args.git_email)
        print(f'Git initialised (no commit yet) with identity: {name} <{email}>')
