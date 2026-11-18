"""A pinned (detached) ATS checkout, like the ATS-app submodule, must not block every command."""
import io
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

import ats


class DetachedSyncTests(unittest.TestCase):
    def test_detached_head_skips_self_update(self):
        calls = []

        def git(*args, **kwargs):
            calls.append(args)
            return SimpleNamespace(stdout='')   # clean tree, no current branch
        err = io.StringIO()
        with patch.object(ats, 'git', side_effect=git), patch.object(ats.Path, 'exists', return_value=True), \
                redirect_stderr(err):
            ats.sync_in()
        self.assertNotIn('fetch', [c[0] for c in calls])
        self.assertIn('self-update skipped', err.getvalue())

    def test_named_branch_still_fast_forwards(self):
        calls = []

        def git(*args, **kwargs):
            calls.append(args)
            return SimpleNamespace(stdout='main' if args[:2] == ('branch', '--show-current') else '')
        with patch.object(ats, 'git', side_effect=git), patch.object(ats.Path, 'exists', return_value=True):
            ats.sync_in()
        self.assertIn(('fetch', 'origin', 'main'), calls)
        self.assertIn(('merge', '--ff-only', 'origin/main'), calls)


if __name__ == '__main__':
    unittest.main()
