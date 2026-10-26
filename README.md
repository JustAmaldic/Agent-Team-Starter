# Agent Team Starter (ATS)

ATS is a Python command-line coordinator for configurable coding agents (Codex CLI, Claude Code, Gemini CLI and OpenCode). It can create isolated application worktrees, run tests and an independent review, and optionally open a draft pull request for an application with a Git remote. Projects without Git can use isolated local snapshots and reviewable patches.

## Requirements

Python 3.10+, Git, Node.js/npm for installing agent CLIs, and optionally GitHub CLI (`gh`) for application pull requests. Agent authentication and subscriptions are independent. Codex and Claude are enabled by default, preserving the existing setup.

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

## Choose your agents

Agent selection is stored per application in `.ats-agents.json`. Existing projects default to Codex and Claude until you change it.

```bash
.ats agents                            # list enabled agents and installed CLIs
.ats agents --enable gemini             # add Gemini to the pool
.ats agents --enable opencode           # add OpenCode
.ats agents --disable claude            # do not select or launch Claude
.ats agents --enable claude --disable codex
.ats install                            # configure only enabled agents
.ats task "Improve tests" --agent gemini
```

Multiple `--enable` and `--disable` flags can be combined. ATS refuses to disable the last agent or run a task with a disabled agent. `auto` routing and usage/sandbox fallback choose from enabled agents only. Gemini CLI and OpenCode require their CLI and authentication to be installed separately; `.ats install` checks that these are available.

**Independent reviews:** for automatic cross-agent review, enable at least two agents with working CLIs. With a single agent ATS may implement a change, but it will not approve or publish a PR without independent review. Multiple agents may work in separate Git worktrees during a split task.

## Tasks

```bash
.ats task "Improve error handling" --push
.ats task "Fix flaky tests" --quiet
.ats doctor
```

The application needs its own Git repository and remote for `--push`. For non-Git applications omit `--push`; ATS creates a local snapshot and a patch for manual inspection and application. Tasks do not automatically merge pull requests.

Agents keep the existing style of the application. ATS gives them a short style brief:
- the style guides and formatter or linter configs the project has;
- the CSS design tokens and fonts in use;
- optional notes from `.ats-project.json`, for example `"style_guide": ["Dutch UI text", "Black with old-rose accent, no new colours"]`.

Reviewers report inconsistent code or UI as a finding.

## Privacy and security

ATS stores review events and agent output locally under `~/.ats-state/<project-id>/data/`, not in this source repository. Set `ATS_STATE_DIR` to choose another local state directory. ATS does not auto-push events or source commits. Project prompts, review content, and local logs may contain sensitive data: do not commit them to the public repository.

Review app code before giving agents access, and keep keys, credentials and `.env` files out of Git. No automation should bypass a required human merge or deployment approval.

## Running ATS from a central place (host integration)

ATS can also run from one central installation instead of a `.ats` clone per project. A host process, such as the ATS desktop app, sets these environment variables per task:

| Variable | Effect |
|---|---|
| `ATS_APP` | Project root to work on (instead of the folder around `.ats` or the current directory). |
| `ATS_EVENTS_FILE` | Every event is also appended to this file as one JSON line, plus live-only `phase` events (`implementation`, `checks`, `review`, `revision`, `approved`, `pr`, `pr_created`, `patch`, `finished`). |
| `ATS_TASK_ID` | Use this UUID as the task id, so the caller knows it before the run starts. |
| `ATS_NO_SELF_UPDATE` | Skip the fast-forward source update at start; the caller manages ATS updates. |

Without these variables ATS behaves exactly as before.

## Optional: private shared history

To share events and reviewed learnings between machines, create a **private** GitHub repository and clone it to `~/.ats-data` (or set `ATS_DATA_REPO` to the clone). ATS verifies with `gh` that the repository is private and pushes new data straight to the data repository's `main` after each task (a normal push on top of the latest `main`, never a force-push; set `ATS_DATA_SYNC=pr` to get one pull request per computer instead, which is also the fallback when `main` is protected). Application repositories are never pushed to `main`. Set `ATS_DATA_REMOTE=OWNER/REPO` to pin the expected repository. `python scripts/bootstrap_private_data.py --repo OWNER/REPO` creates and seeds one.

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

