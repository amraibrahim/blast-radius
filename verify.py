"""Run chosen sqlglot tests against a patched tree, in a forked child, and report failures.

The pinned checkout is read-only. Patched Python modules are served from memory by an
import hook; patched data files (fixtures) are written to /tmp and `open` is redirected.
The parent never imports sqlglot, so every child starts from pristine module state.
"""
import builtins
import importlib.abc
import importlib.util
import io
import json
import os
import pickle
import signal
import sys
import tempfile
import time
import unittest

ALL = '__ALL__'


class _Timeout(Exception):
    """Raised by SIGALRM inside a test that ran far past its expected cost (a hang)."""


class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, root, sources):
        self.root = root
        self.by_module = {}
        for rel, text in sources.items():
            if not rel.endswith('.py'):
                continue
            parts = rel[:-3].split('/')
            package = parts[-1] == '__init__'
            if package:
                parts = parts[:-1]
            self.by_module['.'.join(parts)] = (os.path.join(root, rel), text, package)

    def find_spec(self, name, path=None, target=None):
        entry = self.by_module.get(name)
        if entry is None:
            return None
        filename, _, package = entry
        spec = importlib.util.spec_from_file_location(
            name, filename, loader=self,
            submodule_search_locations=[os.path.dirname(filename)] if package else None)
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        filename, text, _ = self.by_module[module.__spec__.name]
        code = compile(text, filename, 'exec', dont_inherit=True)
        exec(code, module.__dict__)


