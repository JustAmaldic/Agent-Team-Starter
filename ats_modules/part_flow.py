"""Split tasks: planning, building parts (in waves or rolling), merging and integration review.

Moved out of ats.py (t67). Names that live in ats.py are reached as `ats.<name>`,
looked up at call time, so behaviour and test patches on the ats module are unchanged."""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path
import ats_intelligence as intelligence
from ats_modules import checkpoints
from ats_modules import dag as dag_planning
from ats_modules import budget as budgets
from ats_modules import style_guard
from ats_modules import lenient_json
from ats_modules import plan_learning
from ats_modules import review_handoff
from ats_modules import findings_ledger
from ats_modules import usage as usage_meter
from ats_modules.agent_limits import AgentUsageExhausted

import ats

__all__ = ['plan_waves', 'valid_plan', 'subtask_plan', 'DEFAULT_MAX_PLAN_PARTS', 'max_plan_parts', 'should_preplan', 'preplan_task', '_plan_history', 'plan_history', 'other_agent', 'dag_node', 'git_identity', 'commit_all', 'run_part', 'merge_parallel_part', 'subtask_flow', 'RELEASE_CANDIDATES', 'VERSION_ASSIGNMENT', 'release_files', 'release_note', 'assign_release_files', 'integration_fixup', 'record_ledger', 'integration_base', 'integration_review', 'distill_learnings']


def plan_waves(plan):
    """Plan -> waves: lists of parts built at the same time. None when the plan is unusable.

    A string is a part on its own; a list of two strings is a pair the two agents build in
    parallel (parts that do not depend on each other). Waves run in order.
    """
    if isinstance(plan, dict) and plan.get('plan_version') == 2:
        try:
            return [[part['title'] for part in batch] for batch in dag_planning.execution_batches(plan)]
        except ValueError:
            return None
    if not isinstance(plan, list):
        return None
    waves = []
    for entry in plan:
        wave = [entry] if isinstance(entry, str) else entry
        if not isinstance(wave, list) or not 1 <= len(wave) <= ats.MAX_PARALLEL_PARTS:
            return None
        if not all(isinstance(p, str) and p.strip() and not ats.is_placeholder(p) and p not in ats.SUBTASK_EXAMPLE
                   for p in wave):
            return None
        waves.append([p.strip() for p in wave])
    total = sum(len(w) for w in waves)
    return waves if 2 <= total <= ats.MAX_PARTS else None


def valid_plan(plan):
    return ats.plan_waves(plan) is not None


def subtask_plan(output):
    """The plan from an ATS_SUBTASKS_JSON line, or None when the agent implemented directly.

    A marker with an unusable plan is an error: the agent chose to split but gave no real parts.
    """
    dag_lines = re.findall(r'^\s*ATS_DAG_JSON:[^\r\n]*$', output, re.MULTILINE)
    if dag_lines:
        try:
            raw = dag_lines[-1].split('ATS_DAG_JSON:', 1)[1].strip()
            if len(raw) > 28000:
                raise ValueError('DAG marker is too large')
            return dag_planning.validate_dag(lenient_json.loads(raw))
        except (ValueError, TypeError) as exc:
            raise RuntimeError('Invalid task DAG: ' + str(exc)) from exc
    lines = re.findall(r'^\s*ATS_SUBTASKS_JSON:[^\r\n]*$', output, re.MULTILINE)
    if not lines:
        return None
    plan = ats.parse_marked_json(lines[-1], 'ATS_SUBTASKS_JSON')
    if not ats.valid_plan(plan):
        raise RuntimeError('Invalid task plan: expected 2 to 20 concrete parts (strings, or pairs of strings)')
    return plan


DEFAULT_MAX_PLAN_PARTS = 5


def max_plan_parts(conf):
    """Preferred upper bound for a plan (`max_plan_parts`); a longer plan is asked once to merge parts."""
    try:
        value = int((conf or {}).get('max_plan_parts', ats.DEFAULT_MAX_PLAN_PARTS))
    except (TypeError, ValueError):
        return ats.DEFAULT_MAX_PLAN_PARTS
    return min(ats.MAX_PARTS, max(2, value))


def should_preplan(task, mode='auto'):
    """Decide before implementation whether to request a structured multi-agent plan."""
    if mode == 'never':
        return False
    if mode == 'always':
        return True
    text = task.strip()
    # Multi-feature requests and longer tasks deserve independent work packages.
    words = len(text.split())
    separate_goals = sum(text.lower().count(term) for term in (
        ' and ', ' also ', ' daarnaast ', ' bovendien ', ' weekly ',
        ' wekelijks ', ' en ', ';', '\n-', '\n1.', '\n2.'))
    return words >= 65 or (words >= 28 and separate_goals >= 2)


