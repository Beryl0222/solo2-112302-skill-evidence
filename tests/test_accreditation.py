import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from accreditation import (  # noqa: E402
    AccreditationService,
    PermissionDeniedError,
    parse_comment,
    parse_standard_text,
)
from accreditation.engine import evaluate_standard
from accreditation.demo import run_demo


def fact(fact_id, task, operation_id, **overrides):
    data = {
        "fact_id": fact_id,
        "task": task,
        "actor": "林霁",
        "actor_kind": "learner",
        "qualifies_learner": True,
        "completed": True,
        "independence": "independent",
        "mode": "direct",
        "operation_id": operation_id,
        "record_id": overrides.pop("record_id", fact_id + "-record"),
        "status": "active",
    }
    data.update(overrides)
    return data


def school_standard():
    return {
        "id": "school",
        "group": "school",
        "text": "校方标准|能独立完成缺值核对和异常回填。",
        "alternatives": [
            {
                "id": "both",
                "requirements": [
                    {
                        "id": "check",
                        "key": "check",
                        "task": "missing_value_check",
                        "competency_id": "missing_value_check",
                        "independent": True,
                    },
                    {
                        "id": "backfill",
                        "key": "backfill",
                        "task": "exception_backfill",
                        "competency_id": "exception_backfill",
                        "independent": True,
                    },
                ],
            }
        ],
    }


def enterprise_standard():
    std = school_standard()
    std["id"] = "enterprise"
    std["group"] = "enterprise"
    std["distinct_operation_keys"] = [["check", "backfill"]]
    std["text"] = "企业标准|缺值核对与异常回填都须独立，且来自不同实训操作。"
    return std


class ParserTests(unittest.TestCase):
    def test_distinguishes_independent_from_prompted_completion(self):
        parsed = parse_comment(
            {"id": "teacher", "text": "能独立核对缺值，但异常回填是在同伴提示下完成。"},
            learner_name="林霁",
        )
        by_task = {f["task"]: f for f in parsed["facts"]}
        self.assertEqual(by_task["missing_value_check"]["independence"], "independent")
        self.assertEqual(by_task["exception_backfill"]["independence"], "prompted")
        self.assertTrue(by_task["exception_backfill"]["completed"])

    def test_sentence_split_does_not_create_new_operation(self):
        parsed = parse_comment(
            {
                "id": "one-evidence",
                "text": "林霁自行核对缺值。他随后独立完成异常回填。",
            },
            learner_name="林霁",
        )
        ops = {f["operation_id"] for f in parsed["facts"]}
        self.assertEqual(len(ops), 1)

    def test_detects_quoted_teammate_report_and_session(self):
        record = {
            "id": "obs-3",
            "text": "周二罗闻转述|“我看他在同一轮复跑里先核缺值、再回填。”说的是周二第二场操作。",
        }
        parsed = parse_comment(record, learner_name="林霁")
        self.assertTrue(parsed["facts"])
        self.assertTrue(all(f["mode"] == "hearsay" for f in parsed["facts"] if f["task"] in {"missing_value_check", "exception_backfill"}))
        backfill = next(f for f in parsed["facts"] if f["task"] == "exception_backfill")
        self.assertEqual(backfill["operation_id"], "op-day2-session2")
        self.assertEqual(backfill["actor"], "林霁")

    def test_inline_time_and_actor_switch_uses_separate_operation(self):
        parsed = parse_comment(
            {
                "id": "switch",
                "text": "周一林霁工位|林霁自行核过缺值。周二罗闻工位|罗闻独立完成异常回填。",
            },
            learner_name="林霁",
        )
        check = next(f for f in parsed["facts"] if f["task"] == "missing_value_check")
        backfill = next(f for f in parsed["facts"] if f["task"] == "exception_backfill")
        self.assertEqual(check["time_index"], 1)
        self.assertEqual(check["actor_kind"], "learner")
        self.assertEqual(backfill["time_index"], 2)
        self.assertEqual(backfill["actor_kind"], "peer")
        self.assertNotEqual(check["operation_id"], backfill["operation_id"])

    def test_standard_text_parses_conjunction_and_alternative(self):
        std = parse_standard_text(
            "校方通则|能独立完成缺值核对和异常回填，或提交独立复跑并解释回填依据。",
            standard_id="school-s1",
        )
        self.assertEqual(len(std["alternatives"]), 2)
        first_tasks = [r["task"] for r in std["alternatives"][0]["requirements"]]
        self.assertEqual(first_tasks, ["missing_value_check", "exception_backfill"])
        self.assertTrue(all(r["independent"] for r in std["alternatives"][0]["requirements"]))
        second_tasks = [r["task"] for r in std["alternatives"][1]["requirements"]]
        self.assertEqual(second_tasks, ["independent_rerun", "explain_basis"])

        ent = parse_standard_text(
            "企业签发条件|缺值核对与异常回填都须独立完成，且来自不同实训操作。",
            standard_id="enterprise-e1",
        )
        self.assertEqual(ent["distinct_operation_keys"], [["missing_value_check", "exception_backfill"]])


