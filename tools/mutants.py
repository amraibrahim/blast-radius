"""Dev/build calibration from the PINNED repo only (no sample patches, no answers).

Generates synthetic single-site mutants on lines covered by each expensive test, runs the
selector's own static candidates through the verifier, and measures how often an expensive test
fails while every cheaper candidate passes. That conditional rate decides whether listing an
expensive test is worth it when its cheaper co-covering tests all pass.

usage: python tools/mutants.py N_PER_TEST OUT.json [workers]
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
EXPENSIVE_S = 2.0

EQUIV_SWAP = {ast.Is: ast.Eq, ast.Eq: ast.Is, ast.IsNot: ast.NotEq, ast.NotEq: ast.IsNot}
CMP_SWAP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE,
            ast.GtE: ast.Gt, ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}


def mutations(node):
    """Yield (description, mutate_fn) for one AST node. mutate_fn mutates in place."""
    if isinstance(node, ast.Compare):
        for i, op in enumerate(node.ops):
            if type(op) in CMP_SWAP:
                yield 'cmp', lambda n=node, i=i: n.ops.__setitem__(i, CMP_SWAP[type(n.ops[i])]())
            if type(op) in EQUIV_SWAP and isinstance(node.comparators[i], (ast.Constant, ast.Attribute)):
                yield 'is-eq', lambda n=node, i=i: n.ops.__setitem__(i, EQUIV_SWAP[type(n.ops[i])]())
    if isinstance(node, ast.BoolOp):
        yield 'boolop', lambda n=node: setattr(n, 'op', ast.Or() if isinstance(n.op, ast.And) else ast.And())
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        yield 'not', None  # handled by parent replacement
    if isinstance(node, ast.Constant):
        if node.value is True or node.value is False:
            yield 'bool', lambda n=node: setattr(n, 'value', not n.value)
        elif isinstance(node.value, int) and not isinstance(node.value, bool):
            yield 'int', lambda n=node: setattr(n, 'value', n.value + 1)
        elif isinstance(node.value, str) and node.value and len(node.value) < 40:
            yield 'str', lambda n=node: setattr(n, 'value', n.value + 'x')
    if isinstance(node, ast.BinOp):
        swap = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv, ast.FloorDiv: ast.Mult}
        if type(node.op) in swap:
            yield 'binop', lambda n=node: setattr(n, 'op', swap[type(n.op)]())
    if isinstance(node, (ast.If, ast.While)) :
        yield 'negate', lambda n=node: setattr(n, 'test', ast.UnaryOp(op=ast.Not(), operand=n.test))


def make_mutant(rel, line, rng):
    src = open(os.path.join(ROOT, rel), encoding='utf-8').read()
    tree = ast.parse(src)
    options = []
    for node in ast.walk(tree):
        if getattr(node, 'lineno', None) == line and getattr(node, 'end_lineno', None) == line or \
                isinstance(node, (ast.If, ast.While)) and getattr(node, 'lineno', None) == line:
            for desc, fn in mutations(node):
                if fn is not None:
                    options.append((desc, node, fn))
    if not options:
        return None
    desc, node, fn = rng.choice(options)
    # Re-render only the mutated line: unparse the smallest single-line statement holding it.
    lines = src.split('\n')
    stmt = None
    for n in ast.walk(tree):
        if isinstance(n, ast.stmt) and n.lineno == line and n.end_lineno == line:
            stmt = n
    if stmt is None and isinstance(node, (ast.If, ast.While)) and node.test.lineno == node.test.end_lineno == line:
        fn()
        text = lines[line - 1]
        indent = text[:len(text) - len(text.lstrip())]
        kw = 'if' if isinstance(node, ast.If) else 'while'
        if not text.lstrip().startswith((kw + ' ', 'elif ')) or not text.rstrip().endswith(':'):
            return None
        head = 'elif ' if text.lstrip().startswith('elif ') else kw + ' '
        lines[line - 1] = f'{indent}{head}{ast.unparse(node.test)}:'
    elif stmt is not None and not isinstance(stmt, (ast.If, ast.While, ast.For, ast.With, ast.Try) + (ast.FunctionDef, ast.ClassDef)):
        fn()
        text = lines[line - 1]
        indent = text[:len(text) - len(text.lstrip())]
        lines[line - 1] = indent + ast.unparse(stmt)
    else:
        return None
    new = '\n'.join(lines)
    if new == src:
        return None
    try:
        compile(new, rel, 'exec')
    except SyntaxError:
        return None
    diff = ''.join(difflib.unified_diff(src.splitlines(True), new.splitlines(True), f'a/{rel}', f'b/{rel}'))
    return desc, f'diff --git a/{rel} b/{rel}\n' + diff


_state = {}


def _init():
    os.environ['BR_SQLGLOT'] = ROOT
    sys.modules['verify_driver'] = None
    import selector, verify
    tests = json.load(open(os.path.join(HERE, 'sample/tests.json')))['tests']
    index = json.load(gzip.open(os.path.join(HERE, 'index.json.gz'), 'rt'))
    _state['sel'] = selector.Selector(tests, index, ROOT)
    _state['selector'] = selector
    _state['verify'] = verify
    verify.preload()


def _run(sel, verify, sources, numbers):
    ids = [sel.by_number[n]['id'] for n in numbers]
    cpu = sel.index.get('cpu_ms', {})
    to = {i: max(3.0, 8 * cpu.get(i, 1000) / 1000) for i in ids}
    pid, fd = verify.start(sel.root, sources, ids, to)
    res = verify.finish(pid, fd)
    if 'failed' not in res:
        return None
    number_of = {t['id']: t['number'] for t in sel.tests}
    out = set()
    for f in res['failed']:
        if isinstance(f, list):
            out |= set(numbers)
        elif f in number_of:
            out.add(number_of[f])
    return out


def job(args):
    target, rel, line, desc, patch = args
    sel, verify = _state['sel'], _state['verify']
    cand, reasons, sources = sel.analyze(patch)
    if cand == _state['selector'].EVERYTHING or target not in cand:
        return {'target': target, 'rel': rel, 'line': line, 'desc': desc, 'skip': 'not-candidate'}
    if len(cand) > len(sel.all) * 0.5:
        return {'target': target, 'rel': rel, 'line': line, 'desc': desc, 'skip': 'broad', 'n_cand': len(cand)}
    expensive = {n for n in cand if sel.by_number[n]['seconds'] >= EXPENSIVE_S}
    cheap = sorted(cand - expensive)
    cheap_failed = _run(sel, verify, sources, cheap) if cheap else set()
    if cheap_failed is None:
        return {'target': target, 'rel': rel, 'line': line, 'desc': desc, 'skip': 'error'}
    rec = {'target': target, 'rel': rel, 'line': line, 'desc': desc, 'n_cand': len(cand),
           'n_cheap': len(cheap), 'cheap_failed': len(cheap_failed)}
    if not cheap_failed:
        exp_failed = _run(sel, verify, sources, [target])
        rec['target_failed'] = None if exp_failed is None else bool(exp_failed)
    return rec


def main():
    n_per = int(sys.argv[1])
    out = sys.argv[2]
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 6
    tests = json.load(open(os.path.join(HERE, 'sample/tests.json')))['tests']
    index = json.load(gzip.open(os.path.join(HERE, 'index.json.gz'), 'rt'))
    number_of = {t['id']: t['number'] for t in tests}
    idx = [number_of.get(t) for t in index['tests']]
    targets = [t['number'] for t in tests if t['seconds'] >= EXPENSIVE_S]
    only = os.environ.get('TARGETS')
    if only:
        targets = [n for n in targets if any(k in tests[n]['id'] for k in only.split(','))]
    rng = random.Random(int(os.environ.get('SEED', '1234')))
    jobs = []
    for target in targets:
        lines = []
        for rel, table in index['files'].items():
            if not rel.startswith('sqlglot/'):
                continue
            for n, i in table.items():
                if any(idx[x] == target for x in index['sets'][i]):
                    lines.append((rel, int(n)))
        rng.shuffle(lines)
        got = 0
        for rel, n in lines:
            m = make_mutant(rel, n, rng)
            if m:
                jobs.append((target, rel, n) + m)
                got += 1
                if got >= n_per:
                    break
    # interleave targets so partial results cover every expensive test
    buckets = {}
    for j in jobs:
        buckets.setdefault(j[0], []).append(j)
    jobs = [b[i] for i in range(max(map(len, buckets.values()))) for b in buckets.values() if i < len(b)]
    print(f'{len(jobs)} jobs over {len(targets)} expensive tests', file=sys.stderr)
    ctx = mp.get_context('fork')
    results = []
    with ctx.Pool(workers, initializer=_init) as pool:
        for k, r in enumerate(pool.imap_unordered(job, jobs)):
            if r:
                results.append(r)
            if k % 20 == 0:
                print(k, len(results), file=sys.stderr)
                json.dump(results, open(out, 'w'))
    json.dump(results, open(out, 'w'))


if __name__ == '__main__':
    main()
