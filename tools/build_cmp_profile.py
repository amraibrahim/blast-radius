"""Build-time: comparison profile of the pinned commit's own test suite.

Every single-operator comparison in sqlglot using is / is not / == / != / < / <= / > / >= is
rewritten (at import, same file and line numbers) into a call that evaluates the original
comparison and also checks whether the operator's twin would have given a different answer
(`is` vs `==`, `is not` vs `!=`, `<` vs `<=`, `>` vs `>=`) for the values seen. A site whose twins
never disagreed during the whole suite behaves identically under that operator swap for every
test. Reads no patches and no answers.

usage: python tools/build_cmp_profile.py SQLGLOT_CHECKOUT OUT.json
"""
import ast
import concurrent.futures as _cf
import importlib.abc
import importlib.util
import json
import os
import pickle
import sys
import time

if __name__ != '__main__':
    raise SystemExit(0)

ROOT = os.path.abspath(sys.argv[1])
OUT = os.path.abspath(sys.argv[2])
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ['SKIP_INTEGRATION'] = '0'

GROUP = {ast.Is: 'eq', ast.IsNot: 'eq', ast.Eq: 'eq', ast.NotEq: 'eq',
         ast.Lt: 'lt', ast.LtE: 'lt', ast.Gt: 'gt', ast.GtE: 'gt'}
OPS = {ast.Is: lambda a, b: a is b, ast.IsNot: lambda a, b: a is not b,
       ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b,
       ast.Lt: lambda a, b: a < b, ast.LtE: lambda a, b: a <= b,
       ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b}

sites = []          # sid -> (rel, line, col, opname)
site_op = []        # sid -> op function
site_group = []
executed = bytearray()
diverged = bytearray()


def __brcmp__(sid, a, b):
    result = site_op[sid](a, b)
    executed[sid] = 1
    if not diverged[sid]:
        try:
            group = site_group[sid]
            if group == 'eq':
                same = a is b
                if same:
                    div = not (a == b)          # NaN-like values
                else:
                    div = bool(a == b)
            elif group == 'lt':
                div = bool(a < b) != bool(a <= b)
            else:
                div = bool(a > b) != bool(a >= b)
        except BaseException:
            div = True
        if div:
            diverged[sid] = 1
    return result


class Rewriter(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in GROUP:
            return node
        op = type(node.ops[0])
        sid = len(sites)
        sites.append((self.rel, node.lineno, node.col_offset, op.__name__))
        site_op.append(OPS[op])
        site_group.append(GROUP[op])
        executed.append(0)
        diverged.append(0)
        call = ast.Call(func=ast.Name(id='__brcmp__', ctx=ast.Load()),
                        args=[ast.Constant(sid), node.left, node.comparators[0]], keywords=[])
        return ast.copy_location(call, node)


class Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path=None, target=None):
        if not (name == 'sqlglot' or name.startswith('sqlglot.')):
            return None
        rel = name.replace('.', '/')
        for candidate, package in ((rel + '/__init__.py', True), (rel + '.py', False)):
            full = os.path.join(ROOT, candidate)
            if os.path.exists(full):
                return importlib.util.spec_from_file_location(
                    name, full, loader=self,
                    submodule_search_locations=[os.path.dirname(full)] if package else None)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        full = module.__spec__.origin
        rel = os.path.relpath(full, ROOT)
        tree = ast.parse(open(full, encoding='utf-8').read(), full)
        tree = ast.fix_missing_locations(Rewriter(rel).visit(tree))
        module.__dict__['__brcmp__'] = __brcmp__
        exec(compile(tree, full, 'exec', dont_inherit=True), module.__dict__)


sys.meta_path.insert(0, Finder())


class InlineExecutor:
    """Pool work runs in-process (round-tripped through pickle) so it is profiled too."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        if initializer:
            initializer(*initargs)

    def submit(self, fn, *a, **k):
        f = _cf.Future()
        try:
            fn, a, k = pickle.loads(pickle.dumps((fn, a, k)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:
            f.set_exception(e)
        return f

    def map(self, fn, *its, **k):
        return map(fn, *its)

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_cf.ProcessPoolExecutor = InlineExecutor
import pytest  # noqa: E402

t0 = time.time()
status = pytest.main(['tests', '-q', '-p', 'no:cacheprovider', '--tb=line'])
print(f'pytest status {status} in {time.time() - t0:.0f}s', file=sys.stderr)
equivalent = sorted(f'{rel}:{line}:{col}' for (rel, line, col, _), e, d in zip(sites, executed, diverged) if e and not d)
divergent = sum(1 for e, d in zip(executed, diverged) if e and d)
json.dump({'equivalent': equivalent}, open(OUT, 'w'))
print(f'{len(sites)} sites, {sum(executed)} executed, {len(equivalent)} never diverged, {divergent} diverged',
      file=sys.stderr)
