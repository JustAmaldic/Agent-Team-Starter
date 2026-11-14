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

To share events and reviewed learnings between machines, create a **private** GitHub repository and clone it to `~/.ats-data` (or set `ATS_DATA_REPO` to the clone). ATS verifies with `gh` that the repository is private and pushes new data straight to the data repository's `main` after each task (a normal push on top of the latest `main`, never a force-push; if a direct push is rejected, local data is retained for a later retry using `.ats data-sync`; ATS never creates a data PR). Application repositories are never pushed to `main`. Set `ATS_DATA_REMOTE=OWNER/REPO` to pin the expected repository. `python scripts/bootstrap_private_data.py --repo OWNER/REPO` creates and seeds one.

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

## Visual ATS-app planner handoff

ATS-app can ask the **real** CLI planner for preview waves and send a user-edited, validated plan back to the task run:

```bash
python .ats/ats.py plan "Improve ARC inbox security and add a weekly privacy audit"
python .ats/ats.py task "Improve ARC inbox security and add a weekly privacy audit" --plan-file /path/to/approved-plan.json
```

The `plan` command asks an enabled agent to inspect an isolated temporary worktree, emits one `ATS_PLAN_JSON:` line, and removes that preview worktree afterward. No implementation starts during preview.

Approved plan files must contain `{"task":"EXACT TASK DESCRIPTION","waves":["first sequential part",["parallel part A","parallel part B"],"integration and tests"]}`. The CLI validates the task binding and structure before execution. ATS-app stores the plan and passes it with `--plan-file`; it never substitutes a merely decorative UI preview for the actual execution plan. Use distinct files for parallel parts, and retain independent reviews and integration checks.

**Compatibility:** update the ATS-app pinned ATS-core submodule to the commit including these `plan` and `--plan-file` capabilities before enabling the interface. Running a newer app against an older CLI is not supported.

## Google Antigravity CLI provider

Antigravity is an optional fifth agent (disabled by default). Install the official CLI so `agy` is on PATH and run `agy` interactively once to authenticate. Enable it with `.ats agents --enable antigravity`. It supports implementation, planning, independent review, fallback and separate worktrees. ATS uses headless JSON output and validates the terminal status, exit code and denied permissions. Implementations use `--mode=accept-edits`; planning and review use `--mode=plan`. ATS does not use `--dangerously-skip-permissions`.

