"""Dev calibration from the PINNED repo only: how often does an expensive test fail when a random
single-token mutation hits a line it covers? Also records whether the selector's cheaper
candidates fail, giving the joint (cheap fails, target fails) counts.

usage: python tools/expensive_rate.py TEST_SUBSTRING N OUT.json
"""
import gzip
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'tools'))
ROOT = os.path.join(HERE, '.build/sqlglot')
os.environ['BR_SQLGLOT'] = ROOT
sys.modules['verify_driver'] = None
import mutants  # noqa: E402
import selector as S  # noqa: E402
import verify  # noqa: E402

key, n, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
tests = json.load(open(os.path.join(HERE, 'sample/tests.json')))['tests']
index = json.load(gzip.open(os.path.join(HERE, 'index.json.gz'), 'rt'))
sel = S.Selector(tests, index, ROOT)
target = next(t for t in tests if key in t['id'])
tnum = target['number']
idx = {name: k for k, name in enumerate(index['tests'])}
tk = idx[target['id']]
lines = [(rel, int(line)) for rel, table in index['files'].items() if rel.startswith('sqlglot/')
         for line, s in table.items() if tk in index['sets'][s]]
rng = random.Random(int(os.environ.get('SEED', '5')))
rng.shuffle(lines)
number_of = {t['id']: t['number'] for t in tests}


def run(ids, sources):
    pid, fd = verify.start(ROOT, sources, ids, {i: 600.0 for i in ids})
    res = verify.finish(pid, fd)
    if 'failed' not in res:
        return None
    bad = set()
    for f in res['failed']:
        if isinstance(f, list):
            return 'whole'
        bad.add(number_of.get(f))
    return bad


results = []
for rel, line in lines:
    if len(results) >= n:
        break
    m = mutants.make_mutant(rel, line, rng)
    if not m:
        continue
    cand, reasons, sources = sel.analyze(m[1])
    if cand == S.EVERYTHING or tnum not in cand:
        continue
    t_res = run([target['id']], sources)
    if t_res is None or t_res == 'whole':
        continue
    cheap = sorted(c for c in cand if sel.by_number[c]['seconds'] < 2.0)
    c_res = run([sel.by_number[c]['id'] for c in cheap], sources) if cheap else set()
    rec = {'rel': rel, 'line': line, 'desc': m[0], 'n_cand': len(cand), 'target_failed': bool(t_res),
           'cheap_failed': None if c_res is None else (-1 if c_res == 'whole' else len(c_res))}
    results.append(rec)
    print(len(results), rec, file=sys.stderr)
    json.dump(results, open(out, 'w'))
json.dump(results, open(out, 'w'))
