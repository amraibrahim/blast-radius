"""Build-time: measure each test's CPU cost when run the way verify.py runs it (unittest, inline
pools), plus each class's setUpClass/tearDownClass cost. Pinned checkout only.

usage: [EMPTY_FIXTURES=1] python tools/measure_cpu.py SQLGLOT_CHECKOUT TESTS_JSON OUT.json
"""
import importlib
import json
import os
import sys
import time
import unittest

root, tests_json, out = (os.path.abspath(p) for p in sys.argv[1:4])
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import verify  # noqa: E402

verify.preload()
os.chdir(root)
sys.path.insert(0, root)
os.environ['SKIP_INTEGRATION'] = '0'
import concurrent.futures as cf  # noqa: E402
cf.ProcessPoolExecutor = verify._InlineExecutor
if os.environ.get('EMPTY_FIXTURES'):
    # baseline for fixture-only commits: the .sql fixture helpers yield no records
    import tests.helpers as _helpers
    _helpers.load_sql_fixtures = lambda filename: iter(())
    _helpers.load_sql_fixture_pairs = lambda filename: iter(())

tests = json.load(open(tests_json))['tests']
by_class = {}
for t in tests:
    path, cls, meth = t['id'].split('::')
    by_class.setdefault((path, cls), []).append((t['id'], meth))

test_cpu, class_cpu, import_cpu = {}, {}, {}
devnull = open(os.devnull, 'w')
real_out, real_err = sys.stdout, sys.stderr
for (path, cls_name), items in by_class.items():
    mod_name = path[:-3].replace('/', '.')
    t0 = time.process_time()
    module = importlib.import_module(mod_name)
    import_cpu.setdefault(path, time.process_time() - t0)
    cls = getattr(module, cls_name)
    sys.stdout = sys.stderr = devnull
    try:
        t0 = time.process_time()
        cls.setUpClass()
        setup = time.process_time() - t0
        for test_id, meth in items:
            case = cls(meth)
            result = unittest.TestResult()
            t0 = time.process_time()
            case.run(result)
            test_cpu[test_id] = time.process_time() - t0
        t0 = time.process_time()
        cls.tearDownClass()
        cls.doClassCleanups()
        class_cpu[f'{path}::{cls_name}'] = setup + time.process_time() - t0
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    print(f'{path}::{cls_name} setup {class_cpu.get(f"{path}::{cls_name}", 0):.3f}', file=sys.stderr)
json.dump({'test_cpu': test_cpu, 'class_cpu': class_cpu, 'import_cpu': import_cpu}, open(out, 'w'))
