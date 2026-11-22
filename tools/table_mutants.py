"""Dev check of the class-table narrowing, from the PINNED repo only (no sample patches/answers).

Generates single-entry edits to class-level dict literals (value tweaks, entry removals, swapped
values), computes the selector's narrowed candidates, then runs the broad candidates the selector
would pick WITHOUT the table profile through the verifier. Sound iff every broad failure is also a
narrowed candidate.

usage: python tools/table_mutants.py N OUT.json [workers]
"""
import ast
import difflib
import gzip
import json
import multiprocessing as mp
import os
import random
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
ROOT = os.path.join(HERE, '.build/sqlglot')


def tables(rel):
    src = open(os.path.join(ROOT, rel), encoding='utf-8').read()
    tree = ast.parse(src)
    out = []

    def walk(body, quals):
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                walk(stmt.body, quals + [stmt.name])
            elif quals and isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name) and isinstance(stmt.value, ast.Dict):
                out.append(('.'.join(quals) + '.' + stmt.targets[0].id, stmt.value))
    walk(tree.body, [])
    return src, out


def mutate_value(v, src, rng, siblings):
    seg = ast.get_source_segment(src, v)
    if seg is None or v.lineno != v.end_lineno:
        return None
    if isinstance(v, ast.Constant):
        if isinstance(v.value, bool):
            return repr(not v.value)
        if isinstance(v.value, int):
            return repr(v.value + 1)
        if isinstance(v.value, str):
            return repr(v.value + 'x')
    if isinstance(v, ast.Tuple) and v.elts and isinstance(v.elts[0], ast.Constant) \
            and isinstance(v.elts[0].value, int) and not isinstance(v.elts[0].value, bool):
        e = list(v.elts)
        return '(' + ', '.join([repr(e[0].value + 1)] + [ast.get_source_segment(src, x) or '?' for x in e[1:]]) + ')'
    if isinstance(v, (ast.Name, ast.Attribute)):
        others = [s for s in siblings if isinstance(s, (ast.Name, ast.Attribute))
                  and ast.dump(s) != ast.dump(v) and s.lineno == s.end_lineno]
        if others:
            return ast.get_source_segment(src, rng.choice(others))
    return None


def make(rel, rng):
    src, found = tables(rel)
    rng.shuffle(found)
    lines = src.split('\n')
    for qual, d in found:
        entries = [(k, v) for k, v in zip(d.keys, d.values) if k is not None]
        if not entries:
            continue
        k, v = rng.choice(entries)
        if k.lineno != v.end_lineno:
            continue
        kind = rng.choice(['value', 'value', 'drop'])
        text = lines[k.lineno - 1]
        if kind == 'drop':
            # remove the whole entry when it sits alone on its line
            stripped = text.strip()
            entry = text[k.col_offset:v.end_col_offset]
            if stripped.rstrip(',') != entry.strip():
                continue
            new_lines = lines[:k.lineno - 1] + lines[k.lineno:]
        else:
            rep = mutate_value(v, src, rng, d.values)
            if rep is None:
                continue
            new_lines = list(lines)
            new_lines[v.lineno - 1] = text[:v.col_offset] + rep + text[v.end_col_offset:]
        new = '\n'.join(new_lines)
        if new == src:
            continue
        try:
            compile(new, rel, 'exec')
        except SyntaxError:
            continue
        diff = ''.join(difflib.unified_diff(src.splitlines(True), new.splitlines(True), f'a/{rel}', f'b/{rel}'))
        return f'{rel}:{qual}', kind, f'diff --git a/{rel} b/{rel}\n' + diff
    return None


_state = {}


def _init():
    os.environ['BR_SQLGLOT'] = ROOT
    sys.modules['verify_driver'] = None
    import selector, verify
    tests = json.load(open(os.path.join(HERE, 'sample/tests.json')))['tests']
    index = json.load(gzip.open(os.path.join(HERE, 'index.json.gz'), 'rt'))
    _state['narrow'] = selector.Selector(tests, index, ROOT)
    broad_index = dict(index, tables={})
    _state['broad'] = selector.Selector(tests, broad_index, ROOT)
    _state['selector'] = selector
    _state['verify'] = verify
    verify.preload()


def job(args):
    site, kind, patch = args
    narrow, broad, verify, S = _state['narrow'], _state['broad'], _state['verify'], _state['selector']
    nc, nreasons, sources = narrow.analyze(patch)
    if not any(r[0] == 'class-table' for r in nreasons):
        return {'site': site, 'kind': kind, 'skip': 'not-narrowed'}
    bc, _, _ = broad.analyze(patch)
    if bc == S.EVERYTHING:
        return {'site': site, 'kind': kind, 'skip': 'broad-everything'}
    ids = [broad.by_number[n]['id'] for n in sorted(bc | nc)]
    cpu = broad.index.get('cpu_ms', {})
    to = {i: max(5.0, 8 * cpu.get(i, 1000) / 1000) for i in ids}
    pid, fd = verify.start(broad.root, sources, ids, to)
    res = verify.finish(pid, fd)
    if 'failed' not in res:
        return {'site': site, 'kind': kind, 'skip': 'error'}
    number_of = {t['id']: t['number'] for t in broad.tests}
    failed, whole = set(), False
    for f in res['failed']:
        if isinstance(f, list):
            whole = True
        elif f in number_of:
            failed.add(number_of[f])
    missed = sorted(broad.by_number[n]['id'] for n in failed - nc)
    if missed and not os.environ.get('NO_RECHECK'):
        # re-run the missed tests alone, with generous timeouts, to separate load noise from misses
        again = [broad.by_number[n]['id'] for n in failed - nc]
        pid, fd = verify.start(broad.root, sources, again, {i: 120.0 for i in again})
        res2 = verify.finish(pid, fd)
        missed = sorted(f for f in res2.get('failed', again) if isinstance(f, str))
    return {'site': site, 'kind': kind, 'n_narrow': len(nc), 'n_broad': len(bc), 'failed': len(failed),
            'whole': whole, 'missed': missed}


def gen_jobs(n, seed):
    rng = random.Random(seed)
    files = sorted(os.path.relpath(os.path.join(dp, f), ROOT)
                   for dp, _, fs in os.walk(os.path.join(ROOT, 'sqlglot')) for f in fs if f.endswith('.py'))
    files = [f for f in files if tables(f)[1]]
    jobs, seen = [], set()
    tries = 0
    while len(jobs) < n and tries < n * 50:
        tries += 1
        m = make(rng.choice(files), rng)
        if m and m[2] not in seen:
            seen.add(m[2])
            jobs.append(m)
    return jobs


def main():
    n = int(sys.argv[1])
    out = sys.argv[2]
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 6
    jobs = gen_jobs(n, int(os.environ.get('SEED', '7')))
    print(f'{len(jobs)} mutants', file=sys.stderr)
    results = []
    with mp.get_context('fork').Pool(workers, initializer=_init) as pool:
        for k, r in enumerate(pool.imap_unordered(job, jobs)):
            results.append(r)
            if r.get('missed'):
                print('MISS', r, file=sys.stderr)
            if k % 10 == 0:
                print(k, file=sys.stderr)
                json.dump(results, open(out, 'w'))
    json.dump(results, open(out, 'w'))
    done = [r for r in results if 'skip' not in r]
    print(f"{len(done)} checked, {sum(1 for r in done if r['failed'])} with failures, "
          f"{sum(1 for r in done if r['missed'])} unsound, "
          f"narrow {sum(r['n_narrow'] for r in done)} vs broad {sum(r['n_broad'] for r in done)} tests",
          file=sys.stderr)


if __name__ == '__main__':
    main()