class CombinationTests(unittest.TestCase):
    def test_school_allows_one_operation_for_multiple_abilities(self):
        std = school_standard()
        facts = [
            fact("f-check", "missing_value_check", "op-1"),
            fact("f-backfill", "exception_backfill", "op-1"),
        ]
        report = evaluate_standard(facts, std)
        self.assertTrue(report["feasible"])
        self.assertEqual(report["valid_combinations"][0]["unique_operation_count"], 1)

    def test_enterprise_rejects_splitting_one_operation_into_sentences(self):
        std = enterprise_standard()
        facts = [
            fact("f-check", "missing_value_check", "op-1"),
            fact("f-backfill", "exception_backfill", "op-1"),
        ]
        report = evaluate_standard(facts, std)
        self.assertFalse(report["feasible"])
        self.assertTrue(any(g["type"] == "distinct_operation" for g in report["gaps"]))

    def test_duplicate_teacher_and_teammate_report_counts_once(self):
        std = enterprise_standard()
        facts = [
            fact("check-direct", "missing_value_check", "op-1", mode="direct"),
            fact("check-hearsay", "missing_value_check", "op-1", mode="hearsay"),
            fact("backfill", "exception_backfill", "op-2"),
        ]
        report = evaluate_standard(facts, std)
        self.assertTrue(report["feasible"])
        self.assertEqual(len(report["valid_combinations"]), 1)
        combo = report["valid_combinations"][0]
        self.assertEqual(set(combo["operation_ids"]), {"op-1", "op-2"})
        self.assertIn("check-hearsay", combo["all_evidence_fact_ids"])
        self.assertIn("check-hearsay", combo["corroborating_fact_ids"])

    def test_prompted_completion_is_not_independent_candidate(self):
        std = school_standard()
        facts = [
            fact("check", "missing_value_check", "op-1"),
            fact("backfill", "exception_backfill", "op-2", independence="prompted"),
        ]
        report = evaluate_standard(facts, std)
        self.assertFalse(report["feasible"])
        gap = next(g for g in report["gaps"] if g["key"] == "backfill")
        self.assertTrue(any("not_independent" in r for miss in gap["near_misses"] for r in miss["reasons"]))

    def test_later_progress_cannot_pass_earlier_cutoff(self):
        std = school_standard()
        facts = [
            fact("check-mon", "missing_value_check", "op-mon", time_index=1),
            fact("backfill-tue", "exception_backfill", "op-tue", time_index=2),
        ]
        self.assertFalse(evaluate_standard(facts, std, as_of=1)["feasible"])
        self.assertTrue(evaluate_standard(facts, std, as_of=2)["feasible"])
        monday_only = evaluate_standard(facts, std)
        # Operation filtering also prevents attaching Tuesday's act to Monday.
        monday_facts = [f for f in facts if f["operation_id"] == "op-mon"]
        self.assertFalse(evaluate_standard(monday_facts, std)["feasible"])
        self.assertTrue(monday_only)  # report itself is returned


