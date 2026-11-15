import copy
import json
from pathlib import Path
import unittest

from learning.codec import digest, encode
from learning.domain import ANALYSES, validate


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.tables = json.loads(Path("data/releases.json").read_text(encoding="utf-8"))["term-a"]

    def test_summary_does_not_mutate_evidence(self):
        before = digest(self.tables)
        validate(self.tables)
        result = ANALYSES["overview"](self.tables, {})
        self.assertTrue(result)
        encode(result)
        self.assertEqual(before, digest(self.tables))

    def test_missing_table(self):
        del self.tables['people']
        with self.assertRaises(ValueError):
            validate(self.tables)

    def test_duplicate_business_identity(self):
        self.tables['people'].append(copy.deepcopy(self.tables['people'][0]))
        with self.assertRaises(ValueError):
            validate(self.tables)

    def test_summary_independent_of_input_order(self):
        before = ANALYSES["overview"](self.tables, {})
        changed = {name: list(reversed(rows)) for name, rows in self.tables.items()}
        after = ANALYSES["overview"](changed, {})
        self.assertEqual(digest(before), digest(after))


if __name__ == "__main__":
    unittest.main()