def preplan_task(task, task_id, folder, agent, unavailable, conf, quiet=False,
                 mode='auto'):
    """Plan before editing, validate, and make a safe execution decision.

    A complex task never silently falls through to an unbounded one-agent
    implementation if the planning response was missing or malformed.
    """
    if not ats.should_preplan(task, mode):
        return None
    if len(ats.enabled_agents()) < 2:
        raise RuntimeError('Automatic parallel planning requires two enabled agents.')
    print('ATS: planning bounded work before implementation...', flush=True)
    ats.phase('planning', task_id, agent=agent)
    baseline = ats.index_tree(folder)
    prompt = (
        ats.shared_prefix(folder, conf, task_id) +
        'ROLE: planner. Plan ONLY, do not edit any files. '
        f'Project: {ats.APP.name}. Task: {task}\n'
        'Inspect the code to divide this task into as FEW concrete implementable parts as possible '
        f'(usually 2-{ats.max_plan_parts(conf)}, never more than 20) with tests and independently reviewable '
        'outcomes. Every part costs a full agent start, codebase exploration, tests and an independent '
        'review (about five minutes), so merge small steps that belong together (a helper and its tests, '
        'a setting and the code that uses it, a model and its migration) into one part. Split only at '
        'real boundaries: work in separate owned files that can run in parallel, a true dependency, '
        f'or a part whose diff would exceed {conf.get("max_review_diff_chars", 150000)} characters. '
        'Never make a part only for tests, documentation or checking parts that are already reviewed; '
        'each part delivers working code with its own tests. '
        'The configured maximum of ' + str(ats.max_plan_parts(conf)) + ' parts is a HARD planning target: '
        'before returning JSON count tasks, keep no more than this many, and verify every '
        'dependency and owned file. A later correction needs another expensive agent call. '
        'Return exactly ONE LINE starting ATS_DAG_JSON: followed by valid JSON. '
        'Keep every title short (one or two sentences, under 300 characters): what the part delivers '
        'and how it is tested; the implementer reads the full task anyway. Inside JSON strings never '
        'write a bare double quote: copy quoted words from the task with single quotes (\'Nieuwe run\'). '
        'The JSON object has plan_version=2 and tasks (2-20 objects). Each task contains '
        'id, title, depends_on (list of prior task ids), and owned_paths (explicit source '
        'files or directories). For UI parts add reference_screen: the repository-relative path of an existing '
        'similar screen or component from the style inventory. Do not invent a path. '
        'For non-UI parts omit it. Example: ATS_DAG_JSON: '
        '{"plan_version":2,"tasks":[{"id":"contract","title":"Define shared event contracts",'
        '"depends_on":[],"owned_paths":["contracts/"]},'
        '{"id":"scorer","title":"Implement sender safety scoring with regression tests",'
        '"depends_on":["contract"],"owned_paths":["phish.py","tests/test_phish.py"]},'
        '{"id":"audit","title":"Implement newsletter evidence inventory with tests",'
        '"depends_on":["contract"],"owned_paths":["newsletter_audit.py"]},'
        '{"id":"integration","title":"Integrate and test security and newsletter workflows",'
        '"depends_on":["scorer","audit"],"owned_paths":["mail_assist.py"]}]}. '
        'When modules share newly introduced types or APIs, add a small contract-defining node first. '
        'Declare contracts_provided on that node and contracts_required on each consumer, '
        'using the same short identifier (example user_api). Every consumer must depend '
        'directly or indirectly on the contract-defining node. ATS rejects missing or '
        'parallel/unreviewed contract dependencies. Omit these optional fields when no '
        'new shared interface is needed. Acceptance tests for the contract belong in '
        'its owned_paths. Do not make unrelated parts depend on the contract. '
        'Declare ONLY true dependencies, not conceptual phases. '
        'Use separate owned_paths for independently implementable work. '
        'Optimize for the shortest safe critical path, not for the most sequential phases. '
        'Before choosing waves, identify each part\'s prerequisites, files it will own, '
        'and which parts can start immediately after a shared contract is fixed. '
        'Group independent parts into pairs EVEN WHEN they implement different layers '
        '(for example a data store and a standalone classifier), provided neither '
        'requires the other\'s unfinished interfaces and their write sets are disjoint. '
        'Do not make a part wait for an unrelated part merely because the latter belongs '
        'to an earlier conceptual phase. Put true prerequisites BEFORE dependent work. '
        'The execution engine has two agents and completes/reviews/merges an entire '
        'wave before starting the next, so minimize avoidable single-part waves. '
        'Prefer pairs over serial work when both tasks can run against existing '
        'interfaces or an agreed contract and independently pass tests. '
        'Never group parts that modify the same file, require each other\'s output, '
        'or depend on uncommitted code; if uncertain use consecutive strings. '
        'Plan one final integration and cross-feature test pass only when parts must be wired '
        'together in code that none of them owns; otherwise the last component part integrates. '
        'Avoid false dependencies from shared orchestration files. When a new standalone module '
        'can be built against an existing interface, give that module and its tests to an '
        'independent node and reserve minimal wiring in shared files (such as ats.py) '
        'for a final integration node. The standalone module can run in parallel with '
        'another independent implementation. Do not reserve a shared entrypoint in both '
        'owned_paths unless both edits are unavoidable. Do not invent parallel tasks '
        'merely to increase concurrency; contract consumers must wait until reviewed. '
        'Never have two agents modify the same file concurrently. '
        'Every part needs a testable acceptance criterion; include file boundaries '
        'where they are known. Do not include credentials, raw email data, '
        'deployment, live trading, or deletion without explicit approval. '
        'Do not implement any changes. Do not add explanations outside the JSON line.'
        + plan_learning.planner_note(ats.plan_history())
        + ats.release_note(conf)
    )
    last_problem = None
    for attempt in range(2):
        try:
            planner, output = ats.invoke_with_fallback(
                agent, prompt + (f'\nYour prior plan was unusable: {last_problem}. '
                                 'Correct the JSON and return only the marker line.'
                                 if last_problem else ''),
                folder, unavailable, task_id, 'planning', quiet=quiet,
                resume_prompt=(f'Your previous ATS_DAG_JSON plan was rejected: {last_problem}. '
                               f'Revise the existing plan to at most {ats.max_plan_parts(conf)} parts, '
                               'preserving independent work and owned paths. Return only the corrected '
                               'ATS_DAG_JSON line. Do not edit any files.' if last_problem else None))
        finally:
            modified = ats.restore_if_changed(folder, baseline)
            if modified:
                ats.record('planning_warning',
                       'Planner modified files; reverted: ' + ', '.join(modified[:10]),
                       task_id, planner=agent)
        try:
            plan = ats.subtask_plan(output)
        except RuntimeError as exc:
            last_problem = str(exc)
            ats.record('planning_retry', 'Planner response could not be used', task_id,
                   planner=planner, attempt=attempt + 1, reason=last_problem[:300])
            continue
        if plan is not None:
            waves = ats.plan_waves(plan)
            if mode == 'always' and not any(len(wave) == 2 for wave in waves):
                last_problem = 'No safe parallel pair was proposed'
                ats.record('planning_retry', 'Planner did not produce safe parallel work', task_id,
                       planner=planner, attempt=attempt + 1, reason=last_problem)
                continue
            parts = sum(len(w) for w in waves)
            limit = ats.max_plan_parts(conf)
            if parts > limit:
                if attempt == 0:
                    last_problem = (f'The plan has {parts} parts; merge related steps into at most {limit} '
                                    'parts, keeping true dependencies and separate owned files')
                    ats.record('planning_retry', 'Plan exceeds the configured part limit', task_id,
                           planner=planner, attempt=attempt + 1, reason=last_problem,
                           parts=parts, max_plan_parts=limit)
                    continue
                ats.record('planning_warning', f'Plan keeps {parts} parts (more than {limit}) after a merge request',
                       task_id, planner=planner, parts=parts, max_plan_parts=limit)
            ats.record('task_split', 'Validated preflight plan before implementation',
                   task_id, planner=planner, subtasks=plan,
                   parallel_pairs=sum(len(wave) == 2 for wave in waves))
            ats.phase('planned', task_id, waves=len(waves),
                  parallel_pairs=sum(len(wave) == 2 for wave in waves))
            print(f'ATS: {sum(len(w) for w in waves)} parts in {len(waves)} waves; '
                  f'{sum(len(w) == 2 for w in waves)} parallel pairs.', flush=True)
            return plan
        last_problem = 'ATS_SUBTASKS_JSON marker missing'
    raise RuntimeError(
        'Preflight planner did not return a safe, valid task plan after two attempts: '
        + str(last_problem) + '. No implementation started.'
    )


