"""Build-time: table-access profile of the pinned commit's own test suite.

Every dict literal assigned in a sqlglot class body (FUNCTIONS, NUMERIC_ESCAPES, SHOW_PARSERS, ...)
is wrapped, at import, in a dict subclass that records which keys each test reads (value reads:
[] / get) or checks (presence: in), and every whole-table operation (iteration, len, values,
items, copies, merges, comparisons, mutation). Accesses outside a test (import, class creation,
collection) are recorded under ''. A test's setup phase is charged to its whole class.

Reads no patches and no answers.
usage: python tools/build_table_profile.py SQLGLOT_CHECKOUT OUT.json
"""
import ast
import concurrent.futures as _cf
import copy
import enum
import importlib.abc
import importlib.util
import json
import os
import pickle
import sys
import time
from collections import defaultdict

if __name__ != '__main__':
    raise SystemExit(0)

ROOT = os.path.abspath(sys.argv[1])
OUT = os.path.abspath(sys.argv[2])
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ['SKIP_INTEGRATION'] = '0'

current = ''                       # '' = not inside a test
phase = 'call'
# (site, kind, key) -> set of tests;  kind: g (value read), h (presence), I (key enumeration),
# L (size/truthiness), V (all values), M (mutation)
access = defaultdict(set)
setup_access = defaultdict(set)


def key_repr(k):
    if k is None or isinstance(k, (str, int, float, bool, bytes)):
        return repr(k)
    if isinstance(k, tuple) and all(x is None or isinstance(x, (str, int, float, bool, bytes)) for x in k):
        return repr(k)
    if isinstance(k, enum.Enum):
        return f'enum:{type(k).__name__}.{k.name}'
    if isinstance(k, type):
        return f'class:{k.__name__}'
    return f'obj:{type(k).__name__}'


def rec(site, kind, key=None):
    (setup_access if phase == 'setup' else access)[(site, kind, key)].add(current)


class TrackingDict(dict):
    __slots__ = ('_site',)

    def __init__(self, site, data):
        dict.__init__(self, data)
        self._site = site

    # value reads
    def __getitem__(self, k):
        rec(self._site, 'g', key_repr(k))
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        rec(self._site, 'g', key_repr(k))
        return dict.get(self, k, default)

    # presence
    def __contains__(self, k):
        rec(self._site, 'h', key_repr(k))
        return dict.__contains__(self, k)

    # whole-table, enumerating keys (I: the usual first step of copying or caching the table)
    def __iter__(self):
        rec(self._site, 'I')
        return dict.__iter__(self)

    def keys(self):
        rec(self._site, 'I')
        return dict.keys(self)

    def __reversed__(self):
        rec(self._site, 'I')
        return dict.__reversed__(self)

    # whole-table, size / truthiness only (L)
    def __len__(self):
        rec(self._site, 'L')
        return dict.__len__(self)

    # whole-table, values too
    def values(self):
        rec(self._site, 'V')
        return dict.values(self)

    def items(self):
        rec(self._site, 'V')
        return dict.items(self)

    def copy(self):
        rec(self._site, 'V')
        return dict(dict.items(self))

    def __eq__(self, other):
        rec(self._site, 'V')
        return dict.__eq__(self, other)

    def __ne__(self, other):
        rec(self._site, 'V')
        return dict.__ne__(self, other)

    def __or__(self, other):
        rec(self._site, 'V')
        return dict.__or__(dict(dict.items(self)), other)

    def __ror__(self, other):
        rec(self._site, 'V')
        return dict.__or__(dict(other), dict(dict.items(self)))

    def __repr__(self):
        rec(self._site, 'V')
        return dict.__repr__(self)

    def __reduce_ex__(self, protocol):
        rec(self._site, 'V')
        return (dict, (dict(dict.items(self)),))

    # mutation
    def __setitem__(self, k, v):
        rec(self._site, 'M')
        dict.__setitem__(self, k, v)

    def __delitem__(self, k):
        rec(self._site, 'M')
        dict.__delitem__(self, k)

    def __ior__(self, other):
        rec(self._site, 'M')
        dict.update(self, other)
        return self

    def pop(self, *a):
        rec(self._site, 'M')
        return dict.pop(self, *a)

    def popitem(self):
        rec(self._site, 'M')
        return dict.popitem(self)

    def setdefault(self, *a):
        rec(self._site, 'M')
        return dict.setdefault(self, *a)

    def update(self, *a, **k):
        rec(self._site, 'M')
        return dict.update(self, *a, **k)

    def clear(self):
        rec(self._site, 'M')
        return dict.clear(self)


