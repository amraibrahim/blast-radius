"""Build-time: which sqlglot modules `import sqlglot` loads (a crash in one breaks every test).
usage: python tools/core_modules.py SQLGLOT_CHECKOUT OUT.json"""
import json, os, sys
root = os.path.abspath(sys.argv[1])
sys.path.insert(0, root)
import sqlglot  # noqa: E402,F401
core = sorted(m for m in sys.modules if m == 'sqlglot' or m.startswith('sqlglot.'))
json.dump(core, open(sys.argv[2], 'w'))
print(len(core), 'core modules', file=sys.stderr)
