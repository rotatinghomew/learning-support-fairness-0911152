"""支持分流公平性分析。

本模块只读取资料并给出可复核的建议，不修改原始评分，也不改变任何人的
申请资格。核心约定：

* 只有 ``selected_by=random`` 的复核可用于群体误差与有效帮助的估计。
  转介样本的入样概率未知且按评分选入，只作描述，不参与外推。
* 没有随机复核的群体，其误差记为不可识别（null），绝不把未复核者补成
  “不需要帮助”。选中学员的帮助率以部分识别区间表达。
* 规则选择、误差估计、最终验证分别使用互不重叠的入组批次。
* 候选规则必须遵守校区名额；名额按批次人数等比例缩放，以便在不同批次
  之间比较同一规则，部署建议仍使用真实名额。
"""

import math
from collections import Counter, defaultdict

from learning.codec import numeric

GROUP_FIELDS = ("age_band", "digital_access")
SPLIT_ROLES = ("selection", "estimation", "validation")
DEFAULT_SPLITS = {"selection": [0, 1], "estimation": [2], "validation": [3]}
DEFAULT_REQUIREMENTS = {
    "min_verified_help_rate": 0.40,
    "min_age_selection_ratio": 0.75,
    "min_access_selection_ratio": 0.75,
    "max_unreviewed_fraction": 0.40,
    "min_design_coverage": 0.30,
    "min_capacity_utilization": 1.0,
}
REQUIREMENT_BOUNDS = {
    "min_verified_help_rate": (0.0, 1.0),
    "min_age_selection_ratio": (0.0, 1.0),
    "min_access_selection_ratio": (0.0, 1.0),
    "max_unreviewed_fraction": (0.0, 1.0),
    "min_design_coverage": (0.0, 1.0),
    "min_capacity_utilization": (0.0, 1.0),
}
REQUIREMENT_DIRECTION = {
    "min_verified_help_rate": "max",
    "min_age_selection_ratio": "max",
    "min_access_selection_ratio": "max",
    "max_unreviewed_fraction": "min",
    "min_design_coverage": "max",
    "min_capacity_utilization": "max",
}
REQUIREMENT_MEASURE = {
    "min_verified_help_rate": "选中学员中设计随机复核可观测时的帮助率点估计（并另报区间）",
    "min_age_selection_ratio": "年龄组选择率的最小值/最大值",
    "min_access_selection_ratio": "上网条件组选择率的最小值/最大值",
    "max_unreviewed_fraction": "选中学员中没有任何人工复核的比例",
    "min_design_coverage": "选中学员中有入样概率已知随机复核的比例",
    "min_capacity_utilization": "已用名额/本批次缩放后名额",
}
ANALYSIS_VERSION = 1


def _f(value, digits=6):
    return None if value is None else round(float(value), digits)


def _ratio(numerator, denominator):
    return None if not denominator else numerator / denominator


def _wilson(successes, total, z=1.96):
    if not total:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [max(0.0, centre - half), min(1.0, centre + half)]


def _rate(successes, total):
    if not total:
        return None
    return {
        "count": successes,
        "denominator": total,
        "rate": _f(successes / total),
        "ci95": [_f(x) for x in _wilson(successes, total)],
    }


def _auc(labels, scores):
    """Mann-Whitney AUC（平局 0.5）与 Hanley–McNeil 近似区间。"""
    positive = [score for label, score in zip(labels, scores) if label]
    negative = [score for label, score in zip(labels, scores) if not label]
    if not positive or not negative:
        return None
    wins = 0.0
    for left in positive:
        for right in negative:
            wins += 1.0 if left > right else 0.5 if left == right else 0.0
    auc = wins / (len(positive) * len(negative))
    n1, n2 = len(positive), len(negative)
    q1 = auc / (2 - auc)
    q2 = 2 * auc * auc / (1 + auc)
    variance = (auc * (1 - auc) + (n1 - 1) * (q1 - auc * auc) + (n2 - 1) * (q2 - auc * auc)) / (n1 * n2)
    half = 1.96 * math.sqrt(max(0.0, variance))
    return {
        "auc": _f(auc),
        "ci95": [_f(max(0.0, auc - half)), _f(min(1.0, auc + half))],
        "positive": n1,
        "negative": n2,
    }


def _round_half_up(value):
    return int(math.floor(value + 0.5))


def _apportion(weights, total):
    """最大余数法整额分配；结果确定且与输入次序无关。"""
    keys = sorted(weights, key=str)
    if total <= 0:
        return {key: 0 for key in keys}
    if total >= sum(weights.values()):
        return {key: weights[key] for key in keys}
    denominator = sum(weights.values())
    if denominator <= 0:
        return {key: 0 for key in keys}
    raw = {key: weights[key] * total / denominator for key in keys}
    base = {key: int(math.floor(raw[key])) for key in keys}
    left = total - sum(base.values())
    order = sorted(keys, key=lambda key: (-(raw[key] - base[key]), str(key)))
    for key in order[:left]:
        base[key] += 1
    return base


def _cell(row):
    return (row["age_band"], row["digital_access"])


def _matches(row, specification):
    return all(row.get(field) == value for field, value in specification.items())


def _validate_requirements(parameters):
    merged = dict(DEFAULT_REQUIREMENTS)
    provided = parameters.get("requirements", {})
    if not isinstance(provided, dict):
        raise ValueError("requirements 不是对象")
    unknown = set(provided) - set(DEFAULT_REQUIREMENTS)
    if unknown:
        raise ValueError(f"未知公平要求：{sorted(unknown)}")
    for name, value in provided.items():
        number = numeric(value, name)
        low, high = REQUIREMENT_BOUNDS[name]
        if not low <= number <= high:
            raise ValueError(f"公平要求 {name} 超出范围")
        merged[name] = number
    return merged


def _validate_rule(rule):
    if not isinstance(rule, dict):
        raise ValueError("候选规则不是对象")
    allowed = {"name", "label", "family", "by", "by_field", "fraction", "threshold"}
    unknown = set(rule) - allowed
    if unknown:
        raise ValueError(f"候选规则含未知字段：{sorted(unknown)}")
    name = rule.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("候选规则缺少名称")
    family = rule.get("family")
    if family not in {"score", "threshold", "reserve", "priority", "quota"}:
        raise ValueError(f"候选规则族未知：{family}")
    if family in {"reserve", "priority"}:
        by = rule.get("by")
        if not isinstance(by, dict) or not by or set(by) - set(GROUP_FIELDS):
            raise ValueError(f"候选规则 {name} 的分组条件不合法")
        if not all(isinstance(value, str) and value for value in by.values()):
            raise ValueError(f"候选规则 {name} 的分组值不合法")
    if family == "reserve":
        fraction = numeric(rule.get("fraction"), f"{name} fraction")
        if not 0 <= fraction <= 1:
            raise ValueError(f"候选规则 {name} 的预留比例超出范围")
    if family == "threshold":
        threshold = numeric(rule.get("threshold"), f"{name} threshold")
        if not 0 <= threshold <= 1:
            raise ValueError(f"候选规则 {name} 的阈值超出范围")
    if family == "quota":
        if rule.get("by_field") not in set(GROUP_FIELDS) | {"cell"}:
            raise ValueError(f"候选规则 {name} 的配额字段不合法")
    return name