_plan_history = {}


def plan_history():
    """What this project's earlier runs say about dividing work (plan_learning), read once per run."""
    try:
        name = ats.cleanname(ats.project().get('project', ats.APP.name))
    except RuntimeError:
        return plan_learning.history([])
    if name not in ats._plan_history:
        try:
            events = intelligence.read_events(ats.DATA / name / 'events')
        except OSError:
            events = []
        ats._plan_history[name] = plan_learning.history(events)
    return ats._plan_history[name]


def other_agent(agent):
    return ats.alternate_agent(agent)


def dag_node(plan, title):
    """The DAG v2 node (id, owned_paths, depends_on) of a part, or None for list plans."""
    if isinstance(plan, dict) and plan.get('plan_version') == 2:
        return next((n for n in plan.get('tasks', []) if n.get('title', '').strip() == title), None)
    return None


def git_identity(folder):
    """['-c', ...] for an ATS identity when git has none here (local snapshot projects), else []."""
    if ats.execute(['git', 'config', 'user.email'], folder, check=False).stdout.strip():
        return []
    return ['-c', 'user.name=ATS', '-c', 'user.email=ats@localhost']


def commit_all(folder, message):
    """Commit everything in a worktree; returns False when there was nothing to commit."""
    ats.execute(['git', 'add', '-A'], folder)
    if ats.execute(['git', 'diff', '--cached', '--quiet'], folder, check=False).returncode == 0:
        return False
    ats.execute(['git', *ats.git_identity(folder), 'commit', '-m', message], folder)
    return True


