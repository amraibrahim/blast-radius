"""Blast Radius selector.

For each patch against the pinned sqlglot commit:
  1. parse the unified diff and rebuild each changed file from the pinned checkout;
  2. locate every change structurally (AST of the old and new file) and turn it into candidate
     tests using maps built from the pinned commit's own test run: per-test line coverage,
     per-test file reads, per-test class instantiation, per-test directory listings;
  3. optionally run the cheap candidates against the patched tree (allowed and charged) and
     keep only the ones that really fail; expensive candidates are included directly;
  4. pack the selection onto 1..8 workers, choosing the worker count with the lowest cost.

stdout carries only the answer lines; diagnostics go to stderr.
"""
import ast
import gzip
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get('BR_DATA', '/data')
ROOT = os.environ.get('BR_SQLGLOT', '/opt/sqlglot')
INDEX = os.environ.get('BR_INDEX', os.path.join(HERE, 'index.json.gz'))
WORKER_MS = 2000
MAX_WORKERS = 8
EVERYTHING = 'everything'


def log(*args):
    if os.environ.get('BR_VERBOSE'):
        print(*args, file=sys.stderr)


# --------------------------------------------------------------------------- diff parsing

class FileDiff:
    def __init__(self, old_path, new_path):
        self.old_path = old_path
        self.new_path = new_path
        self.hunks = []          # (old_start, [(tag, text)])
        self.new_file = False
        self.deleted = False
        self.binary = False
        self.mode_change = False

    @property
    def path(self):
        return self.old_path if self.old_path != '/dev/null' else self.new_path


def _unquote(path):
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1].encode('latin-1', 'backslashreplace').decode('unicode_escape').encode('latin-1').decode('utf-8')
    return path


def _strip(path):
    path = _unquote(path.strip())
    if path == '/dev/null':
        return path
    return path[2:] if path[:2] in ('a/', 'b/') else path


def parse_diff(text):
    files = []
    current = None
    lines = text.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('diff --git '):
            m = re.match(r'diff --git ("(?:[^"\\]|\\.)*"|\S+) ("(?:[^"\\]|\\.)*"|\S+)$', line)
            if m:
                current = FileDiff(_strip(m[1]), _strip(m[2]))
            else:
                rest = line[len('diff --git '):]
                half = rest.split(' b/', 1)
                current = FileDiff(_strip(half[0]), _strip('b/' + half[1]) if len(half) > 1 else _strip(half[0]))
            files.append(current)
            i += 1
            continue
        if line.startswith('--- ') and i + 1 < len(lines) and lines[i + 1].startswith('+++ '):
            old = _strip(line[4:].split('\t')[0])
            new = _strip(lines[i + 1][4:].split('\t')[0])
            if current is None or current.hunks:
                current = FileDiff(old, new)
                files.append(current)
            else:
                current.old_path, current.new_path = old, new
            if old == '/dev/null':
                current.new_file = True
            if new == '/dev/null':
                current.deleted = True
            i += 2
            continue
        if current is not None and not current.hunks:
            if line.startswith('new file mode'):
                current.new_file = True
            elif line.startswith('deleted file mode'):
                current.deleted = True
            elif line.startswith(('rename from', 'rename to', 'copy from', 'copy to')):
                current.mode_change = True
            elif line.startswith(('old mode', 'new mode')):
                current.mode_change = True
            elif line.startswith('Binary files') or line.startswith('GIT binary patch'):
                current.binary = True
        m = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
        if m and current is not None:
            old_count = int(m[2]) if m[2] is not None else 1
            new_count = int(m[4]) if m[4] is not None else 1
            start = int(m[1])
            body = []
            i += 1
            while i < len(lines) and (old_count > 0 or new_count > 0):
                l = lines[i]
                if l.startswith('\\'):
                    i += 1
                    continue
                tag = l[:1] if l else ' '
                if tag == ' ':
                    old_count -= 1
                    new_count -= 1
                elif tag == '-':
                    old_count -= 1
                elif tag == '+':
                    new_count -= 1
                else:
                    raise ValueError('bad hunk line')
                body.append((tag, l[1:]))
                i += 1
            no_newline = []
            while i < len(lines) and lines[i].startswith('\\'):
                no_newline.append(body[-1][0] if body else ' ')
                i += 1
            current.hunks.append((start, body, no_newline))
            continue
        i += 1
    if text.strip() and not files:
        raise ValueError('unrecognized diff')
    return files


def apply_hunks(old_text, hunks):
    """Return (new_text, deleted_old_lines, insert_points, old_to_new, new_added_lines)."""
    old = old_text.split('\n')
    trailing_newline = old_text.endswith('\n')
    if trailing_newline:
        old = old[:-1]
    out = []
    cursor = 0
    deleted, added = set(), set()
    inserts = {}                 # old gap point -> new line numbers inserted there
    old_to_new = {}
    new_eof_newline = trailing_newline
    for start, body, no_newline in hunks:
        begin = max(start - 1, 0) if any(tag != '+' for tag, _ in body) else start
        if begin < cursor:
            raise ValueError('overlapping hunks')
        for k in range(cursor, begin):
            old_to_new[k + 1] = len(out) + 1
            out.append(old[k])
        cursor = begin
        run_deleted = False          # inside a run of changed lines that removed something
        pending = []                 # insertion points of the current run
        for tag, text in body:
            if tag == ' ':
                if not run_deleted:
                    for point, line in pending:
                        inserts.setdefault(point, []).append(line)
                pending, run_deleted = [], False
            elif tag == '-':
                run_deleted = True
            if tag in ' -':
                if cursor >= len(old) or old[cursor] != text:
                    raise ValueError('patch does not match the pinned source')
                if tag == '-':
                    deleted.add(cursor + 1)
                else:
                    old_to_new[cursor + 1] = len(out) + 1
                    out.append(text)
                cursor += 1
            else:
                out.append(text)
                added.add(len(out))
                pending.append((cursor, len(out)))   # inserted after old line `cursor` (0 = top)
        if not run_deleted:
            for point, line in pending:
                inserts.setdefault(point, []).append(line)
        if no_newline:
            last_tag = body[-1][0] if body else ' '
            if '+' in no_newline or (' ' in no_newline and last_tag == ' '):
                new_eof_newline = False
            elif '-' in no_newline:
                new_eof_newline = True
    for k in range(cursor, len(old)):
        old_to_new[k + 1] = len(out) + 1
        out.append(old[k])
    new_text = '\n'.join(out) + ('\n' if new_eof_newline and out else '')
    return new_text, deleted, inserts, old_to_new, added


