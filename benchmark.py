"""Sample benchmark: runs the selector natively on sample/, scores it, and reports per-commit excess.

Uses sample/answers.json only for evaluation, never as selector input.
usage: python benchmark.py [--static]
"""
import io, json, os, sys, time, contextlib, runpy
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault('BR_DATA', os.path.join(HERE, 'sample'))
os.environ.setdefault('BR_SQLGLOT', os.path.join(HERE, '.build/sqlglot'))
os.environ.setdefault('BR_INDEX', os.path.join(HERE, 'index.json.gz'))
sys.path.insert(0, HERE)
if '--static' in sys.argv:
    sys.modules['verify_driver'] = None
import selector as S

tests = json.load(open(os.path.join(os.environ['BR_DATA'], 'tests.json')))['tests']
ms = {t['number']: round(t['seconds'] * 1000) for t in tests}
name = {t['number']: t['id'].split('/')[-1] for t in tests}
answers = json.load(open(os.path.join(os.environ['BR_DATA'], 'answers.json')))['commits']
buf = io.StringIO()
t0 = time.process_time(); w0 = time.time()
ch0 = os.times()
with contextlib.redirect_stdout(buf):
    S.main()
ch1 = os.times()
cpu = (ch1.user - ch0.user) + (ch1.system - ch0.system) + (ch1.children_user - ch0.children_user) + (ch1.children_system - ch0.children_system)
open(os.path.join(HERE, 'output.jsonl'), 'w').write(buf.getvalue())
rows = []
for line in buf.getvalue().splitlines():
    e = json.loads(line)
    sel = {n for w in e['workers'] for n in w}
    fail = set(answers[e['commit']]['failing'])
    cost = (max(sum(ms[n] for n in w) for w in e['workers']) + 2000 * len(e['workers'])) if e['workers'] else 0
    oracle = S.cost_of(fail, ms)
    miss = fail - sel
    fp = sorted(sel - fail, key=lambda n: -ms[n])
    rows.append((e['commit'], answers[e['commit']]['kind'], len(fail), len(sel), cost, oracle, miss, fp))
total = sum(r[4] for r in rows) + 300000 * sum(1 for r in rows if r[6])
oracle = sum(r[5] for r in rows)
print(f'{"commit":6} {"kind":8} {"fail":>4} {"sel":>5} {"cost":>8} {"oracle":>7} miss  top-false-positives')
for r in sorted(rows, key=lambda r: -(r[4] - r[5] + (300000 if r[6] else 0))):
    print(f'{r[0]:6} {r[1]:8} {r[2]:4} {r[3]:5} {r[4]/1000:8.3f} {r[5]/1000:7.3f} {"MISS" if r[6] else "    "}  ' +
          ', '.join(f'{name[n]}({ms[n]/1000:.2f})' for n in r[7][:3]))
zero_pred = [r for r in rows if r[3] == 0]
zero_true = [r for r in rows if r[2] == 0]
print(f'\nsimulated {total/1000:.3f} s  misses {sum(1 for r in rows if r[6])}  oracle {oracle/1000:.3f} s  '
      f'excess {(total-oracle)/1000:.3f} s')
print(f'zero-test: predicted {len(zero_pred)}, true {len(zero_true)}, correct {sum(1 for r in zero_pred if r[2]==0)}')
print(f'selector cpu (native, incl. children) {cpu:.3f} s, wall {time.time()-w0:.2f} s')
print(f'estimated total {total/1000 + cpu:.3f} s')
print(f'estimated scorer total (cpu x {os.environ.get("BR_CPU_FACTOR","2.2")}) {total/1000 + cpu*float(os.environ.get("BR_CPU_FACTOR","2.2")):.3f} s')
