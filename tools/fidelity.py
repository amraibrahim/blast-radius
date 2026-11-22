"""Dev check: run each sample's static candidates through verify.py and compare with answers."""
import json, os, sys, time, resource
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault('BR_SQLGLOT', os.path.join(HERE, '.build/sqlglot'))
sys.modules['verify_driver'] = None
import selector as S, verify
tests = json.load(open(f'{HERE}/sample/tests.json'))['tests']
import gzip
ix = json.load(gzip.open(f'{HERE}/index.json.gz', 'rt'))
sel = S.Selector(tests, ix, os.environ['BR_SQLGLOT'])
ans = json.load(open(f'{HERE}/sample/answers.json'))['commits']
byn = {t['number']: t for t in tests}
idn = {t['id']: t['number'] for t in tests}
verify.preload()
only = sys.argv[1:] 
tot = 0
for c in sorted(ans):
    if only and c not in only: continue
    text = open(f'{HERE}/sample/commits/{c}.patch').read()
    cand, reasons, sources = sel.analyze(text)
    if cand == S.EVERYTHING: cand = set(sel.all)
    fail = set(ans[c]['failing'])
    run = sorted(cand | fail)
    ids = [byn[n]['id'] for n in run]
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    w = time.time()
    pid, fd = verify.start(sel.root, sources, ids, {byn[n]['id']: max(10, 10*byn[n]['seconds']) for n in run})
    res = verify.finish(pid, fd)
    r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = r1.ru_utime - r0.ru_utime + r1.ru_stime - r0.ru_stime
    tot += cpu
    got = set()
    for f in res.get('failed', []):
        if isinstance(f, list): got |= {n for n in run if byn[n]['file'] == f[1]}
        else: got.add(idn.get(f))
    rec = sum(byn[n]['seconds'] for n in run)
    flag = 'OK ' if got == fail else 'BAD'
    print(f'{flag} {c} ran {len(run):4} rec {rec:6.2f}s cpu {cpu:6.2f}s wall {time.time()-w:5.2f} fail {len(fail)} got {len(got)} '
          f'missed {[byn[n]["id"] for n in fail-got][:3]} extra {[byn[n]["id"] for n in got-fail if n][:3]} {res.get("error","")}')
print('total cpu', round(tot, 2))