# --------------------------------------------------------------------------- AST helpers

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPES = FUNCS + (ast.ClassDef, ast.Lambda)


def _strip_docstrings(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef) + FUNCS):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body[0].value.value = ''
    return tree


def header_span(node):
    """Lines that are executed when a statement is reached (compound statements: the header)."""
    first = node.lineno
    if isinstance(node, FUNCS + (ast.ClassDef,)):
        first = min([d.lineno for d in node.decorator_list] + [node.lineno])
    if isinstance(node, (ast.If, ast.While)):
        return first, node.test.end_lineno
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return first, node.iter.end_lineno
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return first, node.items[-1].context_expr.end_lineno
    if isinstance(node, ast.Try) or (hasattr(ast, 'TryStar') and isinstance(node, ast.TryStar)):
        return first, first
    if isinstance(node, ast.Match):
        return first, node.subject.end_lineno
    if isinstance(node, FUNCS + (ast.ClassDef,)):
        body_start = node.body[0].lineno if node.body else node.end_lineno
        return first, max(first, body_start - 1)
    return first, node.end_lineno


class Structure:
    """Parsed module with quick 'what encloses line N' queries."""

    def __init__(self, text):
        self.tree = ast.parse(text)
        self.nodes = []      # (start, end, node, chain)
        self._walk(self.tree, ())

    def _walk(self, node, chain):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.stmt, ast.Lambda, ast.excepthandler, ast.match_case)) or isinstance(child, ast.expr):
                if isinstance(child, ast.expr) and not isinstance(child, ast.Lambda):
                    self._walk(child, chain)
                    continue
                start = child.lineno
                if isinstance(child, FUNCS + (ast.ClassDef,)) and child.decorator_list:
                    start = min(d.lineno for d in child.decorator_list)
                self.nodes.append((start, child.end_lineno, child, chain))
                self._walk(child, chain + (child,))
            else:
                self._walk(child, chain)

    def innermost(self, line):
        """Innermost statement/lambda containing `line`, with its chain of enclosing nodes."""
        best = None
        for start, end, node, chain in self.nodes:
            if start <= line <= end and isinstance(node, (ast.stmt, ast.Lambda)):
                if best is None or (end - start, -len(chain)) < (best[1] - best[0], -len(best[3])) \
                        or len(chain) > len(best[3]) and start >= best[0] and end <= best[1]:
                    best = (start, end, node, chain)
        return best

    def statements_around(self, line):
        """Nearest statements before and after a gap (for inserted lines)."""
        before = after = None
        for start, end, node, chain in self.nodes:
            if not isinstance(node, ast.stmt):
                continue
            if end <= line and (before is None or end > before[1] or (end == before[1] and len(chain) > len(before[3]))):
                before = (start, end, node, chain)
            if start > line and (after is None or start < after[0] or (start == after[0] and len(chain) > len(after[3]))):
                after = (start, end, node, chain)
        return before, after


def qualname(chain, node=None):
    parts = [n.name for n in chain if isinstance(n, FUNCS + (ast.ClassDef,))]
    if node is not None and isinstance(node, FUNCS + (ast.ClassDef,)):
        parts.append(node.name)
    return '.'.join(parts)


SAFE_EXPR = (ast.Constant, ast.Tuple, ast.List, ast.Set, ast.Dict, ast.Name, ast.Attribute, ast.Lambda,
             ast.UnaryOp, ast.BinOp, ast.BoolOp, ast.Load, ast.arguments, ast.arg, ast.expr_context,
             ast.unaryop, ast.operator, ast.boolop)


def _side_effect_free(node):
    """An expression whose evaluation runs no user code: literals, containers, name/attribute
    lookups, arithmetic on those, and lambdas (whose bodies only run when called)."""
    for n in ast.walk(node):
        if isinstance(n, ast.Lambda):
            continue
        if not isinstance(n, SAFE_EXPR) and not _inside_lambda(node, n):
            return False
    return True


def _inside_lambda(root, target):
    for n in ast.walk(root):
        if isinstance(n, ast.Lambda) and any(m is target for m in ast.walk(n.body)):
            return True
    return False


CACHE_FACTORIES = {'dict', 'set', 'list', 'defaultdict', 'OrderedDict', 'WeakKeyDictionary',
                   'WeakValueDictionary'}
CACHE_STORES = {'setdefault', 'update', 'add', 'append', 'extend', '__setitem__'}
MEMO_DECORATORS = {'lru_cache', 'cache', 'cached_property'}


def cache_fill_spans(tree):
    """[(fill_start, fill_end, owner_start, owner_end, owner_name)] for process-wide memoization:
    code that only runs on a cache miss (the branch that stores into a module-level container,
    and same-module functions called from it, transitively). Per-test coverage charges that code
    to whichever test filled the cache first; every test running the owner depends on it."""
    caches = set()
    module_funcs = {}
    for node in tree.body:
        value = getattr(node, 'value', None)
        targets = node.targets if isinstance(node, ast.Assign) else \
            [node.target] if isinstance(node, ast.AnnAssign) else []
        if value is not None and (isinstance(value, (ast.Dict, ast.Set, ast.List)) or
                                  isinstance(value, ast.Call) and _call_name(value) in CACHE_FACTORIES):
            caches |= {t.id for t in targets if isinstance(t, ast.Name)}
        if isinstance(node, FUNCS):
            module_funcs[node.name] = node
    out = []
    if not caches:
        return out

    def stores(node):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Subscript) and isinstance(sub.ctx, ast.Store) and \
                    isinstance(sub.value, ast.Name) and sub.value.id in caches:
                return True
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and \
                    sub.func.attr in CACHE_STORES and isinstance(sub.func.value, ast.Name) and \
                    sub.func.value.id in caches:
                return True
        return False

    def owners(body):
        for node in ast.walk(ast.Module(body=body, type_ignores=[])):
            if isinstance(node, FUNCS):
                yield node

    for func in owners(tree.body):
        for node in ast.walk(func):
            if not isinstance(node, ast.If) or node is func:
                continue
            branch = next((b for b in (node.body, node.orelse) if b and any(stores(s) for s in b)), None)
            if branch is None or not any(stores(s) for s in branch) or stores(node.test):
                continue
            fills = [(branch[0].lineno, branch[-1].end_lineno)]
            seen, todo = set(), [s for s in branch]
            while todo:
                for sub in ast.walk(todo.pop()):
                    name = _call_name(sub) if isinstance(sub, ast.Call) else None
                    if name in module_funcs and name not in seen and module_funcs[name] is not func:
                        seen.add(name)
                        fn = module_funcs[name]
                        fills.append((min([fn.lineno] + [d.lineno for d in fn.decorator_list]), fn.end_lineno))
                        todo.extend(fn.body)
            for a, b in fills:
                out.append((a, b, func.lineno, func.end_lineno, func.name))
    return out


