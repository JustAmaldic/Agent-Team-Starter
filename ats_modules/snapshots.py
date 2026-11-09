"""Throw-away checkouts of an exact tree, for work that must not touch the task's worktree.

The reviewer works read-only in the worktree and ATS reverts and reports any file that changes
there during a review. Tests running at the same time, or the app started for screenshots, write
files (caches, databases, logs), so they get their own checkout of exactly the tree under review:
a detached `git worktree` of a commit made from that tree (never on a branch, never pushed).

Folders git ignores (node_modules, by default) are not part of a tree; they are linked in from the
worktree so the checkout can still run.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

DEFAULT_LINKS = ('node_modules',)
IDENTITY = {'GIT_AUTHOR_NAME': 'ATS', 'GIT_AUTHOR_EMAIL': 'ats@localhost',
            'GIT_COMMITTER_NAME': 'ATS', 'GIT_COMMITTER_EMAIL': 'ats@localhost'}


def _git(args, cwd, env=None):
    return subprocess.run(['git', *args], cwd=str(cwd), text=True, capture_output=True, check=True,
                          env={**os.environ, **(env or {})}, timeout=120)


def commit_for(folder, ref) -> str:
    """A commit with exactly this tree or commit (a dangling commit for a bare tree)."""
    kind = _git(['cat-file', '-t', ref], folder).stdout.strip()
    if kind == 'commit':
        return _git(['rev-parse', ref], folder).stdout.strip()
    if kind != 'tree':
        raise RuntimeError(f'not a tree or commit: {ref}')
    return _git(['commit-tree', ref, '-m', 'ATS snapshot (never pushed)'], folder, IDENTITY).stdout.strip()


@contextmanager
def checkout(folder, ref, parent=None, links=DEFAULT_LINKS):
    """Yield a temporary detached checkout of `ref` (tree or commit); removed afterwards."""
    folder = Path(folder)
    root = Path(tempfile.mkdtemp(prefix='ats-snapshot-', dir=str(parent) if parent else None))
    target = root / 'tree'
    commit = commit_for(folder, ref)
    _git(['worktree', 'add', '--detach', '--quiet', str(target), commit], folder)
    try:
        for name in links or ():
            source = folder / name
            if source.is_dir() and not (target / name).exists():
                try:
                    (target / name).symlink_to(source.resolve(), target_is_directory=True)
                except OSError:
                    pass   # Windows without symlink rights: the checkout runs without it
        yield target
    finally:
        try:
            _git(['worktree', 'remove', '--force', str(target)], folder)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            pass
        shutil.rmtree(root, ignore_errors=True)
        try:
            _git(['worktree', 'prune'], folder)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            pass