class CompetencyBoundaryTests(unittest.TestCase):
    def test_general_competency_does_not_fill_professional_competency(self):
        service = AccreditationService(learner_name="林霁")
        service.add_standard(
            {
                "id": "general",
                "group": "school",
                "alternatives": [
                    {
                        "id": "g",
                        "requirements": [
                            {"id": "g1", "task": "communication", "competency_id": "general.communication"}
                        ],
                    }
                ],
            }
        )
        service.add_standard(
            {
                "id": "professional",
                "group": "enterprise",
                "alternatives": [
                    {
                        "id": "p",
                        "requirements": [
                            {"id": "p1", "task": "exception_backfill", "competency_id": "professional.backfill", "independent": True}
                        ],
                    }
                ],
            }
        )
        service.add_record(
            {
                "id": "general-evidence",
                "facts": [fact("g-fact", "communication", "op-1", competency_id=None)],
            }
        )
        self.assertTrue(service.verify_competency("general.communication"))
        self.assertFalse(service.verify_competency("professional.backfill"))
        report = service.evaluate("professional")
        self.assertFalse(report["feasible"])


class PermissionAndReviewTests(unittest.TestCase):
    def setUp(self):
        self.service = AccreditationService(learner_name="林霁")
        self.service.add_standard(school_standard())
        self.service.add_standard(enterprise_standard())
        # Check op1; signed backfill op2; another backfill op1 remains.
        self.service.add_record({"id": "r1", "facts": [fact("f1", "missing_value_check", "op1", record_id="r1")]})
        self.service.add_record({"id": "r2", "facts": [fact("f2", "exception_backfill", "op2", record_id="r2")]})
        self.service.add_record({"id": "r3", "facts": [fact("f3", "exception_backfill", "op1", record_id="r3")]})
        self.school = self.service.sign_off(
            signoff_id="school-sign",
            group="school",
            recipient="教务处",
            standard_id="school",
            purpose="school_credit",
            signed_at="周二",
        )
        self.enterprise = self.service.sign_off(
            signoff_id="enterprise-sign",
            group="enterprise",
            recipient="企业技能组",
            standard_id="enterprise",
            purpose="enterprise_certification",
            signed_at="周二",
        )

    def test_role_permissions_are_separate(self):
        with self.assertRaises(PermissionDeniedError):
            self.service.clarify_attribution(
                target={"fact_id": "f2"},
                attribution={"actor_kind": "peer"},
                issuer={"id": "owner", "role": "标准负责人"},
            )
        with self.assertRaises(PermissionDeniedError):
            self.service.issue_interpretation(
                issuer={"id": "registrar", "role": "教务员"},
                text="x",
                scope={"purposes": ["enterprise_certification"]},
            )
        with self.assertRaises(Exception):
            self.service.issue_interpretation(
                issuer={"id": "owner", "role": "标准负责人"}, text="no scope", scope={}
            )

    def test_peer_correction_starts_separate_reviews_and_preserves_signoff(self):
        decision = self.service.clarify_attribution(
            decision_id="dec-peer",
            issuer={"id": "zhou", "role": "教务员"},
            target={"fact_id": "f2"},
            attribution={"actor": "罗闻", "actor_kind": "peer"},
            issued_at="周三",
        )
        self.assertEqual(len(decision["review_ids"]), 2)
        reviews = [self.service.state["reviews"][rid] for rid in decision["review_ids"]]
        by_signoff = {r["signoff_id"]: r for r in reviews}

        self.assertEqual(by_signoff["school-sign"]["suggested_outcome"], "upheld")
        self.assertEqual(by_signoff["enterprise-sign"]["suggested_outcome"], "partially_upheld")
        ent_review = by_signoff["enterprise-sign"]
        self.assertTrue(any(x.startswith("qual:enterprise") for x in ent_review["pending_conclusions"]))

        # Sent payload and its then-current evidence remain unchanged.
        sent_f2 = next(f for f in self.school["snapshot"]["evidence"] if f["fact_id"] == "f2")
        self.assertEqual(sent_f2["actor_kind"], "learner")
        self.assertEqual(sent_f2["independence"], "independent")
        self.assertEqual(self.service.state["facts"]["f2"]["actor_kind"], "peer")
        self.assertEqual(self.school["status"], "sent")

        notices = self.service.notifications_for("企业技能组")
        self.assertTrue(any(n["type"] == "review_pending" for n in notices))
        school_notices = self.service.notifications_for("教务处")
        # School is reviewed and upheld by remaining same-operation evidence;
        # no pending notice is needed there.
        self.assertFalse(any(n["type"] == "review_pending" for n in school_notices))

        completed = self.service.complete_review(
            ent_review["id"],
            reviewer={"id": "enterprise-reviewer", "role": "企业技能组"},
            outcome="revoked",
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.service.state["signoffs"]["enterprise-sign"]["current_status"], "revoked_or_needs_new_evidence")
        self.assertEqual(self.service.state["signoffs"]["enterprise-sign"]["status"], "sent")

    def test_later_progress_does_not_backfill_correction_time_review(self):
        service = AccreditationService(learner_name="林霁")
        service.add_standard(enterprise_standard())
        service.add_record({"id": "r1", "facts": [fact("f1", "missing_value_check", "op1", time_index=1)]})
        service.add_record({"id": "r2", "facts": [fact("f2", "exception_backfill", "op2", time_index=2)]})
        service.sign_off(
            signoff_id="ent",
            group="enterprise",
            recipient="企业技能组",
            standard_id="enterprise",
            signed_at=2,
        )
        # Thursday evidence is already in the ledger when the Wednesday
        # correction is processed out of order.
        service.add_record({"id": "r4", "facts": [fact("f4", "exception_backfill", "op4", time_index=4)]})
        correction = service.clarify_attribution(
            decision_id="dec",
            issuer={"role": "教务员"},
            issued_at=3,
            target={"fact_id": "f2"},
            attribution={"actor_kind": "peer"},
        )
        review = service.state["reviews"][correction["review_ids"][0]]
        self.assertEqual(review["review_as_of"], 3)
        self.assertFalse(review["remaining_evaluation"]["feasible"])
        self.assertTrue(review["pending_conclusions"])

        later = service.review_signoff("ent", as_of=4)
        self.assertTrue(later["remaining_evaluation"]["feasible"])
        self.assertEqual(later["pending_conclusions"], [])


