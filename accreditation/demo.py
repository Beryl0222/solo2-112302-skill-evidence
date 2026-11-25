"""End-to-end synthetic workshop scenario used by the CLI and README."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .service import AccreditationService

ROOT = Path(__file__).resolve().parent.parent


def _compact_review(review: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": review["id"],
        "signoff_id": review["signoff_id"],
        "group": review["group"],
        "recipient": review["recipient"],
        "status": review["status"],
        "pending_conclusions": review["pending_conclusions"],
        "supported_conclusions": review["supported_conclusions"],
        "suggested_outcome": review["suggested_outcome"],
        "feasible_remaining_standard": bool(
            review.get("remaining_evaluation") and review["remaining_evaluation"].get("feasible")
        ),
        "gaps": (review.get("remaining_evaluation") or {}).get("gaps", []),
    }


def run_demo(*, state_path: str | Path | None = None) -> Dict[str, Any]:
    corpus = json.loads((ROOT / "data" / "corpus.json").read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in corpus["records"]}

    service = AccreditationService(learner_name="林霁", state_path=state_path)
    service.add_standard(
        {**by_id["school-s1"], "group": "school", "purpose": "school_credit"}
    )
    service.add_standard(
        {**by_id["enterprise-e1"], "group": "enterprise", "purpose": "enterprise_certification"}
    )
    service.adopt_standard("school", "school-s1", purpose="school_credit")
    service.adopt_standard("enterprise", "enterprise-e1", purpose="enterprise_certification")

    for rid in ("obs-1", "obs-2", "obs-3"):
        service.add_record(by_id[rid])
    service.link_operations(
        ["obs-2:op-day2", "op-day2-session2"],
        canonical_operation_id="op-tue-second-rerun",
    )

    school_handoff = service.import_handoff_text(
        by_id["handoff-1"]["text"], record_id="handoff-1"
    )
    enterprise_handoff = service.import_sent_signoff(
        {
            "id": "enterprise-recommendation-tue",
            "group": "enterprise",
            "purpose": "enterprise_recommendation",
            "recipient": "企业技能组",
            "standard_id": "enterprise-e1",
            "evidence_record_ids": ["obs-1", "obs-3"],
            "granted_competency_ids": ["enterprise-e1"],
            "text": "周二企业入门推荐签认|原评语 obs-1、obs-3，拟用 enterprise-e1。",
        }
    )

    initial = {
        "school": service.evaluate(
            "school-s1", group="school", purpose="school_credit", as_of=2
        ),
        "enterprise": service.evaluate(
            "enterprise-e1", group="enterprise", purpose="enterprise_certification", as_of=2
        ),
    }

    correction = service.clarify_attribution(
        decision_id="op-1",
        issuer={"id": "教务员", "role": "教务员"},
        issued_at="周三",
        target={"operation_id": "op-tue-second-rerun", "task": "exception_backfill"},
        attribution={"actor": "罗闻", "actor_kind": "peer"},
        source_text=by_id["op-1"]["text"],
    )
    interpretation = service.issue_interpretation(
        decision_id="op-2",
        issuer={"id": "标准负责人", "role": "标准负责人"},
        issued_at="周三",
        text=by_id["op-2"]["text"],
        standard_id="enterprise-e1",
        scope={
            "group": "enterprise",
            "purposes": [
                "enterprise_intro_recommendation",
                "enterprise_recommendation",
                "enterprise_certification",
            ],
        },
        rule={
            "type": "restrict_alternative",
            "alternative_key": "independent_rerun",
            "allowed_purposes": ["enterprise_intro_recommendation"],
            "denied_purposes": ["enterprise_recommendation", "enterprise_certification"],
        },
    )

    reviews = [_compact_review(service.state["reviews"][rid]) for rid in correction["review_ids"]]

    supplement = service.submit_supplement(
        by_id["obs-4"],
        standard_requests=[
            {"standard_id": "school-s1", "group": "school", "purpose": "school_credit", "as_of": 4},
            {
                "standard_id": "enterprise-e1",
                "group": "enterprise",
                "purpose": "enterprise_certification",
                "as_of": 4,
            },
        ],
    )

    new_school = service.sign_off(
        signoff_id="school-credit-thu",
        group="school",
        recipient="教务处",
        standard_id="school-s1",
        purpose="school_credit",
        signed_at="周四",
        as_of=4,
    )
    new_enterprise = service.sign_off(
        signoff_id="enterprise-certification-thu",
        group="enterprise",
        recipient="企业技能组",
        standard_id="enterprise-e1",
        purpose="enterprise_certification",
        signed_at="周四",
        as_of=4,
    )

    if state_path:
        service.save()

    return {
        "case_id": service.state["case_id"],
        "initial_feasibility": {
            "school_s1": initial["school"]["feasible"],
            "enterprise_e1": initial["enterprise"]["feasible"],
        },
        "imported_sent_signoffs": [school_handoff["id"], enterprise_handoff["id"]],
        "correction": {
            "id": correction["id"],
            "affected_fact_ids": correction["affected_fact_ids"],
            "review_ids": correction["review_ids"],
        },
        "interpretation": {
            "id": interpretation["id"],
            "scope": interpretation["scope"],
        },
        "reviews": reviews,
        "review_notices": service.state["notices"],
        "supplement": {
            "record_id": supplement["supplement_record_id"],
            "previously_unreferenced_record_ids": supplement[
                "previously_unreferenced_record_ids"
            ],
            "analyses": [
                {
                    "standard_id": a["standard_id"],
                    "before_feasible": a["before_feasible"],
                    "after_feasible": a["after_feasible"],
                    "filled_by_supplement": [
                        {
                            k: v
                            for k, v in item.items()
                            if k not in {"combination", "resolved_by_combination"}
                        }
                        for item in a["filled_by_supplement"]
                    ],
                    "valid_record_combinations": [
                        c["record_ids"] for c in a["classified_combinations"]
                    ],
                    "unreferenced_only_combinations": [
                        c["record_ids"] for c in a["unreferenced_only_combinations"]
                    ],
                }
                for a in supplement["analyses"]
            ],
        },
        "new_signoffs": [
            {
                "id": new_school["id"],
                "group": new_school["group"],
                "recipient": new_school["recipient"],
                "competencies": new_school["granted_competency_ids"],
                "records": new_school["combination"]["record_ids"],
                "operations": new_school["combination"]["operation_ids"],
            },
            {
                "id": new_enterprise["id"],
                "group": new_enterprise["group"],
                "recipient": new_enterprise["recipient"],
                "competencies": new_enterprise["granted_competency_ids"],
                "records": new_enterprise["combination"]["record_ids"],
                "operations": new_enterprise["combination"]["operation_ids"],
            },
        ],
        "state_saved_to": str(state_path) if state_path else None,
    }