def _default_rules():
    rules = [
        {"name": "score_only", "label": "仅按现有困难评分从高到低排序", "family": "score"},
        {"name": "threshold_0.50", "label": "现有评分达到 0.50 才建议", "family": "threshold", "threshold": 0.50},
        {"name": "threshold_0.70", "label": "现有评分达到 0.70 才建议", "family": "threshold", "threshold": 0.70},
    ]
    for fraction in (0.1, 0.2, 0.3, 0.5):
        rules.append({
            "name": f"reserve_age_75plus_{fraction:.1f}",
            "label": f"每校区为 75+ 预留 {fraction:.0%} 名额，其余按评分",
            "family": "reserve", "by": {"age_band": "75+"}, "fraction": fraction,
        })
    for fraction in (0.2, 0.3, 0.5, 0.7):
        rules.append({
            "name": f"reserve_access_low_{fraction:.1f}",
            "label": f"每校区为低上网条件预留 {fraction:.0%} 名额，其余按评分",
            "family": "reserve", "by": {"digital_access": "low"}, "fraction": fraction,
        })
    for fraction in (0.1, 0.2, 0.3):
        rules.append({
            "name": f"reserve_cell_75plus_low_{fraction:.1f}",
            "label": f"每校区为 75+ 且低上网条件预留 {fraction:.0%} 名额，其余按评分",
            "family": "reserve", "by": {"age_band": "75+", "digital_access": "low"}, "fraction": fraction,
        })
    rules.extend([
        {"name": "quota_age", "label": "按年龄组人数比例分配每校区名额，组内按评分", "family": "quota", "by_field": "age_band"},
        {"name": "quota_access", "label": "按上网条件人数比例分配每校区名额，组内按评分", "family": "quota", "by_field": "digital_access"},
        {"name": "quota_cell", "label": "按年龄×上网条件人数比例分配名额，组内按评分", "family": "quota", "by_field": "cell"},
        {"name": "priority_age_75plus", "label": "75+ 优先，名额未满再按评分补足", "family": "priority", "by": {"age_band": "75+"}},
        {"name": "priority_access_low", "label": "低上网条件优先，名额未满再按评分补足", "family": "priority", "by": {"digital_access": "low"}},
    ])
    return rules


def _normalize_rule(rule):
    normalized = {"name": rule["name"], "label": rule.get("label") or rule["name"], "family": rule["family"]}
    if rule["family"] in {"reserve", "priority"}:
        normalized["by"] = dict(rule["by"])
    if rule["family"] == "reserve":
        normalized["fraction"] = float(rule["fraction"])
    if rule["family"] == "threshold":
        normalized["threshold"] = float(rule["threshold"])
    if rule["family"] == "quota":
        normalized["by_field"] = rule["by_field"]
    return normalized


def options(parameters):
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        raise ValueError("分析参数不是对象")
    unknown = set(parameters) - {
        "splits", "requirements", "score_threshold", "min_group_size",
        "min_random_audits", "min_design_audits", "rules",
    }
    if unknown:
        raise ValueError(f"未知分析参数：{sorted(unknown)}")
    raw_splits = parameters.get("splits", DEFAULT_SPLITS)
    if not isinstance(raw_splits, dict) or set(raw_splits) != set(SPLIT_ROLES):
        raise ValueError("splits 必须且只能包含 selection、estimation、validation")
    splits = {}
    assigned = set()
    for role in SPLIT_ROLES:
        value = raw_splits[role]
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError(f"{role} 批次不能为空")
        cohorts = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError(f"{role} 批次编号必须是整数")
            cohorts.append(item)
        if len(set(cohorts)) != len(cohorts):
            raise ValueError(f"{role} 批次编号重复")
        overlap = assigned & set(cohorts)
        if overlap:
            raise ValueError(f"批次不能同时用于多个角色：{sorted(overlap)}")
        assigned |= set(cohorts)
        splits[role] = sorted(cohorts)
    threshold = numeric(parameters.get("score_threshold", 0.5), "score_threshold")
    if not 0 <= threshold <= 1:
        raise ValueError("score_threshold 超出范围")
    min_group_size = parameters.get("min_group_size", 20)
    min_random_audits = parameters.get("min_random_audits", 10)
    min_design_audits = parameters.get("min_design_audits", 5)
    for name, value in (
        ("min_group_size", min_group_size),
        ("min_random_audits", min_random_audits),
        ("min_design_audits", min_design_audits),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} 必须是正整数")
    raw_rules = parameters.get("rules")
    if raw_rules is None:
        rules = _default_rules()
    else:
        if not isinstance(raw_rules, (list, tuple)) or not raw_rules:
            raise ValueError("rules 必须是非空数组")
        names = [_validate_rule(rule) for rule in raw_rules]
        if len(set(names)) != len(names):
            raise ValueError("候选规则名称重复")
        rules = [_normalize_rule(rule) for rule in raw_rules]
    return {
        "splits": splits,
        "requirements": _validate_requirements(parameters),
        "score_threshold": threshold,
        "min_group_size": min_group_size,
        "min_random_audits": min_random_audits,
        "min_design_audits": min_design_audits,
        "rules": rules,
    }


def _prepare(tables):
    """建立按校区、批次索引；不修改输入。"""
    required = {
        "people": {"person", "site", "age_band", "digital_access", "score", "cohort"},
        "audits": {"person", "needs_support", "selected_by", "inclusion_probability"},
        "capacities": {"site", "slots"},
    }
    for name, fields in required.items():
        for row in tables[name]:
            missing = fields - row.keys()
            if missing:
                raise ValueError(f"{name} 缺少字段：{sorted(missing)}")
    people = sorted(tables["people"], key=lambda row: row["person"])
    for row in people:
        cohort = row["cohort"]
        if isinstance(cohort, bool) or not isinstance(cohort, int):
            raise ValueError("入组批次必须是整数")
    sites = sorted({row["site"] for row in people})
    capacity = {}
    for row in tables["capacities"]:
        slots = row["slots"]
        if isinstance(slots, bool) or not isinstance(slots, int) or slots < 0:
            raise ValueError("辅导名额必须是非负整数")
        capacity[row["site"]] = slots
    missing_sites = [site for site in sites if site not in capacity]
    if missing_sites:
        raise ValueError(f"缺少校区名额：{missing_sites}")
    audits = {}
    for row in tables["audits"]:
        if row["selected_by"] not in {"random", "referral"}:
            raise ValueError("复核来源必须是 random 或 referral")
        if not isinstance(row["needs_support"], bool):
            raise ValueError("复核结果必须是布尔值")
        audits[row["person"]] = row
    return {
        "tables": tables,
        "people": people,
        "audits": audits,
        "capacity": {site: capacity[site] for site in sites},
        "sites": sites,
        "people_by_site": {site: [row for row in people if row["site"] == site] for site in sites},
        "people_by_person": {row["person"]: row for row in people},
        "cohorts": sorted({row["cohort"] for row in people}),
    }