class InterpretationTests(unittest.TestCase):
    def test_interpretation_scope_separates_intro_recommendation_from_certification(self):
        service = AccreditationService()
        service.add_standard(
            {
                "id": "ent",
                "group": "enterprise",
                "alternatives": [
                    {"id": "direct", "requirements": [{"id": "d", "task": "exception_backfill"}]},
                    {
                        "id": "rerun-alt",
                        "requirements": [
                            {"id": "r", "key": "independent_rerun", "task": "independent_rerun"},
                            {"id": "e", "task": "explain_basis"},
                        ],
                    },
                ],
            }
        )
        service.issue_interpretation(
            issuer={"id": "owner", "role": "标准负责人"},
            text="复跑替代只适用于入门推荐",
            scope={
                "group": "enterprise",
                "purposes": ["enterprise_intro_recommendation", "enterprise_certification"],
            },
            rule={
                "type": "restrict_alternative",
                "alternative_key": "independent_rerun",
                "allowed_purposes": ["enterprise_intro_recommendation"],
                "denied_purposes": ["enterprise_certification"],
            },
        )
        intro, intro_applied = service.prepared_standard(
            "ent", group="enterprise", purpose="enterprise_intro_recommendation"
        )
        cert, cert_applied = service.prepared_standard(
            "ent", group="enterprise", purpose="enterprise_certification"
        )
        self.assertEqual([a["id"] for a in intro["alternatives"]], ["direct", "rerun-alt"])
        self.assertEqual([a["id"] for a in cert["alternatives"]], ["direct"])
        self.assertTrue(intro_applied)
        self.assertTrue(cert_applied)