## Shared event contract v1

The canonical event, phase and run-state vocabulary is documented in `contracts/events.v1.json` and loaded by `ats_contract.py`. ATS-app keeps a mirrored file at `ats_host/contracts/events.v1.json` and checks compatibility when its pinned ATS submodule is available. Live `phase` messages require timestamp, task ID and event type; persisted event files also require a project. Unknown event types can be kept by consumers without changing run status. Keep both files in sync whenever adding a stage or event.

## Automatic parallel task planning

By default (`--parallel auto`), ATS now asks an enabled agent to plan a multi-feature or long task **before implementation**. A structured `ATS_SUBTASKS_JSON` plan uses strings for sequential steps and two-element arrays for genuinely independent, parallel parts. The existing execution engine gives the pair to separate enabled agents in separate Git worktrees. Each part undergoes independent review; ATS then integrates the work and executes configured checks. Parallelism is never inferred merely because a task mentions multiple features: the planner should isolate file ownership and put dependencies first.

```bash
.ats task "Build independent inbox-safety scanning and a weekly signup audit" --parallel always
.ats task "Fix CSS spacing" --parallel never
.ats task "Extend ARC inbox security, login alerts and weekly privacy report" --parallel auto
```

- `auto` (default): request a structured preflight plan for longer or clearly multi-goal descriptions; prefer safe parallel pairs, but execute sequential waves when parts depend on each other.
- `always`: demand a valid plan with **at least one** independently executable parallel pair. When the planner cannot justify one, ATS refuses implementation rather than forcing unsafe concurrency.
- `never`: skip preflight and use the legacy small-task implementation flow (which can still explicitly return a subtask plan).

The planner gets two attempts to return valid JSON. Missing/malformed plans abort complex work **without beginning implementation**. The planning call is checked for workspace modifications and they are reverted. If fewer than two coding agents are enabled, preflight parallel planning reports a clear error. Existing tests and independent reviewer approval remain mandatory, and no automatic merge or deployment is introduced.

**Important:** `--parallel always` is a planning preference, not permission to edit overlapping files simultaneously. During parallel waves, ATS uses two worktrees and checks integration. A failed review, conflict or unavailable reviewer blocks publishing; inspect the retained worktrees. When using ATS-app, the default `auto` setting applies without a new UI flag.

## Intelligence foundation (experimental)

The following commands provide inspectable, deterministic foundations for the next ATS features:

```bash
.ats risk "Update OAuth token refresh"                  # risk classification before running
.ats task "Update OAuth token refresh" --approve-risk  # deliberate human acknowledgement
.ats performance                                      # historical agent performance by task type
.ats task "Implement the feature" --plan-file plan.json
.ats feedback github-comments.json                    # PR comment candidates with stable IDs
.ats knowledge-check knowledge.json relations.json    # contradictions and superseded items
```

`plan.json` contains a JSON list of two to twenty strings (or pairs of independent strings), using the existing ATS parallel subtask schema. It can be edited and reviewed before the task starts. High-risk tasks are blocked unless `--approve-risk` is passed, but that flag is not a substitute for independent review, tests or a deployment approval. Risk detection is a conservative keyword-based starting point; it does not yet analyze changed file paths after implementation.

Performance scores use completed task events and a minimum of three samples per agent/category before influencing routing. Revision and token statistics are estimates based on available event records; missing usage is not treated as measured zero consumption. Defects reappearing in later PRs are not tracked yet.

Feedback and knowledge-check commands currently accept exported JSON, not automatic GitHub webhook ingestion or a persisted knowledge graph. Contradictions are flagged for humans, never deleted or auto-resolved. The interactive planning editor, graph viewer, secure approval workflow and direct GitHub callback processing remain follow-up application features.
