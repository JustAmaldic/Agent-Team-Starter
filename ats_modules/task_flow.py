"""A whole task run: plan selection, base updates, PR conflicts, finishing and the small commands.

Moved out of ats.py (t67). Names that live in ats.py are reached as `ats.<name>`,
looked up at call time, so behaviour and test patches on the ats module are unchanged."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import tempfile
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
import ats_intelligence as intelligence
from ats_modules import checkpoints
from ats_modules import dag as dag_planning
from ats_modules import budget as budgets
from ats_modules import plan_learning
from ats_modules import write_scope
from ats_modules import merge_guard
from ats_modules import run_report
from ats_modules import findings_ledger
from ats_modules import screen_map
from ats_modules import snapshots
from ats_modules import usage as usage_meter
from ats_modules.agent_limits import BothAgentsExhausted
from ats_modules.agent_limits import AgentUsageExhausted

import ats

__all__ = ['preset_task_id', 'preview_task_plan', 'provided_task_plan', 'PR_CONFLICT_RULES', 'BRANCH_NAME', '_show', 'merge_guard_note', 'fix_pr_conflicts', 'recheck_source', 'task_flow', 'save_run_report', '_task_flow', 'finish_task', 'pr_create_command', 'dependency_lines', 'AGENT_CHECK_REVIEW', 'AGENT_CHECK_RESPONSE', 'agent_version', 'agent_check', 'SCREEN_TASK', 'SCREEN_TASK_LIMIT', 'screen_task', 'screens_command', 'child_run', 'main_check', 'pull_request_argument', 'positive_days']


def preset_task_id():
    """ATS_TASK_ID lets a caller know the task id before the run starts."""
    value = os.environ.get('ATS_TASK_ID', '').strip()
    if not value:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise RuntimeError('ATS_TASK_ID must be a UUID: ' + value)


def preview_task_plan(task, agent='auto', quiet=True):
    """Run ATS's actual preflight planner in a disposable read-only snapshot."""
    repository = ats.execute(['git', 'rev-parse', '--show-toplevel'], check=False)
    local_mode = repository.returncode != 0
    if not local_mode and Path(repository.stdout.strip()).resolve() != ats.APP.resolve():
        raise RuntimeError('Run ATS from the project repository root')
    selected = ats.select(task) if agent == 'auto' else agent
    if selected not in ats.enabled_agents():
        raise RuntimeError('Selected planning agent is disabled')
    unavailable = ats.UnavailableAgents()
    task_id = str(uuid.uuid4())
    folder = ats.APP.parent / f'{ats.APP.name}-ats-plan-{task_id[:8]}'
    try:
        if local_mode:
            ats.make_local_workspace(folder, 'ats/plan-' + task_id[:8])
        else:
            ats.execute(['git', 'worktree', 'add', '--detach', str(folder), 'HEAD'])
        plan = ats.preplan_task(task, task_id, folder, selected, unavailable, ats.project(),
                            quiet=quiet, mode='auto' if ats.should_preplan(task) else 'always')
        print('ATS_PLAN_JSON: ' + json.dumps(plan, ensure_ascii=False), flush=True)
        return plan
    finally:
        if local_mode:
            shutil.rmtree(folder, ignore_errors=True)
        else:
            ats.execute(['git', 'worktree', 'remove', '--force', str(folder)], check=False)