def run_part(task, plan, number, total, part, task_id, folder, branch, implementer, unavailable, conf,
             base, quiet=False, alongside=None, resume_work=False, prior_cycles=()):
    """Implement one part and put it through the review loop. Returns (approved, reviewer, author, cycles)."""
    description = f'{task}\nSubtask {number}/{total}: {part}'
    # The reviewer sees only this part's diff; with the plan it does not flag work that belongs
    # to another part as missing.
    review_task = (description + '\nFull plan (review ONLY this part; other parts are reviewed '
                   'separately): ' + json.dumps(plan, ensure_ascii=False))
    together = (f'Another agent builds part {alongside[0]} ({alongside[1]}) AT THE SAME TIME in a separate '
                'copy; do not implement it and avoid changing the same code. ' if alongside else '')
    node = ats.dag_node(plan, part)
    reference = style_guard.reference_context(folder, node)
    description += reference
    review_task += reference
    owned = list(node['owned_paths']) if node else []
    carried = review_handoff.relevant_findings(prior_cycles, part, owned)
    previous_note = review_handoff.handoff_note(carried)
    owned_open = findings_ledger.for_owner(findings_ledger.build(prior_cycles, plan), node['id']) if node else []
    if owned_open:
        # Context is not ownership: findings in this part's files are this part's to fix.
        previous_note = findings_ledger.owner_note(owned_open) + previous_note
        ats.record('review_handoff', 'Open findings in files this part owns', task_id, subtask=number,
               count=len(owned_open), keys=[e['key'] for e in owned_open], owned=True)
    if carried:
        ats.record('review_handoff', 'Relevant earlier review findings passed to next part',
               task_id, subtask=number, count=len(carried),
               from_subtasks=sorted({v['from_part'] for v in carried if isinstance(v.get('from_part'), int)}))

    ownership = ('Files this part owns: ' + ', '.join(owned) + '. Change other files only when unavoidable, '
                 'and say which and why. ' if owned else '')
    if isinstance(plan, dict) and plan.get('release_owner') and node:
        release = ats.release_files(conf)
        if node['id'] == plan['release_owner']:
            ownership += ('This part owns the release files (' + ', '.join(release) + '): raise the version once '
                          'for the whole task and start its changelog section; later parts do not raise it again. ')
        else:
            ownership += ('Do NOT edit the release files (' + ', '.join(release) + '): the first part raised the '
                          'version and ATS updates the notes for the combined change at the end. ')
    if alongside:
        sibling = ats.dag_node(plan, alongside[1])
        shared = [f for f in plan_learning.shared_files(ats.plan_history()) if not dag_planning.paths_conflict([f], owned)] \
            if owned else plan_learning.shared_files(ats.plan_history())
        off_limits = (list(sibling['owned_paths']) if sibling else []) + shared
        if off_limits:
            ownership += ('Do NOT edit these files (the parallel part owns them, or earlier runs showed they cause '
                          'conflicts): ' + ', '.join(off_limits[:20]) + '; ATS blocks the part if you do. ')
    contract_notes = ''
    if node:
        provided = node.get('contracts_provided', [])
        required = node.get('contracts_required', [])
        if provided:
            contract_notes += ('This part defines shared contracts: ' + ', '.join(provided) +
                               '. Document and test their final interfaces before downstream work. ')
        if required:
            contract_notes += ('Consume reviewed contract interfaces: ' + ', '.join(required) +
                               '. Do not silently change the shared contract. ')
    ats.phase('implementation', task_id, implementer=implementer, subtask=number)
    budget = budgets.Budget.for_part(conf, owned)
    usage_meter.set_context(budget=budget)
    ats.record('budget', f'Budget for part {number}', task_id, subtask=number, **budget.snapshot())
    prompt = (ats.shared_prefix(folder, conf, task_id)
              + 'ROLE: implementer of one part of a planned task.\n'
              'Earlier parts have passed independent review. Preserve them. Add tests. '
              'Do not commit, push, merge, deploy, trade, expose secrets or modify .ats; '
              'ATS commits the work after the review.\n'
              'At the end include ONE LINE ATS_LEARNINGS_JSON: [...] with evidence-backed '
              'items, or ATS_LEARNINGS_JSON: [] when there are none.\n\n'
              f'Overall plan: {json.dumps(plan, ensure_ascii=False)}\n'
              f'Implement ONLY this part: {description}\n' + together + ownership
              + contract_notes + previous_note + ats.part_context(folder, conf, part, owned)
              + plan_learning.self_check_note(ats.plan_history()))
    existing_changes = False
    if resume_work:
        base_tree = ats.execute(['git', 'rev-parse', str(base) + '^{tree}'], folder).stdout.strip()
        existing_changes = ats.index_tree(folder) != base_tree
    if existing_changes:
        author = implementer
        ats.record('subtask_fix_only', 'Repairing unfinished changes in the resumed worktree',
               task_id, subtask=number, implementer=author)
    else:
        try:
            author, output = ats.invoke_with_fallback(implementer, prompt, folder, unavailable,
                                                  task_id, 'implementation', quiet=quiet, fallback=False)
        except (AgentUsageExhausted, RuntimeError) as exc:
            ats.record('escalated', str(exc), task_id, subtask=number, implementer=implementer, branch=branch,
                   blocking_reasons=[f'subtask {number}: {exc}'])
            return False, None, implementer, []
        ats.record('implementation', output[-1600:], task_id, implementer=author, subtask=number)
        ats.capture_learning_proposals(output, task_id)
    reserved = []
    if alongside:
        sibling = ats.dag_node(plan, alongside[1])
        reserved = list(sibling['owned_paths']) if sibling else []
        reserved += [f for f in plan_learning.shared_files(ats.plan_history()) if f not in reserved]
    if isinstance(plan, dict) and plan.get('release_owner') and node and node['id'] != plan['release_owner']:
        # Version and changelog belong to the first part; the fix-up step updates the notes at the end.
        reserved += [f for f in ats.release_files(conf) if f not in reserved]
    try:
        approved, reviewer, author, cycles = ats.review_loop(
            review_task, task_id, folder, branch, author, unavailable, conf,
            quiet=quiet, base=base, subtask=number, owned_paths=owned,
            earlier_review_notes=previous_note, reserved_paths=reserved,
            reference_screen=(node or {}).get('reference_screen'))
    except RuntimeError as exc:   # e.g. the part produced no changes
        ats.record('escalated', str(exc), task_id, subtask=number, implementer=author, branch=branch,
               blocking_reasons=[f'subtask {number}: {exc}'])
        return False, None, author, []
    for cycle in cycles:
        cycle['subtask'] = number
        cycle['implementer'] = author
    if approved:
        ats.record('subtask_completed', part, task_id, subtask=number, implementer=author,
               reviewer=reviewer, branch=branch,
               contracts_provided=node.get('contracts_provided', []) if node else [])
    return approved, reviewer, author, cycles


def merge_parallel_part(task, plan, number, part, task_id, folder, branch, side_branch, author,
                        unavailable, conf, quiet=False):
    """Merge a part built in a side worktree into the task worktree.

    A clean merge with passing checks is done. Otherwise the agent that built the side part resolves
    the conflict (or fixes the failing checks) and the other agent reviews that resolution.
    Returns (approved, reviewer, cycles).
    """
    ats.phase('merge', task_id, subtask=number)
    pre_merge = ats.execute(['git', 'rev-parse', 'HEAD'], folder).stdout.strip()
    merged = ats.execute(['git', *ats.git_identity(folder), 'merge', '--no-ff', '--no-edit', '-m',
                      f'ATS: merge part {number}', side_branch], folder, check=False)
    if merged.returncode == 0:
        checks = ats.run_checks(conf, folder)
        if all(c['passed'] for c in checks):
            ats.record('subtask_merged', f'Part {number} merged cleanly', task_id, subtask=number, branch=branch)
            return True, None, []
        problem = ('The combined parts fail these checks; fix them without dropping either part:\n'
                   + json.dumps([{'command': c['command'], 'output': c['output'][-1500:]}
                                 for c in checks if not c['passed']], ensure_ascii=False))
    else:
        conflicts = ats.execute(['git', 'diff', '--name-only', '--diff-filter=U'], folder, check=False).stdout.split()
        problem = ('Merging your part into the other part produced conflicts in: ' + ', '.join(conflicts[:30])
                   + '. Resolve every conflict marker so BOTH parts keep working.')
    ats.record('merge_conflict', problem[:1200], task_id, subtask=number, implementer=author, branch=branch,
           files=conflicts[:50] if merged.returncode else [])
    prompt = (ats.shared_prefix(folder, conf, task_id) + 'ROLE: implementer, integrating parallel work.\n'
              f'Task: {task}\n'
              f'Overall plan: {json.dumps(plan, ensure_ascii=False)}\n'
              f'You built part {number} ({part}) in parallel with another part. {problem}\n'
              'Change only what the integration needs. Do not commit, push or modify .ats.')
    try:
        fixer, output = ats.invoke_with_fallback(author, prompt, folder, unavailable, task_id, 'implementation',
                                             quiet=quiet, fallback=False)
    except (AgentUsageExhausted, RuntimeError) as exc:
        ats.record('escalated', str(exc), task_id, subtask=number, branch=branch,
               blocking_reasons=[f'merge of part {number}: {exc}'])
        return False, None, []
    ats.record('implementation', output[-1600:], task_id, implementer=fixer, subtask=number)
    review_task = (f'{task}\nIntegration of part {number} ({part}) with the parts before it. The diff '
                   'is that part plus the merge resolution; check that both parts keep working.')
    try:
        approved, reviewer, fixer, cycles = ats.review_loop(review_task, task_id, folder, branch, fixer, unavailable,
                                                        conf, quiet=quiet, base=pre_merge, subtask=number,
                                                        kind='merge')
    except RuntimeError as exc:
        ats.record('escalated', str(exc), task_id, subtask=number, branch=branch,
               blocking_reasons=[f'merge of part {number}: {exc}'])
        return False, None, []
    for cycle in cycles:
        cycle['subtask'] = number
        cycle['implementer'] = fixer
        cycle['merge'] = True
    if approved:
        ats.execute(['git', 'add', '-A'], folder)
        if ats.execute(['git', 'rev-parse', '-q', '--verify', 'MERGE_HEAD'], folder, check=False).returncode == 0:
            ats.execute(['git', *ats.git_identity(folder), 'commit', '--no-edit'], folder)
        else:
            ats.commit_all(folder, f'ATS: integrate part {number}')
        ats.record('subtask_merged', f'Part {number} merged after a reviewed resolution', task_id,
               subtask=number, implementer=fixer, reviewer=reviewer, branch=branch)
    return approved, reviewer, cycles


