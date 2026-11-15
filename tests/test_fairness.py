import copy
import json
from pathlib import Path
import unittest

from learning.codec import digest, encode
from learning.domain import ANALYSES, overview, validate
from learning.fairness import support_fairness


def load(release="term-a"):
    return json.loads(Path("data/releases.json").read_text(encoding="utf-8"))[release]


def all_groups(result):
    """所有角色下的群体条目；角色之间批次不重叠。"""
    return [
        group
        for role in result["existing_score_error"]["by_role"].values()
        for group in role["groups"]
    ]


class FairnessTests(unittest.TestCase):
    def setUp(self):
        self.tables = load()
        validate(self.tables)

    def run_analysis(self, tables=None, parameters=None):
        result = support_fairness(self.tables if tables is None else tables, parameters or {})
        encode(result)  # 不允许 NaN/Infinity，且必须可序列化
        return result

    # --- 抽样机制：随机与转介不能混用 ---
    def test_referral_not_used_as_design_sample(self):
        result = self.run_analysis()
        for group in all_groups(result):
            self.assertGreaterEqual(group["referral_audits"], 0)
            error = group["score_error"]
            if error["estimable"]:
                # 随机复核人数只可能来自 selected_by=random
                self.assertLessEqual(error["random_audits"], group["random_audits"])
                self.assertEqual(error["referral_audits_excluded"], group["referral_audits"])

    def test_high_access_group_is_unestimable_not_negative(self):
        result = self.run_analysis()
        cells = {
            (group.get("age_band"), group.get("digital_access")): group
            for group in all_groups(result)
            if group["dimension"] == "cell"
        }
        for key in (("60-74", "high"), ("75+", "high")):
            error = cells[key]["score_error"]
            self.assertFalse(error["estimable"])
            self.assertIsNone(error.get("auc"))
            self.assertEqual(cells[key]["random_audits"], 0)

    def test_design_check_flags_partial_sampling_frame(self):
        result = self.run_analysis()
        check = result["design_check"]
        self.assertFalse(check["assumption_holds"])
        cohort = check["cohorts"][0]
        self.assertIn("60-74/high", cohort["cells_unsampled"])
        self.assertTrue(any("100%" in issue for issue in cohort["issues"]))

    # --- 未复核者不被补成“不需要帮助” ---
    def test_unreviewed_never_counted_as_negative(self):
        result = self.run_analysis()
        sampling = result["sampling"]
        self.assertEqual(sampling["unreviewed"], 140)
        self.assertEqual(sampling["random_audits"] + sampling["referral_audits"] + sampling["unreviewed"],
                         sampling["people"])
        for group in all_groups(result):
            self.assertEqual(group["people"], group["random_audits"] + group["referral_audits"] + group["unreviewed"])

    def test_zero_audits_are_unestimable(self):
        result = self.run_analysis(load("limited"))
        for group in all_groups(result):
            self.assertFalse(group["score_error"]["estimable"])
            self.assertIsNone(group["score_error"].get("auc"))
        self.assertFalse(result["design_check"]["assumption_holds"])
        self.assertIsNone(result["recommended_rule"])
        self.assertEqual(result["recommendation"]["validation_sample"], [])
        self.assertEqual(result["recommendation"]["status"], "none_feasible")

    def test_help_rate_bounds_are_partial_identification(self):
        result = self.run_analysis()
        for rule in result["rules"]:
            interval = rule["help_rate_interval"]
            if interval["lower"] and interval["upper"]:
                self.assertLessEqual(interval["lower"]["rate"], interval["upper"]["rate"])
                self.assertEqual(interval["lower"]["denominator"], rule["selected"])
                self.assertEqual(interval["upper"]["denominator"], rule["selected"])

    # --- 交叉小群体与小样本 ---
    def test_small_groups_flagged(self):
        result = self.run_analysis(parameters={"min_group_size": 30})
        by_role = result["existing_score_error"]["by_role"]
        for role, entry in by_role.items():
            cells = [group for group in entry["groups"] if group["dimension"] == "cell"]
            self.assertEqual(len(cells), 4, role)
            flagged = {f"{group['age_band']}/{group['digital_access']}" for group in cells if group["small_group"]}
            # 75+ 交叉群体在每个角色批次都低于阈值；两批合并后的 75+/high 在 selection 下也可能低于阈值。
            self.assertIn("75+/low", flagged, role)
            self.assertIn("75+/high", flagged, role)

    def test_group_design_coverage_reported(self):
        result = self.run_analysis()
        for group in all_groups(result):
            expected = group["random_audits"] / group["people"]
            self.assertAlmostEqual(group["design_coverage"], expected, places=6)

    def test_min_random_audits_marks_description_only(self):
        result = self.run_analysis(parameters={"min_random_audits": 10_000})
        for group in all_groups(result):
            error = group["score_error"]
            if error["estimable"]:
                self.assertFalse(error["usable_for_inference"])
                self.assertIn("不足以外推", error["reason"])

    # --- 批次分离 ---
    def test_roles_use_disjoint_cohorts(self):
        result = self.run_analysis()
        roles = result["roles"]
        groups = [set(roles[role]["cohorts"]) for role in ("selection", "estimation", "validation")]
        self.assertEqual(len(set().union(*groups)), sum(len(group) for group in groups))
        self.assertEqual(set(result["validation"]["validation_cohorts"]), groups[2])
        self.assertEqual(set(result["estimation"]["estimation_cohorts"]), groups[1])

    def test_overlapping_splits_rejected(self):
        with self.assertRaises(ValueError):
            self.run_analysis(parameters={"splits": {"selection": [0, 1], "estimation": [1], "validation": [2]}})

    def test_unassigned_cohort_rejected(self):
        with self.assertRaises(ValueError):
            self.run_analysis(parameters={"splits": {"selection": [0], "estimation": [1], "validation": [2]}})

    def test_validation_differs_from_selection_output(self):
        result = self.run_analysis()
        self.assertEqual(result["rules"][0]["evaluated_on"], "selection")
        # 验证名单来自验证批次，不是选择批次的选中者
        cohorts = {row["cohort"] for row in result["recommendation"]["validation_sample"]}
        self.assertTrue(cohorts <= set(result["roles"]["validation"]["cohorts"]))
        self.assertFalse(cohorts & set(result["roles"]["selection"]["cohorts"]))
        self.assertFalse(cohorts & set(result["roles"]["estimation"]["cohorts"]))

    # --- 校区名额 ---
    def test_selection_respects_site_capacity(self):
        result = self.run_analysis()
        for rule in result["rules"]:
            for site, count in rule["selected_by_site"].items():
                self.assertLessEqual(count, rule["slots_by_site"][site])

    def test_capacity_never_exceeds_real_slots(self):
        result = self.run_analysis()
        real = result["scope"]["real_capacity"]
        for rule in result["rules"]:
            for site, slots in rule["slots_by_site"].items():
                self.assertLessEqual(slots, real[site])
        if result["validation"]:
            for site, slots in result["validation"]["slots_by_site"].items():
                self.assertLessEqual(slots, real[site])

    def test_threshold_rule_can_underfill_but_reports_it(self):
        result = self.run_analysis(parameters={"rules": [
            {"name": "strict", "family": "threshold", "threshold": 0.99}
        ]})
        rule = result["rules"][0]
        self.assertLess(rule["capacity_utilization"], 1.0)
        self.assertTrue(rule["sites_below_slots"])

    # --- 可达边界与冲突 ---
    def test_infeasible_reports_frontier_and_conflicts(self):
        result = self.run_analysis()
        feasibility = result["feasibility"]
        self.assertEqual(feasibility["feasible_count"], 0)
        self.assertTrue(feasibility["conflicts"])
        for name, entry in feasibility["best_available"].items():
            if entry is not None:
                self.assertIn("value", entry)
                self.assertIn("rule", entry)
        self.assertIn("jointly_infeasible", feasibility)

    def test_loosened_requirements_can_become_feasible(self):
        result = self.run_analysis(parameters={"requirements": {
            "min_verified_help_rate": 0.0,
            "min_age_selection_ratio": 0.0,
            "min_access_selection_ratio": 0.0,
            "max_unreviewed_fraction": 1.0,
            "min_design_coverage": 0.0,
            "min_capacity_utilization": 0.0,
        }})
        self.assertGreater(result["feasibility"]["feasible_count"], 0)
        self.assertIsNotNone(result["recommended_rule"])
        self.assertIn(result["recommendation"]["status"], {"feasible_on_selection", "candidate_only"})

    def test_requirement_values_are_reported(self):
        result = self.run_analysis()
        for rule in result["rules"]:
            names = {entry["name"] for entry in rule["requirements"]}
            self.assertEqual(names, set(result["requirements"]))
            for entry in rule["requirements"]:
                self.assertIn(entry["status"], {"met", "failed", "unestimable"})

    def test_zero_capacity_returns_no_individual_list(self):
        tables = copy.deepcopy(self.tables)
        for row in tables["capacities"]:
            row["slots"] = 0
        result = self.run_analysis(tables)
        self.assertIsNone(result["recommended_rule"])
        self.assertIsNone(result["deployment_suggestion"])
        self.assertEqual(result["recommendation"]["validation_sample"], [])
        self.assertIn("名额为 0", result["recommendation"]["basis"])

    def test_single_site_zero_capacity_respected(self):
        tables = copy.deepcopy(self.tables)
        for row in tables["capacities"]:
            if row["site"] == "s0":
                row["slots"] = 0
        result = self.run_analysis(tables)
        self.assertEqual(result["deployment_suggestion"]["slots_by_site"]["s0"], 0)
        self.assertEqual(result["deployment_suggestion"]["selected_by_site"]["s0"], 0)

    def test_referral_only_data_yields_no_rule(self):
        tables = copy.deepcopy(self.tables)
        tables["audits"] = [row for row in tables["audits"] if row["selected_by"] != "random"]
        result = self.run_analysis(tables)
        self.assertIsNone(result["recommended_rule"])
        self.assertIsNone(result["deployment_suggestion"])
        self.assertEqual(result["recommendation"]["status"], "none_feasible")

    def test_reserve_group_absent_still_valid(self):
        result = self.run_analysis(parameters={"rules": [
            {"name": "absent", "family": "reserve", "by": {"age_band": "不存在"}, "fraction": 0.5}
        ]})
        self.assertEqual(result["rules"][0]["selected"], result["rules"][0]["slots"])
        self.assertGreater(result["rules"][0]["selected"], 0)

    def test_unknown_rule_family_rejected(self):
        with self.assertRaises(ValueError):
            self.run_analysis(parameters={"rules": [{"name": "x", "family": "magic"}]})

    # --- 不改原评分、不剥夺资格 ---
    def test_does_not_mutate_input_or_scores(self):
        before = digest(self.tables)
        result = self.run_analysis()
        self.assertEqual(before, digest(self.tables))
        self.assertFalse(result["scope"]["score_modified"])
        self.assertTrue(result["scope"]["eligibility_unchanged"])
        self.assertTrue(result["recommendation"]["not_a_gatekeeping_decision"])
        for row in result["recommendation"]["validation_sample"]:
            original = next(person for person in self.tables["people"] if person["person"] == row["person"])
            self.assertEqual(row["score"], original["score"])

    def test_selected_do_not_exceed_population_and_keep_original_fields(self):
        result = self.run_analysis()
        people = {row["person"]: row for row in self.tables["people"]}
        for row in result["recommendation"]["validation_sample"]:
            self.assertIn(row["person"], people)
            for field in ("site", "age_band", "digital_access", "cohort"):
                self.assertEqual(row[field], people[row["person"]][field])

    def test_deployment_suggestion_uses_real_capacity_and_all_people(self):
        result = self.run_analysis()
        deployment = result["deployment_suggestion"]
        self.assertIsNotNone(deployment)
        real = result["scope"]["real_capacity"]
        self.assertEqual(deployment["slots_by_site"], real)
        for site, count in deployment["selected_by_site"].items():
            self.assertEqual(count, real[site])
        # 只使用规则和原始字段，不含被改写的评分
        people = {row["person"]: row for row in self.tables["people"]}
        for row in deployment["people"]:
            self.assertEqual(row["score"], people[row["person"]]["score"])
            self.assertIn(row["label_basis"], {"design_random", "random_without_probability", "referral_only", "unreviewed"})
        self.assertTrue(deployment["not_a_gatekeeping_decision"])

    def test_no_deployment_list_without_design_evidence(self):
        result = self.run_analysis(load("limited"))
        self.assertIsNone(result["deployment_suggestion"])

    # --- 确定性与版本无关性 ---
    def test_deterministic_and_order_independent(self):
        first = self.run_analysis()
        shuffled = {name: list(reversed(rows)) for name, rows in self.tables.items()}
        self.assertEqual(digest(first), digest(self.run_analysis(shuffled)))
        self.assertEqual(digest(first), digest(self.run_analysis()))

    def test_term_b_matches_term_a(self):
        self.assertEqual(digest(self.run_analysis(load("term-a"))), digest(self.run_analysis(load("term-b"))))

    def test_overview_still_works(self):
        result = overview(self.tables, {})
        self.assertEqual(sum(group["people"] for group in result["groups"]), len(self.tables["people"]))


class RegistryTests(unittest.TestCase):
    def test_support_fairness_registered(self):
        self.assertIn("support_fairness", ANALYSES)
        self.assertIn("overview", ANALYSES)


if __name__ == "__main__":
    unittest.main()
