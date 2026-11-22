"""Unit checks for diff handling, scheduling and sample-set safety. Run: python test_selector.py"""
import gzip
import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
import sys
sys.modules['verify_driver'] = None
import selector as S  # noqa: E402

ROOT = os.environ.get('BR_SQLGLOT', os.path.join(HERE, '.build/sqlglot'))


class DiffTests(unittest.TestCase):
    def test_replacement_is_not_an_insertion(self):
        old = 'a\nb\nc\n'
        diff = S.parse_diff('diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n')
        new, deleted, inserts, _, added = S.apply_hunks(old, diff[0].hunks)
        self.assertEqual(new, 'a\nB\nc\n')
        self.assertEqual(deleted, {2})
        self.assertEqual(inserts, {})
        self.assertEqual(added, {2})

    def test_pure_insertion(self):
        old = 'a\nb\n'
        diff = S.parse_diff('diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,3 @@\n a\n+# c\n b\n')
        new, deleted, inserts, _, added = S.apply_hunks(old, diff[0].hunks)
        self.assertEqual(new, 'a\n# c\nb\n')
        self.assertEqual(inserts, {1: [2]})

    def test_mismatch_raises(self):
        diff = S.parse_diff('diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-z\n+y\n')
        with self.assertRaises(ValueError):
            S.apply_hunks('a\n', diff[0].hunks)

    def test_new_and_deleted_files(self):
        d = S.parse_diff('diff --git a/n.py b/n.py\nnew file mode 100644\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1 @@\n+x = 1\n')
        self.assertTrue(d[0].new_file)
        d = S.parse_diff('diff --git a/n.py b/n.py\ndeleted file mode 100644\n')
        self.assertTrue(d[0].deleted)

    def test_garbage(self):
        with self.assertRaises(ValueError):
            S.parse_diff('not a diff')


class ScheduleTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(S.schedule([], {}), ([], 0))

    def test_picks_worker_count(self):
        ms = {0: 10000, 1: 10000, 2: 10000, 3: 10}
        workers, cost = S.schedule([0, 1, 2, 3], ms)
        self.assertEqual(cost, 10010 + 2000 * 3)
        self.assertEqual(sorted(n for w in workers for n in w), [0, 1, 2, 3])

    def test_tiny_set_one_worker(self):
        workers, cost = S.schedule([0, 1], {0: 5, 1: 7})
        self.assertEqual(len(workers), 1)
        self.assertEqual(cost, 2012)


@unittest.skipUnless(os.path.isdir(ROOT), 'pinned checkout not available')
class SampleSafetyTests(unittest.TestCase):
    """Static candidates must contain every failing sample test (the verifier only removes)."""

    def test_no_static_misses(self):
        tests = json.load(open(os.path.join(HERE, 'sample/tests.json')))['tests']
        index = json.load(gzip.open(os.path.join(HERE, 'index.json.gz'), 'rt'))
        answers = json.load(open(os.path.join(HERE, 'sample/answers.json')))['commits']
        sel = S.Selector(tests, index, ROOT)
        for commit, info in answers.items():
            text = open(os.path.join(HERE, 'sample/commits', commit + '.patch')).read()
            cand, _, _ = sel.analyze(text)
            if cand != S.EVERYTHING:
                self.assertTrue(set(info['failing']) <= cand, commit)


if __name__ == '__main__':
    unittest.main()