def _scaled_capacity(context, cohorts):
    """把真实校区名额按评估批次人数等比例缩放，再逐校区整额分配。

    缩放只用于让不同批次、不同角色下的同一规则可比；部署建议使用真实名额。
    单校区人数不足时，把余量按原名额权重转给仍有空位的校区。
    """
    population = [row for row in context["people"] if row["cohort"] in set(cohorts)]
    total_people = len(context["people"])
    if not total_people or not population:
        return {site: 0 for site in context["sites"]}, population
    room = Counter(row["site"] for row in population)
    original = context["capacity"]
    target = _round_half_up(sum(original.values()) * len(population) / total_people)
    target = min(target, len(population))
    scaled = _apportion(original, target)
    for site in context["sites"]:
        scaled[site] = min(scaled.get(site, 0), room[site])
    overflow = target - sum(scaled.values())
    while overflow > 0:
        receivers = {site: original[site] for site in context["sites"] if scaled[site] < room[site]}
        if not receivers:
            break
        extra = _apportion(receivers, overflow)
        moved = 0
        for site in receivers:
            add = min(extra[site], room[site] - scaled[site])
            scaled[site] += add
            moved += add
        if not moved:
            break
        overflow -= moved
    return {site: scaled[site] for site in context["sites"]}, population


def _ordered(rows):
    return sorted(rows, key=lambda row: (-numeric(row["score"], "困难评分"), row["person"]))


def _group_key(row, field):
    if field == "cell":
        return (row["age_band"], row["digital_access"])
    return row[field]


def _select_site(rows, slots, rule, threshold):
    """在单校区内执行一条规则；结果按评分降序、人员编号升序确定。"""
    family = rule["family"]
    ranked = _ordered(rows)
    if family == "score":
        return ranked[:slots]
    if family == "threshold":
        limit = numeric(rule.get("threshold", threshold), "评分阈值")
        return [row for row in ranked if numeric(row["score"], "困难评分") >= limit][:slots]
    if family == "reserve":
        fraction = numeric(rule["fraction"], "预留比例")
        protected = [row for row in ranked if _matches(row, rule["by"])]
        reserved = min(len(protected), _round_half_up(fraction * slots))
        chosen = protected[:reserved]
        used = {row["person"] for row in chosen}
        chosen += [row for row in ranked if row["person"] not in used][:slots - len(chosen)]
        return chosen
    if family == "priority":
        protected = [row for row in ranked if _matches(row, rule["by"])]
        chosen = protected[:slots]
        used = {row["person"] for row in chosen}
        chosen += [row for row in ranked if row["person"] not in used][:slots - len(chosen)]
        return chosen
    if family == "quota":
        field = rule["by_field"]
        groups = Counter(_group_key(row, field) for row in ranked)
        allocation = _apportion(dict(groups), slots)
        chosen = []
        for key in sorted(allocation, key=str):
            pool = [row for row in ranked if _group_key(row, field) == key]
            chosen += pool[:allocation[key]]
        return chosen
    raise ValueError(f"未知规则族：{family}")


def _run_rule(rule, context, cohorts, capacity=None, threshold=0.5):
    population = [row for row in context["people"] if row["cohort"] in set(cohorts)]
    if capacity is None:
        capacity, population = _scaled_capacity(context, cohorts)
    population_ids = {row["person"] for row in population}
    chosen = []
    per_site = {}
    for site in context["sites"]:
        rows = [row for row in context["people_by_site"][site] if row["person"] in population_ids]
        selected = _select_site(rows, max(0, capacity.get(site, 0)), rule, threshold)
        per_site[site] = selected
        chosen += selected
    return chosen, per_site, population, capacity


def _design_check(context, cohorts):
    """核对登记的随机入样概率与实际抽样框覆盖是否一致。

    契约规定 inclusion_probability 是“对全体该 cohort 的随机抽查概率”。
    若某些群体在随机复核中完全缺席，说明实际抽样框只覆盖了部分群体，
    此时不能把该概率直接当成全群体代表性的依据。这里只报告事实，不补值。
    """
    population = [row for row in context["people"] if row["cohort"] in set(cohorts)]
    people_by_cohort = defaultdict(list)
    for row in population:
        people_by_cohort[row["cohort"]].append(row)
    checks = []
    for cohort in sorted(people_by_cohort):
        rows = people_by_cohort[cohort]
        probabilities = sorted({
            context["audits"][row["person"]]["inclusion_probability"]
            for row in rows
            if context["audits"].get(row["person"], {}).get("selected_by") == "random"
            and context["audits"][row["person"]]["inclusion_probability"] is not None
        })
        cells = defaultdict(lambda: {"people": 0, "random": 0})
        for row in rows:
            bucket = cells[(row["age_band"], row["digital_access"])]
            bucket["people"] += 1
            if context["audits"].get(row["person"], {}).get("selected_by") == "random":
                bucket["random"] += 1
        sampled = [key for key, bucket in cells.items() if bucket["random"]]
        unsampled = [key for key, bucket in cells.items() if not bucket["random"]]
        observed_rate = sum(bucket["random"] for bucket in cells.values()) / len(rows) if rows else None
        registered = probabilities[0] if len(probabilities) == 1 else None
        issues = []
        if not sampled:
            issues.append("该批次没有任何入样概率已知的随机复核，设计推断不可进行；未复核者不视为阴性。")
        if unsampled and sampled:
            issues.append(
                "随机复核只出现在部分年龄×上网条件群体，缺席群体的随机入样概率实际为 0；"
                "登记概率不能用于这些群体的代表性和权重。"
            )
        for key in sampled:
            bucket = cells[key]
            if registered is not None and bucket["random"] == bucket["people"] and bucket["people"] > 3 and registered < 0.99:
                issues.append(
                    f"群体 {key[0]}/{key[1]} 的随机复核覆盖率为 100%（{bucket['random']}/{bucket['people']}），"
                    f"高于登记概率 {registered:.3f}，说明该概率不是该群体实际的独立抽样概率。"
                )
        checks.append({
            "cohort": cohort,
            "people": len(rows),
            "random_audits": sum(bucket["random"] for bucket in cells.values()),
            "registered_probabilities": probabilities,
            "observed_random_coverage": _f(observed_rate),
            "cells_sampled": [f"{key[0]}/{key[1]}" for key in sorted(sampled)],
            "cells_unsampled": [f"{key[0]}/{key[1]}" for key in sorted(unsampled)],
            "assumption_holds": not issues,
            "issues": issues,
        })
    return {
        "cohorts": checks,
        "assumption_holds": all(check["assumption_holds"] for check in checks),
        "note": (
            "设计一致性检查只核对登记概率与实测覆盖；不一致时，误差与帮助率只能按群体分别报告，"
            "不能把整个 cohort 的登记概率当成跨群体代表性的依据。"
        ),
    }