def subtask_flow(task, plan, task_id, folder, branch, implementer, unavailable, conf,
                 quiet=False, resumed=None):
    """Build a split task part by part; independent pairs are built and reviewed in parallel.

    A single part: one agent implements, the other reviews, and the reviewer builds the next part.
    A pair: both agents implement one part at the same time (the second in its own worktree), each
    reviews the other's part, then ATS merges the pair (see merge_parallel_part).
    """
    waves = ats.plan_waves(plan)
    if waves is None:
        raise RuntimeError('Task plan must contain 2 to 20 non-empty parts (strings, or pairs of strings)')
    total = sum(len(w) for w in waves)
    history = []
    attribution = list((resumed or {}).get('attribution', []))
    completed = int((resumed or {}).get('completed', 0))
    reviewer, author, number = None, implementer, 0
    for wave in waves:
        if number + len(wave) <= completed:
            for skipped in range(number + 1, number + len(wave) + 1):
                ats.record('subtask_resumed', 'Reusing independently reviewed code at a verified Git checkpoint',
                       task_id, subtask=skipped, branch=branch)
            number += len(wave)
            old = next((a for a in reversed(attribution) if a.get('subtask') == number), None)
            if old:
                reviewer, author = old.get('reviewer'), old.get('implementer')
                implementer = reviewer or implementer
            continue
        if number < completed:
            raise RuntimeError('Checkpoint splits a parallel wave; refusing unsafe resume')
        other = ats.alternate_agent(implementer, unavailable)
        if implementer in unavailable or other is None:
            ats.record('escalated', 'Both agents are required for subtask handoffs', task_id,
                   subtask=number + 1, branch=branch)
            return False, reviewer, implementer, history
        if len(wave) == 1:
            number += 1
            approved, reviewer, author, cycles = ats.run_part(
                task, plan, number, total, wave[0], task_id, folder, branch, implementer, unavailable, conf,
                base=(ats.execute(['git', 'rev-parse', 'HEAD^{tree}'], folder).stdout.strip()
                      if resumed else ats.index_tree(folder)), quiet=quiet,
                prior_cycles=history,
                **({'resume_work': True} if resumed else {}))
            history.extend(cycles)
            if not approved:
                return False, reviewer, author, history
            attribution.append(dict(subtask=number, implementer=author, reviewer=reviewer))
            ats.commit_all(folder, f'ATS: reviewed part {number}')
            checkpoints.save_checkpoint(ats.DATA, task_id, plan, folder,
                                        ats.execute(['git', 'rev-parse', 'HEAD'], folder).stdout.strip(),
                                        number, attribution)
            implementer = reviewer or other
            continue

        # ---- a pair, in parallel: part `first` here, part `second` in a side worktree
        first, second = number + 1, number + 2
        number += 2
        pending = {p['subtask']: p for p in (resumed or {}).get('pending', [])
                   if p['subtask'] in (first, second)}
        if not resumed:
            ats.commit_all(folder, f'ATS: reviewed parts 1-{first - 1}' if first > 1 else 'ATS: start of parallel parts')
        wave_base = (next(iter(pending.values()))['wave_base'] if pending else
                     ats.execute(['git', 'rev-parse', 'HEAD'], folder).stdout.strip())
        side = folder.parent / f'{folder.name}-part{second}'
        side_branch = f'{branch}-part{second}'
        if side.exists():
            if not resumed:
                raise RuntimeError('Parallel side worktree unexpectedly exists: ' + str(side))
            actual_branch = ats.execute(['git', 'symbolic-ref', '--quiet', '--short', 'HEAD'], side).stdout.strip()
            if actual_branch != side_branch:
                raise RuntimeError('Parallel side branch differs from the checkpoint')
        else:
            if second in pending:
                raise RuntimeError('Approved parallel node worktree disappeared; cannot reuse review')
            ats.execute(['git', 'worktree', 'add', '-b', side_branch, str(side), wave_base], folder)
        ats.phase('parallel', task_id, subtasks=[first, second], implementers=[implementer, other])
        results = {}

        def from_checkpoint(number, location):
            saved = pending.get(number)
            if not saved:
                return None
            if Path(saved['worktree']).resolve() != location.resolve():
                raise RuntimeError('Approved DAG node worktree location changed')
            head = ats.execute(['git', 'rev-parse', 'HEAD'], location).stdout.strip()
            dirty = ats.execute(['git', 'status', '--porcelain'], location).stdout.strip()
            if head != saved['commit'] or dirty:
                raise RuntimeError('Approved DAG node snapshot changed: cannot skip review')
            ats.record('subtask_resumed', 'Reusing independently reviewed parallel node',
                   task_id, subtask=number, implementer=saved['implementer'],
                   reviewer=saved['reviewer'], branch=branch)
            return True, saved['reviewer'], saved['implementer'], []

        reuse_a = from_checkpoint(first, folder)
        reuse_b = from_checkpoint(second, side)
        if reuse_a:
            results['a'] = reuse_a
        if reuse_b:
            results['b'] = reuse_b

        timing = {}

        def build(key, *args):
            started = time.monotonic()
            try:
                results[key] = ats.run_part(*args, **({'resume_work': True} if resumed else {}))
            except Exception as exc:
                ats.record('escalated', str(exc), task_id, subtask=args[2], branch=branch,
                       blocking_reasons=[f'subtask {args[2]}: {exc}'])
                results[key] = (False, None, args[8], [])
            finally:
                timing[key] = round(time.monotonic() - started, 1)

        threads = []
        if not reuse_a:
            threads.append(threading.Thread(
                target=build, args=('a', task, plan, first, total, wave[0], task_id, folder, branch,
                                    implementer, unavailable, conf, wave_base, quiet, (second, wave[1]), False, tuple(history))))
        if not reuse_b:
            threads.append(threading.Thread(
                target=build, args=('b', task, plan, second, total, wave[1], task_id, side, branch,
                                    other, unavailable, conf, wave_base, quiet, (first, wave[0]), False, tuple(history))))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if len(timing) == 2:
            # How long the faster part waited for the slower one: the cost of the wave barrier.
            ats.record('wave_timing', f'Parts {first} and {second}: {timing["a"]}s and {timing["b"]}s', task_id,
                   subtasks=[first, second], seconds=[timing['a'], timing['b']],
                   idle_seconds=round(abs(timing['a'] - timing['b']), 1), branch=branch)
        (ok_a, reviewer_a, author_a, cycles_a), (ok_b, reviewer_b, author_b, cycles_b) = results['a'], results['b']
        history.extend(cycles_a + cycles_b)
        if ok_a:
            ats.commit_all(folder, f'ATS: part {first}')
        if ok_b:
            ats.commit_all(side, f'ATS: part {second}')
        if not (ok_a and ok_b):
            approved_pending = []
            for ok, number_in_pair, directory, writer, judge in (
                    (ok_a, first, folder, author_a, reviewer_a),
                    (ok_b, second, side, author_b, reviewer_b)):
                if ok and judge:
                    approved_pending.append({
                        'subtask': number_in_pair, 'worktree': str(directory.resolve()),
                        'commit': ats.execute(['git', 'rev-parse', 'HEAD'], directory).stdout.strip(),
                        'wave_base': wave_base, 'implementer': writer, 'reviewer': judge,
                    })
            if approved_pending:
                checkpoints.save_checkpoint(
                    ats.DATA, task_id, plan, folder,
                    ats.execute(['git', 'rev-parse', 'HEAD'], folder).stdout.strip(),
                    first - 1, attribution, pending=approved_pending)
            print(f'ATS: part {first}/{second} pending; only unapproved DAG nodes require repair',
                  file=sys.stderr)
            return False, reviewer_a or reviewer_b, author_a, history
        attribution.append(dict(subtask=first, implementer=author_a, reviewer=reviewer_a))
        attribution.append(dict(subtask=second, implementer=author_b, reviewer=reviewer_b))
        merged, merge_reviewer, merge_cycles = ats.merge_parallel_part(
            task, plan, second, wave[1], task_id, folder, branch, side_branch, author_b, unavailable, conf,
            quiet=quiet)
        history.extend(merge_cycles)
        if not merged:
            print(f'ATS: merging part {second} needs a human decision; side worktree kept: {side}',
                  file=sys.stderr)
            return False, merge_reviewer, author_b, history
        ats.execute(['git', 'worktree', 'remove', '--force', str(side)], folder, check=False)
        ats.execute(['git', 'branch', '-D', side_branch], folder, check=False)
        checkpoints.save_checkpoint(ats.DATA, task_id, plan, folder,
                                    ats.execute(['git', 'rev-parse', 'HEAD'], folder).stdout.strip(),
                                    number, attribution)
        reviewer, author = reviewer_b, author_b
        implementer = other   # the next wave starts with the other agent
    pairs = sum(len(w) == 2 for w in waves)
    combined = False
    ledger = findings_ledger.build(history, plan)
    if conf.get('integration_review', True) and (pairs or total > 2):
        open_items = findings_ledger.needs_attention(ledger)
        release = ats.release_files(conf) if isinstance(plan, dict) and plan.get('release_owner') else []
        fixer = ats.alternate_agent(author, unavailable) or author   # the last reviewer knows the combined code
        if conf.get('integration_fixup', True):
            fixer, _ = ats.integration_fixup(task, plan, task_id, folder, fixer, unavailable, conf, open_items,
                                         release, quiet=quiet)
        ok, judge, author, cycles = ats.integration_review(task, plan, total, task_id, folder, branch, fixer,
                                                       unavailable, conf, quiet=quiet,
                                                       notes=findings_ledger.integration_note(open_items))
        history.extend(cycles)
        if cycles:
            ledger = findings_ledger.resolve(ledger, [f['id'] for f in cycles[-1]['findings']])
        if not ok:
            ats.record_ledger(task_id, ledger + findings_ledger.build(cycles))
            return False, judge, author, history
        combined = judge is not None
        reviewer = judge or reviewer
        ledger = ledger + [e for e in findings_ledger.build(cycles)
                           if not any(e['key'].endswith('/' + o['key']) for o in ledger)]
    ats.record_ledger(task_id, ledger, combined=combined)
    ats.record('subtasks_completed', 'All parts independently reviewed' + (
               '; the combined change passed an integration review' if combined else
               '; combined diff has not been reviewed'),
           task_id, parts=attribution, branch=branch, combined_review=combined)
    return True, reviewer, author, history