def _call_name(call):
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


TWINS = {(ast.Is, ast.Eq), (ast.Eq, ast.Is), (ast.IsNot, ast.NotEq), (ast.NotEq, ast.IsNot),
         (ast.Lt, ast.LtE), (ast.LtE, ast.Lt), (ast.Gt, ast.GtE), (ast.GtE, ast.Gt)}


def walk_scoped(tree):
    """Yield (node, runs_inside_a_function_call). Decorators, defaults and annotations are
    evaluated where the function is defined; only bodies run when it is called."""
    stack = [(tree, False)]
    while stack:
        node, inside = stack.pop()
        yield node, inside
        if isinstance(node, FUNCS + (ast.Lambda,)):
            outer = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
            if not isinstance(node, ast.Lambda):
                outer += list(node.decorator_list)
                outer += [a.annotation for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs
                          if a.annotation is not None]
                if node.returns is not None:
                    outer.append(node.returns)
                body = node.body
            else:
                body = [node.body]
            stack.extend((n, inside) for n in outer)
            stack.extend((n, True) for n in body)
        else:
            stack.extend((n, inside) for n in ast.iter_child_nodes(node))


def _stmt_text(lines, node):
    start = min([node.lineno] + [d.lineno for d in getattr(node, 'decorator_list', ())])
    return lines[start - 1:node.end_lineno]


def changed_top_level(old_tree, new_tree, old_text=None, new_text=None):
    """Pairs of top-level statements whose (location-free) dumps differ, or None when the
    module's top-level layout changed. Statements with identical source text parse identically,
    so only textually different ones are dumped."""
    if len(old_tree.body) != len(new_tree.body):
        return None
    old_lines = old_text.split('\n') if old_text is not None else None
    new_lines = new_text.split('\n') if new_text is not None else None
    out = []
    for o, n in zip(old_tree.body, new_tree.body):
        if old_lines is not None and new_lines is not None and _stmt_text(old_lines, o) == _stmt_text(new_lines, n):
            continue
        if ast.dump(o) != ast.dump(n):
            out.append((o, n))
    return out


def twin_swap_site(pairs):
    """(line, col) of the one single-operator comparison whose operator was swapped for its twin
    (is/==, is not/!=, </<=, >/>=) when that is the patch's only change to the module, else None.
    `pairs` is changed_top_level's result."""
    if not pairs or len(pairs) != 1:
        return None
    old_tree, new_tree = pairs[0]
    old_cmps = [n for n in ast.walk(old_tree) if isinstance(n, ast.Compare)]
    new_cmps = [n for n in ast.walk(new_tree) if isinstance(n, ast.Compare)]
    if len(old_cmps) != len(new_cmps):
        return None
    changed = [(o, n) for o, n in zip(old_cmps, new_cmps)
               if [type(x) for x in o.ops] != [type(x) for x in n.ops]]
    if len(changed) != 1:
        return None
    o, n = changed[0]
    if len(o.ops) != 1 or len(n.ops) != 1 or (type(o.ops[0]), type(n.ops[0])) not in TWINS:
        return None
    saved = n.ops
    n.ops = [type(o.ops[0])()]
    same = ast.dump(old_tree) == ast.dump(new_tree)
    n.ops = saved
    return (o.lineno, o.col_offset) if same else None


def copy_flag_only(pairs):
    """Whether the patch only changes `copy=False` to `copy=True` on exp.alias_."""
    if not pairs or len(pairs) != 1:
        return None
    old_tree, new_tree = pairs[0]
    old_calls = [n for n in ast.walk(old_tree) if isinstance(n, ast.Call)]
    new_calls = [n for n in ast.walk(new_tree) if isinstance(n, ast.Call)]
    if len(old_calls) != len(new_calls):
        return None

    changed = []
    for old_call, new_call in zip(old_calls, new_calls):
        if ast.dump(old_call) == ast.dump(new_call):
            continue
        old_copy = [k.value for k in old_call.keywords if k.arg == 'copy']
        new_copy = [k.value for k in new_call.keywords if k.arg == 'copy']
        if len(old_copy) != 1 or len(new_copy) != 1:
            return None
        if not (isinstance(old_copy[0], ast.Constant) and isinstance(new_copy[0], ast.Constant)):
            return None
        if (old_copy[0].value, new_copy[0].value) != (False, True):
            return None
        if not isinstance(old_call.func, ast.Attribute) or old_call.func.attr != 'alias_':
            return None

        saved = new_call.keywords
        new_call.keywords = [ast.keyword(arg=k.arg, value=ast.Constant(value=False)) if k.arg == 'copy'
                             else k for k in new_call.keywords]
        same = ast.dump(old_call) == ast.dump(new_call)
        new_call.keywords = saved
        if not same:
            return None
        changed.append((old_call.lineno, old_call.col_offset))
    return changed[0] if len(changed) == 1 else None


def import_bindings(tree):
    """{(bound name, source module, source name, level)} for module-level imports."""
    out = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(((a.asname or a.name).split('.')[0], a.name, '', 0))
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                out.add((a.asname or a.name, node.module or '', a.name, node.level))
    return out


def bound_names(node):
    names = set()
    targets = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    elif isinstance(node, FUNCS + (ast.ClassDef,)):
        return {node.name}
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        return {(a.asname or a.name).split('.')[0] for a in node.names}
    for t in targets:
        for sub in ast.walk(t):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                names.add(sub.attr)
    return names