def _sampling_diagnostic(context, cohorts):
    population = [row for row in context["people"] if row["cohort"] in set(cohorts)]
    ids = {row["person"] for row in population}
    random_rows = [row for row in context["audits"].values() if row["selected_by"] == "random" and row["person"] in ids]
    referral_rows = [row for row in context["audits"].values() if row["selected_by"] == "referral" and row["person"] in ids]
    cells = defaultdict(lambda: {"people": 0, "random": 0, "referral": 0, "unreviewed": 0})
    for row in population:
        bucket = cells[_cell(row)]
        bucket["people"] += 1
        audit = context["audits"].get(row["person"])
        if audit is None:
            bucket["unreviewed"] += 1
        elif audit["selected_by"] == "random":
            bucket["random"] += 1
        else:
            bucket["referral"] += 1
    probabilities = Counter(
        audit["inclusion_probability"] for audit in random_rows if audit["inclusion_probability"] is not None
    )
    unreviewed = [row for row in population if row["person"] not in context["audits"]]
    return {
        "cohorts": sorted(set(cohorts)),
        "people": len(population),
        "random_audits": len(random_rows),
        "referral_audits": len(referral_rows),
        "unreviewed": len(unreviewed),
        "cells": [
            {"age_band": key[0], "digital_access": key[1], **bucket}
            for key, bucket in sorted(cells.items())
        ],
        "random_inclusion_probabilities": [
            {"probability": probability, "audits": count}
            for probability, count in sorted(probabilities.items(), key=lambda item: item[0])
        ],
        "referral_inclusion_probability": "unknown",
        "notes": [
            "只有 selected_by=random 的复核可用于估计群体帮助率与评分误差。",
            "转介样本按评分选入且入样概率未登记，不能与随机样本合并，也不能当随机样本使用。",
            "没有复核记录的人不等于不需要帮助；未复核者不在任何误差分母中。",
        ] if unreviewed or referral_rows else [],
    }


def _weighted_rate(observations):
    """设计加权比例与比值估计的标准误；权重来自已知入样概率。"""
    total = sum(weight for _, weight in observations)
    if not total:
        return None
    successes = sum(weight for label, weight in observations if label)
    rate = successes / total
    variance = sum((weight * (label - rate)) ** 2 for label, weight in observations) / (total * total)
    half = 1.96 * math.sqrt(variance)
    square_weight = sum(weight * weight for _, weight in observations)
    effective = total * total / square_weight
    return {
        "rate": _f(rate),
        "ci95": [_f(max(0.0, rate - half)), _f(min(1.0, rate + half))],
        "weighted_denominator": _f(total),
        "effective_n": _f(effective, 3),
        "design_effect": _f(len(observations) / effective, 3) if effective else None,
    }


def _weighted_fraction(numerator_weight, denominator_weight):
    return None if not denominator_weight else _f(numerator_weight / denominator_weight)


def _design_rows(rows, context, min_random_audits):
    """只取入样概率已知的随机复核；转介记录明确排除。"""
    observations = []
    excluded_referral = 0
    excluded_missing = 0
    for row in rows:
        audit = context["audits"].get(row["person"])
        if audit is None:
            continue
        if audit["selected_by"] != "random":
            excluded_referral += 1
            continue
        probability = audit["inclusion_probability"]
        if probability is None:
            excluded_missing += 1
            continue
        observations.append((audit["needs_support"], 1.0 / numeric(probability, "入样概率"), row))
    usable = len(observations) >= min_random_audits
    return observations, excluded_referral, excluded_missing, usable


def _score_error(rows, context, threshold, min_random_audits):
    """在随机抽查覆盖范围内评估现有评分的判别误差。

    只使用入样概率已知的随机复核。转介样本与无概率记录不进入分母，
    只在结果中计数说明，避免把高分转介误当成随机样本。
    """
    observations, referral, missing, usable = _design_rows(rows, context, min_random_audits)
    if not observations:
        return {
            "estimable": False,
            "reason": "该群体没有入样概率已知的随机复核，无法估计评分误差",
            "referral_audits_excluded": referral,
            "random_audits_without_probability": missing,
        }
    labels = [label for label, _, _ in observations]
    weights = [weight for _, weight, _ in observations]
    scores = [numeric(row["score"], "困难评分") for _, _, row in observations]
    result = {
        "estimable": True,
        "random_audits": len(observations),
        "referral_audits_excluded": referral,
        "random_audits_without_probability": missing,
        "design_weighted_need_rate": _weighted_rate(list(zip(labels, weights))),
        "auc": _auc(labels, scores),
        "threshold": threshold,
        "unweighted_need_rate": _rate(sum(labels), len(labels)),
    }
    positive_weight = sum(weight for label, weight in zip(labels, weights) if label)
    negative_weight = sum(weight for label, weight in zip(labels, weights) if not label)
    true_positive = sum(weight for label, weight, flag in zip(labels, weights, [score >= threshold for score in scores]) if label and flag)
    false_negative = sum(weight for label, weight, flag in zip(labels, weights, [score >= threshold for score in scores]) if label and not flag)
    false_positive = sum(weight for label, weight, flag in zip(labels, weights, [score >= threshold for score in scores]) if not label and flag)
    true_negative = sum(weight for label, weight, flag in zip(labels, weights, [score >= threshold for score in scores]) if not label and not flag)
    result["confusion"] = {
        "true_positive_weight": _f(true_positive),
        "false_negative_weight": _f(false_negative),
        "false_positive_weight": _f(false_positive),
        "true_negative_weight": _f(true_negative),
        "false_negative_rate": _weighted_fraction(false_negative, positive_weight),
        "false_positive_rate": _weighted_fraction(false_positive, negative_weight),
        "positive_predictive_value": _weighted_fraction(true_positive, true_positive + false_positive),
        "sensitivity": _weighted_fraction(true_positive, positive_weight),
        "specificity": _weighted_fraction(true_negative, negative_weight),
    }
    result["usable_for_inference"] = usable
    if not usable:
        result["reason"] = f"随机复核少于 {min_random_audits} 人，误差仅作描述，不足以外推"
    return result


