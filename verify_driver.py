"""Decide, per commit, whether running candidate tests now is cheaper than listing them.

Listing a candidate set C costs sched(C) (busiest worker + 2 s/worker). Running a subset R of it
here costs its CPU, and then only the tests that actually failed need listing. The expensive
tests (e.g. a 28 s parser stress test) are cheaper to list than to run, so the plan is: list the
expensive top of C directly, run the cheap rest, and list whatever of the rest fails.

Safety: any test whose run errors, times out, or whose file fails to import is listed. If a child
dies or the wall budget runs out, the whole candidate set is listed, exactly as without running.
"""
import os
import select
import signal
import sys
import time

import verify
from selector import schedule, log

# Scorer CPU per local CPU second. Calibrated from the pinned suite: recorded seconds (labeling
# machine) / CPU measured here, median over tests > 50 ms. Override with BR_CPU_FACTOR.
CPU_FACTOR = float(os.environ.get('BR_CPU_FACTOR', '2.2'))
CLEAN_CHILD_MS = 320.0   # local ms for a child that must import sqlglot itself (core module patched)
WARM_CHILD_MS = 40.0     # local ms for a child forked from a parent with sqlglot imported
MIN_SAVING_MS = 400.0    # expected saving required before running anything (model error margin)
P_FAIL = 0.5             # prior that the run set contains a failing test
PARALLEL = 2             # the run has 2 cores
WALL_PER_COMMIT = 3.5    # seconds of wall budget per commit for verification
WALL_CAP = 150.0


class Plan:
    def __init__(self, commit, candidates, sources):
        self.commit = commit
        self.candidates = set(candidates)
        self.sources = sources
        self.listed = set(candidates)       # what we print if nothing is run
        self.run = []
        self.est_ms = 0.0
        self.saving = 0.0
        self.probe = False


def is_clean(selector, index, sources):
    core = set(index.get('core_modules') or ())
    return not core or any(rel.endswith('.py') and rel.startswith('sqlglot/') and
                           selector.module_name(rel) in core for rel in sources)


def estimate(selector, index, numbers, clean=True):
    """Scorer-ms to run `numbers` in one child."""
    cpu = index.get('cpu_ms', {})
    cls_cpu = index.get('class_cpu_ms', {})
    imp = index.get('import_cpu_ms', {})
    total = CLEAN_CHILD_MS if clean else WARM_CHILD_MS
    classes, files = set(), set()
    for n in numbers:
        t = selector.by_number[n]
        total += cpu.get(t['id'], t['seconds'] * 1000 / CPU_FACTOR) + 0.3
        classes.add(t['id'].rsplit('::', 1)[0])
        files.add(t['file'])
    total += sum(cls_cpu.get(c, 0.0) for c in classes)
    total += sum(imp.get(f, 5.0) for f in files)
    return total * CPU_FACTOR


def failure_prior(reasons):
    """Chance that some candidate fails. A test that reads an edited data file (fixture) almost
    always fails; code edits split roughly evenly between breaking and not breaking."""
    if any(r[0] == 'syntax-error' for r in reasons):
        return 1.0
    if any(r[0] == 'data-file' and r[2] > 0 for r in reasons):
        return 0.9
    return P_FAIL


IMPORT_TIME = {'inserted-member', 'inserted-module', 'module-names', 'module-stmt', 'module-gap', 'import-same-bindings', 'class-body',
               'class-attr', 'class-unseen', 'def'}


def plan_commit(selector, index, commit, candidates, sources, reasons=()):
    plan = Plan(commit, candidates, sources)
    # Code that runs at import time changed: always check the module still imports.
    plan.probe = any(r[0] in IMPORT_TIME and str(r[1]).startswith('sqlglot/') for r in reasons)
    p_fail = failure_prior(reasons)
    clean = is_clean(selector, index, sources)
    if not candidates or len(candidates) >= len(selector.all) * 0.5:
        return plan                      # nothing to run, or so broad that listing is cheaper
    ms = selector.ms
    static = schedule(candidates, ms)[1]
    cpu = index.get('cpu_ms', {})
    run_cost = {n: cpu.get(selector.by_number[n]['id'], ms[n] / CPU_FACTOR) * CPU_FACTOR for n in candidates}
    # Rank by how much more it costs to run than to list.
    order = sorted(candidates, key=lambda n: (-(run_cost[n] - 0.0), n))
    best = (static, None)
    for j in range(len(order) + 1):
        listed, run = order[:j], order[j:]
        if not run:
            break
        listed_cost = schedule(listed, ms)[1] if listed else 0.0
        extra = WORKER_MS_FAIL if not listed else 1000.0
        cost = estimate(selector, index, run, clean) + listed_cost + p_fail * extra
        if cost < best[0] and cost + MIN_SAVING_MS < static:
            best = (cost, (set(listed), run))
    if best[1] is not None:
        plan.listed, plan.run = best[1]
        plan.est_ms = estimate(selector, index, plan.run, clean)
        plan.saving = static - best[0]
    return plan