An optional `ATS_ANTIGRAVITY_MODEL` environment variable chooses a model slug from `agy models`. Headless mode may require narrowly scoped permission rules for shell commands; permissions are not automatically broadened. See [official docs](https://www.antigravity.google/docs/cli/headless/).

## Host-controlled agent availability

ATS-app passes a comma-separated `ATS_BLOCKED_AGENTS` environment variable for quota-exhausted CLIs, such as `claude`. ATS excludes these from all new agent selection and cross-agent review, independent of the persisted enabled-agent settings. The host clears its block at the expected subscription reset and new ATS runs can select Claude again. CLI usage still follows existing usage-limit error handling. This does not relax independent reviews or use Claude while it is marked unavailable.

## Per-agent model overrides

ATS-app can supply a model ID independently for each enabled CLI through `ATS_MODEL_CODEX`, `ATS_MODEL_CLAUDE`, `ATS_MODEL_GEMINI`, `ATS_MODEL_OPENCODE` and `ATS_MODEL_ANTIGRAVITY`. A blank setting preserves the provider's default. Nonempty values must be valid model IDs accepted by the selected CLI. The CLI invocation receives `--model <model-id>`; unsupported model IDs fail visibly instead of quietly choosing a different model. The selected model does not bypass review, sandboxing, agent enablement or quota limits.

## Contract-first planning (t50)

DAG v2 supports optional `contracts_provided` and `contracts_required` arrays on each task node.
Use the same short identifier to connect a shared type or API contract to its consumers.
ATS enforces exactly one provider and requires each consumer to depend on the provider,
directly or transitively. Therefore the contract's implementation, tests and independent
review must finish before dependent work starts. Independent components remain parallel.

```json
{"plan_version":2,"tasks":[
  {"id":"contract","title":"Define shared public API contracts","depends_on":[],
   "owned_paths":["contracts/"],"contracts_provided":["public_api"]},
  {"id":"server","title":"Implement server against the stable API","depends_on":["contract"],
   "owned_paths":["src/server.py"],"contracts_required":["public_api"]},
  {"id":"client","title":"Implement client against the stable API","depends_on":["contract"],
   "owned_paths":["src/client.js"],"contracts_required":["public_api"]}
]}
```

This adds contract-level ordering to the existing batch planner; it does **not**
yet remove the wave barrier (full dynamic t49 scheduler remains separate).

## Test environment

Test commands such as `python -m unittest` use the project's virtualenv: `python_env` from `.ats-project.json`, otherwise `.venv` or `venv` in the project folder. ATS puts its scripts folder first in `PATH` for the test commands and the agent CLIs. Before any agent starts, ATS checks that the packages in `requirements.txt`, `requirements-dev.txt` and `requirements-test.txt` are installed there; `ats env-check` prints the same check as JSON.

A missing package stops the run as a configuration problem with the exact package and Python, before implementation, or, if it only shows in a test, without asking an agent to repair it. Agents never install packages. After installing, `ats task "<same task>" --recheck-from <run id>` tests and reviews the kept worktree again without rebuilding it.

## Checking the agent CLIs

`ats agent-check` runs every installed agent CLI on a throw-away fixture repository: one review (must end with `ATS_FINDINGS_JSON` and a `VERDICT` line and change nothing) and one fix (must change the file and give `ATS_RESPONSES_JSON`). It prints the CLI version, the problems and the end of each answer as JSON; run it after updating Codex or Claude. `ATS_LIVE_AGENT_TESTS=1` runs the same as a test. `ATS_CLAUDE_STREAM=1` lets Claude stream its steps (`stream-json`) so the app shows them live as `agent_activity`; Codex steps are always shown.

## Token-efficient runs

ATS reads token usage and session ids from the CLIs' JSON output (`claude -p --output-format json`, `codex exec --json`) and records an `agent_usage` event per call: agent, model, phase, part, review round, and input, output, cache-read and cache-write tokens separately (no prompts or answers). `ATS_STRUCTURED_OUTPUT=0` switches back to plain text; an older CLI without JSON output falls back automatically.

Per phase a model can be chosen with `ATS_MODEL_<AGENT>_<PHASE>` (`PLANNING`, `IMPLEMENTATION`, `REVISION`, `LEARNING`), for example a lighter model for learnings. Reviews always use the agent's main model (`ATS_MODEL_<AGENT>`), and so does every phase of a high-risk task.

Project settings in `.ats-project.json` (all optional):

| Setting | Default | What it does |
| --- | --- | --- |
| `max_plan_parts` | `5` | preferred maximum of parts in a plan; a longer plan is asked once to merge related steps |
| `integration_review` | `true` | one review of the combined change after parallel or more than two parts |
| `integration_fixup` | `true` | before the integration review, one agent call fixes findings still open from the part reviews and updates the release notes |
| `release_files` | detected | version and changelog files; only the first part of a plan edits them |
| `required_tools` | detected | programs the tests need, checked before agents start (`node` for JavaScript tests) |
| `python_env` | `.venv` or `venv` when present | the project's virtualenv for test commands and agents (relative to the project folder, or absolute) |
| `environment_check` | `true` | before any agent starts, check that the requirement files are installed in the Python the test commands use |
| `pre_review_gate` | `true` | cheap checks before the review (see POLICY.md) |
| `gate_commands` | `[]` | extra gate checks, e.g. `[{"name": "ruff", "command": "ruff check ."}]` |
| `targeted_test_command` | from `test_commands` | command for related tests; `{files}` is replaced by the test files |
| `max_repairs` | `3` | automatic repairs per part (gate and tests together) |
| `subtask_token_budget` / `subtask_time_budget` | `2000000` / `3600` | budget per part |
| `review_level_min` | `light` | minimum review depth: `light`, `standard` or `deep` |
| `delta_review` | `true` | delta re-reviews from round two |
| `review_same_provider_fallback` | `false` | allow a new session of the same provider to review when the other agent is unavailable |
| `context_package` | `true` | repository map and part context in prompts |
| `knowledge_budget` | `4000` | characters of vetted learnings per task |
| `parallel_review_tests` | `true` | run the full test commands at the same time as the review, in a throw-away checkout (needs the pre-review gate); approval still needs every test to pass |
| `screen_map` | `true` | use `.ats-screens.json` in prompts and reviews (see below) |
| `flaky_rerun` | `true` | a failing test command is run once more (pytest: only the failed tests); when it then passes it counts as passed and is recorded as unstable (`test_flaky`), so no repair round is spent on it. Skipped when the first run took longer than `flaky_rerun_max_seconds` (300) |
| `write_scope` | `"block"` | a task that asks to write outside the project (home folder, system or temp folder, another drive, `..`) stops before any agent starts; `"warn"` only records it, `"off"` skips the check. Events hold the path category only |
| `ui_preview` | off | start the app and give the reviewer screenshots of the changed screens (see below) |

`ATS_SESSION_REUSE=0` turns session reuse off; `ATS_SESSION_MAX_TURNS` (4) and `ATS_SESSION_MAX_CONTEXT` (400000 new tokens; cache reads do not count) bound it.


### Consistent UI changes

Put the project's visual contract in `DESIGN.md`: existing controls, CSS tokens,
reference screens and UI language. ATS includes a bounded excerpt and an inventory
of CSS classes/UI functions in both agents' style brief. The inventory is cached per
worktree snapshot and refreshes when relevant files change.

The pre-review gate checks only new UI declarations against the existing project.
It flags hex/function colour literals, font families, px/em/rem sizes and inline style attributes/objects;
named colours and other units are left to review. Comments are masked before checking;
it does not require rewriting legacy CSS. Define new shared tokens centrally when
necessary. This is a conservative static heuristic, not complete CSS parsing or a
replacement for browser/visual testing. Projects without existing style declarations
still receive the inventory and inline-style check, but no invented palette/scale.

Intentional departures can be recorded in `.ats-project.json`:

```json
{
  "style_check": true,
  "style_check_exceptions": [
    {"path": "web/chart.js", "rule": "inline", "reason": "Positions are calculated from chart data"}
  ]
}
```

Exceptions match the exact path and rule, require a reason, and appear in review
evidence. Use `style_check: false` only as an explicit project decision. An exception
never bypasses independent review. For DAG UI tasks, include e.g.
`"reference_screen": "web/settings.js"`; it must be an existing file inside the
worktree. The planner is instructed to choose it from the inventory. Legacy list
plans remain valid; put the reference file directly in their part description.
Style findings use the existing learning call to produce project-scoped rules;
there is no extra learning-agent invocation.

### Screen map

`.ats-screens.json` in the project root tells ATS which files, functions, routes and tests make
up each screen, and how the user calls it:

```json
{"version": 1, "screens": [
  {"name": "Device detail", "aliases": ["apparaatpagina", "device pagina"], "route": "#device",
   "files": ["static/js/overzicht.js", "static/index.html"], "symbols": ["showDevicePage"],
   "api": ["GET /api/devices/{id}"], "tests": ["tests/test_devices.py"]}]}
```

When a task names a screen (by name, alias, route or function; also inside Dutch compounds such as
`instellingenpagina`), planner, builder and reviewer get its files in the prompt and start there
instead of searching the repository. A task that talks about a screen without naming a known one
gets the list of screens. A change that adds a screen the map does not list gets a MINOR finding.

`ats screens` prints a draft made from the code (render functions, `view-…` sections, file-based
and router-config routes, HTML page handlers) next to what is already listed. `ats screens --task`
prints a task text: run it as a normal task and the agents complete the map (aliases, routes,
merging duplicates) through the usual review and pull request. `ats screens --match "<text>"`
shows what a task would get.

### Screenshots for the reviewer

With `ui_preview` and a screen map, a change to UI files gets screenshots: ATS starts the app on a
free 127.0.0.1 port in a throw-away checkout, opens each changed screen (and the part's
`reference_screen`) with Playwright at the configured widths, and gives the reviewer the PNG files:
`before` (base), `after` (this change) and `reference`. Browser console errors go along as text.

```json
{"ui_preview": {"command": "python3 server.py --port {port}", "url": "http://127.0.0.1:{port}",
                "ready_path": "/", "timeout": 40, "widths": [1280, 390], "before": true}}
```

A route with a parameter needs a `preview` path in the screen map (for example `/devices/demo`).
`env` adds variables for the preview app (no secrets). Playwright must be installed for the Python
that runs ATS (`pip install playwright`, then `python -m playwright install chromium`); ATS never
installs it. A preview that cannot start is a note for the reviewer, never a blocked run. The
images stay in ATS's state folder (`~/.ats-state/…/screenshots/<run>`), not in the repository.

### Tests next to the review

With the pre-review gate on, the fast checks and the tests of the changed files pass before the
review starts. The full test commands then run at the same time as the review, in a throw-away
checkout of exactly the reviewed code (files the tests write never reach the worktree). Approval
waits for them: a failing suite blocks approval and goes back to the builder together with the
review's findings. A test command whose program is missing still stops the run before any agent
starts. `parallel_review_tests: false` restores the old order (tests first, the reviewer sees their
output).

## Quality checks added to every run

- **Routing reason.** With `--agent auto` the `agent_routing` event holds the score per agent and
  why (keywords, recent reviews, results per task category).
- **Dependencies.** New or changed entries in `requirements*.txt`, `pyproject.toml`, `package.json`
  and similar files are named to the reviewer (needed? known package? pinned?), recorded as
  `dependency_changes` and listed separately in the PR description.
- **Stale screen map.** A path in `.ats-screens.json` that no longer exists is looked up again by
  file name (a moved file is used with its new path), reported as `screen_map_stale` and the
  agent is asked to correct the map in its change.
- **Fewer tokens.** The reviewer gets only the last line of the output of passing test commands.
- **CLI fixture.** `ats agent-check --fixture cli-fixture.json` also writes an anonymous record
  (CLI versions, OS, error categories; paths, tokens and e-mail addresses redacted) and prints
  what differs from the fixture already in that file.

## Fixing the merge conflicts of a pull request

    ats task "Resolve the merge conflicts of pull request #12" --fix-pr 12 --pr-head feature-x --pr-base main --push

ATS starts a worktree at the head of the pull request, merges its base in and lets an agent resolve
the conflicts with one rule above all: keep the functionality of both sides. Afterwards the merge
guard compares the names both sides define (functions, classes, tests, routes, constants, ids) with
the resolved files and gives the reviewer every name that disappeared, unless one side removed it on
purpose (`merge_guard` event). The other agent reviews the whole change on top of the new base and
every test command must pass. Only then is the pull request's own branch updated, with a normal
fast-forward push: never forced, so work pushed meanwhile is never overwritten. ATS never merges the
pull request. Pull requests from forks are not supported.