class SupplementTests(unittest.TestCase):
    def test_supplement_identifies_distinct_operation_gap(self):
        service = AccreditationService()
        service.add_standard(enterprise_standard())
        service.add_record(
            {
                "id": "same-op",
                "facts": [
                    fact("check", "missing_value_check", "op1"),
                    fact("backfill-same", "exception_backfill", "op1"),
                ],
            }
        )
        supplement = service.submit_supplement(
            {
                "id": "new-evidence",
                "facts": [fact("backfill-other", "exception_backfill", "op2", record_id="new-evidence")],
            },
            standard_requests=[{"standard_id": "enterprise"}],
        )
        analysis = supplement["analyses"][0]
        self.assertFalse(analysis["before_feasible"])
        self.assertTrue(analysis["after_feasible"])
        self.assertTrue(any(x["gap_type"] == "distinct_operation" for x in analysis["filled_by_supplement"]))

    def test_unreferenced_old_evidence_can_form_new_combination(self):
        service = AccreditationService()
        service.add_standard(enterprise_standard())
        service.add_record({"id": "used-check", "facts": [fact("used-check-f", "missing_value_check", "used-op1")]})
        service.add_record({"id": "used-backfill", "facts": [fact("used-backfill-f", "exception_backfill", "used-op2")]})
        service.add_record({"id": "unused-check", "facts": [fact("unused-check-f", "missing_value_check", "new-op3")]})
        service.add_record({"id": "unused-backfill", "facts": [fact("unused-backfill-f", "exception_backfill", "new-op4")]})
        result = service.submit_supplement(
            {
                "id": "student-note",
                "facts": [fact("explain", "explain_basis", "op5", independent=False)],
            },
            referenced_record_ids=["used-check", "used-backfill"],
            standard_requests=[{"standard_id": "enterprise"}],
        )
        analysis = result["analyses"][0]
        self.assertIn("unused-check", result["previously_unreferenced_record_ids"])
        self.assertIn("unused-backfill", result["previously_unreferenced_record_ids"])
        self.assertTrue(analysis["unreferenced_only_combinations"])
        combo = analysis["unreferenced_only_combinations"][0]
        self.assertEqual(set(combo["record_ids"]), {"unused-check", "unused-backfill"})


class CorpusDemoTests(unittest.TestCase):
    def test_bundled_scenario_covers_review_supplement_and_new_signoffs(self):
        result = run_demo()
        self.assertEqual(
            result["initial_feasibility"],
            {"school_s1": False, "enterprise_e1": False},
        )
        self.assertEqual(len(result["reviews"]), 2)
        self.assertTrue(all(r["pending_conclusions"] for r in result["reviews"]))
        analyses = {a["standard_id"]: a for a in result["supplement"]["analyses"]}
        self.assertFalse(analyses["school-s1"]["before_feasible"])
        self.assertTrue(analyses["school-s1"]["after_feasible"])
        self.assertFalse(analyses["enterprise-e1"]["before_feasible"])
        self.assertTrue(analyses["enterprise-e1"]["after_feasible"])
        self.assertEqual(len(result["new_signoffs"]), 2)
        enterprise = next(s for s in result["new_signoffs"] if s["group"] == "enterprise")
        self.assertEqual(set(enterprise["operations"]), {"obs-1:op-day1", "obs-4:op-day4"})


class PersistenceTests(unittest.TestCase):
    def test_state_can_be_reopened_for_repeat_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "case.json"
            service = AccreditationService.open(path)
            service.add_standard(school_standard())
            service.add_record({"id": "r1", "facts": [fact("f1", "missing_value_check", "op1")]})
            service.save()

            reopened = AccreditationService.open(path)
            self.assertIn("school", reopened.state["standards"])
            self.assertIn("f1", reopened.state["facts"])
            self.assertEqual(reopened.evaluate("school")["standard_id"], "school")


if __name__ == "__main__":
    unittest.main()
