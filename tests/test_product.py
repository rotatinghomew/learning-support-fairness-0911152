from collections import Counter
import json
from pathlib import Path
import unittest

from learning.domain import overview, validate


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.tables = json.loads(Path("data/releases.json").read_text(encoding="utf-8"))["term-a"]

    def test_audited_population_does_not_expand(self):
        result = overview(self.tables, {})["groups"]
        self.assertEqual(sum(row["people"] for row in result), len(self.tables["people"]))
        self.assertEqual(sum(row["audited"] for row in result), len(self.tables["audits"]))

    def test_unobserved_labels_are_not_negative(self):
        self.tables["audits"] = []
        validate(self.tables)
        self.assertEqual(sum(row["audited"] for row in overview(self.tables, {})["groups"]), 0)
        self.assertGreater(sum(row["people"] for row in overview(self.tables, {})["groups"]), 0)

    def test_zero_inclusion_probability_rejected(self):
        self.tables["audits"][0]["inclusion_probability"] = 0
        with self.assertRaises(ValueError):
            validate(self.tables)