RELEASE_CANDIDATES = ('CHANGELOG.md', 'CHANGELOG', 'CHANGES.md', 'VERSION', 'version.txt')


VERSION_ASSIGNMENT = re.compile(r'^(__version__|VERSION)\s*=\s*["\']\d+\.\d+', re.MULTILINE)


def release_files(conf, root=None):
    """The files every change bumps (version and changelog): `release_files`, or detected.

    Detected: a changelog or VERSION file at the top, and a package __init__.py one level down that
    assigns VERSION or __version__. When several parts each bumped these, they conflicted; ATS gives
    them to one part (assign_release_files).
    """
    configured = (conf or {}).get('release_files')
    if isinstance(configured, list):
        return [str(p) for p in configured if isinstance(p, str) and p.strip()]
    root = Path(root or ats.APP)
    found = [name for name in ats.RELEASE_CANDIDATES if (root / name).is_file()]
    for init in sorted(root.glob('*/__init__.py'))[:30]:
        try:
            if ats.VERSION_ASSIGNMENT.search(init.read_text(encoding='utf-8', errors='replace')[:4000]):
                found.append(init.relative_to(root).as_posix())
        except OSError:
            continue
    return found


def release_note(conf):
    files = ats.release_files(conf)
    if not files:
        return ''
    return ('\nRelease files (version and changelog): ' + ', '.join(files) + '. Give them to the FIRST part '
            '(no dependencies); no other part owns or edits them. ATS enforces this and updates the notes at the end.')