ORDER = {}                         # site -> key reprs of the literal's non-spread entries, in source order


def __brtab__(site, value, keys=None):
    if keys is not None and site not in ORDER:
        ORDER[site] = [key_repr(k) for k in keys]
    return TrackingDict(site, value) if type(value) is dict else value


def _pure(node):
    """Key expressions that are safe to evaluate a second time."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Tuple):
        return all(_pure(e) for e in node.elts)
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.Attribute):
        return _pure(node.value)
    return False


class Rewriter(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel
        self.classes = []

    def visit_FunctionDef(self, node):
        saved, self.classes = self.classes, []
        self.generic_visit(node)
        self.classes = saved
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.classes.append(node.name)
        qual = '.'.join(self.classes)
        body = []
        for stmt in node.body:
            stmt = self.visit(stmt)
            target = None
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                target = stmt.targets[0].id
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
                target = stmt.target.id
            if target and isinstance(stmt.value, ast.Dict):
                site = f'{self.rel}:{qual}.{target}'
                args = [ast.Constant(site), stmt.value]
                plain = [k for k in stmt.value.keys if k is not None]
                if all(_pure(k) for k in plain):
                    args.append(ast.Tuple(elts=[copy.deepcopy(k) for k in plain], ctx=ast.Load()))
                stmt.value = ast.copy_location(ast.Call(
                    func=ast.Name(id='__brtab__', ctx=ast.Load()), args=args, keywords=[]), stmt.value)
            body.append(stmt)
        node.body = body
        self.classes.pop()
        return node


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
        module.__dict__['__brtab__'] = __brtab__
        exec(compile(tree, full, 'exec', dont_inherit=True), module.__dict__)


sys.meta_path.insert(0, Finder())


class InlineExecutor:
    """Pool work runs in-process (round-tripped through pickle) so its accesses are seen."""

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

# Everything sqlglot builds at import time is charged to '' (import), not to the first test.
import pkgutil  # noqa: E402
import sqlglot  # noqa: E402,F401
for _m in pkgutil.walk_packages([os.path.join(ROOT, 'sqlglot')], 'sqlglot.'):
    try:
        importlib.import_module(_m.name)
    except Exception as _e:
        print('eager import failed', _m.name, _e, file=sys.stderr)


class Plugin:
    def pytest_runtest_setup(self, item):
        global current, phase
        current, phase = item.nodeid, 'setup'

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        global phase
        phase = 'call'
        yield

    def pytest_runtest_logfinish(self, nodeid, location):
        global current, phase
        current, phase = '', 'call'


t0 = time.time()
status = pytest.main(['tests', '-q', '-p', 'no:cacheprovider', '--tb=line'], plugins=[Plugin()])
print(f'pytest status {status} in {time.time() - t0:.0f}s', file=sys.stderr)


def class_of(test):
    return test.rsplit('::', 1)[0]


out = defaultdict(lambda: defaultdict(list))
for (site, kind, key), tests in access.items():
    out[site][f'{kind}|{key}' if key is not None else kind] = sorted(tests)
for (site, kind, key), tests in setup_access.items():
    k = f'{kind}|{key}' if key is not None else kind
    out[site][k] = sorted(set(out[site].get(k, [])) | {'@class:' + class_of(t) for t in tests if t})
json.dump({'access': out, 'order': ORDER}, open(OUT, 'w'))
print(f'{len(out)} sites accessed', file=sys.stderr)