def _group_rows(context, cohorts):
    population = [row for row in context["people"] if row["cohort"] in set(cohorts)]
    groups = defaultdict(list)
    for row in population:
        groups[("age_band", row["age_band"])].append(row)
        groups[("digital_access", row["digital_access"])].append(row)
        groups[("cell",) + _cell(row)].append(row)
    return population, groups


def _group_descriptor(name, rows):
    if name[0] == "cell":
        return {"dimension": "cell", "age_band": name[1], "digital_access": name[2]}
    return {"dimension": name[0], name[0]: name[1]}


def _group_error(context, cohorts, options):
    """按角色批次分别估计群体误差；不使用其他角色的资料。"""
    _, groups = _group_rows(context, cohorts)
    entries = []
    for key in sorted(groups, key=str):
        rows = groups[key]
        audit_counts = Counter(
            "random" if context["audits"].get(row["person"], {}).get("selected_by") == "random"
            else "referral" if row["person"] in context["audits"] else "unreviewed"
            for row in rows
        )
        entries.append({
            **_group_descriptor(key, rows),
            "people": len(rows),
            "random_audits": audit_counts["random"],
            "referral_audits": audit_counts["referral"],
            "unreviewed": audit_counts["unreviewed"],
            "small_group": len(rows) < options["min_group_size"],
            "design_coverage": _f(audit_counts["random"] / len(rows)),
            "referral_audits_with_unknown_probability": audit_counts["referral"],
            "score_error": _score_error(rows, context, options["score_threshold"], options["min_random_audits"]),
        })
    return entries


def _strip_private(entries):
    return [{key: value for key, value in entry.items() if not key.startswith("_")} for entry in entries]


def _selection_profile(context, chosen, per_site, population, capacity):
    cells = Counter(_cell(row) for row in population)
    age = Counter(row["age_band"] for row in population)
    access = Counter(row["digital_access"] for row in population)
    selected_cells = Counter(_cell(row) for row in chosen)
    selected_age = Counter(row["age_band"] for row in chosen)
    selected_access = Counter(row["digital_access"] for row in chosen)
    age_rates = {key: _ratio(selected_age[key], age[key]) for key in age}
    access_rates = {key: _ratio(selected_access[key], access[key]) for key in access}
    cell_rates = {key: _ratio(selected_cells[key], cells[key]) for key in cells}

    def parity(rates):
        values = [value for value in rates.values() if value is not None]
        if not values or max(values) == 0:
            return None
        return _f(min(values) / max(values))

    return {
        "site_counts": {site: len(per_site.get(site, [])) for site in context["sites"]},
        "site_slots": {site: capacity.get(site, 0) for site in context["sites"]},
        "age_selection": {
            key: {"people": age[key], "selected": selected_age[key], "rate": _f(age_rates[key])}
            for key in sorted(age)
        },
        "access_selection": {
            key: {"people": access[key], "selected": selected_access[key], "rate": _f(access_rates[key])}
            for key in sorted(access)
        },
        "cell_selection": {
            f"{key[0]}/{key[1]}": {"people": cells[key], "selected": selected_cells[key], "rate": _f(cell_rates[key])}
            for key in sorted(cells)
        },
        "age_parity": parity(age_rates),
        "access_parity": parity(access_rates),
    }


def _rule_outcome(rule, context, cohorts, options, capacity=None):
    """评估一条规则：有效帮助、随机覆盖、群体选择与校区名额。

    帮助率不把未复核者补成“不需要帮助”。选中学员的标签状态分为三类：
    设计随机复核（可外推）、仅有转介复核（标签已知但抽样机制未知）、
    完全未复核。结果同时给出设计估计、最宽部分识别区间与两者之间的
    差，任何单点数字都附带其假设。
    """
    chosen, per_site, population, used_capacity = _run_rule(
        rule, context, cohorts, capacity=capacity, threshold=options["score_threshold"]
    )
    chosen = sorted(chosen, key=lambda row: row["person"])
    observed_positive = 0
    observed_labeled = 0
    design_positive = 0
    design_covered = 0
    referral_covered = 0
    unreviewed = 0
    missing_probability = 0
    for row in chosen:
        audit = context["audits"].get(row["person"])
        if audit is None:
            unreviewed += 1
        elif audit["selected_by"] == "random" and audit["inclusion_probability"] is not None:
            design_covered += 1
            design_positive += int(audit["needs_support"])
            observed_labeled += 1
            observed_positive += int(audit["needs_support"])
        elif audit["selected_by"] == "random":
            missing_probability += 1
        else:
            referral_covered += 1
            observed_labeled += 1
            observed_positive += int(audit["needs_support"])
    selected = len(chosen)
    capacity_slots = sum(used_capacity.values())
    design_rate = _weighted_rate([
        (bool(context["audits"][row["person"]]["needs_support"]),
         1.0 / numeric(context["audits"][row["person"]]["inclusion_probability"], "入样概率"))
        for row in chosen
        if context["audits"].get(row["person"], {}).get("selected_by") == "random"
        and context["audits"][row["person"]]["inclusion_probability"] is not None
    ])
    lower = _rate(observed_positive, selected)
    upper = _rate(observed_positive + selected - observed_labeled, selected)
    population_design = [
        row for row in population
        if context["audits"].get(row["person"], {}).get("selected_by") == "random"
        and context["audits"][row["person"]]["inclusion_probability"] is not None
    ]
    population_positive = sum(int(context["audits"][row["person"]]["needs_support"]) for row in population_design)
    profile = _selection_profile(context, chosen, per_site, population, used_capacity)
    if design_covered:
        verified_lower = design_rate["ci95"][0]
        verified_upper = design_rate["ci95"][1]
    else:
        verified_lower = verified_upper = None
    return {
        "name": rule["name"],
        "label": rule.get("label", rule["name"]),
        "family": rule["family"],
        "slots": capacity_slots,
        "selected": selected,
        "capacity_utilization": _f(_ratio(selected, capacity_slots)),
        "sites_below_slots": [
            site for site in context["sites"] if len(per_site.get(site, [])) < used_capacity.get(site, 0)
        ],
        "selected_by_site": profile["site_counts"],
        "slots_by_site": profile["site_slots"],
        "design_covered": design_covered,
        "design_coverage": _f(_ratio(design_covered, selected)),
        "design_random_positive": design_positive,
        "referral_audits_in_selection": referral_covered,
        "unreviewed_in_selection": unreviewed,
        "random_audits_without_probability": missing_probability,
        "design_help_rate": design_rate,
        "design_help_rate_ci_lower": _f(verified_lower),
        "design_help_rate_ci_upper": _f(verified_upper),
        "help_rate_interval": {
            "lower": lower,
            "upper": upper,
            "lower_assumption": "未复核者与无概率复核者一律不视为需要帮助（只给出下界）。",
            "upper_assumption": "未复核者一律视为需要帮助（只给出上界）。",
            "note": "转介标签是已观测事实，计入区间端点，但转介抽样机制未知，因此不进入设计估计。",
        },
        "population_design_random": len(population_design),
        "population_design_positive": population_positive,
        "design_positive_recall": _f(_ratio(design_positive, population_positive)),
        "age_selection": profile["age_selection"],
        "access_selection": profile["access_selection"],
        "cell_selection": profile["cell_selection"],
        "age_parity": profile["age_parity"],
        "access_parity": profile["access_parity"],
        "_chosen": chosen,
        "_per_site": per_site,
        "_population": population,
        "_capacity": used_capacity,
    }


