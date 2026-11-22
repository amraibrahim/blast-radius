"""Derive coverage and fixture relationships from an unmodified pinned checkout.

No patches or sample answers are read. Run: python build_index.py CHECKOUT OUTPUT
"""
import json
import os
import sys
import gzip
from collections import defaultdict
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
OUTPUT = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ['SKIP_INTEGRATION'] = '0'
import coverage
import pytest

cov = coverage.Coverage(source=[str(ROOT / 'sqlglot')], data_file=None, config_file=False)
current = None
fixtures = defaultdict(set)
results = {}
collected = set()

def audit(event, args):
    if event == 'open' and current and isinstance(args[0], str):
        name = os.path.abspath(args[0])
        prefix = str(ROOT) + '/tests/'
        if name.startswith(prefix) and '/fixtures/' in name:
            fixtures[os.path.relpath(name, ROOT)].add(current)

class Plugin:
    def pytest_collection_modifyitems(self, items):
        collected.update(item.nodeid for item in items)

    def pytest_runtest_setup(self, item):
        global current
        current = item.nodeid
        cov.switch_context(current)

    def pytest_runtest_logreport(self, report):
        if report.failed:
            results[report.nodeid] = report.longreprtext[-1500:]

    def pytest_runtest_logfinish(self, nodeid):
        global current
        current = None
        cov.switch_context('')

sys.addaudithook(audit)
cov.start()
status = pytest.main(['tests', '-q', '--tb=short', '-p', 'no:cacheprovider'], plugins=[Plugin()])
cov.stop()
data = cov.get_data()
files = {}
for filename in data.measured_files():
    path = Path(filename)
    relative = str(path.relative_to(ROOT))
    lines = {str(n): sorted(c for c in contexts if c) for n, contexts in data.contexts_by_lineno(filename).items()}
    lines = {n: cs for n, cs in lines.items() if cs}
    files[relative] = {'source': path.read_text(), 'lines': lines}
# Include unexecuted source so semantic comparison and fallback still work.
for path in (ROOT / 'sqlglot').rglob('*.py'):
    files.setdefault(str(path.relative_to(ROOT)), {'source': path.read_text(), 'lines': {}})
index = {'commit': 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa', 'files': files,
         'fixtures': {p: sorted(cs) for p, cs in fixtures.items()},
         'collected': sorted(collected), 'baseline_failures': results, 'pytest_status': int(status)}
with gzip.open(OUTPUT, 'wt') as f:
    json.dump(index, f, separators=(',', ':'))
print(f'Index: {len(files)} sources, {len(fixtures)} fixtures, {len(collected)} tests, {len(results)} baseline failures', file=sys.stderr)