WORKER_MS_FAIL = 2300.0    # a failure found in a run set with nothing else listed opens one worker


def decide(selector, plans):
    index = selector.index if hasattr(selector, 'index') else {}
    started = time.time()
    budget = min(WALL_CAP, WALL_PER_COMMIT * max(1, len(plans)))
    work = [plan_commit(selector, index, c, s, src, r) for c, s, r, src in plans]
    queue = sorted((p for p in work if p.run or p.probe), key=lambda p: -p.saving)
    if queue:
        # Heavy third-party imports are shared by forking only when several children need them.
        heavy = {rel for rel, text in selector.all_sources().items() if rel.startswith('tests/') and
                 any(f'import {m}' in text or f'from {m}' in text for m in ('pandas', 'duckdb', 'numpy'))}
        needing = sum(1 for p in queue if any(selector.by_number[n]['file'] in heavy for n in p.run))
        if needing >= 2:
            verify.preload()
        sys.stdout.flush()
        sys.stderr.flush()
    running = {}
    timeouts = {}
    cpu = index.get('cpu_ms', {})
    for t in selector.tests:
        local = cpu.get(t['id'], t['seconds'] * 1000) / 1000
        timeouts[t['id']] = max(3.0, 8 * local)
    # Children for patches to modules that `import sqlglot` loads must start pristine; every other
    # child can inherit an already-imported sqlglot, which saves the import in each child.
    core = set(index.get('core_modules') or ())

    def needs_clean(p):
        return not core or any(rel.endswith('.py') and rel.startswith('sqlglot/') and
                               selector.module_name(rel) in core for rel in p.sources)

    pending = sorted(queue, key=lambda p: not needs_clean(p))
    warmed = False
    while pending or running:
        while pending and len(running) < PARALLEL:
            p = pending.pop(0)
            if not warmed and core and not needs_clean(p):
                warm(selector.root)
                warmed = True
            remaining = budget - (time.time() - started)
            if p.run and p.est_ms / 1000 / CPU_FACTOR * 1.5 > remaining:
                p.run = []          # out of budget: list the candidates instead
                p.listed = set(p.candidates)
                continue
            ids = [selector.by_number[n]['id'] for n in sorted(p.run)]
            pid, fd = verify.start(selector.root, p.sources, ids, timeouts)
            limit = time.time() + max(5.0, 4 * p.est_ms / 1000 / CPU_FACTOR + 3)
            running[fd] = (p, pid, limit)
        if not running:
            break
        ready, _, _ = select.select(list(running), [], [], 0.5)
        now = time.time()
        for fd in list(running):
            p, pid, limit = running[fd]
            if fd in ready:
                result = verify.finish(pid, fd)
                del running[fd]
                absorb(selector, p, result)
            elif now > limit or now - started > budget + 20:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                verify.finish(pid, fd)
                del running[fd]
                log(p.commit, 'verification killed; listing all candidates')
                p.listed = set(p.candidates)
                p.run = []
    return [(p.commit, p.listed) for p in work]


def warm(root):
    """Import pristine sqlglot core in the parent so later children inherit it."""
    try:
        if root not in sys.path:
            sys.path.insert(0, root)
        os.environ['SKIP_INTEGRATION'] = '0'
        sys.dont_write_bytecode = True
        import sqlglot  # noqa: F401
        import tests.helpers  # noqa: F401
    except BaseException as exc:  # a child imports on its own anyway
        log('warm import failed', repr(exc))


def absorb(selector, plan, result):
    if 'failed' not in result:
        log(plan.commit, 'verification error', result.get('error'))
        plan.listed = set(plan.candidates)
        return
    number_of = {t['id']: t['number'] for t in selector.tests}
    run = set(plan.run)
    failed = set()
    for item in result['failed']:
        if isinstance(item, list):           # ['FILE', path], ['IMPORT', module path] or ['ERR', ...]
            if item[0] == 'IMPORT':
                dependents = selector.import_dependents(item[1])
                log(plan.commit, 'changed module fails to import', item[1])
                plan.listed = set(selector.all) if dependents == 'everything' else \
                    set(plan.listed) | set(plan.candidates) | dependents
                return
            if item[0] == 'FILE':
                failed |= {n for n in run if selector.by_number[n]['file'] == item[1]}
            else:
                failed |= run
        elif item in number_of and number_of[item] in run:
            failed.add(number_of[item])
        else:
            failed |= run                    # unrecognised report: be safe
    log(plan.commit, f'ran {len(run)} failed {len(failed)} est {plan.est_ms/CPU_FACTOR:.0f}ms local, actual {result.get("cpu", 0)*1000:.0f}ms, saving {plan.saving/1000:.2f}s')
    plan.listed = set(plan.listed) | failed