def _requirement_values(outcome, options):
    """每条要求的可观测取值；None 表示该规则下不可识别，不补齐。"""
    help_rate = outcome["design_help_rate"]
    estimable = outcome["design_covered"] >= options["min_design_audits"]
    return {
        "min_verified_help_rate": None if not estimable or help_rate is None else help_rate["rate"],
        "min_age_selection_ratio": outcome["age_parity"],
        "min_access_selection_ratio": outcome["access_parity"],
        "max_unreviewed_fraction": _f(_ratio(outcome["unreviewed_in_selection"], outcome["selected"]))
        if outcome["selected"] else None,
        "min_design_coverage": outcome["design_coverage"],
        "min_capacity_utilization": outcome["capacity_utilization"],
    }


def _requirement_status(name, value, target):
    if value is None:
        return "unestimable"
    if REQUIREMENT_DIRECTION[name] == "max":
        return "met" if value >= target - 1e-12 else "failed"
    return "met" if value <= target + 1e-12 else "failed"


def _requirement_report(outcome, requirements, options):
    values = _requirement_values(outcome, options)
    report = []
    for name, target in requirements.items():
        value = values[name]
        entry = {
            "name": name,
            "measure": REQUIREMENT_MEASURE[name],
            "direction": REQUIREMENT_DIRECTION[name],
            "target": _f(target),
            "value": _f(value),
            "status": _requirement_status(name, value, target),
            "gap": None if value is None else _f(target - value if REQUIREMENT_DIRECTION[name] == "max" else value - target),
        }
        if name == "min_verified_help_rate" and value is None:
            entry["reason"] = (
                f"选中学员中设计随机复核不足 {options['min_design_audits']} 人，"
                "帮助率不可估计；不会用转介标签或未复核者补足。"
            )
        report.append(entry)
    return report


def _overall_status(report):
    if any(item["status"] == "failed" for item in report):
        return "infeasible"
    if any(item["status"] == "unestimable" for item in report):
        return "unverified"
    return "feasible"


def _assess(outcome, requirements, options):
    report = _requirement_report(outcome, requirements, options)
    return {
        "status": _overall_status(report),
        "requirements": report,
        "met_count": sum(item["status"] == "met" for item in report),
        "failed": [item["name"] for item in report if item["status"] == "failed"],
        "unestimable": [item["name"] for item in report if item["status"] == "unestimable"],
    }


def _evaluate_rules(rules, context, cohorts, options):
    evaluated = []
    for rule in rules:
        outcome = _rule_outcome(rule, context, cohorts, options)
        assessed = _assess(outcome, options["requirements"], options)
        record = {key: value for key, value in outcome.items() if not key.startswith("_")}
        record.update(assessed)
        record["selection_cohorts"] = cohorts
        record["evaluated_on"] = "selection"
        evaluated.append((rule, outcome, record))
    return evaluated


def _best_for(name, evaluated, requirements):
    best = None
    for _, _, record in evaluated:
        item = next(entry for entry in record["requirements"] if entry["name"] == name)
        if item["value"] is None:
            continue
        if best is None or (
            item["value"] > best["value"] if REQUIREMENT_DIRECTION[name] == "max" else item["value"] < best["value"]
        ):
            best = {"value": _f(item["value"]), "rule": record["name"]}
    return best


def _conflict_explanation(evaluated, requirements):
    feasible = [record for _, _, record in evaluated if record["status"] == "feasible"]
    unverified = [record for _, _, record in evaluated if record["status"] == "unverified"]
    if feasible:
        return {
            "feasible_count": len(feasible),
            "feasible_rules": [record["name"] for record in feasible],
            "unverified_rules": [record["name"] for record in unverified],
            "explanation": "至少一条候选规则同时达到全部要求；仍需用独立验证批次确认后再采用。",
        }
    best = {name: _best_for(name, evaluated, requirements) for name in requirements}
    conflicts = []
    individually_short = []
    for name, entry in best.items():
        if entry is None:
            individually_short.append(name)
            conflicts.append(f"{name} 在所有候选规则下都不可识别（缺少入样概率已知的随机复核），无法判定是否达标。")
            continue
        if _requirement_status(name, entry["value"], requirements[name]) == "failed":
            rule_record = next(record for _, _, record in evaluated if record["name"] == entry["rule"])
            losses = [
                f"{other}={next(item['value'] for item in rule_record['requirements'] if item['name'] == other)}（要求 {_f(target)}）"
                for other, target in requirements.items()
                if other != name and next(item for item in rule_record["requirements"] if item["name"] == other)["status"] != "met"
            ]
            individually_short.append(name)
            conflicts.append(
                f"{name} 单项最高可达 {entry['value']}（规则 {entry['rule']}），仍低于要求 {_f(requirements[name])}；"
                + ("该规则同时无法满足：" + "、".join(losses) if losses else "该规则已满足其余要求，但自身数值仍不够。")
            )
    jointly = sorted(evaluated, key=lambda item: (-item[2]["met_count"], item[2]["name"]))
    top = [item for item in jointly if item[2]["met_count"] == jointly[0][2]["met_count"]]
    conflict_sets = []
    for _, _, record in top[:5]:
        conflict_sets.append({
            "rule": record["name"],
            "met": record["met_count"],
            "failed": [item["name"] for item in record["requirements"] if item["status"] == "failed"],
            "unestimable": [item["name"] for item in record["requirements"] if item["status"] == "unestimable"],
        })
    if not individually_short:
        conflicts.append(
            "每一项要求单独都能被某条规则满足，但没有任何一条规则同时满足全部要求："
            "例如提高设计随机覆盖要求把名额更多分给低上网条件群体，这又压低该群体的选择率比例；"
            "放宽群体选择比例则会让高上网条件群体几乎占满名额，未复核比例随之上升。"
            "这些是规则族与资料结构造成的联合冲突，不存在一个同时满足全部要求的默认阈值。"
        )
    elif len(individually_short) == 1:
        conflicts.append("除单项已达上限的要求外，其余要求之间仍无法同时满足；见下列达标最多的规则及其缺口。")
    return {
        "feasible_count": 0,
        "feasible_rules": [],
        "unverified_rules": [record["name"] for record in unverified],
        "best_available": best,
        "individually_unattainable": individually_short,
        "jointly_infeasible": not individually_short,
        "top_rules": conflict_sets,
        "explanation": (
            "在给定候选规则、校区名额和公平要求下，没有规则同时达标。"
            "下面按每项要求列出全候选集里的最好可达值、达到它的规则，以及达标最多的规则仍缺哪些要求；"
            "这些缺口来自资料结构与规则族的相互作用，不是调整一个默认阈值就能消除。"
        ),
        "conflicts": conflicts,
    }