def scan_dynamic_patterns(texts):
    """(prefix, suffix) of attribute names sqlglot reads by computed name: getattr/hasattr with
    an f-string or concatenation, or a dir() scan of a class filtered by startswith/endswith.
    ('', '') means any name."""
    found = set()
    for rel, text in texts.items():
        if not rel.startswith('sqlglot/') or ('getattr' not in text and 'dir(' not in text):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, FUNCS):
                continue
            modules = {t.id for a in ast.walk(fn) if isinstance(a, ast.Assign)
                       and isinstance(a.value, ast.Call) and 'import_module' in ast.unparse(a.value.func)
                       for t in a.targets if isinstance(t, ast.Name)}
            filters = set()
            for c in ast.walk(fn):
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and \
                        c.func.attr in ('startswith', 'endswith') and c.args and \
                        isinstance(c.args[0], ast.Constant) and isinstance(c.args[0].value, str):
                    filters.add((c.args[0].value, '') if c.func.attr == 'startswith' else ('', c.args[0].value))
            for c in ast.walk(fn):
                if not (isinstance(c, ast.Call) and isinstance(c.func, ast.Name)):
                    continue
                if c.func.id == 'dir' and c.args:
                    if isinstance(c.args[0], ast.Name) and c.args[0].id in modules:
                        continue          # module attributes, not class attributes
                    suffixes = {f for f in filters if f[1]}
                    found |= suffixes or filters or {('', '')}
                elif c.func.id in ('getattr', 'hasattr') and len(c.args) >= 2:
                    arg = c.args[1]
                    if isinstance(arg, ast.JoinedStr) and arg.values:
                        first, last = arg.values[0], arg.values[-1]
                        prefix = first.value if isinstance(first, ast.Constant) else ''
                        suffix = last.value if isinstance(last, ast.Constant) and last is not first else ''
                        if prefix or suffix:
                            found.add((prefix, suffix))
                    elif isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add):
                        prefix = arg.left.value if isinstance(arg.left, ast.Constant) else ''
                        suffix = arg.right.value if isinstance(arg.right, ast.Constant) else ''
                        if prefix or suffix:
                            found.add((prefix, suffix))
    return found


# --------------------------------------------------------------------------- selection

