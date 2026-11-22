"""Boundary checks for patch parsing and conservative selection."""
import difflib
import unittest
from selector import Selector, schedule

class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.source = 'def f():\n    return 1\n\ndef g():\n    return 2\n'
        self.tests = [{'number': 0, 'id': 'tests/test_x.py::test_f', 'file': 'tests/test_x.py', 'seconds': 1.0},
                      {'number': 1, 'id': 'tests/test_x.py::test_g', 'file': 'tests/test_x.py', 'seconds': 1.0}]
        self.selector = Selector(self.tests, {'files': {'sqlglot/x.py': {'source': self.source,
            'lines': {'2': [self.tests[0]['id']], '5': [self.tests[1]['id']]}}},
            'fixtures': {'tests/fixtures/x.sql': [self.tests[0]['id']]}})

    def patch(self, updated):
        return 'diff --git a/sqlglot/x.py b/sqlglot/x.py\n' + ''.join(difflib.unified_diff(
            self.source.splitlines(True), updated.splitlines(True), fromfile='a/sqlglot/x.py', tofile='b/sqlglot/x.py'))

    def test_body(self):
        self.assertEqual(self.selector.select(self.patch(self.source.replace('return 1', 'return 3'))), {0})

    def test_comment(self):
        self.assertEqual(self.selector.select(self.patch(self.source.replace('    return 1', '    # comment\n    return 1'))), set())

    def test_invalid_python(self):
        self.assertEqual(self.selector.select(self.patch(self.source.replace('return 1', 'return ('))), {0, 1})

    def test_mismatch(self):
        self.assertEqual(self.selector.select(self.patch(self.source.replace('return 1', 'return 3')).replace('-    return 1', '-    return 9')), {0, 1})

    def test_empty(self):
        self.assertEqual(self.selector.select(''), set())

    def test_unknown_format(self):
        self.assertEqual(self.selector.select('unexpected patch'), {0, 1})

    def test_fixture(self):
        self.assertEqual(self.selector.select('diff --git a/tests/fixtures/x.sql b/tests/fixtures/x.sql\n'), {0})

    def test_rename(self):
        self.assertEqual(self.selector.select('diff --git a/sqlglot/x.py b/sqlglot/y.py\nrename from sqlglot/x.py\nrename to sqlglot/y.py\n'), {0, 1})

    def test_schedule(self):
        self.assertEqual(schedule([]), [])
        workers = schedule(self.tests)
        self.assertEqual(len(workers), 1)
        self.assertEqual(set(workers[0]), {0, 1})

if __name__ == '__main__':
    unittest.main()