class _InlineExecutor:
    """Stand-in for ProcessPoolExecutor: spawned children would import the unpatched tree."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        if initializer:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        import concurrent.futures as cf
        future = cf.Future()
        try:
            # a real pool pickles the call and its result (sqlglot pickles via serde)
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            future.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except _Timeout:
            raise
        except BaseException as exc:  # noqa: BLE001 - mirror executor semantics
            future.set_exception(exc)
        return future

    def map(self, fn, *iterables, **_):
        return list(map(fn, *iterables))

    def shutdown(self, *_, **__):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Result(unittest.TestResult):
    def __init__(self, timeouts=None):
        super().__init__()
        self.bad = set()
        self.timeouts = timeouts or {}

    @staticmethod
    def _pytest_id(test):
        parts = test.id().rsplit('.', 2)
        return f"{parts[0].replace('.', '/')}.py::{parts[1]}::{parts[2]}" if len(parts) == 3 else ''

    def startTest(self, test):
        super().startTest(test)
        signal.setitimer(signal.ITIMER_REAL, self.timeouts.get(self._pytest_id(test), 10.0))

    def stopTest(self, test):
        signal.setitimer(signal.ITIMER_REAL, 0)
        super().stopTest(test)

    def _mark(self, test):
        test_id = getattr(test, 'id', lambda: '')()
        self.bad.add(test_id)

    def addError(self, test, err):
        if isinstance(test, unittest.case._SubTest):
            test = test.test_case
        if not hasattr(test, '_testMethodName'):
            # setUpClass / module fixture failure: unittest reports a _ErrorHolder.
            self.bad.add(('ERR', str(test)))
            return
        self._mark(test)

    def addFailure(self, test, err):
        self._mark(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._mark(test)

    def addUnexpectedSuccess(self, test):
        self._mark(test)


def _child(root, sources, test_ids, timeouts, extra_files, fixture_records=False):
    """Executed in the forked child. Returns a JSON-able dict."""
    os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ['SKIP_INTEGRATION'] = '0'
    sys.dont_write_bytecode = True

    # Data-file overrides go to private temp files; `open` maps the original path to them.
    redirect = {}
    tmpdir = None
    for rel, text in sources.items():
        if rel.endswith('.py') and (rel.startswith('sqlglot/') or rel.startswith('tests/')):
            continue
        if tmpdir is None:
            tmpdir = tempfile.mkdtemp(prefix='br')
        target = os.path.join(tmpdir, str(len(redirect)))
        with open(target, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
        redirect[os.path.join(root, rel)] = target
    if redirect:
        real_open = builtins.open
        real_io_open = io.open

        def _map(file):
            if isinstance(file, (str, os.PathLike)):
                name = os.path.abspath(os.fspath(file))
                return redirect.get(name, file)
            return file

        def patched_open(file, *args, **kwargs):
            return real_open(_map(file), *args, **kwargs)

        def patched_io_open(file, *args, **kwargs):
            return real_io_open(_map(file), *args, **kwargs)

        builtins.open = patched_open
        io.open = patched_io_open
        import gzip
        real_gzip_open = gzip.open
        gzip.open = lambda file, *a, **k: real_gzip_open(_map(file), *a, **k)

    if fixture_records and redirect:
        _restrict_fixture_loaders(redirect)
    finder = _Finder(root, sources)
    stale = [m for m in finder.by_module if m in sys.modules]
    if stale:
        raise RuntimeError(f'patched modules already imported: {stale}')
    sys.meta_path.insert(0, finder)
    import concurrent.futures as cf
    cf.ProcessPoolExecutor = _InlineExecutor
    import concurrent.futures.process as cfp
    cfp.ProcessPoolExecutor = _InlineExecutor

    def on_alarm(signum, frame):
        raise _Timeout()

    signal.signal(signal.SIGALRM, on_alarm)

    failed = set()
    # A changed module that no longer imports breaks every test that imports it, run here or not.
    for rel in sources:
        if rel.endswith('.py') and rel.startswith('sqlglot/'):
            name = rel[:-3].replace('/', '.')
            if name.endswith('.__init__'):
                name = name[:-9]
            try:
                signal.alarm(60)
                importlib.import_module(name)
                signal.alarm(0)
            except BaseException:
                signal.alarm(0)
                failed.add(('IMPORT', rel))
    if failed:
        return [list(item) for item in failed]
    by_module = {}
    for test_id in test_ids:
        path, cls, method = test_id.split('::')
        module = path[:-3].replace('/', '.')
        by_module.setdefault(module, {}).setdefault(cls, []).append(method)

    loader = unittest.TestLoader()
    for module_name, classes in by_module.items():
        ids_here = [f"{module_name.replace('.', '/')}.py::{c}::{m}" for c, ms in classes.items() for m in ms]
        try:
            signal.alarm(60)
            module = importlib.import_module(module_name)
            signal.alarm(0)
        except BaseException:  # import failure fails every test in the file
            signal.alarm(0)
            failed.add(('FILE', module_name.replace('.', '/') + '.py'))
            continue
        for cls_name, methods in classes.items():
            cls = getattr(module, cls_name, None)
            if cls is None:
                failed.update(f"{module_name.replace('.', '/')}.py::{cls_name}::{m}" for m in methods)
                continue
            suite = unittest.TestSuite()
            for method in methods:
                try:
                    suite.addTest(loader.loadTestsFromName(method, cls))
                except BaseException:
                    failed.add(f"{module_name.replace('.', '/')}.py::{cls_name}::{method}")
            result = _Result(timeouts)
            try:
                signal.alarm(120)   # class fixtures; tests re-arm their own timer
                suite.run(result)
                signal.alarm(0)
            except _Timeout:
                signal.alarm(0)
                failed.update(f"{module_name.replace('.', '/')}.py::{cls_name}::{m}" for m in methods)
                continue
            except BaseException:
                signal.alarm(0)
                failed.update(f"{module_name.replace('.', '/')}.py::{cls_name}::{m}" for m in methods)
                continue
            for item in result.bad:
                if isinstance(item, tuple):  # class fixture error -> whole class
                    failed.update(f"{module_name.replace('.', '/')}.py::{cls_name}::{m}" for m in methods)
                else:
                    # unittest id: tests.test_x.TestX.test_y
                    parts = item.rsplit('.', 2)
                    if len(parts) == 3:
                        failed.add(f"{parts[0].replace('.', '/')}.py::{parts[1]}::{parts[2]}")
    out = []
    for item in failed:
        out.append(list(item) if isinstance(item, tuple) else item)
    return out


def preload():
    """Import heavy third-party modules once in the parent; children inherit them."""
    for name in ('numpy', 'pandas', 'pandas.testing', 'duckdb', 'unittest.mock', 'concurrent.futures',
                 'concurrent.futures.process', 'multiprocessing', 'dateutil', 'pytz', 'decimal', 'datetime'):
        try:
            importlib.import_module(name)
        except BaseException:
            pass


def _restrict_fixture_loaders(redirect):
    """Fixture-only commits: the code is the pinned code, so every fixture record the edit left
    unchanged behaves exactly as in the pinned (passing) run. The test helpers that read .sql
    fixtures therefore yield only the records that differ from the pinned file (a multiset
    difference; both versions are read by the helper itself). Tests that read a fixture any other
    way see the whole patched file, as before."""
    import json as _json
    from collections import Counter
    import tests.helpers as helpers
    fixtures_dir = os.path.abspath(helpers.FIXTURES_DIR)
    keys = {os.path.abspath(k): k for k in redirect}

    def wrap(real):
        def loader(filename):
            full = os.path.abspath(os.path.join(fixtures_dir, filename))
            if full not in keys:
                yield from real(filename)
                return
            new = list(real(filename))            # the patched text (open is redirected)
            target = redirect.pop(keys[full])
            try:
                old = list(real(filename))        # the pinned file on disk
            finally:
                redirect[keys[full]] = target
            seen = Counter(_json.dumps(r, sort_keys=True, default=str) for r in old)
            for rec in new:
                k = _json.dumps(rec, sort_keys=True, default=str)
                if seen[k] > 0:
                    seen[k] -= 1
                else:
                    yield rec

        loader.__name__ = real.__name__
        return loader

    helpers.load_sql_fixtures = wrap(helpers.load_sql_fixtures)
    helpers.load_sql_fixture_pairs = wrap(helpers.load_sql_fixture_pairs)


def start(root, sources, test_ids, timeouts, extra_files=None, fixture_records=False):
    """Fork a child that runs the tests; returns (pid, read_fd)."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        code = 0
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            payload = json.dumps({'failed': _child(root, sources, test_ids, timeouts, extra_files, fixture_records)})
        except BaseException as exc:  # noqa: BLE001
            payload = json.dumps({'error': repr(exc)})
            code = 1
        data = payload.encode()
        while data:
            n = os.write(write_fd, data)
            data = data[n:]
        os.close(write_fd)
        os._exit(code)
    os.close(write_fd)
    return pid, read_fd


def finish(pid, read_fd, deadline=None):
    chunks = []
    while True:
        chunk = os.read(read_fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(read_fd)
    _, _, usage = os.wait4(pid, 0)
    cpu = usage.ru_utime + usage.ru_stime
    try:
        result = json.loads(b''.join(chunks).decode())
    except ValueError:
        result = {'error': 'no result'}
    result['cpu'] = cpu
    return result
