from collections import Counter, defaultdict
import math

import numpy as np

from learning.codec import numeric, require_fields, unique
from learning.fairness import support_fairness

PRODUCT = '数字助教支持分流'
KEYS = {'people': ['person'], 'audits': ['person'], 'capacities': ['site']}


def validate(tables):
    if set(tables) != set(KEYS):
        raise ValueError("资料表集合与产品契约不一致")
    for name, fields in KEYS.items():
        require_fields(tables[name], fields)
        unique(tables[name], fields)
    people = {row["person"] for row in tables["people"]}
    for row in tables["people"]:
        if not 0 <= numeric(row["score"], "困难评分") <= 1:
            raise ValueError("评分超界")
    for row in tables["audits"]:
        if row["person"] not in people or not isinstance(row["needs_support"], bool):
            raise ValueError("复核记录不合法")
        probability = row["inclusion_probability"]
        if probability is not None and not 0 < probability <= 1:
            raise ValueError("入样概率不合法")
    for row in tables["capacities"]:
        if row["slots"] < 0:
            raise ValueError("辅导名额为负")


def overview(tables, parameters):
    groups = defaultdict(list)
    audits = {row["person"]: row for row in tables["audits"]}
    for row in tables["people"]:
        groups[(row["age_band"], row["digital_access"])].append(row)
    return {"groups": [{"age_band": key[0], "digital_access": key[1], "people": len(rows),
                        "audited": sum(row["person"] in audits for row in rows),
                        "mean_score": float(np.mean(sorted(row["score"] for row in rows)))}
                       for key, rows in sorted(groups.items())], "capacity": sum(row["slots"] for row in tables["capacities"])}


ANALYSES = {"overview": overview, "support_fairness": support_fairness}
