"""Dev check of fixture-record restriction, from the PINNED repo only (no sample patches/answers).

Makes sample-style fixture edits (a token inserted into a random line, a comment line, a ';'),
then runs each edit's candidate tests through the verifier twice: whole, and with the fixture
loaders restricted to changed records. Sound iff the failure sets are equal.

usage: python tools/fixture_check.py N OUT.json
"""
import difflib
import gzip
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
ROOT = os.path.join(HERE, '.build/sqlglot')
os.environ['BR_SQLGLOT'] = ROOT
sys.modules['verify_driver'] = None
import selector as S  # noqa: E402
import verify  # noqa: E402

TOKENS = ['x', 'NULL', '42', '0', 'a', 'AS', '1', 'b', 'xa']


def edit(rel, rng, kind):
    src = open(os.path.join(ROOT, rel), encoding='utf-8').read()
    lines = src.split('\n')
    k = rng.randrange(len(lines))
    while not lines[k].strip():
        k = rng.randrange(len(lines))
    if kind == 'comment':
        lines.insert(k, '-- note')
    elif kind == 'semicolon':
        lines[k] = lines[k] + ';'
    else:
        words = lines[k].split(' ')
        j = rng.randrange(len(words) + 1)
        words.insert(j, rng.choice(TOKENS))
        lines[k] = ' '.join(words)
    new = '\n'.join(lines)
    diff = ''.join(difflib.unified_diff(src.splitlines(True), new.splitlines(True), f'a/{rel}', f'b/{rel}'))
    return f'diff --git a/{rel} b/{rel}\n' + diff


def run(sel, sources, ids, restrict):
    t = time.process_time()
    pid, fd = verify.start(sel.root, sources, ids, {i: 600.0 for i in ids}, fixture_records=restrict)
    res = verify.finish(pid, fd)
    return res, res.get('cpu', 0.0)


def main():
    n, out = int(sys.argv[1]), sys.argv[2]
    rng = random.Random(int(os.environ.get('SEED', '9')))
    tests = json.load(open(os.path.join(HERE, 'sample/tests.json')))['tests']
    index = json.load(gzip.open(os.path.join(HERE, 'index.json.gz'), 'rt'))
    sel = S.Selector(tests, index, ROOT)
    verify.preload()
    sqls = sorted(os.path.relpath(os.path.join(dp, f), ROOT)
                  for dp, _, fs in os.walk(os.path.join(ROOT, 'tests/fixtures')) for f in fs if f.endswith('.sql'))
    heavy = [r for r in sqls if 'tpc-' in r]
    results = []
    for i in range(n):
        rel = rng.choice(heavy) if i % 2 == 0 else rng.choice(sqls)
        kind = 'comment' if i % 7 == 3 else 'semicolon' if i % 7 == 5 else 'token'
        patch = edit(rel, rng, kind)
        cand, reasons, sources = sel.analyze(patch)
        if cand == S.EVERYTHING or not cand:
            results.append({'rel': rel, 'kind': kind, 'n_cand': 0 if not cand else -1})
            continue
        ids = [sel.by_number[c]['id'] for c in sorted(cand)]
        full, cpu_full = run(sel, sources, ids, False)
        part, cpu_part = run(sel, sources, ids, True)
        f1 = sorted(map(str, full.get('failed', ['ERROR'])))
        f2 = sorted(map(str, part.get('failed', ['ERROR'])))
        rec = {'rel': rel, 'kind': kind, 'n_cand': len(ids), 'failed_full': f1, 'failed_restricted': f2,
               'same': f1 == f2, 'cpu_full': round(cpu_full, 2), 'cpu_restricted': round(cpu_part, 2)}
        results.append(rec)
        print(i, rel, kind, len(ids), 'same' if f1 == f2 else 'DIFF', len(f1), cpu_full, cpu_part, file=sys.stderr)
        json.dump(results, open(out, 'w'), indent=1)
    json.dump(results, open(out, 'w'), indent=1)


if __name__ == '__main__':
    main()