class Selector:
    def __init__(self, tests, index, root=ROOT):
        self.root = root
        self.index = index
        self.tests = tests
        self.by_number = {t['number']: t for t in tests}
        self.ms = {t['number']: round(t['seconds'] * 1000) for t in tests}
        number_of = {t['id']: t['number'] for t in tests}
        self.all = frozenset(self.ms)
        self.idx = [number_of.get(t) for t in index['tests']]
        self.raw_sets = index['sets']
        self._sets = {}
        self.files = index['files']
        self.import_lines = index.get('import_lines', {})
        self.opens = index.get('opens', {})
        self.listed = index.get('listed', {})
        self.classes = index.get('classes', {})
        self.by_file = {}
        for t in tests:
            self.by_file.setdefault(t['file'], set()).add(t['number'])
        self.by_class = {}
        for t in tests:
            self.by_class.setdefault(t['id'].rsplit('::', 1)[0], set()).add(t['number'])
        self._source = {}
        self._structure = {}
        self._file_cover = {}
        self._texts = None
        self._changed_imports = {}
        self._dynamic = None
        self.equivalent_sites = set(index.get('equivalent_sites', ()))
        self.tables = index.get('tables', {})
        self._new_trees = {}
        self._patched = {}

    # -- index access
    def tset(self, i):
        s = self._sets.get(i)
        if s is None:
            s = frozenset(n for n in (self.idx[x] for x in self.raw_sets[i]) if n is not None)
            self._sets[i] = s
        return s

    def line_cover(self, path, lines):
        table = self.files.get(path)
        out = set()
        if table:
            for n in lines:
                i = table.get(str(n))
                if i is not None:
                    out |= self.tset(i)
        return out

    def file_cover(self, path):
        if path not in self._file_cover:
            out = set()
            for i in set(self.files.get(path, {}).values()):
                out |= self.tset(i)
            self._file_cover[path] = frozenset(out)
        return self._file_cover[path]

    def source(self, path):
        if path not in self._source:
            try:
                with open(os.path.join(self.root, path), encoding='utf-8', newline='') as f:
                    self._source[path] = f.read()
            except (OSError, UnicodeDecodeError):
                self._source[path] = None
        return self._source[path]

    def structure(self, path, text=None):
        key = path if text is None else (path, hash(text))
        if key not in self._structure:
            self._structure[key] = Structure(self.source(path) if text is None else text)
        return self._structure[key]

    def all_sources(self):
        if self._texts is None:
            self._texts = {}
            for base in ('sqlglot', 'tests'):
                for dirpath, _, names in os.walk(os.path.join(self.root, base)):
                    for name in names:
                        if name.endswith('.py'):
                            full = os.path.join(dirpath, name)
                            rel = os.path.relpath(full, self.root)
                            try:
                                with open(full, encoding='utf-8') as f:
                                    self._texts[rel] = f.read()
                            except (OSError, UnicodeDecodeError):
                                pass
        return self._texts

    @staticmethod
    def module_name(path):
        parts = path[:-3].split('/')
        if parts[-1] == '__init__':
            parts = parts[:-1]
        return '.'.join(parts)

    def class_users(self, path, qual):
        """Tests that instantiated class `qual` of module `path` (or any subclass)."""
        key = f'{self.module_name(path)}:{qual}'
        i = self.classes.get(key)
        return None if i is None else self.tset(i)

    def test_class_users(self, path, qual):
        """For classes defined in test files: every test in that class or its subclasses."""
        name = qual.split('.')[0]
        out = set()
        for cls_id, nums in self.by_class.items():
            if cls_id == f'{path}::{name}':
                out |= nums
        # subclasses elsewhere (e.g. dialect test classes deriving from Validator)
        pattern = re.compile(r'class\s+\w+\s*\([^)]*\b' + re.escape(name) + r'\b')
        for rel, text in self.all_sources().items():
            if rel.startswith('tests/') and name in text:
                for m in pattern.finditer(text):
                    sub = re.match(r'class\s+(\w+)', m.group(0))[1]
                    out |= self.by_class.get(f'{rel}::{sub}', set())
        return out

    def importers_of(self, path):
        """Source files that can name module `path`'s globals (the module itself and importers)."""
        mod = self.module_name(path)
        parent, _, short = mod.rpartition('.')
        out = {path}
        for rel, text in self.all_sources().items():
            if mod in text or (parent and f'from {parent} import' in text and short in text):
                out.add(rel)
        return out

    def ref_forms(self, rel, home_path, names):
        """How module `rel` can name globals `names` of module `home_path`:
        (bare names bound to them, names the module object itself is bound to)."""
        if rel == home_path:
            return set(names), False
        tree = self.tree(rel)
        if tree is None:
            return set(), False
        mod = self.module_name(home_path)
        parent, _, short = mod.rpartition('.')
        package = self.module_name(rel).rpartition('.')[0] if not rel.endswith('__init__.py') \
            else self.module_name(rel)
        bare, aliases = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                source = node.module or ''
                if node.level:
                    base = package.split('.')
                    base = base[:len(base) - (node.level - 1)] if node.level > 1 else base
                    source = '.'.join(base + ([source] if source else []))
                for a in node.names:
                    if source == mod and (a.name in names or a.name == '*'):
                        bare.add(a.asname or a.name) if a.name != '*' else bare.update(names)
                    if source == parent and a.name == short:
                        aliases.add(a.asname or a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == mod:
                        aliases.add(a.asname or a.name)
        return bare, aliases

    def references(self, names, home_path, module_scope=False):
        """Coverage of every function that references any of `names`. For module globals
        (module_scope) only references that resolve to `home_path`'s binding count."""
        out = set()
        found = False
        for rel, text in self.all_sources().items():
            if not any(n in text for n in names):
                continue
            if module_scope:
                bare, attrs = self.ref_forms(rel, home_path, names)
                if not bare and not attrs:
                    continue
            else:
                bare, attrs = set(names), None
            try:
                st = self.structure(rel)
            except SyntaxError:
                continue

            def refers(node):
                # attrs: None = any `.name` access counts; else only `<module alias>.name`
                return any((isinstance(x, ast.Name) and x.id in bare) or
                           (isinstance(x, ast.Attribute) and x.attr in names and
                            (attrs is None or ast.unparse(x.value) in attrs))
                           for x in ast.walk(node))

            for start, end, node, chain in st.nodes:
                if isinstance(node, FUNCS + (ast.Lambda,)) and refers(node):
                    found = True
                    out |= self.line_cover(rel, range(start, end + 1))
            # names used in class bodies (e.g. TRANSFORMS tables) -> users of that class
            for start, end, node, chain in st.nodes:
                if isinstance(node, ast.ClassDef):
                    for stmt in node.body:
                        if isinstance(stmt, FUNCS + (ast.ClassDef,)):
                            continue
                        if refers(stmt):
                            users = self.class_users(rel, qualname(chain, node))
                            found = True
                            out |= users if users is not None else self.file_cover(rel)
                            break
        return out, found

    # -- per-file analysis
    def analyze_python(self, path, old, new, deleted, inserts, old_to_new, added, reasons):
        try:
            old_tree = ast.parse(old)
        except SyntaxError:
            reasons.append(('unparsable-baseline', path))
            return EVERYTHING
        try:
            new_tree = ast.parse(new)
        except SyntaxError:
            # The module cannot be imported: every test that imports it fails.
            reasons.append(('syntax-error', path))
            return self.import_dependents(path)
        selected = set(self.opens_for(path))
        pairs = changed_top_level(old_tree, new_tree, old, new)
        if pairs == []:
            reasons.append(('ast-identical', path, len(selected)))
            return selected
        self._new_trees[path] = new_tree
        site = twin_swap_site(pairs)
        if site is not None and f'{path}:{site[0]}:{site[1]}' in self.equivalent_sites:
            # The comparison and its twin agreed on every value the whole suite fed it, so every
            # test runs exactly as before.
            reasons.append(('equivalent-swap', path, site[0], len(selected)))
            return selected
        copy_site = copy_flag_only(pairs)
        if copy_site is not None:
            reasons.append(('equivalent-copy', path, copy_site[0], len(selected)))
            return selected
        self._changed_imports[path] = import_bindings(old_tree) ^ import_bindings(new_tree)
        self._changed_imports[path] = {b[0] for b in self._changed_imports[path]}
        st = self.structure(path)
        new_st = self.structure(path, new)
        lines = sorted(deleted)
        points = sorted(inserts)
        regions = []
        for n in lines:
            regions.append(('old', n))
        for p in points:
            regions.append(('gap', p))
        is_test = path.startswith('tests/')
        for kind, n in regions:
            if kind == 'old':
                found = st.innermost(n)
                candidates = [found] if found else []
            else:
                handled = self.inserted_definitions(path, new_st, inserts.get(n, ()), is_test, reasons)
                if handled is not None:
                    selected |= handled
                    continue
                before, after = st.statements_around(n)
                # an inserted line sits in the block of its nearest neighbours
                candidates = [x for x in (before, after) if x]
                # if the gap is inside a statement (e.g. a new dict entry), use that statement
                inner = st.innermost(n) if n >= 1 else None
                if inner and inner[0] <= n < inner[1]:
                    candidates.append(inner)
            if not candidates:
                reasons.append(('module-gap', path, n))
                cover = self.file_cover(path)
                selected |= cover
                continue
            for start, end, node, chain in candidates:
                selected |= self.region(path, st, start, end, node, chain, n, kind, is_test, reasons)
        if not is_test:
            fills = [f for f in cache_fill_spans(old_tree)
                     if any(f[0] <= n <= f[1] for kind, n in regions)]
            for a, b, oa, ob, owner in fills:
                got = self.line_cover(path, range(oa, ob + 1))
                reasons.append(('cache-fill', path, a, owner, len(got)))
                selected |= got
        return selected

    def inserted_definitions(self, path, new_st, new_lines, is_test, reasons):
        """Whole statements inserted into a class body or at module level bind new names (a new
        method can override what a base class did). Returns None when the insertion is inside a
        function body or inside an existing statement; the caller then uses its neighbours."""
        lines = set(new_lines)
        if not lines:
            return None
        tops = []
        for start, end, node, chain in new_st.nodes:
            if not isinstance(node, ast.stmt) or not all(k in lines for k in range(start, end + 1)):
                continue
            parent = chain[-1] if chain else None
            if parent is not None and isinstance(parent, ast.stmt) and \
                    all(k in lines for k in range(parent.lineno, parent.end_lineno + 1)):
                continue      # not maximal: its parent is inserted too
            tops.append((node, chain))
        if not tops:
            return None
        out = set()
        for node, chain in tops:
            scope = next((c for c in reversed(chain) if isinstance(c, SCOPES)), None)
            if isinstance(scope, FUNCS + (ast.Lambda,)):
                return None
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                reasons.append(('inserted-constant-expr', path))
                continue      # a bare string/docstring adds no binding
            names = bound_names(node)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = set(self._changed_imports.get(path, names))
            if isinstance(scope, ast.ClassDef):
                qual = qualname(chain[:chain.index(scope)], scope)
                users = self.test_class_users(path, qual) if is_test else self.class_users(path, qual)
                if users is None:
                    users = self.file_cover(path)
                readers = self.attribute_readers(names) if names and not is_test else None
                if readers is not None and not readers:
                    readers = None    # a new member nobody names can still be found by iteration
                got = set(users) & readers if readers is not None else set(users)
                reasons.append(('inserted-member', path, qual, sorted(names)[:3], len(got)))
                out |= got
            else:
                if is_test:
                    got = set(self.by_file.get(path, ())) | self.test_importers(path)
                else:
                    got, _ = self.references(names, path, module_scope=True) if names else (set(), False)
                reasons.append(('inserted-module', path, sorted(names)[:3], len(got)))
                out |= got
        return out

    def opens_for(self, path):
        i = self.opens.get(path)
        return self.tset(i) if i is not None else frozenset()

    def region(self, path, st, start, end, node, chain, line, kind, is_test, reasons):
        func_chain = [c for c in chain if isinstance(c, FUNCS + (ast.Lambda,))]
        inner_scope = next((c for c in reversed(chain) if isinstance(c, SCOPES)), None)
        in_body = isinstance(inner_scope, FUNCS + (ast.Lambda,))
        if in_body and not (isinstance(node, FUNCS) and header_span(node)[0] <= line <= header_span(node)[1]):
            # Executable code inside a function: the change runs only if this statement runs.
            span = header_span(node) if isinstance(node, ast.stmt) else (start, end)
            if kind == 'gap':
                span = (min(span[0], line), max(span[1], line + 1))
            lines = range(span[0], span[1] + 1)
            cover = self.line_cover(path, lines)
            if isinstance(node, ast.Return) or isinstance(node, (ast.Raise, ast.Continue, ast.Break)):
                pass
            reasons.append(('stmt', path, line, type(node).__name__, len(cover)))
            return cover
        if isinstance(node, FUNCS):
            # signature, defaults or decorators: every caller of the function
            cover = self.line_cover(path, range(start, node.end_lineno + 1))
            reasons.append(('def', path, line, node.name, len(cover)))
            if not chain or all(isinstance(c, ast.ClassDef) for c in chain):
                # decorators/defaults are evaluated at import, so an import crash is possible
                pass
            return cover
        if isinstance(node, ast.Lambda):
            cover = self.line_cover(path, range(start, end + 1))
            reasons.append(('lambda', path, line, len(cover)))
            return cover
        owner = next((c for c in reversed(chain) if isinstance(c, ast.ClassDef)), None)
        if isinstance(node, ast.ClassDef):
            owner_chain = chain
            owner = node
        else:
            owner_chain = chain[:chain.index(owner)] if owner is not None else ()
        if owner is not None:
            qual = qualname(owner_chain, owner)
            if not is_test and node is not owner and any(stmt is node for stmt in owner.body):
                got = self.table_entry_tests(path, qual, node, line, reasons)
                if got is not None:
                    return got
            if is_test:
                users = self.test_class_users(path, qual)
            else:
                users = self.class_users(path, qual)
                if users is not None and '.' in qual:
                    # a nested Tokenizer/Parser/Generator is also reached through its outer class
                    outer = self.class_users(path, qual.rsplit('.', 1)[0])
                    if outer is not None:
                        users = set(users) | outer
            if users is not None:
                # class attributes are read through instances of the class or its subclasses
                names = bound_names(node) if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) else set()
                if names and not is_test:
                    readers = self.attribute_readers(names)
                    if readers is not None:
                        narrowed = set(users) & readers
                        reasons.append(('class-attr', path, line, qual, sorted(names)[:3], len(users), len(narrowed)))
                        return narrowed
                reasons.append(('class-body', path, line, qual, len(users)))
                return set(users)
            names = bound_names(node) if node is not owner else set()
            names.add(owner.name)
            refs, found = self.references(names, path)
            users = self.file_cover(path) | refs
            reasons.append(('class-unseen', path, line, qual, len(users)))
            return users
        # module level
        names = bound_names(node)
        if is_test:
            cover = set(self.by_file.get(path, ()))
            cover |= self.test_importers(path)
            reasons.append(('test-module', path, line, len(cover)))
            return cover
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # only names whose binding actually changed matter; an import that now fails is
            # caught by the verifier's import probe
            names = set(self._changed_imports.get(path, names))
            if not names:
                reasons.append(('import-same-bindings', path, line))
                return set()
        refs, found = self.references(names, path, module_scope=True) if names else (set(), False)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # an import only binds names: what matters is who uses them (the import itself
            # failing is caught by the verifier's import probe)
            reasons.append(('module-import', path, line, sorted(names)[:5], len(refs)))
            return refs
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and found:
            reasons.append(('module-names', path, line, sorted(names)[:5], len(refs)))
            return refs
        cover = self.file_cover(path) | refs
        reasons.append(('module-stmt', path, line, type(node).__name__, len(cover)))
        return cover

    def table_entry_tests(self, path, qual, node, line, reasons):
        """Edits to entries of a class-level dict literal (a dialect's FUNCTIONS, NUMERIC_ESCAPES,
        ...): the tests that read or checked exactly those keys in the pinned suite, or None when
        the table profile cannot vouch for it (its values were read in bulk, it was copied or
        enumerated while tests ran, mutated, or a changed key was consulted while classes were
        being built)."""
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
        if target is None or not isinstance(node.value, ast.Dict):
            return None
        site = f'{path}:{qual}.{target}'
        profile = self.tables.get(site)
        new_node = self.new_class_stmt(path, qual, target)
        if profile is None or new_node is None or not isinstance(new_node.value, ast.Dict):
            return None
        old_d, new_d = node.value, new_node.value
        dump = ast.dump
        old_spreads = [dump(v) for k, v in zip(old_d.keys, old_d.values) if k is None]
        new_spreads = [dump(v) for k, v in zip(new_d.keys, new_d.values) if k is None]
        if old_spreads != new_spreads:
            return None
        old_entries = [(k, v) for k, v in zip(old_d.keys, old_d.values) if k is not None]
        new_entries = [(k, v) for k, v in zip(new_d.keys, new_d.values) if k is not None]
        order = profile.get('o')
        imported = set(profile.get('imp', ()))
        runtime = set(profile['a'])
        # whole-value reads or mutation anywhere; or key enumeration while tests run, which is how
        # a table gets copied into a lazily built cache (e.g. a per-class dispatch dict) whose
        # later users never touch the table again
        bulk = {'V', 'M'} & (imported | runtime) | {'I', 'K'} & runtime
        changed = set()
        edited = []
        same_keys = len(old_entries) == len(new_entries) and all(
            dump(a[0]) == dump(b[0]) for a, b in zip(old_entries, new_entries))
        if same_keys:
            # only values changed: map each edited entry to its runtime key by source position
            if order is None or len(order) != len(old_entries) or len(set(order)) != len(order):
                return None
            for j, (a, b) in enumerate(zip(old_entries, new_entries)):
                if dump(a[1]) != dump(b[1]):
                    changed.add(order[j])
                    edited += [a[1], b[1]]
            kinds = ('g',)
        else:
            # keys added, removed or renamed: only literal keys can be matched safely
            def literal(k):
                try:
                    value = ast.literal_eval(k)
                except ValueError:
                    return None
                return repr(value) if isinstance(value, (str, int, float, bool, bytes, tuple, type(None))) else None
            old_keys = [literal(k) for k, _ in old_entries]
            new_keys = [literal(k) for k, _ in new_entries]
            if None in old_keys or None in new_keys or len(set(old_keys)) != len(old_keys) \
                    or len(set(new_keys)) != len(new_keys):
                return None
            old_map = {k: dump(v) for k, (_, v) in zip(old_keys, old_entries)}
            new_map = {k: dump(v) for k, (_, v) in zip(new_keys, new_entries)}
            changed = set(old_map) ^ set(new_map)
            changed |= {k for k in set(old_map) & set(new_map) if old_map[k] != new_map[k]}
            edited += [v for k, (_, v) in zip(old_keys, old_entries) if k in changed]
            edited += [v for k, (_, v) in zip(new_keys, new_entries) if k in changed]
            bulk |= {'I', 'L', 'K'} & (imported | runtime)   # the key set itself was consumed
            kinds = ('g', 'h')
        if bulk or not changed:
            return None
        # every edited entry must be side-effect free to evaluate at class creation (no calls,
        # comprehensions, walrus, ...); then the only difference is what the entry's readers see
        if not all(_side_effect_free(v) for v in edited):
            return None
        if any(f'{kind}|{key}' in imported for key in changed for kind in kinds):
            return None
        out = set()
        for key in changed:
            for kind in kinds:
                i = profile['a'].get(f'{kind}|{key}')
                if i is not None:
                    out |= self.tset(i)
        reasons.append(('class-table', path, line, site, sorted(changed)[:3], len(out)))
        return out

    def new_class_stmt(self, path, qual, target):
        tree = self._new_trees.get(path)
        if tree is None:
            return None
        body = tree.body
        for name in qual.split('.'):
            found = [c for c in body if isinstance(c, ast.ClassDef) and c.name == name]
            if len(found) != 1:
                return None
            body = found[0].body
        hits = [stmt for stmt in body if
                (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
                 and stmt.targets[0].id == target) or
                (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == target)]
        return hits[0] if len(hits) == 1 else None

    def attribute_readers(self, names):
        """Tests that execute a line reading attribute `names` at run time, or None when the
        attribute may be consumed in a way line coverage cannot see (import/class-creation time,
        getattr by string, bare-name use in a class body)."""
        for prefix, suffix in self.dynamic_patterns():
            if any(n.startswith(prefix) and n.endswith(suffix) and len(n) > len(prefix) + len(suffix) - 1
                   for n in names):
                return None   # looked up by a computed name (e.g. generator `<key>_sql` dispatch)
        out = set()
        for rel, text in self.all_sources().items():
            if not any(n in text for n in names):
                continue
            tree = self.tree(rel)
            if tree is None:
                return None
            imported = set(self.import_lines.get(rel, ()))
            for node, in_function in walk_scoped(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in names:
                    return None
                if isinstance(node, ast.Name) and node.id in names and rel.startswith('sqlglot/'):
                    # the defining assignment itself, or a bare use while a class body runs
                    if not in_function and not isinstance(node.ctx, ast.Store):
                        return None
                    continue          # a bare name inside a function is a local/global, not the attribute
                if not (isinstance(node, ast.Attribute) and node.attr in names):
                    continue
                span = range(node.lineno, node.end_lineno + 1)
                if not in_function or any(n in imported for n in span):
                    return None       # read while modules/classes are being built
                out |= self.line_cover(rel, span)
        return out

    def tree(self, rel):
        trees = self.__dict__.setdefault('_trees', {})
        if rel not in trees:
            try:
                trees[rel] = ast.parse(self.all_sources()[rel])
            except (SyntaxError, KeyError):
                trees[rel] = None
        return trees[rel]

    def import_dependents(self, path):
        """Every test that would fail if module `path` could not be imported."""
        mod = self.module_name(path)
        core = self.index.get('core_modules')
        if path.startswith('tests/') or core is None or mod in core:
            if path.startswith('tests/') and path in self.by_file and not self.test_importers(path):
                return set(self.by_file[path])
            return EVERYTHING
        out = set(self.file_cover(path))
        for key, i in self.classes.items():
            if key.split(':', 1)[0] == mod:
                out |= self.tset(i)
        out |= self.opens_for(path)
        short = mod.rsplit('.', 1)[-1]
        for rel, text in self.all_sources().items():
            if rel.startswith('tests/') and (mod in text or f'import {short}' in text and mod.rsplit('.', 1)[0] in text):
                out |= self.by_file.get(rel, set())
        return out

    def dynamic_patterns(self):
        """Computed-name attribute lookups: precomputed for the pinned tree, plus any in the
        files this patch changes (a patch could add a new dispatch site)."""
        if self._dynamic is None:
            pinned = self.index.get('dynamic_patterns')
            if pinned is None:
                found = scan_dynamic_patterns(self.all_sources())
            else:
                found = {tuple(x) for x in pinned}
            self._dynamic = found
        extra = scan_dynamic_patterns(self._patched) if self._patched else set()
        return self._dynamic | extra

    def test_importers(self, path):
        mod = self.module_name(path)
        short = mod.split('.')[-1]
        out = set()
        for rel, text in self.all_sources().items():
            if rel.startswith('tests/') and rel != path and (mod in text or f'import {short}' in text):
                out |= self.by_file.get(rel, set())
        return out

    def analyze(self, text):
        """Returns (selected test numbers | EVERYTHING, reasons, patched sources)."""
        reasons = []
        self._patched = {}
        try:
            diffs = parse_diff(text)
        except (ValueError, IndexError) as exc:
            return EVERYTHING, [('bad-diff', str(exc))], {}
        selected = set()
        sources = {}
        for d in diffs:
            path = d.path
            if d.binary or d.mode_change and d.old_path != d.new_path:
                reasons.append(('rename-or-binary', path))
                return EVERYTHING, reasons, {}
            if d.new_file:
                if not d.new_path.endswith('.py') or True:
                    directory = os.path.dirname(d.new_path)
                    hits = set()
                    for listed, i in self.listed.items():
                        if listed == directory or listed.startswith(directory + '/') or directory.startswith(listed + '/'):
                            hits |= self.tset(i)
                    hits |= self.opens_for(d.new_path)
                    new_text = '\n'.join(t for _, body, _ in d.hunks for tag, t in body if tag == '+') + '\n'
                    sources[d.new_path] = new_text
                    reasons.append(('new-file', d.new_path, len(hits)))
                    if d.new_path.endswith('.py') and os.path.basename(d.new_path) in ('conftest.py', '__init__.py'):
                        return EVERYTHING, reasons, {}
                    selected |= hits
                continue
            old = self.source(path)
            if old is None:
                reasons.append(('unknown-file', path))
                if path.startswith(('sqlglot/', 'tests/')):
                    return EVERYTHING, reasons, {}
                continue
            if d.deleted:
                reasons.append(('deleted', path))
                return EVERYTHING, reasons, {}
            try:
                try:
                    new, deleted, inserts, old_to_new, added = apply_hunks(old, d.hunks)
                except ValueError:
                    if '\r' in old:
                        raise
                    # a patch saved with CRLF line endings against an LF file
                    hunks = [(st, [(tag, t[:-1] if t.endswith('\r') else t) for tag, t in body], nn)
                             for st, body, nn in d.hunks]
                    new, deleted, inserts, old_to_new, added = apply_hunks(old, hunks)
            except ValueError as exc:
                reasons.append(('apply-failed', path, str(exc)))
                return EVERYTHING, reasons, {}
            sources[path] = new
            if path.endswith('.py'):
                self._patched[path] = new
            if new == old:
                continue
            if path.endswith('.py') and path.startswith(('sqlglot/', 'tests/')):
                got = self.analyze_python(path, old, new, deleted, inserts, old_to_new, added, reasons)
                if got is EVERYTHING:
                    return EVERYTHING, reasons, sources
                selected |= got
            else:
                hits = set(self.opens_for(path))
                changed = [t for _, body, _ in d.hunks for tag, t in body if tag != ' ']
                if path.endswith('.sql') and changed and all(t.strip().startswith('--') or not t.strip() for t in changed):
                    reasons.append(('data-comment', path, len(hits)))   # loaders drop '--' lines
                else:
                    reasons.append(('data-file', path, len(hits)))
                selected |= hits
        return selected, reasons, sources


# --------------------------------------------------------------------------- scheduling

def lpt(items, k, ms):
    loads = [0] * k
    workers = [[] for _ in range(k)]
    for n in items:
        j = loads.index(min(loads))
        workers[j].append(n)
        loads[j] += ms[n]
    return workers, loads


def schedule(numbers, ms):
    numbers = sorted(numbers, key=lambda n: (-ms[n], n))
    if not numbers:
        return [], 0
    best = None
    for k in range(1, min(MAX_WORKERS, len(numbers)) + 1):
        workers, loads = lpt(numbers, k, ms)
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[1]:
            best = ([w for w in workers if w], cost)
        if max(loads) == ms[numbers[0]]:
            break          # the longest test already bounds the makespan; more workers only add cost
    return best


def cost_of(numbers, ms):
    return schedule(numbers, ms)[1]


# --------------------------------------------------------------------------- main

def load():
    with open(os.path.join(DATA, 'tests.json'), encoding='utf-8') as f:
        tests = json.load(f)['tests']
    with gzip.open(INDEX, 'rt', encoding='utf-8') as f:
        index = json.load(f)
    return tests, index


def main():
    started = time.process_time()
    tests, index = load()
    selector = Selector(tests, index)
    commits_dir = os.path.join(DATA, 'commits')
    names = sorted(n for n in os.listdir(commits_dir) if n.endswith('.patch'))
    plans = []
    for name in names:
        commit = name[:-len('.patch')]
        try:
            with open(os.path.join(commits_dir, name), encoding='utf-8', errors='surrogateescape') as f:
                text = f.read()
            selected, reasons, sources = selector.analyze(text)
        except Exception as exc:  # never let one odd patch sink the batch
            selected, reasons, sources = EVERYTHING, [('crash', repr(exc))], {}
        if selected is EVERYTHING:
            selected = set(selector.all)
        plans.append((commit, set(selected), reasons, sources))
        log(commit, len(selected), reasons)

    results = decide(selector, plans)
    # Experiments only (off by default): never list these test ids.
    drop = {t['number'] for t in selector.tests if t['id'] in set(filter(None, os.environ.get('BR_DROP', '').split(',')))}
    if os.environ.get('BR_DROP_MIN_S'):
        drop |= {t['number'] for t in selector.tests if t['seconds'] >= float(os.environ['BR_DROP_MIN_S'])}
    if drop:
        results = [(commit, set(chosen) - drop) for commit, chosen in results]
    out = []
    for commit, chosen in results:
        workers, _ = schedule(chosen, selector.ms)
        out.append(json.dumps({'commit': commit, 'workers': workers}, separators=(',', ':')))
    sys.stdout.write('\n'.join(out) + ('\n' if out else ''))
    sys.stdout.flush()
    log(f'cpu {time.process_time() - started:.3f}s')


def decide(selector, plans):
    """Hook for runtime verification; the static answer is the candidate set itself."""
    if os.environ.get('BR_NO_VERIFY'):
        return [(c, s) for c, s, _, _ in plans]
    try:
        import verify_driver
    except ImportError:
        return [(c, s) for c, s, _, _ in plans]
    return verify_driver.decide(selector, plans)


if __name__ == '__main__':
    sys.path.insert(0, HERE)
    main()
