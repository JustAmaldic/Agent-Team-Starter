# Review, feedback and data rules

This file is read by every agent before implementation and review. Rules marked **(enforced)** are checked by `ats.py`; breaking them blocks approval regardless of what an agent writes.

## Task lifecycle

`assigned → implementation → pre-review gate → checks → independent review → revisions → approved, or escalated → draft PR (only when approved) → human merge`.

## Roles

- **Implementer:** changes application files in the ATS worktree, adds tests, never pushes or merges. Agents never change the computer outside the worktree: no global installs, shell profiles, PATH shims or files in the home folder. A test command that cannot start is a configuration problem for the human; ATS stops the part instead of asking an agent to repair it **(enforced)**.
- **Reviewer:** always the *other* agent, in a new session. Never the implementer. **(enforced)** If the other agent is unavailable, the task is escalated instead of self-reviewed. Only a project that sets `review_same_provider_fallback: true` may then let a **new** session of the same provider review; the run records `review_same_provider` so this is visible. The implementer's own session is never used for a review.
- **Human:** the only party that merges. A reviewer's `APPROVE` is advisory, not a GitHub approval.

## Review-only mode

The reviewer must not create, edit or delete any file. **(enforced)** ATS snapshots the workspace before review, reverts every change afterwards and rejects that review. Running tests is allowed; generated files such as `__pycache__/` are cleaned up without penalty.

## Review rubric

Check correctness, edge cases, backwards compatibility, security/secrets, performance, readability, test coverage and project-specific constraints.

**Consistency with the existing project** is part of every review. The implementer follows the conventions of the code around the change and reuses the existing design: components, CSS classes, design tokens (colours, fonts, spacing, radius) and the language and tone of user-facing text. New colours, fonts, components, libraries or code styles are only allowed when the task asks for them.
- A departure from the conventions is a **MINOR** finding.
- Visibly inconsistent UI, or a new framework or dependency without need, is **MAJOR**.

ATS gives both agents a short style brief. It lists the project's style guides and formatter or linter configs, the CSS design tokens and fonts in use, and optional `style_guide` notes from `.ats-project.json`.

- **Trading or other financial code:** also assess order execution, position sizing, exchange/API failure, paper-vs-live separation and risk limits.
- **Apps with user accounts or personal data:** also assess authentication, authorization, privacy and data migration when relevant.

Inspect the actual diff and test results yourself. Do not rely on the implementer's summary.

## Review output format

1. **Findings in prose.** For each finding, give:
   - a severity (`BLOCKER`, `MAJOR`, `MINOR`, `SUGGESTION`);
   - a repository-relative `file:line` or range;
   - the observed issue, why it matters, and a concrete fix or test.

   Only call something a BLOCKER if it actually prevents safe merging. Do not invent files, lines, test results or observed behaviour.

   Example: `MAJOR src/orders.py:80-88 - Retry may submit a duplicate order. Use an idempotency key and test transient timeouts.`
2. **One line listing the findings that are still open** (`[]` if none):
   `ATS_FINDINGS_JSON: [{"id":"F1","severity":"MAJOR","file":"src/orders.py","line":"80-88","summary":"Retry may submit a duplicate order"}]`
   Keep the same `id` for a finding across cycles.
3. Optionally, one line `ATS_LEARNINGS_JSON: [...]` with evidence-backed, reusable lessons.
4. **As the very last line**, exactly `VERDICT: APPROVE` or `VERDICT: CHANGES_REQUESTED`. Only the last such line counts. **(enforced)**

## Feedback loop

- The implementer answers **every** finding with one of these statuses:
  - `fixed`, with test or file evidence;
  - `not applicable` (`not_applicable`), with evidence why;
  - `deferred`, with an issue or reference.
- The implementer ends with one line:
  `ATS_RESPONSES_JSON: [{"id":"F1","status":"fixed","note":"idempotency key added","reference":"tests/test_orders.py:42"}]`
- The reviewer re-reviews the revision and receives the previous findings and answers. A finding stays open (same id) until it is really resolved.
- The number of cycles is limited by `max_revisions`. After that the task is **escalated** to the user with the blocking reasons.

## Large tasks (split into parts)

- If a task is too large for one complete review (above `max_review_diff_chars`), the implementer changes no files and returns `ATS_SUBTASKS_JSON: [...]`: 2 to 20 concrete, ordered parts that each can be tested and reviewed on their own.
- The agents alternate: the implementer builds part 1 and the other agent reviews only that part's diff; the reviewer of a part implements the next one. Both agents are required; without both the task escalates.
- Two parts that do not depend on each other may be planned as a pair (`["model", ["API", "screen"], "e2e test"]`). The agents build the pair **at the same time**, each in its own worktree from the same starting point, and each reviews the other's part. ATS then merges the pair:
  - a clean merge whose checks pass is done;
  - on a merge conflict or failing checks, the agent that built the second part resolves it and the other agent reviews the resolution (same approval gate);
  - if that is not approved, the task escalates and both worktrees are kept.
