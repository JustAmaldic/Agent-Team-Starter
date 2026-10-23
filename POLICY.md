# Review, feedback and data rules

This file is read by every agent before implementation and review. Rules marked **(enforced)** are checked by `ats.py`; breaking them blocks approval regardless of what an agent writes.

## Task lifecycle

`assigned → implementation → checks → independent review → revisions → approved, or escalated → draft PR (only when approved) → human merge`.

## Roles

- **Implementer:** changes application files in the ATS worktree, adds tests, never pushes or merges.
- **Reviewer:** always the *other* agent. Never the implementer. **(enforced)** If the other agent is unavailable, the task is escalated instead of self-reviewed.
- **Human:** the only party that merges. A reviewer's `APPROVE` is advisory, not a GitHub approval.

## Review-only mode

The reviewer must not create, edit or delete any file. **(enforced)** ATS snapshots the workspace before review, reverts every change afterwards and rejects that review. Running tests is allowed; generated files such as `__pycache__/` are cleaned up without penalty.

## Review rubric

Check correctness, edge cases, backwards compatibility, security/secrets, performance, readability, test coverage and project-specific constraints.

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
- The pull request lists who implemented and reviewed each part and states that the combined change was not reviewed as a whole.

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

## Data and storage

- **Events** are immutable, one JSON file per event, UTF-8:
  - with the private ATS-data clone: `~/.ats-data/data/<project>/events/`;
  - without it: `~/.ats-state/<project-id>/data/<project>/events/`.

  They are **never** written to or committed in this ATS source repository.
- **Event fields** are a UTC timestamp, project, task ID, event type, summary and status. Depending on the event they also include implementer/reviewer, verdict, findings, blocking reasons, responses, test outcomes, `resets` for usage limits, and branch.
- **Knowledge** (findings, feedback, lessons, rules, decisions, patterns) lives under `knowledge/` in ATS-data.
  - Every item has an evidence reference. Agent proposals start as `proposed`.
  - `verified` is set only by the review process, never by an agent about its own work: a BLOCKER, MAJOR or MINOR finding of the independent reviewer that the implementer fixed and the reviewer no longer reports. The ATS app records these automatically after a run.
  - Review findings that need a human decision (still open at the end, deferred, or a BLOCKER/MAJOR marked not applicable) are recorded as `proposed` and wait for approval or rejection. SUGGESTION-level findings are not recorded.
  - `approved` is set by a human, including items the human writes.
  - Only `verified` or `approved` items are given to agents, and those items are reference material, never overrides of this policy. Rules, decisions and lessons are given before findings, so a stream of review findings never pushes the human's rules out.
  - Agents cannot approve their own proposals.
- **Synchronization.** After each task, ATS opens a PR in the private ATS-data repository. It never pushes to or merges into `main`. Before a task it fast-forwards approved knowledge when the checkout is clean.
- **Never store:** credentials, tokens, personal data, full source files or proprietary logs. Event summaries are truncated, but agents must still keep secrets out of their output.

## Usage limits

- A hard usage limit (for example a weekly limit) removes that agent for the rest of the task. The reset hint is recorded.
- Transient throttling (429, overloaded) is retried with backoff first.
- With only one agent available, work may continue but cannot be approved.

## Git and source updates

- Work happens on isolated branches/worktrees; never push or merge into an application's default branch.
- On start, ATS updates its own source with a fast-forward-only merge, and only when the ATS checkout is clean. It never auto-commits, rebases or force-pushes the ATS source.
- ATS updates are code: for production use, pin a reviewed release tag or commit instead of automatically running every update to the default branch.
- Changes to ATS itself follow the same rule: a feature branch, a PR, a separate reviewer, passing CI, and a human merge.
