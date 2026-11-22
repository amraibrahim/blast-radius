"""Build-time: run the pinned sqlglot test suite once, recording per-test line coverage
and per-test file reads. Reads no patches and no answers.

usage: python tools/build_coverage.py SQLGLOT_CHECKOUT OUT.json
"""
import dis, json, os, sys, time
from collections import defaultdict
from pathlib import Path

if __name__ != '__main__':
    raise SystemExit(0)
ROOT = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
os.environ['SKIP_INTEGRATION'] = '0'
import pytest
import concurrent.futures as _cf
import pickle

class InlineExecutor:
    """Run pool work synchronously in-process so coverage attributes it to the test."""
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=()):
        if initializer:
            initializer(*initargs)
    def submit(self, fn, *a, **k):
        # Round-trip through pickle exactly like a real process pool would.
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

mon = sys.monitoring
TOOL = 3
mon.use_tool_id(TOOL, 'blast-radius')
SRC = (str(ROOT / 'sqlglot') + os.sep, str(ROOT / 'tests') + os.sep)
hits = defaultdict(set)          # test id ('' = import/collection time) -> {(file, line)}
current = None
bucket = hits['']

def on_line(code, line):
    fn = code.co_filename
    if fn.startswith(SRC):
        bucket.add((fn, line))
    return mon.DISABLE

PROLOGUE = {'RESUME', 'MAKE_CELL', 'COPY_FREE_VARS', 'RETURN_GENERATOR', 'POP_TOP', 'NOP', 'CACHE'}
_first_line = {}

def first_body_line(code):
    """Line of the first instruction after the prologue. LINE fires only when the line changes,
    so a body that starts on its own definition line (a lambda, `def f(): return x`) never
    produces a LINE event for that line; PY_START records it instead."""
    line = _first_line.get(code)
    if line is None:
        line = 0
        for ins in dis.get_instructions(code):
            if ins.opname not in PROLOGUE and ins.positions and ins.positions.lineno:
                line = ins.positions.lineno
                break
        _first_line[code] = line
    return line

def on_start(code, offset):
    fn = code.co_filename
    if fn.startswith(SRC):
        line = first_body_line(code)
        if line:
            bucket.add((fn, line))
    return mon.DISABLE

mon.register_callback(TOOL, mon.events.LINE, on_line)
mon.register_callback(TOOL, mon.events.PY_START, on_start)
mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)
opens = defaultdict(set)
outcomes = {}
prefix = str(ROOT) + os.sep

def audit(event, args):
    if event == 'open' and current is not None and isinstance(args[0], (str, bytes, os.PathLike)):
        try:
            name = os.path.abspath(os.fsdecode(args[0]))
        except Exception:
            return
        if name.startswith(prefix) and not name.endswith('.pyc'):
            (setup_opens if phase == 'setup' else opens)[current].add(os.path.relpath(name, ROOT))
    elif event in ('os.listdir', 'os.scandir', 'glob.glob') and current is not None:
        try:
            name = os.path.abspath(os.fsdecode(args[0]) if args and args[0] is not None else '.')
        except Exception:
            return
        if name.startswith(prefix):
            listed[current].add(os.path.relpath(name, ROOT))

# Which sqlglot classes each test instantiates (recorded with their MRO). This lets a change to
# a class body (constants, tables) map to the tests that actually use that class.
instances = defaultdict(set)

def _wrap(base):
    original = base.__init__
    def __init__(self, *a, **k):
        if current is not None:
            instances[current].add(type(self))
        original(self, *a, **k)
    base.__init__ = __init__

import sqlglot
from sqlglot.dialects.dialect import Dialect
from sqlglot.tokens import Tokenizer
from sqlglot.parser import Parser
from sqlglot.generator import Generator
from sqlglot import exp as _exp
from sqlglot import schema as _schema
for _base in (Dialect, Tokenizer, Parser, Generator, _exp.Expr, _schema.Schema):
    _wrap(_base)

# Import every sqlglot module now, so code that runs at import / class-creation time is recorded
# as import-time (context '') instead of being charged to whichever test first loads a dialect.
import pkgutil, importlib
for _m in pkgutil.walk_packages([str(ROOT / 'sqlglot')], 'sqlglot.'):
    try:
        importlib.import_module(_m.name)
    except Exception as _e:
        print('eager import failed', _m.name, _e, file=sys.stderr)

setup_hits = defaultdict(set)    # lines first executed during a test's setup phase (setUpClass/setUp)
setup_opens = defaultdict(set)
listed = defaultdict(set)
phase = 'call'
order = []

class Plugin:
    def pytest_runtest_setup(self, item):
        global current, bucket, phase
        current = item.nodeid
        order.append(current)
        phase = 'setup'
        bucket = setup_hits[current]
        mon.restart_events()

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        global bucket, phase
        phase = 'call'
        bucket = hits[item.nodeid]
        mon.restart_events()
        yield

    def pytest_runtest_logreport(self, report):
        if report.failed:
            outcomes[report.nodeid] = 'failed'
    def pytest_runtest_logfinish(self, nodeid, location):
        global current, bucket
        current = None
        bucket = hits['']
        mon.restart_events()

sys.addaudithook(audit)
t0 = time.time()
status = pytest.main(['tests', '-q', '-p', 'no:cacheprovider', '--tb=line'], plugins=[Plugin()])
mon.set_events(TOOL, 0)
print(f'pytest status {status} in {time.time()-t0:.0f}s', file=sys.stderr)
files = defaultdict(lambda: defaultdict(list))
for test, pairs in hits.items():
    for fn, line in pairs:
        files[os.path.relpath(fn, ROOT)][str(line)].append(test)
setup_files = defaultdict(lambda: defaultdict(list))
for test, pairs in setup_hits.items():
    for fn, line in pairs:
        setup_files[os.path.relpath(fn, ROOT)][str(line)].append(test)
classes = defaultdict(set)
for test, types in instances.items():
    for ty in types:
        for c in ty.__mro__:
            if c.__module__.startswith('sqlglot'):
                classes[f'{c.__module__}:{c.__qualname__}'].add(test)
json.dump({'order': order, 'setup_files': setup_files, 'setup_opens': {k: sorted(v) for k, v in setup_opens.items()}, 'listed': {k: sorted(v) for k, v in listed.items()}, 'classes': {k: sorted(v) for k, v in classes.items()}, 'files': files, 'opens': {k: sorted(v) for k, v in opens.items()}, 'failed': outcomes}, open(OUT, 'w'))
print('done', len(files), len(hits), file=sys.stderr)
