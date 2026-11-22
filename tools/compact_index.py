"""Compact raw per-test coverage (tools/build_coverage.py output) into the runtime index.

usage: python tools/compact_index.py RAW.json OUT.json.gz [CPU.json [CORE.json]]
Everything here is derived from one run of the pinned commit's own test suite.
"""
import gzip
import json
import os
import sys
from collections import defaultdict

raw = json.load(open(sys.argv[1]))
out = sys.argv[2]
cpu = json.load(open(sys.argv[3])) if len(sys.argv) > 3 else None
core = json.load(open(sys.argv[4])) if len(sys.argv) > 4 else None

tests = list(dict.fromkeys(raw['order']))
num = {t: i for i, t in enumerate(tests)}
by_class = defaultdict(list)
for t in tests:
    by_class[t.rsplit('::', 1)[0]].append(num[t])


def expand_setup(test):
    # setUpClass/setUp work is charged to whichever test triggers it; a break there fails the class.
    return by_class[test.rsplit('::', 1)[0]]


sets, set_id = [], {}


def intern(ids):
    key = tuple(sorted(set(ids)))
    if key not in set_id:
        set_id[key] = len(sets)
        sets.append(list(key))
    return set_id[key]


files, import_lines = {}, {}
lines_acc = defaultdict(lambda: defaultdict(set))
for rel, lines in raw['files'].items():
    for n, cs in lines.items():
        for c in cs:
            if c:
                lines_acc[rel][int(n)].add(num[c])
            else:
                import_lines.setdefault(rel, set()).add(int(n))
for rel, lines in raw['setup_files'].items():
    for n, cs in lines.items():
        for c in cs:
            lines_acc[rel][int(n)].update(expand_setup(c))
for rel in sorted(lines_acc):
    files[rel] = {str(n): intern(ids) for n, ids in sorted(lines_acc[rel].items())}
import_lines = {k: sorted(v) for k, v in sorted(import_lines.items())}

opens = defaultdict(set)
for test, paths in raw['opens'].items():
    for p in paths:
        opens[p].add(num[test])
for test, paths in raw['setup_opens'].items():
    for p in paths:
        opens[p].update(expand_setup(test))
listed = defaultdict(set)
for test, paths in raw['listed'].items():
    for p in paths:
        listed[p].add(num[test])
classes = {k: intern(num[t] for t in v) for k, v in sorted(raw['classes'].items())}

index = {'commit': 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa', 'tests': tests, 'sets': sets,
         'files': files, 'import_lines': import_lines,
         'opens': {p: intern(v) for p, v in sorted(opens.items())},
         'listed': {p: intern(v) for p, v in sorted(listed.items())},
         'classes': classes}
if core:
    index['core_modules'] = core
table_profile = os.environ.get('TABLE_PROFILE')
if table_profile:
    # per class-level dict literal: key order and which tests read/checked which keys
    raw_tables = json.load(open(table_profile))
    tables = {}
    for site, accesses in raw_tables['access'].items():
        entry = {'imp': [], 'a': {}}
        for access_key, who in accesses.items():
            ids = set()
            for t in who:
                if t == '':
                    entry['imp'].append(access_key)
                elif t.startswith('@class:'):
                    ids.update(by_class.get(t[len('@class:'):], ()))
                elif t in num:
                    ids.add(num[t])
            if ids:
                entry['a'][access_key] = intern(ids)
            elif access_key[0] in 'KVM':
                entry['a'][access_key] = intern(())
        if site in raw_tables['order']:
            entry['o'] = raw_tables['order'][site]
        tables[site] = entry
    for site, order in raw_tables['order'].items():
        tables.setdefault(site, {'imp': [], 'a': {}, 'o': order})
    index['tables'] = tables
cmp_profile = os.environ.get('CMP_PROFILE')
if cmp_profile:
    # comparison sites whose operator twins never disagreed in the pinned suite (build_cmp_profile)
    index['equivalent_sites'] = json.load(open(cmp_profile))['equivalent']
dominance_files = [f for f in os.environ.get('DOMINANCE', '').split(',') if f]
if dominance_files:
    # Synthetic mutants of the pinned repo (tools/mutants.py): for each expensive test, how often
    # its cheaper co-candidates all passed (n) and it still failed (a).
    tests_json = json.load(open(os.environ['TESTS_JSON']))['tests']
    id_of = {t['number']: t['id'] for t in tests_json}
    dom = defaultdict(lambda: [0, 0])
    for path in dominance_files:
        for r in json.load(open(path)):
            if 'skip' in r or r.get('cheap_failed') or r.get('target_failed') is None:
                continue
            if r.get('n_cheap', 1) < 1:
                continue
            dom[id_of[r['target']]][0] += 1
            dom[id_of[r['target']]][1] += int(bool(r['target_failed']))
    index['dominance'] = dict(dom)
root = os.environ.get('SQLGLOT_ROOT')
if root:
    # computed-name attribute lookups in the pinned tree (see selector.scan_dynamic_patterns)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.modules['verify_driver'] = None
    import selector
    texts = {}
    for dirpath, _, names in os.walk(os.path.join(root, 'sqlglot')):
        for name in names:
            if name.endswith('.py'):
                full = os.path.join(dirpath, name)
                texts[os.path.relpath(full, root)] = open(full, encoding='utf-8').read()
    index['dynamic_patterns'] = sorted(selector.scan_dynamic_patterns(texts))
if cpu:
    # local CPU (ms) of each test run inline the way verify.py runs it; class fixtures and imports
    index['cpu_ms'] = {t: round(v * 1000, 1) for t, v in cpu['test_cpu'].items()}
    index['class_cpu_ms'] = {t: round(v * 1000, 1) for t, v in cpu['class_cpu'].items()}
    index['import_cpu_ms'] = {t: round(v * 1000, 1) for t, v in cpu['import_cpu'].items()}
with gzip.open(out, 'wt', compresslevel=9) as f:
    json.dump(index, f, separators=(',', ':'))
print(f'{len(tests)} tests, {len(sets)} sets, {len(files)} files, {len(opens)} opened, {len(listed)} listed, '
      f'{len(classes)} classes, {os.path.getsize(out)/1e6:.2f} MB', file=sys.stderr)