def _validation(context, rule, options):
    """在验证批次上按该批次的缩放名额复核推荐规则；不重新拟合或改阈值。"""
    cohorts = options["splits"]["validation"]
    outcome = _rule_outcome(rule, context, cohorts, options)
    assessed = _assess(outcome, options["requirements"], options)
    record = {key: value for key, value in outcome.items() if not key.startswith("_")}
    record.update(assessed)
    record["validation_cohorts"] = cohorts
    record["_chosen"] = outcome["_chosen"]
    record["_per_site"] = outcome["_per_site"]
    return record


def _recommendation(evaluated, has_design_evidence):
    """选规则：先看是否全部达标；否则不把默认阈值包装成公平规则。

    数据集完全没有入样概率已知的随机复核时返回 None：此时无法估计帮助率，
    按结构指标挑一条规则再生成个人名单，等于在零证据下给出优先顺序。
    返回 (选中条目或 None, 是否可行, 依据)。
    """
    if not has_design_evidence:
        return None, False, (
            "资料中没有任何入样概率已知的随机复核，有效帮助与评分误差均不可估计；"
            "在零证据下不选择分流规则、不生成个人优先名单，只报告抽样诊断与资料缺口。"
        )
    feasible = [item for item in evaluated if item[2]["status"] == "feasible"]
    if feasible:
        def key(item):
            record = item[2]
            lower = record["design_help_rate_ci_lower"]
            help_rate = lower if lower is not None else -1.0
            unreviewed = _ratio(record["unreviewed_in_selection"], record["selected"]) or 1.0
            parities = [value for value in (record["age_parity"], record["access_parity"]) if value is not None]
            parity = min(parities) if parities else 0.0
            return (-help_rate, unreviewed, -parity, record["name"])
        ordered = sorted(feasible, key=key)
        return ordered[0], True, "存在同时达标的规则；按帮助率下界、未复核比例、群体选择率依次排序后取第一条。"

    estimable = [
        item for item in evaluated
        if item[2]["design_covered"] > 0 or any(
            entry["status"] != "unestimable" for entry in item[2]["requirements"]
        )
    ]
    if not estimable:
        return None, False, "所有候选规则的全部公平指标都不可估计，不选择规则。"

    def rank(item):
        record = item[2]
        failed = sum(entry["status"] == "failed" for entry in record["requirements"])
        unestimable = sum(entry["status"] == "unestimable" for entry in record["requirements"])
        return (failed, unestimable, -record["met_count"], -(record["design_coverage"] or 0.0), record["name"])

    best = sorted(estimable, key=rank)[0]
    return best, False, (
        "没有规则同时达到全部公平要求，因此不宣称任何默认阈值为公平方案。"
        "下列为“明确不达标项最少、其次不可估计项最少”的规则，仅作为可复核的候选，不是公平通过的结论。"
    )


def _deployment_suggestion(rule, context, options):
    """把推荐规则应用到当前全部人群，使用真实校区名额。

    该名单只用于人工联系的优先顺序，不是对个人是否需要帮助的判定；
    它不参与规则选择或误差估计，且明确标注每个人的标签来源。
    """
    capacity = dict(context["capacity"])
    chosen, per_site, population, used = _run_rule(
        rule, context, context["cohorts"], capacity=capacity
    )
    chosen = sorted(chosen, key=lambda row: row["person"])
    rows = []
    for row in chosen:
        audit = context["audits"].get(row["person"])
        if audit is None:
            basis = "unreviewed"
            needs = None
        elif audit["selected_by"] == "random" and audit["inclusion_probability"] is not None:
            basis = "design_random"
            needs = audit["needs_support"]
        elif audit["selected_by"] == "random":
            basis = "random_without_probability"
            needs = audit["needs_support"]
        else:
            basis = "referral_only"
            needs = None
        rows.append({
            "person": row["person"],
            "site": row["site"],
            "age_band": row["age_band"],
            "digital_access": row["digital_access"],
            "score": row["score"],
            "cohort": row["cohort"],
            "label_basis": basis,
            "needs_support": needs,
        })
    return {
        "rule": rule["name"],
        "applied_to": "全部学员",
        "used_capacity": used,
        "selected": len(chosen),
        "selected_by_site": {site: len(per_site.get(site, [])) for site in context["sites"]},
        "slots_by_site": {site: used.get(site, 0) for site in context["sites"]},
        "not_a_gatekeeping_decision": True,
        "notice": "这是按推荐规则生成的优先联系建议，不是资格审查；任何人仍可主动申请辅导。",
        "people": rows,
    }