def assign_release_files(plan, files):
    """Give the release files to the FIRST part of a DAG plan and to no other part.

    The first part is the one that has to pass a version check first; later parts no longer need
    to bump (the check compares with the base branch), so they keep their hands off these files
    and the fix-up step before the integration review updates the notes for the whole change.
    """
    if not files or not isinstance(plan, dict) or not plan.get('tasks'):
        return plan
    tasks = [dict(node) for node in plan['tasks']]
    for node in tasks:
        node['owned_paths'] = [p for p in node.get('owned_paths', [])
                               if not dag_planning.paths_conflict([p], files)]
    first = next((n for n in tasks if not n.get('depends_on')), tasks[0])
    first['owned_paths'] = list(dict.fromkeys(first['owned_paths'] + list(files)))
    return dict(plan, tasks=tasks, release_owner=first['id'])


def integration_fixup(task, plan, task_id, folder, implementer, unavailable, conf, open_items, release,
                      quiet=False):
    """Before the integration review: fix the findings still open from the part reviews, and bring
    the release notes in line with the combined change. One agent call; skipped when nothing to do."""
    if not open_items and not release:
        return implementer, []
    work = []
    if open_items:
        work.append(findings_ledger.note(open_items, 'Fix these findings that are still open from the reviews of '
                                                     'the parts (any file may be changed for them): '))
    if release:
        work.append('Update the release notes in ' + ', '.join(release) + ' so the section of the current version '
                    'describes the combined change of all parts (no text about later parts); do not raise the '
                    'version again.')
    prompt = (ats.shared_prefix(folder, conf, task_id) + 'ROLE: implementer, finishing a planned task before its '
              'integration review. Do not commit, push or modify .ats; ATS commits after the review.\n'
              f'Task: {task}\nPlan: {json.dumps(plan, ensure_ascii=False)}\n' + '\n'.join(work)
              + '\nEnd with ONE LINE ATS_RESPONSES_JSON: [{"id":"<key>","status":"fixed|not_applicable|deferred",'
              '"note":"<evidence>"}] for every finding key above (or [] when there were none).')
    ats.phase('revision', task_id, implementer=implementer, integration=True)
    try:
        implementer, output = ats.invoke_with_fallback(implementer, prompt, folder, unavailable, task_id, 'revision',
                                                   quiet=quiet)
    except (AgentUsageExhausted, RuntimeError) as exc:
        ats.record('findings_fixup', 'Fix-up before the integration review failed: ' + str(exc)[:300], task_id,
               implementer=implementer, keys=[e['key'] for e in open_items])
        return implementer, []
    responses = ats.normalize_responses(ats.parse_marked_json(output, 'ATS_RESPONSES_JSON'))
    ats.record('findings_fixup', f'{len(open_items)} open findings handed to the fix-up step', task_id,
           implementer=implementer, keys=[e['key'] for e in open_items], responses=responses,
           release_files=release)
    ats.capture_learning_proposals(output, task_id)
    return implementer, responses


def record_ledger(task_id, ledger, combined=None):
    """The findings register of the task, for the run report and the PR description."""
    open_items = findings_ledger.needs_attention(ledger)
    ats._run_flags.setdefault(task_id, {})['ledger'] = ledger
    if combined is not None:
        ats._run_flags[task_id]['combined_review'] = combined
    ats.record('findings_ledger', f'{len(ledger)} findings, {len(open_items)} still open', task_id,
           entries=ledger[:100], open=[e['key'] for e in open_items])


def integration_base(folder, conf):
    """Where the task branch left the base branch: the combined change of all parts starts there."""
    ref = 'origin/' + conf.get('base_branch', 'main')
    found = ats.execute(['git', 'merge-base', 'HEAD', ref], folder, check=False).stdout.strip()
    if found:
        return found
    roots = ats.execute(['git', 'rev-list', '--max-parents=0', 'HEAD'], folder, check=False).stdout.split()
    return roots[0] if roots else 'HEAD'


