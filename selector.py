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
    deleted, inserts, added = set(), set(), set()
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
                    inserts.update(pending)
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
                pending.append(cursor)       # inserted after old line `cursor` (0 = top)
                out.append(text)
                added.add(len(out))
        if not run_deleted:
            inserts.update(pending)
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

    def references(self, names, home_path):
        """Coverage of every function in sqlglot/tests that references any of `names`."""
        out = set()
        found = False
        for rel, text in self.all_sources().items():
            if not any(n in text for n in names):
                continue
            try:
                st = self.structure(rel)
            except SyntaxError:
                continue
            for start, end, node, chain in st.nodes:
                if not isinstance(node, FUNCS + (ast.Lambda,)):
                    continue
                hit = False
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Name) and sub.id in names) or \
                            (isinstance(sub, ast.Attribute) and sub.attr in names):
                        hit = True
                        break
                if hit:
                    found = True
                    out |= self.line_cover(rel, range(start, end + 1))
            # names used in class bodies (e.g. TRANSFORMS tables) -> users of that class
            for start, end, node, chain in st.nodes:
                if isinstance(node, ast.ClassDef):
                    for stmt in node.body:
                        if isinstance(stmt, FUNCS + (ast.ClassDef,)):
                            continue
                        if any((isinstance(s, ast.Name) and s.id in names) or
                               (isinstance(s, ast.Attribute) and s.attr in names) for s in ast.walk(stmt)):
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
            # The module cannot be imported: every test file importing it errors at collection.
            reasons.append(('syntax-error', path))
            return EVERYTHING
        selected = set(self.opens_for(path))
        if ast.dump(old_tree) == ast.dump(new_tree):
            reasons.append(('ast-identical', path, len(selected)))
            return selected
        docstring_only = ast.dump(_strip_docstrings(old_tree)) == ast.dump(_strip_docstrings(ast.parse(new)))
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
        if docstring_only:
            reasons.append(('docstring-only', path))
        return selected

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
        refs, found = self.references(names, path) if names else (set(), False)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Import, ast.ImportFrom)) and found:
            reasons.append(('module-names', path, line, sorted(names)[:5], len(refs)))
            return refs
        cover = self.file_cover(path) | refs
        reasons.append(('module-stmt', path, line, type(node).__name__, len(cover)))
        return cover

    def attribute_readers(self, names):
        """Tests that execute a line reading attribute `names` at run time, or None when the
        attribute may be consumed in a way line coverage cannot see (import/class-creation time,
        getattr by string, bare-name use in a class body)."""
        out = set()
        for rel, text in self.all_sources().items():
            if not any(n in text for n in names):
                continue
            try:
                st = self.structure(rel)
            except SyntaxError:
                return None
            imported = set(self.import_lines.get(rel, ()))
            for node in ast.walk(st.tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in names:
                    return None
                hit = (isinstance(node, ast.Attribute) and node.attr in names) or \
                      (isinstance(node, ast.Name) and node.id in names and rel.startswith('sqlglot/'))
                if not hit:
                    continue
                span = range(node.lineno, node.end_lineno + 1)
                found = st.innermost(node.lineno)
                in_function = found is not None and any(
                    isinstance(c, FUNCS + (ast.Lambda,)) for c in found[3] + (found[2],))
                if isinstance(node, ast.Name):
                    # the defining assignment itself, or a bare use in a class body
                    if not in_function:
                        if isinstance(node.ctx, ast.Store):
                            continue
                        return None
                    continue          # a bare name inside a function is a local/global, not the attribute
                if not in_function or any(n in imported for n in span):
                    return None       # read while modules/classes are being built
                out |= self.line_cover(rel, span)
        return out

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
                new, deleted, inserts, old_to_new, added = apply_hunks(old, d.hunks)
            except ValueError as exc:
                reasons.append(('apply-failed', path, str(exc)))
                return EVERYTHING, reasons, {}
            sources[path] = new
            if new == old:
                continue
            if path.endswith('.py') and path.startswith(('sqlglot/', 'tests/')):
                got = self.analyze_python(path, old, new, deleted, inserts, old_to_new, added, reasons)
                if got is EVERYTHING:
                    return EVERYTHING, reasons, sources
                selected |= got
            else:
                hits = set(self.opens_for(path))
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
    out = []
    for commit, chosen in results:
        workers, _ = schedule(chosen, selector.ms)
        out.append(json.dumps({'commit': commit, 'workers': workers}, separators=(',', ':')))
    sys.stdout.write('\n'.join(out) + ('\n' if out else ''))
    sys.stdout.flush()
    log(f'cpu {time.process_time() - started:.3f}s')


def decide(selector, plans):
    """Hook for runtime verification; the static answer is the candidate set itself."""
    try:
        import verify_driver
    except ImportError:
        return [(c, s) for c, s, _, _ in plans]
    return verify_driver.decide(selector, plans)


if __name__ == '__main__':
    sys.path.insert(0, HERE)
    main()