def support_fairness(tables, parameters):
    options_ = options(parameters)
    context = _prepare(tables)
    cohorts = sorted({row["cohort"] for row in context["people"]})
    assigned = set().union(*map(set, options_["splits"].values()))
    unknown_cohorts = [cohort for cohort in cohorts if cohort not in assigned]
    if unknown_cohorts:
        raise ValueError(f"以下批次未分配角色：{unknown_cohorts}")
    missing_cohorts = [cohort for cohort in sorted(assigned) if cohort not in set(cohorts)]
    if missing_cohorts:
        raise ValueError(f"参数引用了不存在的批次：{missing_cohorts}")
    selection_cohorts = options_["splits"]["selection"]
    estimation_cohorts = options_["splits"]["estimation"]
    validation_cohorts = options_["splits"]["validation"]

    sampling = _sampling_diagnostic(context, cohorts)
    group_errors = {
        role: {
            "cohorts": options_["splits"][role],
            "groups": _group_error(context, options_["splits"][role], options_),
        }
        for role in SPLIT_ROLES
    }

    has_design_evidence = any(
        audit["selected_by"] == "random" and audit["inclusion_probability"] is not None
        for audit in context["audits"].values()
    )
    has_capacity = sum(context["capacity"].values()) > 0
    evaluated = _evaluate_rules(options_["rules"], context, selection_cohorts, options_)
    if not has_capacity:
        chosen, feasible_found, basis = None, False, (
            "全部校区名额为 0，无法在不超出名额的前提下生成优先联系建议；"
            "不选择规则、不生成个人名单，只报告资料与误差诊断。"
        )
    else:
        chosen, feasible_found, basis = _recommendation(evaluated, has_design_evidence)

    estimation_record = None
    validation_record = None
    if chosen is not None:
        rule = chosen[0]
        estimation_outcome = _rule_outcome(rule, context, estimation_cohorts, options_)
        estimation_record = {key: value for key, value in estimation_outcome.items() if not key.startswith("_")}
        estimation_record.update(_assess(estimation_outcome, options_["requirements"], options_))
        estimation_record["estimation_cohorts"] = estimation_cohorts
        validation_record = _validation(context, rule, options_)
        validation_passed = validation_record["status"] == "feasible"
    else:
        validation_passed = False

    rules_output = []
    for _, outcome, record in evaluated:
        entry = dict(record)
        entry["zero_design_review_sites"] = [
            site for site, count in record["selected_by_site"].items()
            if count and not any(
                context["audits"].get(row["person"], {}).get("selected_by") == "random"
                and context["audits"][row["person"]]["inclusion_probability"] is not None
                for row in outcome["_per_site"].get(site, [])
            )
        ]
        entry["sample_groups_without_design_review"] = sorted({
            f"{row['age_band']}/{row['digital_access']}"
            for row in outcome["_chosen"]
            if context["audits"].get(row["person"], {}).get("selected_by") != "random"
        })
        rules_output.append(entry)

    if chosen is None:
        recommendation = {
            "rule": None,
            "status": "none_feasible",
            "basis": basis,
            "meets_requirements_on_selection_cohorts": False,
            "validation_passed": False,
            "not_a_gatekeeping_decision": True,
            "notice": (
                "当前候选规则没有一条同时达到全部要求。因此不提供默认阈值作为“公平规则”，"
                "也不生成未经复核的个体优先名单；如必须先行分流，应由工作人员按规则的可达边界人工取舍并记录理由。"
            ),
            "validation_sample": [],
        }
    else:
        rule = chosen[0]
        recommendation = {
            "rule": rule["name"],
            "label": rule.get("label", rule["name"]),
            "status": "feasible_on_selection" if feasible_found else "candidate_only",
            "basis": basis,
            "meets_requirements_on_selection_cohorts": feasible_found,
            "validation_passed": validation_passed,
            "not_a_gatekeeping_decision": True,
            "notice": (
                "该建议只排序人工辅导的优先联系顺序，不改变任何学员主动申请辅导的资格，也不改动原评分。"
                + ("" if feasible_found else " 该规则并非公平通过，只是全候选集中达标项最多、设计覆盖最高的候选，需人工复核。")
            ),
            "validation_sample": [
                {
                    "person": row["person"],
                    "site": row["site"],
                    "age_band": row["age_band"],
                    "digital_access": row["digital_access"],
                    "score": row["score"],
                    "cohort": row["cohort"],
                    "label_basis": (
                        "design_random" if context["audits"].get(row["person"], {}).get("selected_by") == "random"
                        and context["audits"][row["person"]]["inclusion_probability"] is not None
                        else "referral_only" if context["audits"].get(row["person"], {}).get("selected_by") == "referral"
                        else "unreviewed"
                    ),
                    "needs_support": context["audits"].get(row["person"], {}).get("needs_support")
                    if context["audits"].get(row["person"], {}).get("selected_by") == "random"
                    and context["audits"][row["person"]]["inclusion_probability"] is not None else None,
                }
                for row in sorted(validation_record["_chosen"], key=lambda row: row["person"])
            ],
            "validation_sample_note": "这是验证批次上的回放样本，用于检查规则在新批次的表现，不是最终联系名单；最终建议见 deployment_suggestion。",
        }

    return {
        "analysis_version": ANALYSIS_VERSION,
        "product": "数字助教支持分流",
        "scope": {
            "people": len(context["people"]),
            "cohorts": cohorts,
            "sites": context["sites"],
            "real_capacity": context["capacity"],
            "real_capacity_total": sum(context["capacity"].values()),
            "score_modified": False,
            "eligibility_unchanged": True,
        },
        "roles": {
            "selection": {"cohorts": selection_cohorts, "purpose": "比较候选规则并决定推荐规则"},
            "estimation": {"cohorts": estimation_cohorts, "purpose": "估计推荐规则的有效帮助与误差"},
            "validation": {"cohorts": validation_cohorts, "purpose": "独立复核推荐规则；不参与选择或调参"},
            "separation": "三个角色使用互不重叠的入组批次；验证批次不参与规则选择、阈值调整或名额分配。",
        },
        "sampling": sampling,
        "design_check": _design_check(context, cohorts),
        "existing_score_error": {
            "threshold": options_["score_threshold"],
            "by_role": group_errors,
            "note": (
                "仅在随机抽查覆盖范围内可估计；转介样本不能代替随机样本，未复核者不计入任何误差分母。"
                "estimable=false 表示该群体没有入样概率已知的随机复核，误差不可识别，不补值为 0。"
            ),
        },
        "requirements": options_["requirements"],
        "rules": rules_output,
        "feasibility": _conflict_explanation(evaluated, options_["requirements"]),
        "recommended_rule": None if chosen is None else chosen[0]["name"],
        "estimation": estimation_record,
        "validation": None if validation_record is None else {
            key: value for key, value in validation_record.items() if not key.startswith("_")
        },
        "recommendation": recommendation,
        "deployment_suggestion": None if chosen is None else _deployment_suggestion(chosen[0], context, options_),
        "limitations": [
            "随机抽查只覆盖部分群体；未覆盖群体的评分误差不可识别，结果以 null 表示，不补成“不需要帮助”。",
            "转介样本的入样概率未知且与评分相关，只用于说明既有转介，不进入设计误差或设计帮助率。",
            "小群体随机复核很少，区间很宽；这些结果只作描述，不用于个体结论。",
            "帮助率区间下界把未复核与无概率复核者都视为不需要帮助，上界把未复核者视为需要帮助；真实值在两者之间，现有资料无法唯一确定。",
            "候选规则的选择、误差估计与最终验证使用不同批次；验证结果不作为重新选择规则的依据。",
            "本分析不修改原评分，不改变任何人申请辅导的资格；建议名单仅用于优先联系顺序。",
        ],
    }