def provided_task_plan(path, task):
    """Validate explicit user-edited plan from the app; never trust plan file paths as code."""
    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.stat().st_size > 32768:
        raise RuntimeError('Plan file missing or too large')
    payload = json.loads(source.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or payload.get('task') != task:
        raise RuntimeError('Plan is not bound to the requested task')
    if payload.get('plan_version') == 2:
        try:
            graph = dag_planning.validate_dag(payload)
            return graph
        except ValueError as exc:
            raise RuntimeError('Invalid DAG task plan: ' + str(exc)) from exc
    plan = payload.get('waves')
    if not ats.valid_plan(plan):
        raise RuntimeError('Invalid custom task plan (2-20 parts, up to 2 per wave)')
    return plan


PR_CONFLICT_RULES = (
    ' This branch is pull request #{number}. KEEP THE FUNCTIONALITY OF BOTH SIDES: every function, class, test, '
    'route, setting, UI element and text that exists on either side must still exist and work after your '
    'resolution, unless one side removed it on purpose. Never resolve a conflict by taking one side wholesale: '
    'read both versions, understand what each side intended and combine them. Where both sides changed the same '
    'lines, write code that does both. In version and changelog files keep the entries of both sides (the pull '
    'request gets a version above the one on {base}). Do not remove or weaken tests to make the merge pass. '
    'Change nothing that the merge does not need.')


BRANCH_NAME = re.compile(r'(?!-)[A-Za-z0-9._/-]{1,200}')


def _show(folder, ref, path):
    out = ats.execute(['git', 'show', f'{ref}:{path}'], folder, check=False)
    return out.stdout if out.returncode == 0 else None


def merge_guard_note(folder, task_id, ours, theirs):
    """Compare what both sides define with the resolved files; record and return the note for the reviewer."""
    ancestor = ats.execute(['git', 'merge-base', ours, theirs], folder, check=False).stdout.strip()
    if not ancestor:
        return ''
    changed = lambda ref: set(ats.execute(['git', 'diff', '--name-only', ancestor, ref], folder, check=False).stdout.split('\n'))
    both = sorted(p for p in changed(ours) & changed(theirs) if p)[:200]
    files = {}
    for path in both:
        target = Path(folder) / path
        try:
            resolved = target.read_text(encoding='utf-8', errors='replace') if target.is_file() else None
        except OSError:
            resolved = None
        files[path] = (ats._show(folder, ancestor, path), ats._show(folder, ours, path), ats._show(folder, theirs, path), resolved)
    found = merge_guard.check(files)
    ats.record('merge_guard', f'{len(found)} files lost names in the conflict resolution' if found
           else 'The conflict resolution kept every name of both sides', task_id, files=found[:30],
           checked=len(both))
    return merge_guard.note(found)


def fix_pr_conflicts(task, task_id, folder, branch, conf, pr, agent, unavailable, push, quiet=False):
    """Resolve the merge conflicts of an open pull request and update its branch.

    The worktree is on a new ATS branch that starts at the PR's head. The PR's base is merged in; an
    agent resolves the conflicts under PR_CONFLICT_RULES, the merge guard lists names that got lost,
    the other agent reviews the whole change and every test command must pass. Only then is the PR's
    own branch updated, with a normal fast-forward push (never forced). ATS never merges the PR.
    """
    number, head, base = pr['number'], pr['head'], pr['base']
    ours = ats.execute(['git', 'rev-parse', 'HEAD'], folder).stdout.strip()
    theirs = f'origin/{base}'
    pr_conf = dict(conf, base_branch=base)
    ok, cycles = ats.update_from_base(
        task, task_id, folder, branch, pr_conf, agent, unavailable, quiet=quiet,
        rules=ats.PR_CONFLICT_RULES.format(number=number, base=base),
        guard=lambda where: ats.merge_guard_note(where, task_id, ours, theirs))
    merged = ats.execute(['git', 'merge-base', '--is-ancestor', theirs, 'HEAD'], folder, check=False).returncode == 0
    if not ok or not merged:
        if ok:   # nothing was merged (for example the fetch failed): never report a fix that did not happen
            reason = f'could not merge {theirs} into pull request #{number}; nothing was changed'
            ats.record('escalated', reason, task_id, branch=branch, blocking_reasons=[reason])
        print(f'ATS: the conflicts of pull request #{number} were not resolved; its branch is unchanged.',
              file=sys.stderr)
        ats.phase('finished', task_id, approved=False, branch=branch, worktree=str(folder))
        return 'escalated'
    if ats.execute(['git', 'rev-parse', 'HEAD'], folder).stdout.strip() == ours:
        reason = f'pull request #{number} already contains {theirs}; there is nothing to resolve'
        ats.record('blocked', reason, task_id, branch=branch, blocking_reasons=[reason])
        ats.phase('finished', task_id, approved=False, branch=branch)
        return 'blocked'
    ats.phase('approved', task_id, cycle=max(0, len(cycles) - 1))
    if push:
        ats.phase('pr', task_id, branch=head)
        # A plain push: it only succeeds as a fast-forward, so work pushed to the PR meanwhile is never lost.
        pushed = ats.execute(['git', 'push', '--quiet', 'origin', f'HEAD:refs/heads/{head}'], folder, check=False,
                         timeout=180)
        if pushed.returncode:
            reason = (f'the resolution is ready on {branch}, but the branch of pull request #{number} changed '
                      'meanwhile or could not be pushed: ' + (pushed.stderr or pushed.stdout).strip()[-300:])
            ats.record('escalated', reason, task_id, branch=branch, blocking_reasons=[reason])
            ats.phase('finished', task_id, approved=False, branch=branch, worktree=str(folder))
            return 'escalated'
        ats.record('pr_conflicts_resolved', f'Pull request #{number} updated with {base}; a human still merges it',
               task_id, branch=head, number=number, base=base, review_cycles=len(cycles))
        if pr.get('url'):
            ats.phase('pr_created', task_id, url=pr['url'])
    ats.phase('finished', task_id, approved=True, branch=branch, worktree=str(folder))
    return 'approved'


def recheck_source(source_id, agent):
    """The kept worktree of an earlier run that was not approved, to test and review it again.

    Returns (folder, branch, implementer). The implementer is the agent that built it, so the
    other agent reviews; nothing is rebuilt.
    """
    try:
        source_id = str(uuid.UUID(source_id))
    except ValueError as exc:
        raise RuntimeError('Invalid recheck source ID') from exc
    folder = (ats.APP.parent / f'{ats.APP.name}-ats-{source_id[:8]}').resolve()
    if not folder.is_dir():
        raise RuntimeError('The kept worktree of that run no longer exists')
    # Paths compared resolved: git prints forward slashes and long names on Windows.
    top = ats.execute(['git', 'rev-parse', '--show-toplevel'], folder, check=False).stdout.strip()
    if not top or Path(top).resolve() != folder:
        raise RuntimeError('The kept folder is not a Git worktree')
    roots = {Path(line[len('worktree '):]).resolve()
             for line in ats.execute(['git', 'worktree', 'list', '--porcelain'], ats.APP).stdout.splitlines()
             if line.startswith('worktree ')}
    if folder not in roots:
        raise RuntimeError('The kept worktree is not attached to this project')
    branch = ats.execute(['git', 'symbolic-ref', '--quiet', '--short', 'HEAD'], folder, check=False).stdout.strip()
    if not branch.startswith('ats/'):
        raise RuntimeError('The kept branch is not managed by ATS')
    built = [e.get('implementer') for e in intelligence.read_events(ats.DATA / ats.cleanname(ats.project().get('project', ats.APP.name)) / 'events')
             if e.get('task_id') == source_id and e.get('event_type') == 'implementation' and e.get('implementer')]
    implementer = built[-1] if built else agent
    if implementer == 'auto' or implementer not in ats.enabled_agents():
        raise RuntimeError('Cannot tell which agent built the kept work; start the recheck with --agent')
    return folder, branch, implementer


def task_flow(task, agent, push, quiet=False, parallel='auto', approved_risk=False, plan_file=None,
              resume_from=None, recheck_from=None, expect_base=None, fix_pr=None):
    """Persist run_finished_v1 once, after all work, including base updates and publishing."""
    task_id = ats.preset_task_id() or str(uuid.uuid4())
    started = time.monotonic()
    outcome = 'error'
    with ats.RUN_PROGRESS_LOCK:
        ats._run_progress[task_id] = 0
    try:
        outcome = ats._task_flow(task, agent, push, task_id, quiet=quiet, parallel=parallel,
                             approved_risk=approved_risk, plan_file=plan_file, resume_from=resume_from,
                             recheck_from=recheck_from, expect_base=expect_base, fix_pr=fix_pr)
    except (KeyboardInterrupt, SystemExit):
        outcome = 'interrupted'
        raise
    finally:
        with ats.RUN_PROGRESS_LOCK:
            review_rounds = ats._run_progress.pop(task_id)
        propagating = sys.exc_info()[0] is not None
        try:
            ats.record('run_finished', 'Task run completed', task_id, approved=outcome == 'approved',
                   duration_seconds=round(max(0, time.monotonic() - started), 1),
                   review_rounds=review_rounds, outcome=outcome)
        except Exception:
            if not propagating:
                raise
            print('ATS: could not persist run_finished; original error preserved', file=sys.stderr)
        ats.save_run_report(task_id)


def save_run_report(task_id):
    """Best effort: a report that cannot be written never changes the outcome of the run."""
    try:
        name = ats.cleanname(ats.project().get('project', ats.APP.name))
        path = run_report.write(ats.DATA, name, task_id, intelligence.read_events(ats.DATA / name / 'events'))
        print('Run report: ' + str(path))
    except Exception as exc:
        print('ATS: could not write the run report: ' + str(exc)[:300], file=sys.stderr)


def _task_flow(task, agent, push, task_id, quiet=False, parallel='auto', approved_risk=False,
               plan_file=None, resume_from=None, recheck_from=None, expect_base=None, fix_pr=None):
    conf=ats.project()
    risk = intelligence.risk_profile(task)
    if risk['require_human_approval'] and not approved_risk:
        raise RuntimeError('High-risk task requires --approve-risk after reviewing: ' + json.dumps(risk))
    repository = ats.execute(['git', 'rev-parse', '--show-toplevel'], check=False)
    local_mode = repository.returncode != 0
    if not local_mode and Path(repository.stdout.strip()).resolve() != ats.APP.resolve():
        raise RuntimeError('Run ATS from the application repository root')
    if local_mode and push:
        raise RuntimeError('This project is not a Git repository. Run .ats task without --push; local changes will be exported as a patch.')
    if agent != 'auto' and agent not in ats.enabled_agents():
        raise RuntimeError(f'{agent} is disabled. Use .ats agents --enable {agent}')
    routing = None
    if agent == 'auto':
        agent, route_scores, route_reasons = ats.route(task)
        routing = {'chosen': agent, 'scores': route_scores, 'reasons': route_reasons}
    unavailable=ats.UnavailableAgents()
    branch='ats/'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+task_id[:8]
    folder=ats.APP.parent / f'{ats.APP.name}-ats-{task_id[:8]}'
    resumed = None
    rechecked = None
    if fix_pr and (local_mode or resume_from or plan_file or recheck_from):
        raise RuntimeError('Fixing a pull request needs a Git project and cannot be combined with a plan, resume or recheck')
    if recheck_from:
        if local_mode or resume_from or plan_file:
            raise RuntimeError('Recheck needs a Git project and cannot be combined with a resume or plan file')
        folder, branch, rechecked = ats.recheck_source(recheck_from, agent)
        agent = rechecked
    if resume_from:
        if local_mode or not plan_file:
            raise RuntimeError('Checkpoint resume needs a Git project and original plan file')
        try:
            source_id = str(uuid.UUID(resume_from))
        except ValueError as exc:
            raise RuntimeError('Invalid resume source ID') from exc
        plan_for_resume = ats.provided_task_plan(plan_file, task)
        source = checkpoints.read_checkpoint(ats.DATA, source_id)
        if not source:
            raise RuntimeError('No reviewed checkpoint available for this source run')
        folder = Path(source.get('worktree', '')).resolve()
        if (folder.parent != ats.APP.parent.resolve() or
                not re.fullmatch(re.escape(ats.APP.name) + r'-ats-[0-9a-f]{8}', folder.name) or
                not folder.is_dir()):
            raise RuntimeError('Checkpoint worktree does not match original project')
        branch = ats.execute(['git', 'symbolic-ref', '--quiet', '--short', 'HEAD'], folder).stdout.strip()
        if not branch.startswith('ats/'):
            raise RuntimeError('Resumed branch is not managed by ATS')
        if ats.execute(['git', 'rev-parse', '--show-toplevel'], folder).stdout.strip() != str(folder):
            raise RuntimeError('Resumed folder is not a Git worktree')
        roots = ats.execute(['git', 'worktree', 'list', '--porcelain'], ats.APP).stdout.splitlines()
        if 'worktree ' + str(folder) not in roots:
            raise RuntimeError('Resumed worktree is not attached to the original Git repository')
        head = ats.execute(['git', 'rev-parse', 'HEAD'], folder).stdout.strip()
        resumed = checkpoints.verified_resume(ats.DATA, source_id, plan_for_resume, folder, head)
        if not resumed:
            reason = checkpoints.diagnose(ats.DATA, source_id, plan_for_resume, folder, head) or 'INVALID'
            ats.record('resume_rejected', checkpoints.explain(reason), task_id, live=False, reason=reason,
                   source_run=source_id)
            raise RuntimeError(f'Source checkpoint cannot be resumed ({reason}): {checkpoints.explain(reason)}. '
                               'Existing code was not modified')
    ats.record('assigned',task,task_id,implementer=agent,branch=branch,task_categories=intelligence.categories(task))
    ats.record('risk_profile', 'Risk-based execution profile', task_id, **risk)
    if routing:
        ats.record('agent_routing', f"Routing chose {routing['chosen']}: "
               + '; '.join(f"{name} {score}" for name, score in sorted(routing['scores'].items())), task_id, **routing)
    outside = write_scope.external_targets(task, ats.APP) if conf.get('write_scope', 'block') != 'off' else []
    if outside:
        problem = write_scope.reason(outside)
        if conf.get('write_scope', 'block') == 'warn':
            ats.record('write_scope', 'Task names write targets outside the project', task_id, categories=outside,
                   blocked=False)
            print('ATS: warning: ' + problem, file=sys.stderr)
        else:
            # Before any agent starts; the event holds the path category only, never the path.
            ats.record('write_scope', 'Task names write targets outside the project', task_id, categories=outside,
                   blocked=True)
            ats.record('blocked', problem, task_id, branch=branch, blocking_reasons=[problem])
            print('ATS: ' + problem, file=sys.stderr)
            ats.phase('finished', task_id, approved=False, branch=branch)
            return 'blocked'
    resolved_env = ats.configure_test_env(conf)
    if resolved_env['env'] is not None:
        print(f"ATS: tests use the virtualenv {resolved_env['env']}.", flush=True)
    problem = ats.environment_problem(conf, task_id)
    if problem:
        # Before any agent starts: no implementation is spent on work that cannot be tested here.
        ats.record('blocked', problem, task_id, branch=branch, blocking_reasons=[problem])
        print('ATS: ' + problem, file=sys.stderr)
        ats.phase('finished', task_id, approved=False, branch=branch)
        return 'blocked'
    ats._run_flags[task_id] = {'task': task, 'high_risk': risk['level'] == 'high'}
    usage_meter.reset_context()
    usage_meter.set_context(task_id=task_id)
    worktree_created=False
    try:
        if not any(ats.executable(name) for name in ats.enabled_agents()):
            raise RuntimeError('No enabled agent CLI found; run .ats agents and install an enabled CLI')
        if rechecked:
            ats.record('rechecked', 'Testing and reviewing the kept worktree again, without rebuilding', task_id,
                   source_run=recheck_from, branch=branch, implementer=agent, worktree=str(folder))
            print(f'ATS: testing and reviewing the kept work on {branch} again.', flush=True)
        elif resumed:
            ats.record('resumed', 'Restored verified reviewed checkpoint', task_id,
                   source_run=resume_from, completed=resumed['completed'],
                   completed_nodes=resumed.get('completed_nodes', []), branch=branch,
                   plan_changed=bool(resumed.get('plan_changed')), replanned=resumed.get('replanned', []),
                   routing_changed=bool(resumed.get('routing_changed')))
            print(f'ATS: resuming {resumed["completed"]} reviewed parts on {branch}.', flush=True)
        elif fix_pr:
            for name in (fix_pr['head'], fix_pr['base']):
                if ats.execute(['git', 'fetch', '--quiet', 'origin', name], check=False, timeout=180).returncode:
                    raise RuntimeError(f'Could not fetch origin/{name} for pull request #{fix_pr["number"]}')
            start = 'origin/' + fix_pr['head']
            ats.execute(['git', 'worktree', 'add', '-b', branch, str(folder), start])
            ats.record('base_commit', 'Branch starts at the head of the pull request', task_id, live=False, branch=branch,
                   ref=start, commit=ats.execute(['git', 'rev-parse', start], check=False).stdout.strip(),
                   pull_request=fix_pr['number'])
            print(f'ATS: resolving the conflicts of pull request #{fix_pr["number"]} ({start} with origin/{fix_pr["base"]}).',
                  flush=True)
        elif local_mode:
            ats.make_local_workspace(folder, branch)
            print('ATS: project has no Git repository; using an isolated local snapshot.', flush=True)
        else:
            start = ats.fresh_base(conf)
            moved = ats.base_moved(start, expect_base)
            if moved:
                # The plan (or preflight) was made on other code: never let agents work on code nobody looked at.
                ats.record('blocked', moved, task_id, branch=branch, blocking_reasons=[moved])
                print('ATS: ' + moved, file=sys.stderr)
                ats.phase('finished', task_id, approved=False, branch=branch)
                return 'blocked'
            ats.execute(['git','worktree','add','-b',branch,str(folder),start])
            ats.record('base_commit', 'Task branch starts here', task_id, live=False, branch=branch, ref=start,
                   commit=ats.execute(['git', 'rev-parse', start], check=False).stdout.strip())
            print(f'ATS: task branch starts from {start}.', flush=True)
        worktree_created=True
        base_commit = ats.execute(['git', 'rev-parse', 'HEAD'], folder).stdout.strip()
        if fix_pr:
            ats.phase('implementation', task_id, implementer=agent, branch=branch, worktree=str(folder))
            return ats.fix_pr_conflicts(task, task_id, folder, branch, conf, fix_pr, agent, unavailable, push, quiet=quiet)
        if rechecked:
            approved, reviewer, implementer, history = ats.review_loop(
                task, task_id, folder, branch, agent, unavailable, conf, quiet=quiet,
                base=ats.integration_base(folder, conf))
            return ats.finish_task(task, task_id, folder, branch, push, conf, implementer, reviewer, approved,
                               history, unavailable, quiet, local_mode)
        if plan_file:
            preflight_plan = ats.provided_task_plan(plan_file, task)
            ats.record('task_split', 'Human-approved editable plan', task_id, subtasks=preflight_plan,
                   source='app_plan')
            ats.phase('planned', task_id, waves=len(ats.plan_waves(preflight_plan)),
                  parallel_pairs=sum(len(w) == 2 for w in ats.plan_waves(preflight_plan)))
        else:
            preflight_plan = ats.preplan_task(task, task_id, folder, agent, unavailable, conf,
                                         quiet=quiet, mode=parallel)
        if isinstance(preflight_plan, dict) and not preflight_plan.get('release_owner'):
            preflight_plan = ats.assign_release_files(preflight_plan, ats.release_files(conf))
        if isinstance(preflight_plan, dict):   # the files of every part help choose the learnings
            ats._run_flags[task_id]['paths'] = [p for n in preflight_plan.get('tasks', []) for p in n.get('owned_paths', [])]
        ats.phase('implementation', task_id, implementer=agent, branch=branch, worktree=str(folder))
        prompt = (ats.shared_prefix(folder, conf, task_id)
                  + "ROLE: implementer.\n"
                  f"Task: {task}. "
                  "Assess task size before editing. If it is too large for one complete review "
                  f"(maximum diff {conf.get('max_review_diff_chars', 150000)} characters), "
                  "make NO file changes and return ONE LINE ATS_SUBTASKS_JSON: "
                  '["first bounded implementation with tests", "next bounded implementation with tests"]. '
                  f"Use as few concrete ordered parts as possible (usually 2 to {ats.max_plan_parts(conf)}, "
                  "at most 20) that together complete the task; merge small steps that belong together; "
                  "each part must be independently testable and reviewable. "
                  "Two parts that do not depend on each other and touch different code may be given as a "
                  'pair, e.g. ["shared model", ["API endpoint", "settings screen"], "end-to-end test"]: '
                  "the two agents then build that pair at the same time and review each other's part. "
                  "Other parts run in order, alternating implementer and reviewer. "
                  "For a small task implement it normally without that marker. "
                  "Change only necessary application files and add tests. "
                  "Do not commit, push, merge, deploy, trade, expose secrets or modify .ats; "
                  "ATS commits your work after the review."
                  + ats.part_context(folder, conf, task)
                  + plan_learning.self_check_note(ats.plan_history())
                  + '\nAt the end include ONE LINE ATS_LEARNINGS_JSON: '
                  + '[{"kind":"finding","title":"<title>","observation":"<observation>",'
                  + '"evidence":"<evidence>"}] (replace the placeholders, or use []). '
                  + 'Only evidence-backed items, no secrets; proposals are not approved rules.')
        if preflight_plan is not None:
            plan = preflight_plan
            implementer = agent
        else:
            budget = budgets.Budget.for_part(conf)
            usage_meter.set_context(budget=budget)
            ats.record('budget', 'Budget for the task', task_id, **budget.snapshot())
            initial_tree = ats.index_tree(folder)
            implementer,result=ats.invoke_with_fallback(
                agent,prompt,folder,unavailable,task_id,'implementation',quiet=quiet)
            ats.record('implementation',result[-1600:],task_id,implementer=implementer,branch=branch)
            ats.capture_learning_proposals(result,task_id)
            plan = ats.subtask_plan(result.replace(prompt, ''))
            if plan is not None and ats.index_tree(folder) != initial_tree:
                raise RuntimeError('Task planning modified files; worktree retained for diagnosis')
        if plan is not None:
            if preflight_plan is None:
                ats.record('task_split', 'Task divided into reviewable parts', task_id, subtasks=plan)
            approved,reviewer,implementer,history=ats.subtask_flow(
                task,plan,task_id,folder,branch,implementer,unavailable,conf,quiet=quiet,resumed=resumed)
        else:
            approved,reviewer,implementer,history=ats.review_loop(task,task_id,folder,branch,implementer,unavailable,conf,quiet=quiet,
                                                               base=base_commit)
        return ats.finish_task(task, task_id, folder, branch, push, conf, implementer, reviewer, approved,
                           history, unavailable, quiet, local_mode)
    except AgentUsageExhausted as exc:
        message = str(exc) if isinstance(exc, BothAgentsExhausted) else 'Usage voor beide agents is op'
        print(message, file=sys.stderr)
        ats.record('blocked', message, task_id, branch=branch, resets=exc.resets)
        saved = checkpoints.read_checkpoint(ats.DATA, task_id) if worktree_created else None
        if saved and (saved.get('completed') or saved.get('pending')):
            # Reviewed work is safe in the checkpoint: the run can continue after the reset (Smart Resume).
            ats.record('usage_paused', 'Paused at the last reviewed checkpoint until the usage limit resets',
                   task_id, branch=branch, resets=exc.resets, completed=saved.get('completed', 0),
                   worktree=str(folder), resumable=True)
        if worktree_created:
            print('Worktree retained: '+str(folder),file=sys.stderr)
        return 'blocked'
    except Exception as e:
        ats.record('error',str(e)[:2000],task_id,implementer=agent,branch=branch)
        if worktree_created:
            print('Worktree retained: '+str(folder),file=sys.stderr)
        raise


def finish_task(task, task_id, folder, branch, push, conf, implementer, reviewer, approved, history,
                unavailable, quiet, local_mode):
    """After the review: learnings, the commit, and (when approved) the base update and the pull request."""
    if history and 'ledger' not in ats._run_flags.get(task_id, {}):
        ledger = findings_ledger.build(history)
        if ledger:
            ats.record_ledger(task_id, ledger)
    learning_agent = reviewer or ats.alternate_agent(implementer) or implementer
    ats.distill_learnings(task, task_id, folder, history,
                      learning_agent, unavailable, quiet=quiet)
    # Never commit edits that have not passed independent review.
    if approved:
        ats.execute(['git','add','-A'],folder)
    staged = approved and ats.execute(['git', 'diff', '--cached', '--quiet'], folder, check=False).returncode != 0
    if not staged:
        pass   # the agent already committed everything itself; nothing left to commit
    elif local_mode:
        ats.execute(['git', '-c', 'user.name=ATS', '-c', 'user.email=ats@localhost',
                 'commit', '-m', 'ATS: '+task[:65]], folder)
    else:
        ats.execute(['git','commit','-m','ATS: '+task[:65]],folder)
    print(f'Branch: {branch} | approved: {approved} | workspace: {folder}')
    if local_mode:
        patch = ats.export_local_patch(folder)
        if patch:
            ats.phase('patch', task_id, patch=str(patch))
            print(f'Local patch: {patch}')
            print('Review and apply from your application directory: git apply --check "' + str(patch) + '"')
            print('Then: git apply "' + str(patch) + '"')
        else:
            print('No local patch generated.')
        print('Original project unchanged. GitHub PR unavailable without a project remote.')
        ats.phase('finished', task_id, approved=approved, branch=branch, worktree=str(folder))
        return 'approved' if approved else 'escalated'
    if not approved:
        print('Independent review/checks incomplete: PR publishing blocked; worktree preserved')
        ats.phase('finished', task_id, approved=False, branch=branch, worktree=str(folder))
        return 'escalated'
    parts = {}
    for c in history:   # the approving (last) cycle of every part
        if 'subtask' in c:
            parts[c['subtask']] = f"Part {c['subtask']}: implementer {c['implementer']}, reviewer {c['reviewer']}"
    run_flags = ats._run_flags.get(task_id, {})
    scope = ('\nEvery part was reviewed on its own diff, and the combined change passed an integration review.'
             if run_flags.get('combined_review') else
             '\nEvery part was reviewed on its own diff; the combined change was not reviewed as a whole.')
    attribution = ('\n'.join(parts[n] for n in sorted(parts)) + scope if parts else '')
    still_open = findings_ledger.summary_lines(run_flags.get('ledger') or findings_ledger.build(history))
    if still_open:
        attribution += ('\nFindings still open (they did not block approval; decide before merging):\n'
                        + '\n'.join(still_open[:20]))
    if push:
        updated, update_cycles = ats.update_from_base(task, task_id, folder, branch, conf, implementer,
                                                  unavailable, quiet=quiet)
        history.extend(update_cycles)
        if not updated:
            message = (f"Not published: the branch could not be updated with the latest "
                       f"{conf.get('base_branch', 'main')} without a human decision; worktree preserved")
            ats.record('escalated', message, task_id, branch=branch, blocking_reasons=[message])
            print('ATS: ' + message, file=sys.stderr)
            ats.phase('finished', task_id, approved=False, branch=branch, worktree=str(folder))
            return 'escalated'
        ats.phase('pr', task_id, branch=branch)
        ats.execute(['git','push','-u','origin',branch],folder)
        if not shutil.which('gh'):
            print('gh missing: branch pushed but the pull request must be created manually')
            ats.phase('finished', task_id, approved=True, branch=branch, worktree=str(folder))
            return 'approved'
        base=conf.get('base_branch','main')
        body = (f'Task ID: {task_id}\nImplementer: {implementer}\nReviewer: {reviewer}\nReview cycles: {len(history)}\n'
                f'{attribution}\n{ats.dependency_lines(task_id)}Review/check outcomes in ATS data. Human merge required.')
        outcome=ats.execute(ats.pr_create_command(conf, base, branch, 'ATS: '+task[:65], body),folder)
        print(outcome.stdout)
        ats.phase('pr_created', task_id, url=outcome.stdout.strip()[-300:])
    ats.phase('finished', task_id, approved=True, branch=branch, worktree=str(folder))
    return 'approved'


def pr_create_command(conf, base, branch, title, body):
    """`gh pr create` for an approved run: ready for review, so it can be reviewed and merged at once.
    `draft_pr: true` in .ats-project.json opens a draft instead. ATS never merges either way."""
    draft = ['--draft'] if conf.get('draft_pr', False) is True else []
    return ['gh', 'pr', 'create', *draft, '--base', base, '--head', branch, '--title', title, '--body', body]


def dependency_lines(task_id):
    """New or changed dependencies, always listed separately in the PR description."""
    changes = [c for c in ats._run_flags.get(task_id, {}).get('dependencies', []) if c.get('added')]
    if not changes:
        return ''
    return ('New or changed dependencies:\n' + '\n'.join(f"- {c['file']}: {', '.join(c['added'][:10])}" for c in changes[:10])
            + '\n')


AGENT_CHECK_REVIEW = (
    'You review a tiny change in this repository. calc.py defines add(a, b) but returns a - b. '
    'Do not edit any file. Report the bug as one finding and request changes. Output exactly these two '
    'lines at the end:\nATS_FINDINGS_JSON: [{"id":"F1","severity":"MAJOR","file":"calc.py","line":"2",'
    '"summary":"add returns a - b instead of a + b"}]\nVERDICT: CHANGES_REQUESTED')


AGENT_CHECK_RESPONSE = (
    'Fix calc.py so add(a, b) returns a + b. Change nothing else. End with exactly this line:\n'
    'ATS_RESPONSES_JSON: [{"id":"F1","status":"fixed","note":"add returns a + b","reference":"calc.py:2"}]')


def agent_version(agent):
    try:
        out = subprocess.run([ats.executable(agent), '--version'], capture_output=True, text=True, timeout=60,
                             encoding='utf-8', errors='replace')
        return (out.stdout or out.stderr).strip().splitlines()[0][:120]
    except (OSError, subprocess.SubprocessError, IndexError, TypeError):
        return 'unknown'


def agent_check(agents=None):
    """The contract between ATS and the real agent CLIs, on a disposable fixture repository.

    For each installed agent: one review (must give ATS_FINDINGS_JSON and a VERDICT line, and must not
    change files) and one fix (must change calc.py and give ATS_RESPONSES_JSON). Records the CLI version
    and the end of the output, so a failing CLI update gives a reproducible diagnosis.
    """
    chosen = [a for a in (agents or ats.enabled_agents()) if a in ('codex', 'claude', 'gemini', 'opencode')]
    report = {'ats': 'agent-check', 'platform': sys.platform, 'agents': {}}
    for agent in chosen:
        entry = {'version': None, 'ok': False, 'problems': [], 'review_tail': '', 'fix_tail': ''}
        report['agents'][agent] = entry
        if not ats.executable(agent):
            entry['problems'].append(f'{agent} is not installed or not in PATH')
            continue
        entry['version'] = ats.agent_version(agent)
        with tempfile.TemporaryDirectory(prefix='ats-agent-check-') as tmp:
            repo = Path(tmp)
            ats.execute(['git', 'init', '-q'], repo)
            (repo / 'calc.py').write_text('def add(a, b):\n    return a - b\n', encoding='utf-8')
            ats.execute(['git', *ats.git_identity(repo), 'add', '-A'], repo)
            ats.execute(['git', *ats.git_identity(repo), 'commit', '-q', '-m', 'fixture'], repo)
            tree = ats.index_tree(repo)
            try:
                review = ats.invoke(agent, ats.AGENT_CHECK_REVIEW, repo, quiet=True, backoff=(), purpose='review')
            except Exception as exc:   # report, never crash the check
                entry['problems'].append(f'review call failed: {str(exc)[:300]}')
                continue
            entry['review_tail'] = review[-1500:]
            if ats.parse_verdict(review) != 'CHANGES_REQUESTED':
                entry['problems'].append('review: no valid last VERDICT line (expected VERDICT: CHANGES_REQUESTED)')
            if not ats.normalize_findings(ats.parse_marked_json(review, 'ATS_FINDINGS_JSON')):
                entry['problems'].append('review: ATS_FINDINGS_JSON missing or not parsable')
            if ats.restore_if_changed(repo, tree):
                entry['problems'].append('review: the agent changed files in review-only mode')
            try:
                fix = ats.invoke(agent, ats.AGENT_CHECK_RESPONSE, repo, quiet=True, backoff=(), purpose='revision')
            except Exception as exc:
                entry['problems'].append(f'fix call failed: {str(exc)[:300]}')
                continue
            entry['fix_tail'] = fix[-1500:]
            if 'a + b' not in (repo / 'calc.py').read_text(encoding='utf-8', errors='replace'):
                entry['problems'].append('fix: calc.py was not changed to return a + b')
            if not ats.normalize_responses(ats.parse_marked_json(fix, 'ATS_RESPONSES_JSON')):
                entry['problems'].append('fix: ATS_RESPONSES_JSON missing or not parsable')
        entry['ok'] = not entry['problems']
    return report


SCREEN_TASK = (
    'Create or complete {file} in the project root: the map ATS uses to find the files of the screen a task names. '
    'Below is a draft ATS made from the code; check every entry against the code. Keep entries a person already '
    'wrote (no "generated" flag) unchanged unless they are wrong. For each real screen of the app: one entry with '
    'name (as the app shows it), aliases (how the user says it, in Dutch and English, for example '
    "'apparaatpagina', 'device detail'), route (path or #hash that opens it), preview (a path that opens it "
    'without parameters, e.g. with a demo id, only when the route has a parameter), files (the HTML/JS/CSS and '
    'server files that build it), symbols (render functions or components), api (endpoints it calls) and tests. '
    'Merge entries that are the same screen (a view section and the function that renders it); drop entries that '
    'are not a screen (helpers, small toggles); remove the "generated" flag from entries you checked. Change no '
    'other file. Draft from the code: {draft}')


SCREEN_TASK_LIMIT = 3900   # the ATS app accepts tasks up to 4000 characters


def screen_task(draft):
    """The task text with a compact draft (name | route | files | functions, one screen per entry)."""
    lines = []
    for screen in draft.get('screens', []):
        mark = '' if screen.get('generated') else ' (written by a person)'
        lines.append(' | '.join([screen['name'] + mark, screen.get('route') or '-', ','.join(screen.get('files', [])[:3]),
                                 ','.join(screen.get('symbols', [])[:3])]))
    head = ats.SCREEN_TASK.format(file=screen_map.FILE, draft='')
    room = ats.SCREEN_TASK_LIMIT - len(head) - 80
    text, kept = '', 0
    for line in lines:
        if len(text) + len(line) + 3 > room:
            break
        text += ('; ' if text else '') + line
        kept += 1
    if kept < len(lines):
        text += f'; … {len(lines) - kept} more, look for them in the code'
    return head + '[name | route | files | functions] ' + text


def screens_command(text=None, task=False):
    data, problem = screen_map.load(ats.APP)
    if text is not None:
        return {'file': screen_map.FILE, 'exists': data is not None, 'problem': problem,
                'matches': screen_map.match(data, text), 'context': screen_map.context(data, text).strip()}
    draft, added = screen_map.generate(ats.APP, data)
    result = {'file': screen_map.FILE, 'exists': data is not None, 'problem': problem,
              'screens': len((data or {}).get('screens', [])), 'detected_new': added}
    if task:
        result['task'] = ats.screen_task(draft)
    else:
        result['draft'] = draft
    return result


def child_run(argv):
    if not argv: raise RuntimeError('Usage: ats run -- command [args]')
    p=subprocess.Popen(argv,cwd=ats.APP,start_new_session=(os.name!='nt'))
    def forward(sig, _frame):
        try:
            if os.name!='nt': os.killpg(p.pid,sig)
            else: p.send_signal(sig)
        except ProcessLookupError: pass
    original={}
    for sig in (signal.SIGINT,signal.SIGTERM):
        original[sig]=signal.getsignal(sig);signal.signal(sig,forward)
    try: return p.wait()
    finally:
        for sig,handler in original.items():signal.signal(sig,handler)


def main_check(ref=None):
    """Run the project's test commands on a clean checkout of the base branch (a local CI).

    Fetches origin/<base_branch>, checks that exact commit out in a throw-away worktree, runs every
    test command there (unstable tests get their one rerun) and prints one ATS_MAIN_CHECK_JSON line.
    Changes nothing in the project, its branches or the remote.
    """
    conf = ats.project()
    base = conf.get('base_branch', 'main')
    target = ref or f'origin/{base}'
    if not re.fullmatch(r'[A-Za-z0-9._/-]{1,200}', target) or '..' in target or target.startswith('-'):
        raise RuntimeError('Invalid ref for main-check')
    if not (ats.APP / '.git').exists():
        raise RuntimeError('main-check needs a Git project')
    if ref is None:
        ats.execute(['git', 'fetch', '--quiet', 'origin', base], check=False, timeout=180)
    commit = ats.execute(['git', 'rev-parse', '--verify', target + '^{commit}'], check=False).stdout.strip()
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise RuntimeError(f'{target} not found')
    result = {'ref': target, 'commit': commit, 'checks': [], 'passed': None}
    if not conf.get('test_commands'):
        result['note'] = 'no test_commands configured'
    else:
        ats.configure_test_env(conf)
        with snapshots.checkout(ats.APP, commit, parent=ats.APP.parent) as copy:
            started = time.monotonic()
            checks = ats.run_checks(conf, copy)
            result['seconds'] = round(time.monotonic() - started, 1)
        result['checks'] = [{'command': str(c['command'])[:300], 'passed': c['passed'], 'flaky': bool(c.get('flaky')),
                             'cannot_start': bool(c.get('cannot_start')), 'output': c.get('output', '')[-800:]}
                            for c in checks]
        result['passed'] = all(c['passed'] for c in checks)
    print('ATS_MAIN_CHECK_JSON: ' + json.dumps(result, ensure_ascii=False), flush=True)
    return result


def pull_request_argument(args):
    if not args.fix_pr:
        return None
    if args.fix_pr < 1 or not args.pr_head or not args.pr_base:
        raise RuntimeError('--fix-pr needs a pull request number, --pr-head and --pr-base')
    for name in (args.pr_head, args.pr_base):
        if not ats.BRANCH_NAME.fullmatch(name) or '..' in name:
            raise RuntimeError('Invalid branch name for --fix-pr')
    url = args.pr_url if args.pr_url and re.fullmatch(r'https://github\.com/[\w.-]+/[\w.-]+/pull/\d+', args.pr_url) else None
    return {'number': args.fix_pr, 'head': args.pr_head, 'base': args.pr_base, 'url': url}


def positive_days(value):
    try:
        days = int(value)
    except ValueError:
        days = 0
    if not 1 <= days <= 3650:
        raise argparse.ArgumentTypeError('--days must be a whole number from 1 to 3650')
    return days
