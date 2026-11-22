"""Dev: run the static analysis on hand-made edge-case patches against the pinned tree."""
import difflib, gzip, json, os, sys
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE); sys.modules['verify_driver'] = None
import selector as S
ROOT = os.path.join(HERE, '.build/sqlglot')
T = json.load(open(f'{HERE}/sample/tests.json'))['tests']
sel = S.Selector(T, json.load(gzip.open(f'{HERE}/index.json.gz', 'rt')), ROOT)
ms = sel.ms

def edit(rel, old, new, count=1):
    src = open(os.path.join(ROOT, rel)).read()
    assert old in src, (rel, old)
    dst = src.replace(old, new, count)
    return f'diff --git a/{rel} b/{rel}\n' + ''.join(difflib.unified_diff(src.splitlines(True), dst.splitlines(True), f'a/{rel}', f'b/{rel}'))

cases = {
 'readme': edit('README.md', 'SQLGlot', 'SQLglot'),
 'test-method': edit('tests/test_parser.py', 'parse_one("SELECT 1", max_nodes=1_000_000)', 'parse_one("SELECT 2", max_nodes=1_000_000)'),
 'validator': edit('tests/dialects/test_dialect.py', 'def validate_identity(', 'def validate_identity(  '),
 'helpers-fn': edit('tests/helpers.py', 'if line and not line.startswith("--")', 'if line and not line.startswith("-")'),
 'new-file': 'diff --git a/sqlglot/dialects/foo.py b/sqlglot/dialects/foo.py\nnew file mode 100644\n--- /dev/null\n+++ b/sqlglot/dialects/foo.py\n@@ -0,0 +1,1 @@\n+X = 1\n',
 'new-expr-file': 'diff --git a/sqlglot/expressions/foo.py b/sqlglot/expressions/foo.py\nnew file mode 100644\n--- /dev/null\n+++ b/sqlglot/expressions/foo.py\n@@ -0,0 +1,1 @@\n+X = 1\n',
 'deleted': 'diff --git a/sqlglot/dialects/drill.py b/sqlglot/dialects/drill.py\ndeleted file mode 100644\n',
 'syntax-dialect': edit('sqlglot/parsers/dremio.py', 'class DremioParser(parser.Parser):', 'class DremioParser(parser.Parser)'),
 'syntax-core': edit('sqlglot/helper.py', 'def seq_get(', 'def seq_get(('),
 'module-const': edit('sqlglot/time.py', 'def format_time(', 'def format_time(', 1),
 'decorator': edit('sqlglot/optimizer/simplify.py', '@catch(ModuleNotFoundError, UnsupportedUnit)', '@catch(ModuleNotFoundError)') if '@catch(ModuleNotFoundError, UnsupportedUnit)' in open(f'{ROOT}/sqlglot/optimizer/simplify.py').read() else '',
 'signature-default': edit('sqlglot/helper.py', 'def seq_get(seq: Sequence[T], index: int)', 'def seq_get(seq: Sequence[T], index: int = 0)') if 'def seq_get(seq: Sequence[T], index: int)' in open(f'{ROOT}/sqlglot/helper.py').read() else '',
 'docstring': edit('sqlglot/transforms.py', '1. You cannot specify the (+) operator', '1. You can not specify the (+) operator'),
 'tokentype-enum': edit('sqlglot/tokenizer_core.py', '    L_PAREN = auto()', '    L_PAREN = auto()\n    L_PAREN2 = auto()'),
 'import-change': edit('sqlglot/optimizer/scope.py', 'from sqlglot.helper import find_new_name, mypyc_attr, seq_get', 'from sqlglot.helper import find_new_name, mypyc_attr, seq_get, ensure_list'),
 'crlf-noeol': edit('sqlglot/generators/drill.py', 'return self._ilike_func_sql(expression)', 'return self._ilike_func_sql(expression) ').replace('\n', '\r\n'),
 'rename': 'diff --git a/sqlglot/dialects/drill.py b/sqlglot/dialects/drill2.py\nsimilarity index 100%\nrename from sqlglot/dialects/drill.py\nrename to sqlglot/dialects/drill2.py\n',
 'garbage': 'hello world\n',
 'empty': '',
}
for name, patch in cases.items():
    if not patch and name != 'empty':
        print(f'{name:18} (skipped: anchor not found)'); continue
    try:
        cand, reasons, _ = sel.analyze(patch)
    except Exception as e:
        print(f'{name:18} CRASH {e!r}'); continue
    if cand == S.EVERYTHING:
        print(f'{name:18} EVERYTHING {reasons[:2]}'); continue
    print(f'{name:18} {len(cand):5} tests cost {S.cost_of(cand, ms)/1000:7.2f}s {[r[:4] for r in reasons][:3]}')

more = {
 'new-method': edit('sqlglot/generators/clickhouse.py', '    def timestamptrunc_sql(self', '    def cot_sql(self, expression):\n        return "COT"\n\n    def timestamptrunc_sql(self'),
 'new-module-fn': edit('sqlglot/helper.py', 'def seq_get(', 'def brand_new_helper():\n    return 1\n\n\ndef seq_get('),
 'new-class-const': edit('sqlglot/dialects/tsql.py', '    LOG_BASE_FIRST = False', '    LOG_BASE_FIRST = False\n    SAFE_DIVISION = True'),
 'cache-fill': edit('sqlglot/generator.py', '        if expr_cls and expr_cls not in dispatch:', '        if expr_cls and expr_cls in dispatch:'),
 'table-value': edit('sqlglot/dialects/snowflake.py', '"u": (16, 4, 4, 0xFFFF)', '"u": (17, 4, 4, 0xFFFF)'),
 'table-cached': edit('sqlglot/generators/druid.py', 'f"ARRAY[{self.expressions(e)}]"', 'f"ARRAY({self.expressions(e)})"'),
 'del-method': edit('sqlglot/generators/drill.py', '    def escape_sql(self', '    def _old_escape_sql(self'),
 'test-class-attr': edit('tests/test_optimizer.py', 'class TestOptimizer(unittest.TestCase):\n    maxDiff = None', 'class TestOptimizer(unittest.TestCase):\n    maxDiff = 1'),
}
for name, patch in more.items():
    cand, reasons, _ = sel.analyze(patch)
    if cand == S.EVERYTHING:
        print(f'{name:18} EVERYTHING {reasons[:2]}'); continue
    print(f'{name:18} {len(cand):5} tests cost {S.cost_of(cand, ms)/1000:7.2f}s {[r[:5] for r in reasons][:3]}')
