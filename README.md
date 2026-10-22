# Agent Team Starter (ATS)

ATS is a Python command-line coordinator for Codex CLI and Claude Code. It can create isolated application worktrees, run tests and an independent review, and optionally open a draft pull request for an application with a Git remote. Projects without Git can use isolated local snapshots and reviewable patches.

## Requirements

Python 3.10+, Git, Node.js/npm for installing agent CLIs, and optionally GitHub CLI (`gh`) for application pull requests. Codex and Claude Code authentication and subscriptions are independent.

## Install

From the root of your application:

```bash
git clone https://github.com/JustAmaldic/Agent-Team-Starter.git .ats
printf '\n.ats/\n' >> .gitignore
sh .ats/setup-shell.sh
source ~/.zshrc
.ats install
.ats doctor
```

On Windows, run `powershell -NoProfile -File .ats/setup.ps1` for the PowerShell shortcut, and open a new terminal. Use `py -3 .ats/ats.py doctor` without a shell shortcut.

## Tasks

```bash
.ats task "Improve error handling" --push
.ats task "Fix flaky tests" --quiet
.ats doctor
```

The application needs its own Git repository and remote for `--push`. For non-Git applications omit `--push`; ATS creates a local snapshot and a patch for manual inspection and application. Tasks do not automatically merge pull requests.

## Privacy and security

ATS stores review events and agent output locally under `~/.ats-state/<project-id>/data/`, not in this source repository. Set `ATS_STATE_DIR` to choose another local state directory. ATS does not auto-push events or source commits. Project prompts, review content, and local logs may contain sensitive data: do not commit them to the public repository.

Review app code before giving agents access, and keep keys, credentials and `.env` files out of Git. No automation should bypass a required human merge or deployment approval.

## Optional: private shared history

To share events and reviewed learnings between machines, create a **private** GitHub repository and clone it to `~/.ats-data` (or set `ATS_DATA_REPO` to the clone). ATS verifies with `gh` that the repository is private and opens a pull request with new data after each task; it never pushes to `main`. Set `ATS_DATA_REMOTE=OWNER/REPO` to pin the expected repository. `python scripts/bootstrap_private_data.py --repo OWNER/REPO` creates and seeds one.

## Development

```bash
python -m unittest discover -s tests -v
python -m compileall -q ats.py
```

CI runs these tests on Ubuntu, macOS, and Windows. Development changes should use feature branches and pull requests.

## License

MIT, see [LICENSE](LICENSE).

## Security

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.