def integration_review(task, plan, total, task_id, folder, branch, implementer, unavailable, conf, quiet=False,
                       notes=''):
    """One review of the combined change after all parts: only how the parts work together.

    Parts are reviewed one by one, so problems between them (interfaces, duplicated logic, wiring)
    can slip through. Returns (approved, reviewer, implementer, cycles); a combined diff too large to
    review completely is skipped (recorded), not escalated.
    """
    base = ats.integration_base(folder, conf)
    diff = ats.execute(['git', 'diff', base, 'HEAD'], folder, check=False).stdout
    if not diff.strip():
        return True, None, implementer, []
    if len(diff) > int(conf.get('max_review_diff_chars', 150000)):
        ats.record('integration_review', 'Skipped: the combined change is too large to review completely',
               task_id, branch=branch, skipped=True, approved=None, files=[])
        return True, None, implementer, []
    usage_meter.set_context(budget=budgets.Budget.for_part(conf))
    review_task = (f'{task}\nINTEGRATION REVIEW of the combined change of all {total} parts. Every part already '
                   'passed its own independent review: do not repeat per-part findings. Look only at how the parts '
                   'work together: interfaces and data passed between parts, duplicated or conflicting logic, '
                   'wiring and configuration, end-to-end behaviour, and whether tests cover the combination. '
                   'Plan: ' + json.dumps(plan, ensure_ascii=False))
    settings = dict(conf, max_revisions=min(1, int(conf.get('max_revisions', 2))))
    try:
        approved, reviewer, implementer, cycles = ats.review_loop(review_task, task_id, folder, branch, implementer,
                                                              unavailable, settings, quiet=quiet, base=base,
                                                              earlier_review_notes=notes, kind='integration')
    except RuntimeError as exc:
        ats.record('integration_review', 'Integration review could not run: ' + str(exc)[:300], task_id,
               branch=branch, approved=False, files=[])
        return False, None, implementer, []
    for cycle in cycles:
        cycle['integration'] = True
    files = sorted({f['file'] for c in cycles for f in c['findings'] if f.get('file')})
    ats.record('integration_review', 'Combined change ' + ('approved' if approved else 'not approved'), task_id,
           branch=branch, approved=approved, reviewer=reviewer, cycles=len(cycles), files=files[:50])
    if approved:
        ats.commit_all(folder, 'ATS: integration review')
    return approved, reviewer, implementer, cycles


def distill_learnings(task, task_id, folder, history, preferred, unavailable, quiet=False):
    """After the reviews: turn the findings into reusable learnings for future tasks.

    One short, read-only agent call (the reviewer, else the other agent). Low-risk learnings are
    approved automatically; drastic ones wait for a human. Never fails the task.
    """
    findings = []
    for cycle in history:
        answers = {r.get('id'): r for r in cycle.get('responses') or []}
        for finding in cycle.get('findings') or []:
            answer = answers.get(finding.get('id')) or {}
            findings.append({k: v for k, v in {
                'id': finding.get('id'), 'severity': finding.get('severity'),
                'where': f"{finding.get('file') or ''}:{finding.get('line') or ''}".strip(':'),
                'summary': finding.get('summary'), 'cycle': cycle.get('cycle'), 'part': cycle.get('subtask'),
                'answer': answer.get('status'), 'note': answer.get('note')}.items() if v not in (None, '')})
    if not findings:
        return []
    known = sorted(ats.known_titles())[:60]
    prompt = (f'Read {ats.ATS/"POLICY.md"}. You are distilling lessons from a finished ATS task. Do NOT change any '
              f'files.\nTask: {task}\nReview findings and the implementer\'s answers: '
              + json.dumps(findings[:40], ensure_ascii=False) +
              '\nAlready known (do not repeat): ' + json.dumps(known, ensure_ascii=False) +
              '\nWrite at most 5 learnings that help FUTURE tasks avoid these problems: general and actionable, '
              'not a description of this change; skip one-off typos. kind: "lesson" (insight), "rule" (always/never '
              'do X) or "pattern". scope: "shared" when it applies to any project, else "project". risk: "high" only '
              'if following it could delete or overwrite data, rewrite git history, weaken review/tests/security, '
              'touch credentials or money, or do something irreversible; otherwise "low". evidence: file:line or '
              'finding id. For style/visual consistency findings, propose an actionable project-scoped rule '
              'that names the existing component, design token or reference screen to reuse. Do not generalize '
              'one project palette into a shared rule. Only describe a fix as established when a later '
              'independent review confirms it; unresolved findings are evidence for investigation, not proven fixes. '
              'Keep the existing risk checks and never weaken validation.\nEnd with ONE LINE: ATS_LEARNINGS_JSON: [{"kind":"lesson","title":"<title>",'
              '"observation":"<observation>","evidence":"<evidence>","scope":"project","risk":"low"}] '
              '(replace the placeholders, or use []).')
    tree = ats.index_tree(folder)
    try:
        ats.phase('learning', task_id, agent=preferred)
        agent, output = ats.invoke_with_fallback(preferred, prompt, folder, unavailable, task_id, 'learning',
                                             quiet=quiet)
        saved = ats.capture_learning_proposals(output.replace(prompt, ''), task_id)
    except Exception as exc:   # learning is a bonus; the task result stands
        print(f'ATS: distilling learnings skipped: {str(exc)[:200]}', file=sys.stderr)
        return []
    finally:
        changed = ats.restore_if_changed(folder, tree)
        if changed:
            print('ATS: the learning step changed files; reverted: ' + ', '.join(changed[:10]), file=sys.stderr)
    waiting = [item for item in saved if item['status'] == 'proposed']
    if saved:
        ats.record('learnings', f'{len(saved)} learnings distilled ({len(waiting)} need a human)', task_id,
               learnings=[{'id': i['id'], 'title': i['title'], 'status': i['status'],
                           'needs_human': i.get('needs_human')} for i in saved])
        print(f'ATS: {len(saved)} learnings stored; {len(waiting)} need your approval.', flush=True)
    return saved
