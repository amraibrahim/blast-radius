"""Dev: a synthetic practice set from the PINNED repo only (no sample patches or answers used).

The sample commits are single-token mutations of sqlglot code. This generates more of the same
(tools/mutants.py's mutation operators on random covered lines), labels each one by running its
broad static candidate set (expensive tests included) through the verifier, and writes a data
directory benchmark.py can score:  OUT/commits/<id>.patch, OUT/tests.json, OUT/answers.json.

usage: python tools/synth.py N OUT_DIR [workers]
"""
import gzip
import json
import multiprocessing as mp
import os
import random
import shutil
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'tools'))
ROOT = os.path.join(HERE, '.build/sqlglot')

_state = {}


def _init():
    os.environ['BR_SQLGLOT'] = ROOT
    sys.modules['verify_driver'] = None
    import selector, verify
    tests = json.load(open(os.path.join(HERE, 'sample/tests.json')))['tests']
    index = json.load(gzip.open(os.path.join(HERE, 'index.json.gz'), 'rt'))
    # labels come from the broadest static analysis: no table / comparison narrowing
    _state['sel'] = selector.Selector(tests, dict(index, tables={}, equivalent_sites=[]), ROOT)
    _state['selector'] = selector
    _state['verify'] = verify
    if os.environ.get("PRELOAD"):
        verify.preload()


def label(job):
    cid, patch = job
    sel, S, verify = _state['sel'], _state['selector'], _state['verify']
    cand, reasons, sources = sel.analyze(patch)
    if cand == S.EVERYTHING:
        cand = set(sel.all)
    ids = [sel.by_number[n]['id'] for n in sorted(cand)]
    if not ids:
        return cid, [], 0
    pid, fd = verify.start(sel.root, sources, ids, {i: 300.0 for i in ids})
    res = verify.finish(pid, fd)
    if 'failed' not in res:
        return cid, None, len(ids)
    number_of = {t['id']: t['number'] for t in sel.tests}
    failed = set()
    for f in res['failed']:
        if isinstance(f, list):
            if f[0] == 'FILE':
                failed |= set(sel.by_file.get(f[1], ()))
            elif f[0] == 'IMPORT':
                dep = sel.import_dependents(f[1])
                failed |= set(sel.all) if dep == S.EVERYTHING else set(dep)
            else:
                return cid, None, len(ids)
        elif f in number_of:
            failed.add(number_of[f])
    return cid, sorted(failed), len(ids)


def main():
    import mutants
    n = int(sys.argv[1])
    out = sys.argv[2]
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    rng = random.Random(int(os.environ.get('SEED', '2026')))
    index = json.load(gzip.open(os.path.join(HERE, 'index.json.gz'), 'rt'))
    lines = [(rel, int(k)) for rel, table in index['files'].items() if rel.startswith('sqlglot/')
             for k in table]
    jobs, seen = [], set()
    while len(jobs) < n:
        rel, line = rng.choice(lines)
        m = mutants.make_mutant(rel, line, rng)
        if m and m[1] not in seen:
            seen.add(m[1])
            jobs.append((f's{len(jobs):03d}', m[1]))
    os.makedirs(os.path.join(out, 'commits'), exist_ok=True)
    for cid, patch in jobs:
        open(os.path.join(out, 'commits', cid + '.patch'), 'w').write(patch)
    shutil.copy(os.path.join(HERE, 'sample/tests.json'), os.path.join(out, 'tests.json'))
    answers = {}
    path = os.path.join(out, 'answers.json')
    if os.path.exists(path):
        try:
            answers = json.load(open(path))['commits']     # resume: same seed, same jobs
        except ValueError:
            answers = {}
    todo = [j for j in jobs if j[0] not in answers]
    with mp.get_context('fork').Pool(workers, initializer=_init) as pool:
        for k, (cid, failed, ncand) in enumerate(pool.imap_unordered(label, todo)):
            if failed is None:
                os.remove(os.path.join(out, 'commits', cid + '.patch'))
                print(cid, 'unlabelable, dropped', file=sys.stderr)
                continue
            answers[cid] = {'failing': failed, 'kind': 'code'}
            print(k, cid, 'candidates', ncand, 'failing', len(failed), file=sys.stderr)
            tmp = path + '.tmp'
            json.dump({'commits': answers}, open(tmp, 'w'))
            os.replace(tmp, path)
    json.dump({'commits': answers}, open(os.path.join(out, 'answers.json'), 'w'))


if __name__ == '__main__':
    main()