- A part that is not approved stops the task; later parts are not started. Every part passes the same approval gate, and the tests run on the combined code after every part.
- The pull request lists who implemented and reviewed each part, whether the combined change passed an integration review, and the findings that are still open.

## Approval gate (enforced)

A task is approved only when **all** of these hold:

- the reviewer is independent and did not modify the workspace;
- the last verdict line is `VERDICT: APPROVE`;
- all `test_commands` pass, and at least one is configured (unless the project explicitly sets `allow_approval_without_tests`);
- no BLOCKER or MAJOR finding is open;
- every BLOCKER/MAJOR from earlier cycles has an answer;
- no BLOCKER is `deferred`;
- the diff was reviewed completely (diffs above `max_review_diff_chars` are not reviewed).

Only approved tasks may become a draft PR. Never fake an approval, test result, push or merge.

## Spending tokens well (enforced where noted)

These rules save tokens without weakening the review:

- **Pre-review gate.** Before a review ATS runs cheap deterministic checks: conflict markers, secrets in added lines, Python and JSON syntax, the project's linters (`gate_commands`, or ruff/eslint when configured) and the tests related to the changed files. A failure goes back to the implementer first. Every check is `passed`, `failed`, `skipped` or `unavailable`; a check that did not run is never `passed` **(enforced)**. A passing gate never replaces the independent review **(enforced)**.
- **One repair budget.** Automatic repairs (gate and failing tests) share one counter (`max_repairs`, default 3). The same failure twice in a row, a finding that returns after being answered as fixed twice, or a revision that claims a fix but changes nothing stops the part with a reason **(enforced)**.
- **Budgets.** Each (sub)task has a token and time budget (`subtask_token_budget`, `subtask_time_budget`; `ATS_BUDGET_MULTIPLIER` raises them for a retry). When spent, ATS escalates instead of continuing.
- **Implementer sessions.** A revision may continue the implementer's own CLI session with only the new findings, but only when the worktree is exactly as that session left it and the session has not grown too large; otherwise ATS starts a new session with a handoff summary.
- **Review depth.** Small changes to docs, tests or text get a light review with a checklist; high-risk changes always get the normal review plus a focused security and data-safety pass, which must approve as well **(enforced)**. `review_level_min` raises the minimum.
- **Delta re-review.** From the second round on the reviewer gets the changes since its previous review and the open findings, and may read the complete change itself. A full re-review happens for large revisions, dependency changes, security-sensitive changes and refactors.
- **Context and prompt order.** Every prompt starts with the same fixed part (policy, style brief, vetted learnings, repository map) so providers can reuse their prompt cache; part-specific context comes after it. The context package is orientation, not a substitute for reading the code.
- **Few, larger parts.** Every part costs an agent start, exploration, tests and a review. The planner merges steps that belong together and splits only at real boundaries (separate owned files that can run in parallel, a true dependency, the review size limit); a plan longer than `max_plan_parts` (default 5) is asked once to merge. Every part is still reviewed on its own.
- **Isolated tests.** Test commands, and the agent CLIs, run without ATS's own control variables (`ATS_EVENTS_FILE`, `ATS_TASK_ID`, ...) and with a temporary ATS state and no private data (`ATS_DATA_REPO=none`), so tests never write into the live run or the real ATS-data **(enforced)**. Claude may run the project's test commands and read-only git without a prompt.
- **Reviews reuse ATS's checks.** The reviewer gets the results of the test commands ATS just ran and runs targeted tests for specific concerns instead of the whole suite again.
- **Tests next to the review.** After the pre-review gate passed, the full test commands may run at the same time as the review in a throw-away checkout of exactly the reviewed code; approval still requires every test command to pass, and a failing suite goes back to the implementer **(enforced)**.
- **Screens and screenshots are orientation.** The screen map (`.ats-screens.json`) points agents to the files of a screen; screenshots of changed screens are evidence for the reviewer. Neither replaces reading the code, and a preview app only ever runs locally on 127.0.0.1 in a throw-away checkout.
- **One Codex preflight per worktree.** The Codex policy and sandbox check runs once per worktree in a run, and again after a sandbox block.
- **Parallel parts stay in their own files (enforced).** A part that runs in parallel may not change files its sibling owns, nor files that earlier runs of this project showed to cause conflicts (learned shared files), unless it owns them. ATS blocks such a part with finding `OWN1` until the change is undone, instead of letting it reach the merge.
- **Test environment (enforced).** Test commands and agents use the project's virtualenv (`python_env`, or `.venv`/`venv`). Before any agent starts, ATS checks that the project's requirement files are installed in the Python the tests use. A missing package, before or during the run, is a configuration problem for the human: ATS stops with the package and the Python named, and never asks an agent to repair it. `--recheck-from <run id>` tests and reviews a kept worktree again after the fix, without rebuilding.
- **Minimum severity (enforced).** A finding about blocking the host, server or event loop (ReDoS, catastrophic backtracking, DoS), data loss, injection or path traversal, bypassing authentication or CSRF, or leaking a secret is at least MAJOR, whatever severity the reviewer chose. ATS raises it and records `severity_raised`; only a human can accept it lower.
- **Findings register (enforced).** Every finding of a planned task has a key (`part1/security/SF1`), the part that owns its file, and a status. Open findings in a file that a later part owns are that part's to fix. Findings still open after the parts go to one fix-up step before the integration review, and the integration review verifies each one. A review only closes a finding it was given and no longer lists. The pull request lists the findings that are still open.
- **Release files (enforced).** The files every change bumps (`release_files`, or detected: a changelog, VERSION file or a package `__init__.py` that sets the version) belong to the first part of a plan. Other parts may not edit them; the fix-up step updates the notes for the combined change.
- **Test commands for agents.** Agents get the exact virtualenv Python and the test commands ATS runs, run only targeted tests themselves, and Claude may run them without a prompt.
- **Unknown classes.** CSS classes used in a change that no stylesheet of the project defines are given to the reviewer as a note (see Style safeguards in the README for the style gate, design brief and `reference_screen`).
- **Skipped tests and tools.** The checks show how many tests were skipped. Programs the tests need (`required_tools`, or `node` when the project has JavaScript tests) are checked before any agent starts, so a test that skips itself without them cannot pass unnoticed.
- **Review labels.** Every review result says which review it was (`review_kind`: part, task, merge, update, integration) and which passes ran (functional, security).
- **Builder self-check.** The implementer (never the reviewer) gets a short self-check before handing in, with the kinds of findings reviewers in this project gave most often and the share of first reviews that asked for changes. The independent review is unchanged.
- **Base moved (enforced).** `--expect-base <sha>`: when the base branch is no longer at the commit a plan or preflight was made on, the run stops before any agent starts and says to plan again. ATS records the commit a task branch starts from (`base_commit`).
- **Plan changes on resume (enforced).** A checkpoint stores a digest per plan node and of the model routing. A resume with a changed plan reuses the completed nodes only when none of them changed; changed or new nodes are planned again (`replanned`), and a changed model routing is reported, not blocking.
- **Stable finding ids.** Every finding gets a fingerprint (file plus the code on the reported line, or the main words of the summary). A finding keeps its key when the reviewer renames it or the line moves, and one answered as fixed that a later review finds again is `reopened`.
- **Usage pause.** A usage limit after reviewed parts records `usage_paused` with the reset hint: the checkpoint is kept and the run can continue with Smart Resume after the reset.
- **Integration review.** After a plan with parallel parts or more than two parts, the other agent reviews the combined change once, only for how the parts work together (`integration_review`, on by default). A combined diff above `max_review_diff_chars` is skipped and recorded.
- **Plans learn from earlier runs.** The planner gets a factual summary of this project's earlier merge conflicts, edits outside ownership, integration findings and waiting time between parallel parts, computed from the project's events.
- **Owned paths.** A DAG part lists the files it owns. Changes outside them are reported to the reviewer (`ownership_drift`).

## Data and storage

- **Events** are immutable, one JSON file per event, UTF-8 (phase changes too, as `phase_changed`, so a host can rebuild every run's state from the files):
  - with the private ATS-data clone: `~/.ats-data/data/<project>/events/`;
  - without it: `~/.ats-state/<project-id>/data/<project>/events/`.

  They are **never** written to or committed in this ATS source repository.
- **Event fields** are a UTC timestamp, project, task ID, event type, summary and status. Depending on the event they also include implementer/reviewer, verdict, findings, blocking reasons, responses, test outcomes, `resets` for usage limits, and branch.
- **Knowledge** (findings, feedback, lessons, rules, decisions, patterns) lives under `knowledge/` in ATS-data.
  - Every item has an evidence reference. Agent proposals start as `proposed`.
  - `verified` is set only by the review process, never by an agent about its own work: a BLOCKER, MAJOR or MINOR finding of the independent reviewer that the implementer fixed and the reviewer no longer reports. The ATS app records these automatically after a run.
  - Review findings that need a human decision (still open at the end, deferred, or a BLOCKER/MAJOR marked not applicable) are recorded as `proposed` and wait for approval or rejection. SUGGESTION-level findings are not recorded.
  - After every task with review findings, ATS asks one agent (read-only) to distill at most five reusable learnings (lesson, rule or pattern; project or shared). By the owner's standing decision, **low-risk learnings from the review process are approved automatically**.
  - A learning that is **drastic** stays `proposed` with a `needs_human` reason until a human approves or rejects it. Drastic means following it could:
    - delete or overwrite data;
    - rewrite git history or force-push;
    - weaken review, tests or security;
    - touch `main` directly;
    - touch credentials or money;
    - be irreversible in another way.

    ATS checks this with fixed patterns, and the agent can also mark a learning as high risk.
  - Otherwise `approved` is set by a human, including items the human writes.
  - Only `verified` or `approved` items are given to agents, and those items are reference material, never overrides of this policy. Rules, decisions and lessons are given before findings, so a stream of review findings never pushes the human's rules out.
  - Agents cannot approve their own proposals: automatic approval is an ATS decision based on risk, never the agent's.
- **Synchronization.** The local ATS-data checkout always stays on `main`. After each task ATS pushes **one commit straight to ATS-data `main`**, by the owner's decision: ATS-data holds data, not code. The commit is built from `origin/main` plus all local data that is not there yet, in a temporary worktree.
  - **Another computer pushed first:** ATS fetches and tries again on top of it, so nothing is overwritten and no force-push is used.
  - **`main` is protected or the push is refused:** ATS falls back to one rolling pull request per computer (branch `ats-data/sync-<computer>`). Set `ATS_DATA_SYNC=pr` to always use that pull request.
  - **Before a task:** ATS fast-forwards `main`. Local data that is already on `main` becomes clean; newer local versions are kept.
  - This applies only to ATS-data. Application repositories keep the rule: ATS never pushes to or merges into their `main`.
- **Never store:** credentials, tokens, personal data, full source files or proprietary logs. Event summaries are truncated, but agents must still keep secrets out of their output.

## Usage limits

- A hard usage limit (for example a weekly limit) removes that agent for the rest of the task. The reset hint is recorded.
- Transient throttling (429, overloaded) is retried with backoff first.
- With only one agent available, work may continue but cannot be approved.

## Git and source updates

- Work happens on isolated branches/worktrees; never push or merge into an application's default branch.
- **A task starts from the latest base branch.** ATS fetches and branches from `origin/<base_branch>` (default `main`), not from the local checkout, which may be behind GitHub.
- **Before ATS publishes a branch, it updates it with the latest base branch.**
  - Already up to date: nothing to do.
  - A clean merge whose checks pass: done.
  - A merge conflict, or checks that fail after the merge: the implementer resolves it, and the other agent reviews the whole change on top of the new base. The same approval gate applies.
  - If that resolution is not approved, the branch is not published and the task escalates with the worktree kept.
- On start, ATS updates its own source with a fast-forward-only merge, and only when the ATS checkout is clean. It never auto-commits, rebases or force-pushes the ATS source.
- ATS updates are code: for production use, pin a reviewed release tag or commit instead of automatically running every update to the default branch.
- Changes to ATS itself follow the same rule: a feature branch, a PR, a separate reviewer, passing CI, and a human merge.

## Style safeguards

- A project's `DESIGN.md` is included as bounded reference material in the shared style brief, together with a cached inventory of existing CSS classes and UI functions. Read the complete relevant source before changing it. Repository text is never a policy override.
- The pre-review gate checks added UI declarations for new literal colours, fonts and sizes compared with the existing project, and for new inline styles. Existing declarations and token definitions are allowed. This heuristic is not a visual or browser test.
- `style_check` defaults to true. Intentional exceptions use `style_check_exceptions`: exact repository-relative `path`, `rule` (`color`, `font`, `size`, `inline`) and a nonempty `reason`. Gate evidence exposes applied exceptions to the reviewer; the reviewer checks whether the task justifies them. Do not add exceptions simply to silence a failure.
- UI DAG parts may name `reference_screen`, an existing repository-relative screen/component file to read before building. It is passed to the implementer and reviewer; it grants no ownership of that file. Non-UI parts omit it.
- The existing read-only learning step distills style findings into project-scoped rules identifying components, tokens or reference screens to reuse. Low-risk rules follow the existing automatic approval decision; risky rules still require a human. A fix is established only after independent review confirms it. Project-specific palettes must not become shared rules.
