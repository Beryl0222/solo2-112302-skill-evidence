"""Stateful, repeatable accreditation office workflow."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from .engine import (
    enumerate_combinations,
    evaluate_standard,
    find_capability_evidence,
    normalize_standard,
)
from .errors import NotFoundError, PermissionDeniedError, QualificationError, ValidationError
from .parsing import parse_comment, weekday_index

ACADEMIC_ROLES = {
    "academic_registrar",
    "academic_affairs",
    "academic_admin",
    "academic",
    "teaching_affairs",
    "registrar",
    "school_affairs",
    "教务员",
    "教务",
    "教务老师",
    "学分组",
}
STANDARD_OWNER_ROLES = {
    "standard_owner",
    "standards_owner",
    "standard_manager",
    "standards_manager",
    "标准负责人",
    "标准管理员",
}
REVIEWER_ROLES = ACADEMIC_ROLES | STANDARD_OWNER_ROLES | {
    "enterprise_reviewer",
    "enterprise",
    "企业技能组",
    "学分组",
}


def new_state(case_id: Optional[str] = None, learner_name: str = "学员") -> Dict[str, Any]:
    return {
        "schema": "accreditation.state/v1",
        "case_id": case_id or "case-1",
        "learner_name": learner_name,
        "counter": 0,
        "records": {},
        "record_order": [],
        "facts": {},
        "fact_order": [],
        "standards": {},
        "standard_order": [],
        "adoptions": [],
        "decisions": [],
        "signoffs": {},
        "signoff_order": [],
        "reviews": {},
        "review_order": [],
        "notices": [],
        "operation_aliases": {},
    }


def process_round(
    *,
    comments: Optional[Iterable[Dict[str, Any]]] = None,
    records: Optional[Iterable[Dict[str, Any]]] = None,
    standards: Optional[Iterable[Dict[str, Any]]] = None,
    standards_by_group: Optional[Dict[str, Union[Dict[str, Any], Iterable[Dict[str, Any]]]]] = None,
    learner_name: str = "学员",
    as_of: Any = None,
    state_path: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Import one round and evaluate every automatically adopted group."""
    office = AccreditationService.from_round(
        comments=comments,
        records=records,
        standards=standards,
        standards_by_group=standards_by_group,
        learner_name=learner_name,
        state_path=state_path,
    )
    group_reports = {}
    for group in (standards_by_group or {}).keys():
        group_reports[group] = office.evaluate_group(group, as_of=as_of)
    return {"office": office, "groups": group_reports}


class AccreditationService:
    """Manage comments, standards, sign-offs, corrections and gap reviews.

    The class is deliberately JSON-friendly: ``state`` contains only mappings,
    lists and scalar values.  Use :meth:`save` / :meth:`open` for repeatable
    file-based processing.
    """

    def __init__(
        self,
        state: Optional[Dict[str, Any]] = None,
        *,
        state_path: Optional[Union[str, Path]] = None,
        learner_name: Optional[str] = None,
    ) -> None:
        self.state_path = Path(state_path) if state_path else None
        self.state = state or new_state(learner_name=learner_name or "学员")
        if learner_name:
            self.state["learner_name"] = learner_name

    @classmethod
    def open(cls, path: Union[str, Path]) -> "AccreditationService":
        path = Path(path)
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
        else:
            state = new_state()
        return cls(state, state_path=path)

    def save(self, path: Optional[Union[str, Path]] = None) -> Path:
        target = Path(path) if path else self.state_path
        if target is None:
            raise ValidationError("no state path was supplied")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
        self.state_path = target
        return target

    @classmethod
    def from_round(
        cls,
        *,
        comments: Optional[Iterable[Dict[str, Any]]] = None,
        records: Optional[Iterable[Dict[str, Any]]] = None,
        standards: Optional[Iterable[Dict[str, Any]]] = None,
        standards_by_group: Optional[Dict[str, Union[Dict[str, Any], Iterable[Dict[str, Any]]]]] = None,
        learner_name: str = "学员",
        state_path: Optional[Union[str, Path]] = None,
    ) -> "AccreditationService":
        """Create an office and import one new round in a single call.

        ``standards_by_group`` may map ``school``/``enterprise`` to one
        standard mapping or a list of mappings; adoptions are created
        automatically for those groups.
        """
        service = cls(learner_name=learner_name, state_path=state_path)
        all_standards = list(standards or [])
        adoptions: List[Dict[str, Any]] = []
        for group, group_standards in (standards_by_group or {}).items():
            if isinstance(group_standards, dict):
                group_standards = [group_standards]
            for std in group_standards or []:
                std = copy.deepcopy(std)
                std.setdefault("group", group)
                all_standards.append(std)
                adoptions.append(
                    {"group": group, "standard_id": std.get("id") or std.get("standard_id")}
                )
        service.add_round(
            records=records,
            comments=comments,
            standards=all_standards,
            adoptions=adoptions,
        )
        return service

    # ------------------------------------------------------------- ids
    def _next(self, prefix: str) -> str:
        self.state["counter"] += 1
        return f"{prefix}-{self.state['counter']}"

    def _learner(self) -> str:
        return self.state.get("learner_name") or "学员"

    # ------------------------------------------------------------- intake
    def add_competency(self, competency: Dict[str, Any]) -> Dict[str, Any]:
        cid = competency.get("id") or competency.get("competency_id")
        if not cid:
            raise ValidationError("competency requires id")
        item = {
            "id": cid,
            "kind": competency.get("kind", "professional"),
            "label": competency.get("label", cid),
            "description": competency.get("description", ""),
            "explicit_relations": competency.get("explicit_relations", []),
        }
        self.state.setdefault("competencies", {})[cid] = item
        return item

    def add_standard(self, standard: Dict[str, Any]) -> Dict[str, Any]:
        std = normalize_standard(standard)
        sid = std["id"]
        # Preserve the caller's original wording separately.
        std["original_input"] = copy.deepcopy(standard)
        std.setdefault("group", standard.get("group"))
        std.setdefault("purpose", standard.get("purpose"))
        std.setdefault("version", standard.get("version", "1"))
        if sid not in self.state["standards"]:
            self.state["standard_order"].append(sid)
        self.state["standards"][sid] = std
        return std

    def adopt_standard(
        self,
        group: str,
        standard_id: str,
        *,
        purpose: Optional[str] = None,
        adopted_at: Any = None,
    ) -> Dict[str, Any]:
        if standard_id not in self.state["standards"]:
            raise NotFoundError(f"unknown standard: {standard_id}")
        adoption = {
            "group": group,
            "standard_id": standard_id,
            "purpose": purpose,
            "adopted_at": adopted_at,
        }
        self.state["adoptions"].append(adoption)
        return adoption

    def add_record(
        self,
        record: Union[str, Dict[str, Any]],
        *,
        parse_text: bool = True,
        peer_names: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if isinstance(record, str):
            record = {"text": record}
        rid = record.get("id") or record.get("record_id") or self._next("rec")
        if rid in self.state["records"]:
            raise ValidationError(f"record already exists: {rid}")

        stored = copy.deepcopy(record)
        stored["id"] = rid
        self.state["records"][rid] = stored
        self.state["record_order"].append(rid)

        explicit_facts = record.get("facts")
        if explicit_facts:
            for raw_fact in explicit_facts:
                fact = copy.deepcopy(raw_fact)
                fact["record_id"] = rid
                self._store_fact(fact)
        elif parse_text and (record.get("text") or record.get("content")) and record.get("kind") not in {
            "standard",
            "decision",
            "interpretation",
            "handoff",
            "opinion",
        }:
            parsed = parse_comment(
                record,
                learner_name=record.get("learner_name") or self._learner(),
                peer_names=peer_names or record.get("peer_names"),
            )
            for fact in parsed["facts"]:
                fact.pop("local_fact_id", None)
                self._store_fact(fact)

        return stored

    def _store_fact(self, fact: Dict[str, Any]) -> Dict[str, Any]:
        fid = fact.get("fact_id") or fact.get("id") or self._next("fact")
        fact["fact_id"] = fid
        fact.setdefault("status", "active")
        fact.setdefault("revisions", [])
        # Apply a previously registered alias if one exists.
        op = fact.get("operation_id")
        if op and op in self.state.get("operation_aliases", {}):
            fact["operation_id"] = self.state["operation_aliases"][op]
        self.state["facts"][fid] = fact
        if fid not in self.state["fact_order"]:
            self.state["fact_order"].append(fid)
        return fact

    def add_round(
        self,
        *,
        round_id: Optional[str] = None,
        records: Optional[Iterable[Dict[str, Any]]] = None,
        comments: Optional[Iterable[Dict[str, Any]]] = None,
        standards: Optional[Iterable[Dict[str, Any]]] = None,
        adoptions: Optional[Iterable[Dict[str, Any]]] = None,
        peer_names: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Bring in one new round of comments and adopted standards."""
        round_id = round_id or self._next("round")
        summary = {"round_id": round_id, "record_ids": [], "standard_ids": []}
        for std in standards or []:
            stored = self.add_standard(std)
            summary["standard_ids"].append(stored["id"])
        for adoption in adoptions or []:
            self.adopt_standard(**adoption)
        for record in list(records or []) + list(comments or []):
            stored = self.add_record(record, peer_names=peer_names)
            summary["record_ids"].append(stored["id"])
        if summary["record_ids"]:
            summary["operation_merges"] = self.resolve_operation_identities()
        self.state.setdefault("rounds", {})[round_id] = summary
        return summary

    def ingest_corpus(self, corpus: Dict[str, Any]) -> Dict[str, Any]:
        """Import the repository's synthetic corpus shape."""
        ids = {"records": [], "standards": [], "signoffs": []}
        for record in corpus.get("records", []):
            kind = record.get("kind")
            if kind == "standard":
                std = self.add_standard(
                    {
                        "id": record["id"],
                        "text": record["text"],
                        "group": "school" if record["id"].startswith("school") else "enterprise",
                    }
                )
                ids["standards"].append(std["id"])
            elif kind == "handoff":
                self.add_record(record, parse_text=False)
                signoff = self.import_handoff_text(record["text"], record_id=record["id"])
                ids["signoffs"].append(signoff["id"])
                ids["records"].append(record["id"])
            else:
                self.add_record(record)
                ids["records"].append(record["id"])
        ids["operation_merges"] = self.resolve_operation_identities()
        return ids

    def link_operations(
        self,
        operation_ids: Sequence[str],
        *,
        canonical_operation_id: Optional[str] = None,
    ) -> str:
        """Treat several operation labels/ids as the same real operation."""
        if not operation_ids:
            raise ValidationError("operation_ids must not be empty")
        canonical = canonical_operation_id or operation_ids[0]
        for alias in operation_ids:
            self.state["operation_aliases"][alias] = canonical
            for fact in self.state["facts"].values():
                if fact.get("operation_id") == alias:
                    fact["operation_id"] = canonical
        return canonical

    def resolve_operation_identities(self) -> List[str]:
        """Best-effort resolution of obvious cross-record operation references.

        Structured ``operation_id`` values are authoritative.  This resolver
        only merges parser-generated day/session identities and conservative
        “same round/session” references; ambiguous cases should use
        :meth:`link_operations` or a registrar attribution clarification.
        """

        merged: List[str] = []

        def op_info(op_id: str) -> Dict[str, Any]:
            fs = [f for f in self.state["facts"].values() if f.get("operation_id") == op_id]
            days = {f.get("time_index") for f in fs if f.get("time_index") is not None}
            return {
                "op_id": op_id,
                "facts": fs,
                "day": next(iter(days)) if len(days) == 1 else None,
                "tasks": {f.get("task") for f in fs},
                "has_rerun": any(f.get("task") == "independent_rerun" for f in fs),
                "hearsay": all(f.get("mode") == "hearsay" for f in fs) if fs else False,
                "same_round_ref": any(
                    f.get("operation_ref") and "同一" in str(f.get("operation_ref")) for f in fs
                ),
                "canonical_session": bool(re.fullmatch(r"op-day\d+-session\d+", op_id)),
            }

        op_ids = list({f.get("operation_id") for f in self.state["facts"].values() if f.get("operation_id")})
        infos = [op_info(op) for op in op_ids]

        # Explicit “周二第二场” anchors can absorb a same-day rerun record.
        for anchor in infos:
            if not anchor["canonical_session"] or anchor["day"] is None:
                continue
            for other in infos:
                if other["op_id"] == anchor["op_id"] or other["day"] != anchor["day"]:
                    continue
                if other["canonical_session"]:
                    continue
                overlap = anchor["tasks"] & other["tasks"]
                if overlap and (anchor["has_rerun"] and other["has_rerun"] or len(overlap) >= 2):
                    self.link_operations([other["op_id"]], canonical_operation_id=anchor["op_id"])
                    merged.append(f"{other['op_id']}->{anchor['op_id']}")

        # Rebuild after merges, then resolve explicit “同一轮” hearsay refs to
        # one unique direct same-day operation with overlapping task(s).
        op_ids = list({f.get("operation_id") for f in self.state["facts"].values() if f.get("operation_id")})
        infos = [op_info(op) for op in op_ids]
        for hearsay in infos:
            if not hearsay["hearsay"] or not hearsay["same_round_ref"] or hearsay["day"] is None:
                continue
            candidates = []
            for direct in infos:
                if direct["op_id"] == hearsay["op_id"] or direct["day"] != hearsay["day"]:
                    continue
                if direct["hearsay"]:
                    continue
                if hearsay["tasks"] & direct["tasks"]:
                    candidates.append(direct["op_id"])
            if len(candidates) == 1:
                target = candidates[0]
                self.link_operations([hearsay["op_id"]], canonical_operation_id=target)
                merged.append(f"{hearsay['op_id']}->{target}")

        return sorted(set(merged))

    # ------------------------------------------------------------- facts
    def active_facts(
        self,
        *,
        as_of: Any = None,
        record_ids: Optional[Sequence[str]] = None,
        fact_ids: Optional[Sequence[str]] = None,
        operation_ids: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        rid_set = set(record_ids or [])
        fid_set = set(fact_ids or [])
        op_set = set(operation_ids or [])
        facts = []
        for fid in self.state["fact_order"]:
            fact = self.state["facts"][fid]
            if fact.get("status") != "active":
                continue
            if rid_set and fact.get("record_id") not in rid_set:
                continue
            if fid_set and fid not in fid_set:
                continue
            if op_set and fact.get("operation_id") not in op_set:
                continue
            if as_of is not None:
                cutoff = weekday_index(as_of) if not isinstance(as_of, (int, float)) else as_of
                fi = fact.get("time_index")
                if cutoff is not None and fi is not None and fi > cutoff:
                    continue
            facts.append(fact)
        return facts

    # ----------------------------------------------------- interpretations
    def _role(self, issuer: Any, issuer_role: Optional[str] = None) -> str:
        if isinstance(issuer, dict):
            role = issuer.get("role") or issuer.get("actor_role") or issuer.get("role_id")
            return str(role or issuer_role or "")
        return str(issuer_role or "")

    def _require_role(self, issuer: Any, issuer_role: Optional[str], allowed: set[str]) -> Dict[str, str]:
        role = self._role(issuer, issuer_role)
        normalized = {role, role.strip()}
        if role not in allowed:
            raise PermissionDeniedError(
                f"role {role!r} is not permitted; required one of {sorted(allowed)}"
            )
        if isinstance(issuer, dict):
            issuer_id = issuer.get("id") or issuer.get("name") or role
        else:
            issuer_id = issuer or role
        return {"id": str(issuer_id), "role": role}

    def issue_interpretation(
        self,
        *,
        interpretation_id: Optional[str] = None,
        decision_id: Optional[str] = None,
        issuer: Any = None,
        issuer_role: Optional[str] = None,
        text: str = "",
        rule: Optional[Dict[str, Any]] = None,
        scope: Optional[Dict[str, Any]] = None,
        standard_id: Optional[str] = None,
        issued_at: Any = None,
    ) -> Dict[str, Any]:
        actor = self._require_role(issuer, issuer_role, STANDARD_OWNER_ROLES)
        if isinstance(scope, str):
            scope_text = scope
            if scope in {"school", "enterprise"}:
                scope = {"group": scope}
            elif scope in self.state["standards"]:
                scope = {"standard_ids": [scope]}
            else:
                scope = {"description": scope_text}
        if not scope:
            raise ValidationError("标准解释必须带作用范围 scope")
        if not any(scope.get(k) for k in ("groups", "group", "purposes", "purpose", "standard_ids", "standard_id", "conclusion_kinds", "description")):
            raise ValidationError("scope must identify at least one group, purpose/conclusion, standard, or description")
        decision = {
            "id": interpretation_id or decision_id or self._next("interp"),
            "type": "standard_interpretation",
            "issuer": actor,
            "issued_at": issued_at,
            "text": text,
            "rule": rule or {},
            "scope": scope,
            "standard_id": standard_id or scope.get("standard_id"),
        }
        self.state["decisions"].append(decision)
        return decision

    def _interpretation_matches(
        self,
        decision: Dict[str, Any],
        standard: Dict[str, Any],
        *,
        group: Optional[str],
        purpose: Optional[str],
    ) -> bool:
        scope = decision.get("scope") or {}
        sid = standard["id"]
        std_ids = scope.get("standard_ids") or ([scope["standard_id"]] if scope.get("standard_id") else [])
        if decision.get("standard_id"):
            std_ids = list(std_ids) + [decision["standard_id"]]
        if std_ids and sid not in std_ids and "*" not in std_ids:
            return False
        groups = scope.get("groups") or ([scope["group"]] if scope.get("group") else [])
        if groups and group not in groups and standard.get("group") not in groups:
            return False
        purposes = scope.get("purposes") or scope.get("purpose") or scope.get("conclusion_kinds") or []
        if isinstance(purposes, str):
            purposes = [purposes]
        if purposes and purpose not in purposes:
            return False
        return True

    def prepared_standard(
        self,
        standard_id: str,
        *,
        group: Optional[str] = None,
        purpose: Optional[str] = None,
    ) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        if standard_id not in self.state["standards"]:
            raise NotFoundError(f"unknown standard: {standard_id}")
        standard = copy.deepcopy(self.state["standards"][standard_id])
        applied: List[Dict[str, Any]] = []
        for decision in self.state["decisions"]:
            if decision.get("type") != "standard_interpretation":
                continue
            if not self._interpretation_matches(decision, standard, group=group, purpose=purpose):
                continue
            rule = decision.get("rule") or {}
            rtype = rule.get("type")
            if rtype in {"disable_alternative", "restrict_alternative"}:
                alt_id = rule.get("alternative_id")
                alt_key = rule.get("alternative_key")
                allowed_purposes = rule.get("allowed_purposes")
                denied_purposes = rule.get("denied_purposes") or []
                remove = False
                if rtype == "disable_alternative":
                    remove = True
                elif rtype == "restrict_alternative":
                    if allowed_purposes and purpose not in allowed_purposes:
                        remove = True
                    if purpose in denied_purposes:
                        remove = True
                if remove:
                    before = len(standard.get("alternatives", []))
                    standard["alternatives"] = [
                        alt
                        for alt in standard.get("alternatives", [])
                        if not (
                            (alt_id and alt.get("id") == alt_id)
                            or (
                                alt_key
                                and any((req.get("key") == alt_key or req.get("task") == alt_key) for req in alt.get("requirements", []))
                            )
                        )
                    ]
                    if len(standard["alternatives"]) != before:
                        applied.append({"decision_id": decision["id"], "effect": "removed_alternative", "rule": rule})
                        continue
            applied.append({"decision_id": decision["id"], "effect": "scope_recorded", "rule": rule})
        return standard, applied

    # ----------------------------------------------------- attribution
    def clarify_attribution(
        self,
        target: Dict[str, Any],
        attribution: Optional[Dict[str, Any]] = None,
        *,
        issuer: Any = None,
        issuer_role: Optional[str] = None,
        decision_id: Optional[str] = None,
        issued_at: Any = None,
        reason: str = "",
        source_text: str = "",
        create_reviews: bool = True,
        review_signoff_ids: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        actor = self._require_role(issuer, issuer_role, ACADEMIC_ROLES)
        if isinstance(target, str):
            target = {"fact_id": target}
        if not target:
            raise ValidationError("target is required for attribution clarification")
        attribution = attribution or {}
        facts = self.find_facts(target)
        if not facts:
            raise NotFoundError(f"no facts match correction target: {target}")

        decision = {
            "id": decision_id or self._next("dec"),
            "type": "attribution_clarification",
            "issuer": actor,
            "issued_at": issued_at,
            "target": copy.deepcopy(target),
            "attribution": copy.deepcopy(attribution),
            "reason": reason,
            "source_text": source_text,
            "affected_fact_ids": [f["fact_id"] for f in facts],
            "review_ids": [],
        }

        for fact in facts:
            before = {
                k: fact.get(k)
                for k in ("actor", "actor_kind", "qualifies_learner", "independence", "completed", "task", "operation_id", "mode", "status")
            }
            fact.setdefault("revisions", []).append(
                {"decision_id": decision["id"], "issued_at": issued_at, "before": before}
            )
            changes = copy.deepcopy(attribution)
            if "actor" in changes and "actor_kind" not in changes:
                if changes["actor"] == self._learner():
                    changes["actor_kind"] = "learner"
                elif changes["actor"] in {"同伴", "队友"} or changes["actor"] in self._peer_names():
                    changes["actor_kind"] = "peer"
            if changes.get("actor_kind") == "learner":
                changes.setdefault("qualifies_learner", True)
            if changes.get("actor_kind") == "peer":
                changes.setdefault("qualifies_learner", False)
            if changes.get("invalidated"):
                changes["status"] = "excluded"
                changes.pop("invalidated")
            for key, value in changes.items():
                fact[key] = value

        self.state["decisions"].append(decision)
        if create_reviews:
            reviews = self.start_reviews_for_correction(
                decision["id"], signoff_ids=review_signoff_ids
            )
            decision["review_ids"] = [r["id"] for r in reviews]
        return decision

    def _peer_names(self) -> set[str]:
        names = {"同伴", "队友", "旁人"}
        for fact in self.state["facts"].values():
            if fact.get("actor_kind") == "peer" and fact.get("actor"):
                names.add(fact["actor"])
        return names

    def find_facts(self, target: Dict[str, Any]) -> List[Dict[str, Any]]:
        fact_ids = set(target.get("fact_ids") or [])
        if target.get("fact_id"):
            fact_ids.add(target["fact_id"])
        record_ids = set(target.get("record_ids") or [])
        if target.get("record_id"):
            record_ids.add(target["record_id"])
        op_ids = set(target.get("operation_ids") or [])
        if target.get("operation_id"):
            op_ids.add(target["operation_id"])
        tasks = set(target.get("tasks") or [])
        if target.get("task"):
            tasks.add(target["task"])
        span_contains = target.get("span_contains")

        matches = []
        for fact in self.state["facts"].values():
            if fact_ids and fact["fact_id"] not in fact_ids:
                continue
            if record_ids and fact.get("record_id") not in record_ids:
                continue
            if op_ids and fact.get("operation_id") not in op_ids:
                continue
            if tasks and fact.get("task") not in tasks:
                continue
            if span_contains and span_contains not in (fact.get("span") or ""):
                continue
            matches.append(fact)
        return matches

    # ----------------------------------------------------------- evaluation
    def evaluate(
        self,
        standard_id: str,
        *,
        group: Optional[str] = None,
        purpose: Optional[str] = None,
        as_of: Any = None,
        record_ids: Optional[Sequence[str]] = None,
        fact_ids: Optional[Sequence[str]] = None,
        operation_ids: Optional[Sequence[str]] = None,
        include_hearsay: Optional[bool] = None,
    ) -> Dict[str, Any]:
        standard, interpretations = self.prepared_standard(
            standard_id, group=group, purpose=purpose
        )
        if include_hearsay is None:
            include_hearsay = bool(standard.get("allow_hearsay", True))
        facts = self.active_facts(
            as_of=as_of, record_ids=record_ids, fact_ids=fact_ids, operation_ids=operation_ids
        )
        report = evaluate_standard(
            facts, standard, as_of=as_of, include_hearsay=include_hearsay
        )
        report["group"] = group or standard.get("group")
        report["purpose"] = purpose
        report["applied_interpretations"] = interpretations
        report["evaluated_fact_ids"] = [f["fact_id"] for f in facts]
        return report

    def adopted_standard_ids(self, group: str, purpose: Optional[str] = None) -> List[str]:
        ids = []
        for adoption in self.state["adoptions"]:
            if adoption["group"] != group:
                continue
            if purpose is not None and adoption.get("purpose") not in (None, purpose):
                continue
            if adoption["standard_id"] not in ids:
                ids.append(adoption["standard_id"])
        if not ids:
            ids = [sid for sid, std in self.state["standards"].items() if std.get("group") == group]
        return ids

    def evaluate_group(
        self,
        group: Optional[str] = None,
        *,
        group_name: Optional[str] = None,
        purpose: Optional[str] = None,
        as_of: Any = None,
        standard_ids: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        group = group or group_name
        if not group:
            raise ValidationError("group is required")
        sids = list(standard_ids or self.adopted_standard_ids(group, purpose))
        reports = [
            self.evaluate(sid, group=group, purpose=purpose, as_of=as_of) for sid in sids
        ]
        return {
            "group": group,
            "purpose": purpose,
            "as_of": as_of,
            "standard_reports": reports,
            "feasible_standard_ids": [r["standard_id"] for r in reports if r["feasible"]],
        }

    def verify_competency(
        self,
        competency_id: str,
        *,
        group: Optional[str] = None,
        purpose: Optional[str] = None,
        as_of: Any = None,
    ) -> List[Dict[str, Any]]:
        standards = []
        for sid, std in self.state["standards"].items():
            if group and std.get("group") not in (None, group):
                continue
            prepared, _ = self.prepared_standard(sid, group=group, purpose=purpose)
            standards.append(prepared)
        return find_capability_evidence(
            self.active_facts(as_of=as_of), standards, competency_id, as_of=as_of
        )

    # ------------------------------------------------------------- signoff
    def sign_off(
        self,
        *,
        group: str,
        recipient: str,
        standard_id: str,
        signoff_id: Optional[str] = None,
        purpose: str = "qualification",
        competency_ids: Optional[List[str]] = None,
        combination_index: int = 0,
        signer: Any = None,
        signed_at: Any = None,
        as_of: Any = None,
        include_hearsay: Optional[bool] = None,
        message: str = "",
        source_record_ids: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        report = self.evaluate(
            standard_id,
            group=group,
            purpose=purpose,
            as_of=as_of,
            include_hearsay=include_hearsay,
        )
        if not report["feasible"]:
            raise QualificationError(
                json.dumps(
                    {
                        "message": "standard has no valid independent evidence combination",
                        "standard_id": standard_id,
                        "gaps": report["gaps"],
                    },
                    ensure_ascii=False,
                )
            )
        combos = report["valid_combinations"]
        if combination_index >= len(combos):
            raise ValidationError(f"combination_index out of range: {combination_index}")
        combo = combos[combination_index]
        granted = list(competency_ids or combo["competency_ids"])
        unsupported = [cid for cid in granted if cid not in combo["competency_ids"]]
        if unsupported:
            raise QualificationError(f"competencies not awarded by this combination: {unsupported}")

        sid = signoff_id or self._next("sign")
        qualification_id = f"qual:{group}:{purpose}:{standard_id}"
        standard_snapshot, interpretations = self.prepared_standard(
            standard_id, group=group, purpose=purpose
        )
        original_texts = {}
        source_ids = list(combo["record_ids"]) + list(source_record_ids or [])
        for rid in dict.fromkeys(source_ids):
            rec = self.state["records"].get(rid)
            if rec:
                original_texts[rid] = rec.get("text") or rec.get("content") or ""
        evidence_snapshot = [
            copy.deepcopy(self.state["facts"][fid])
            for fid in combo.get("all_evidence_fact_ids", combo["fact_ids"])
            if fid in self.state["facts"]
        ]
        signoff = {
            "id": sid,
            "kind": "signoff",
            "group": group,
            "purpose": purpose,
            "recipient": recipient,
            "signer": signer,
            "sent_at": signed_at,
            "message": message,
            "status": "sent",
            "current_status": "sent",
            "standard_id": standard_id,
            "qualification_id": qualification_id,
            "granted_competency_ids": granted,
            "combination": combo,
            "source_record_ids": list(dict.fromkeys(source_ids)),
            "original_texts": original_texts,
            "snapshot": {
                "standard": standard_snapshot,
                "applied_interpretations": interpretations,
                "as_of": as_of,
                "include_hearsay": report["include_hearsay"],
                "evidence": evidence_snapshot,
                "caliber": f"{group}:{purpose}:{standard_id}",
            },
            "pending_conclusions": [],
            "conclusion_statuses": {cid: "supported" for cid in granted},
            "review_ids": [],
            "history": [{"status": "sent", "at": signed_at}],
        }
        self.state["signoffs"][sid] = signoff
        self.state["signoff_order"].append(sid)
        self._notice(
            "signoff_sent",
            recipient,
            sid,
            f"签认 {sid} 已发送，结论：{', '.join(granted) or standard_id}",
        )
        return signoff

    def sign_capability(
        self,
        *,
        competency_id: str,
        group: str,
        recipient: str,
        signoff_id: Optional[str] = None,
        purpose: str = "capability_acknowledgement",
        standard_id: Optional[str] = None,
        match_index: int = 0,
        signer: Any = None,
        signed_at: Any = None,
        as_of: Any = None,
        message: str = "",
    ) -> Dict[str, Any]:
        """Sign one granular capability when direct requirement evidence exists.

        This does not award a whole standard/certification and therefore does
        not bypass AND/OR combinations or distinct-operation rules.
        """
        matches = self.verify_competency(
            competency_id, group=group, purpose=None, as_of=as_of
        )
        if standard_id:
            matches = [m for m in matches if m["standard_id"] == standard_id]
        if not matches:
            raise QualificationError(f"no qualifying evidence for competency {competency_id!r}")
        if match_index >= len(matches):
            raise ValidationError(f"match_index out of range: {match_index}")
        match = matches[match_index]
        sid = signoff_id or self._next("cap-sign")
        standard_snapshot, interpretations = self.prepared_standard(
            match["standard_id"], group=group, purpose=purpose
        )
        fact = copy.deepcopy(self.state["facts"][match["fact_id"]])
        record_id = match.get("record_id")
        original_texts = {}
        if record_id and record_id in self.state["records"]:
            original_texts[record_id] = self.state["records"][record_id].get("text", "")
        signoff = {
            "id": sid,
            "kind": "granular_capability_signoff",
            "group": group,
            "purpose": purpose,
            "recipient": recipient,
            "signer": signer,
            "sent_at": signed_at,
            "message": message,
            "status": "sent",
            "current_status": "sent",
            "standard_id": match["standard_id"],
            "qualification_id": None,
            "granted_competency_ids": [competency_id],
            "combination": {
                "granular": True,
                "fact_ids": [match["fact_id"]],
                "all_evidence_fact_ids": [match["fact_id"]],
                "record_ids": [record_id] if record_id else [],
                "operation_ids": [match.get("operation_id")],
                "competency_ids": [competency_id],
                "match": match,
            },
            "source_record_ids": [record_id] if record_id else [],
            "original_texts": original_texts,
            "snapshot": {
                "standard": standard_snapshot,
                "applied_interpretations": interpretations,
                "as_of": as_of,
                "evidence": [fact],
                "caliber": f"{group}:{purpose}:{match['standard_id']}:granular",
            },
            "pending_conclusions": [],
            "conclusion_statuses": {competency_id: "supported"},
            "review_ids": [],
            "history": [{"status": "sent", "at": signed_at}],
        }
        self.state["signoffs"][sid] = signoff
        self.state["signoff_order"].append(sid)
        self._notice(
            "capability_signed",
            recipient,
            sid,
            f"单项能力 {competency_id} 的签认 {sid} 已发送。",
        )
        return signoff

    def import_sent_signoff(self, signoff: Dict[str, Any]) -> Dict[str, Any]:
        sid = signoff.get("id") or self._next("sign")
        if sid in self.state["signoffs"]:
            raise ValidationError(f"signoff already exists: {sid}")
        standard_id = signoff.get("standard_id")
        snapshot_standard = None
        if standard_id and standard_id in self.state["standards"]:
            snapshot_standard, interps = self.prepared_standard(
                standard_id,
                group=signoff.get("group"),
                purpose=signoff.get("purpose"),
            )
        else:
            interps = []
        record_ids = signoff.get("evidence_record_ids") or signoff.get("record_ids") or []
        original_texts = {
            rid: self.state["records"][rid].get("text", "")
            for rid in record_ids
            if rid in self.state["records"]
        }
        granted = signoff.get("granted_competency_ids") or []
        stored = {
            "id": sid,
            "kind": "historical_signoff",
            "group": signoff.get("group", "unknown"),
            "purpose": signoff.get("purpose", "qualification"),
            "recipient": signoff.get("recipient", ""),
            "sent_at": signoff.get("sent_at"),
            "raw_text": signoff.get("text", ""),
            "status": "sent",
            "current_status": "sent",
            "standard_id": standard_id,
            "qualification_id": f"qual:{signoff.get('group')}:{signoff.get('purpose')}:{standard_id}",
            "evidence_record_ids": record_ids,
            "granted_competency_ids": granted,
            "claimed_qualified": signoff.get("claimed_qualified", True),
            "original_texts": original_texts,
            "snapshot": {
                "standard": snapshot_standard,
                "applied_interpretations": interps,
                "caliber": signoff.get("caliber") or f"{signoff.get('group')}:{signoff.get('purpose')}:{standard_id}",
                "legacy": True,
            },
            "pending_conclusions": [],
            "conclusion_statuses": {cid: "claimed" for cid in granted},
            "review_ids": [],
            "history": [{"status": "sent", "at": signoff.get("sent_at"), "legacy": True}],
        }
        self.state["signoffs"][sid] = stored
        self.state["signoff_order"].append(sid)
        return stored

    def import_handoff_text(self, text: str, *, record_id: Optional[str] = None) -> Dict[str, Any]:
        """Import the repository's compact handoff wording."""
        recipient_match = re.search(r"收件方([^，,。\s]+)", text)
        standard_match = re.search(r"(school-s\d+|enterprise-e\d+|[\w-]*s\d+|[\w-]*e\d+)", text.split("适用")[-1])
        ids = re.findall(r"(?:obs|op|rec)-\d+", text)
        standard_id = standard_match.group(1) if standard_match else None
        group = "school" if "校方" in text or "学分" in text else "enterprise" if "企业" in text else "unknown"
        return self.import_sent_signoff(
            {
                "id": record_id,
                "group": group,
                "recipient": recipient_match.group(1) if recipient_match else "",
                "text": text,
                "standard_id": standard_id,
                "evidence_record_ids": [x for x in ids if x.startswith("obs")],
                "granted_competency_ids": [],
                "purpose": "school_credit" if group == "school" else "enterprise_recommendation",
            }
        )

    # ------------------------------------------------------------- reviews
    def _signoff_affected_by_facts(self, signoff: Dict[str, Any], fact_ids: Sequence[str]) -> bool:
        fact_id_set = set(fact_ids)
        record_ids = set()
        op_ids = set()
        for fid in fact_ids:
            fact = self.state["facts"].get(fid)
            if fact:
                if fact.get("record_id"):
                    record_ids.add(fact["record_id"])
                if fact.get("operation_id"):
                    op_ids.add(fact["operation_id"])

        combo = signoff.get("combination") or {}
        combo_fact_ids = set(combo.get("all_evidence_fact_ids", [])) | set(combo.get("fact_ids", []))
        if fact_id_set & combo_fact_ids:
            return True
        if record_ids & set(combo.get("record_ids", [])):
            return True
        if op_ids & set(combo.get("operation_ids", [])):
            return True
        if record_ids & set(signoff.get("evidence_record_ids", [])):
            return True
        # Legacy/handoff sign-offs name comments rather than fact ids.  Derive
        # their current operations so an operation-level correction can trigger
        # the separate school and enterprise reviews.
        legacy_record_ids = set(signoff.get("evidence_record_ids", []))
        if legacy_record_ids:
            legacy_ops = {
                f.get("operation_id")
                for f in self.state["facts"].values()
                if f.get("record_id") in legacy_record_ids
            }
            if op_ids & legacy_ops:
                return True
        snap_evidence = signoff.get("snapshot", {}).get("evidence", [])
        snap_records = {e.get("record_id") for e in snap_evidence}
        snap_ops = {e.get("operation_id") for e in snap_evidence}
        if record_ids & snap_records or op_ids & snap_ops:
            return True
        return False

    def start_reviews_for_correction(
        self,
        decision_id: str,
        *,
        signoff_ids: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        decision = next((d for d in self.state["decisions"] if d["id"] == decision_id), None)
        if decision is None:
            raise NotFoundError(f"unknown decision: {decision_id}")
        affected_fact_ids = decision.get("affected_fact_ids", [])
        if signoff_ids is None:
            # A case is one learner/workshop round.  Review every sent
            # conclusion separately; the report itself shows whether this
            # correction actually affected its combination.
            signoffs = [self.state["signoffs"][sid] for sid in self.state["signoff_order"]]
        else:
            signoffs = [self.state["signoffs"][sid] for sid in signoff_ids]

        reviews = []
        for signoff in signoffs:
            review = self._create_review(signoff, trigger_decision_id=decision_id)
            review["correction_affected"] = self._signoff_affected_by_facts(
                signoff, affected_fact_ids
            )
            reviews.append(review)
        return reviews

    def review_signoff(
        self,
        signoff_id: str,
        *,
        trigger_decision_id: Optional[str] = None,
        as_of: Any = None,
    ) -> Dict[str, Any]:
        signoff = self._get_signoff(signoff_id)
        if trigger_decision_id:
            existing_id = next(
                (
                    rid
                    for rid in self.state["review_order"]
                    if self.state["reviews"][rid].get("signoff_id") == signoff_id
                    and self.state["reviews"][rid].get("trigger_decision_id") == trigger_decision_id
                ),
                None,
            )
            if existing_id:
                return self.state["reviews"][existing_id]
        return self._create_review(signoff, trigger_decision_id=trigger_decision_id, as_of=as_of)

    def _get_signoff(self, signoff_id: str) -> Dict[str, Any]:
        if signoff_id not in self.state["signoffs"]:
            raise NotFoundError(f"unknown signoff: {signoff_id}")
        return self.state["signoffs"][signoff_id]

    def _create_review(
        self,
        signoff: Dict[str, Any],
        *,
        trigger_decision_id: Optional[str],
        as_of: Any = None,
    ) -> Dict[str, Any]:
        rid = self._next("review")
        standard_id = signoff.get("standard_id")

        # A correction issued on Wednesday must not be repaired/back-filled by
        # a Thursday progress record when computing the correction-time review.
        if as_of is None and trigger_decision_id:
            trigger = next(
                (d for d in self.state["decisions"] if d.get("id") == trigger_decision_id),
                None,
            )
            if trigger and trigger.get("issued_at") is not None:
                as_of = trigger["issued_at"]

        remaining_report = None
        feasible = False
        snap_std = signoff.get("snapshot", {}).get("standard")
        if snap_std:
            # Use the standard wording/caliber snapshotted at sending. Current
            # standards/interpretations are not retroactively substituted.
            facts = self.active_facts(as_of=as_of)
            include_hearsay = signoff.get("snapshot", {}).get("include_hearsay", True)
            remaining_report = evaluate_standard(
                facts, snap_std, as_of=as_of, include_hearsay=include_hearsay
            )
            feasible = bool(remaining_report.get("feasible"))
        elif standard_id and standard_id in self.state["standards"]:
            remaining_report = self.evaluate(
                standard_id,
                group=signoff.get("group"),
                purpose=signoff.get("purpose"),
                as_of=as_of,
            )
            feasible = bool(remaining_report.get("feasible"))

        # Separately replay the corrected attribution against the original
        # send-time cutoff.  Later progress can support a new sign-off, but it
        # must not rewrite whether the earlier sent conclusion was supportable.
        original_send_report = None
        send_as_of = signoff.get("sent_at")
        if snap_std and send_as_of is not None and send_as_of != as_of:
            original_send_report = evaluate_standard(
                self.active_facts(as_of=send_as_of),
                snap_std,
                as_of=send_as_of,
                include_hearsay=signoff.get("snapshot", {}).get("include_hearsay", True),
            )

        competency_conclusions = list(signoff.get("granted_competency_ids", []))
        if "qualification_id" in signoff:
            qualification_id = signoff.get("qualification_id")
        else:
            qualification_id = (
                f"qual:{signoff.get('group')}:{signoff.get('purpose')}:{standard_id}"
            )
        original = ([qualification_id] if qualification_id else []) + competency_conclusions
        supported: List[str] = []
        individually_supported: List[str] = []
        pending: List[str] = []

        if feasible:
            if qualification_id:
                supported.append(qualification_id)
            for cid in competency_conclusions:
                if self._combo_awards(remaining_report, cid):
                    supported.append(cid)
                elif signoff.get("kind") == "granular_capability_signoff" and self._competency_supported_in_snapshot(
                    cid,
                    snapshot_standard=snap_std,
                    as_of=as_of,
                    group=signoff.get("group"),
                    purpose=signoff.get("purpose"),
                ):
                    supported.append(cid)
                else:
                    pending.append(cid)
        else:
            # The signed combination/credential is a conclusion in its own
            # right.  Granular evidence may survive without repairing an
            # enterprise distinct-operation combination.
            if qualification_id:
                pending.append(qualification_id)
            for cid in competency_conclusions:
                if self._competency_supported_in_snapshot(
                    cid,
                    snapshot_standard=snap_std,
                    as_of=as_of,
                    group=signoff.get("group"),
                    purpose=signoff.get("purpose"),
                ):
                    individually_supported.append(cid)
                    supported.append(cid)
                else:
                    pending.append(cid)

        if not competency_conclusions:
            # Legacy handoffs only identify the qualification by group/purpose.
            pass

        if not pending:
            suggested = "upheld"
        elif individually_supported:
            suggested = "partially_upheld"
        else:
            suggested = "revoke_or_new_evidence"

        review = {
            "id": rid,
            "signoff_id": signoff["id"],
            "group": signoff.get("group"),
            "recipient": signoff.get("recipient"),
            "trigger_decision_id": trigger_decision_id,
            "status": "review_pending",
            "as_of": as_of,
            "original_conclusions": original,
            "qualification_conclusion": qualification_id,
            "supported_conclusions": supported,
            "individually_supported_conclusions": individually_supported,
            "pending_conclusions": pending,
            "suggested_outcome": suggested,
            "review_as_of": as_of,
            "remaining_evaluation": remaining_report,
            "original_send_as_of": send_as_of,
            "original_send_evaluation": original_send_report,
            "original_send_supported": (
                bool(original_send_report.get("feasible")) if original_send_report else None
            ),
        }
        self.state["reviews"][rid] = review
        self.state["review_order"].append(rid)
        signoff.setdefault("review_ids", []).append(rid)
        if pending:
            signoff["current_status"] = "review_pending"
            signoff["pending_conclusions"] = pending
            for cid in pending:
                signoff.setdefault("conclusion_statuses", {})[cid] = "review_pending"
            self._notice(
                "review_pending",
                signoff.get("recipient", ""),
                signoff["id"],
                f"签认 {signoff['id']} 的结论 {pending} 待复核；原文和当时口径保持不变。",
                review_id=rid,
                pending_conclusions=pending,
            )
        else:
            signoff["current_status"] = "upheld"
        return review

    def _combo_awards(self, report: Optional[Dict[str, Any]], competency_id: str) -> bool:
        if not report:
            return False
        return any(competency_id in combo.get("competency_ids", []) for combo in report.get("valid_combinations", []))

    def _competency_supported_in_snapshot(
        self,
        competency_id: str,
        *,
        snapshot_standard: Optional[Dict[str, Any]],
        as_of: Any,
        group: Optional[str],
        purpose: Optional[str],
    ) -> bool:
        facts = self.active_facts(as_of=as_of)
        if snapshot_standard:
            return bool(find_capability_evidence(facts, [snapshot_standard], competency_id, as_of=as_of))
        return bool(self.verify_competency(competency_id, group=group, purpose=purpose, as_of=as_of))

    def complete_review(
        self,
        review_id: str,
        *,
        reviewer: Any = None,
        reviewer_role: Optional[str] = None,
        outcome: Optional[str] = None,
        completed_at: Any = None,
        note: str = "",
        as_of: Any = None,
    ) -> Dict[str, Any]:
        if reviewer_role is not None or reviewer is not None:
            self._require_role(reviewer, reviewer_role, REVIEWER_ROLES)
        if review_id not in self.state["reviews"]:
            raise NotFoundError(f"unknown review: {review_id}")
        review = self.state["reviews"][review_id]
        final_outcome = outcome or review.get("suggested_outcome")
        review["status"] = "completed"
        review["final_outcome"] = final_outcome
        review["completed_at"] = completed_at
        review["reviewer"] = reviewer if isinstance(reviewer, dict) else {"id": reviewer, "role": reviewer_role}
        review["note"] = note

        signoff = self._get_signoff(review["signoff_id"])
        pending = set(review.get("pending_conclusions", []))
        if final_outcome in {"upheld"}:
            for cid in pending:
                signoff["conclusion_statuses"][cid] = "upheld"
            signoff["pending_conclusions"] = []
            signoff["current_status"] = "upheld"
        elif final_outcome in {"revoked", "revoke_or_new_evidence"}:
            for cid in pending:
                signoff["conclusion_statuses"][cid] = "revoked_pending_new_evidence"
            signoff["pending_conclusions"] = []
            signoff["current_status"] = "revoked_or_needs_new_evidence"
        elif final_outcome in {"partially_upheld", "partially_revoked"}:
            # Supported items remain; pending items are not automatically restored.
            for cid in pending:
                signoff["conclusion_statuses"][cid] = "revoked_pending_new_evidence"
            signoff["pending_conclusions"] = []
            signoff["current_status"] = "partially_upheld"
        else:
            raise ValidationError(f"unsupported review outcome: {final_outcome}")
        signoff.setdefault("history", []).append(
            {"review_id": review_id, "outcome": final_outcome, "at": completed_at, "note": note}
        )
        self._notice(
            "review_completed",
            signoff.get("recipient", ""),
            signoff["id"],
            f"签认 {signoff['id']} 复核结果：{final_outcome}",
            review_id=review_id,
        )
        return review

    # ---------------------------------------------------------- supplement
    def submit_supplement(
        self,
        record: Union[str, Dict[str, Any]],
        *,
        referenced_record_ids: Optional[Sequence[str]] = None,
        standard_requests: Optional[Sequence[Dict[str, Any]]] = None,
        peer_names: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        existing_ids = set(self.state["record_order"])
        stored = self.add_record(record, peer_names=peer_names)
        new_id = stored["id"]
        new_fact_ids = [fid for fid in self.state["fact_order"] if self.state["facts"][fid].get("record_id") == new_id]

        if referenced_record_ids is None:
            referenced: set[str] = set()
            for signoff in self.state["signoffs"].values():
                referenced.update(signoff.get("evidence_record_ids", []))
                referenced.update((signoff.get("combination") or {}).get("record_ids", []))
        else:
            referenced = set(referenced_record_ids)
        evidence_record_ids = {
            f.get("record_id") for f in self.state["facts"].values() if f.get("record_id")
        }
        unreferenced_old = [
            rid
            for rid in self.state["record_order"]
            if rid != new_id
            and rid in existing_ids
            and rid in evidence_record_ids
            and rid not in referenced
        ]

        requests = list(standard_requests or [])
        if not requests:
            requests = [
                {"standard_id": sid, "group": std.get("group"), "purpose": std.get("purpose")}
                for sid, std in self.state["standards"].items()
            ]

        analyses = []
        for req in requests:
            sid = req["standard_id"]
            group = req.get("group")
            purpose = req.get("purpose")
            as_of = req.get("as_of")
            before = self.evaluate(
                sid,
                group=group,
                purpose=purpose,
                as_of=as_of,
                record_ids=[rid for rid in self.state["record_order"] if rid != new_id],
            )
            after = self.evaluate(sid, group=group, purpose=purpose, as_of=as_of)
            filled = self._filled_gaps(before, after, new_id, new_fact_ids)
            combos = after.get("valid_combinations", [])
            classified = []
            for combo in combos:
                rids = set(combo.get("record_ids", []))
                old_rids = rids - {new_id}
                unused_used = sorted(rid for rid in old_rids if rid not in referenced)
                item = {
                    **combo,
                    "includes_supplement": new_id in rids,
                    "uses_previously_unreferenced_evidence": bool(unused_used),
                    "previously_unreferenced_record_ids": unused_used,
                    "only_previously_unreferenced_old_evidence": bool(old_rids) and all(rid not in referenced for rid in old_rids),
                }
                classified.append(item)
            analyses.append(
                {
                    "standard_id": sid,
                    "group": group,
                    "purpose": purpose,
                    "before_feasible": before["feasible"],
                    "after_feasible": after["feasible"],
                    "actual_gaps_before": before["gaps"],
                    "filled_by_supplement": filled,
                    "after_report": after,
                    "classified_combinations": classified,
                    "unreferenced_old_record_ids": unreferenced_old,
                    "unreferenced_only_combinations": [
                        c for c in classified if not c["includes_supplement"] and c["only_previously_unreferenced_old_evidence"]
                    ],
                    "supplement_combinations_using_unused_evidence": [
                        c for c in classified if c["includes_supplement"] and c["uses_previously_unreferenced_evidence"]
                    ],
                }
            )

        return {
            "supplement_record_id": new_id,
            "new_fact_ids": new_fact_ids,
            "referenced_record_ids": sorted(referenced),
            "previously_unreferenced_record_ids": unreferenced_old,
            "analyses": analyses,
        }

    def _filled_gaps(
        self,
        before: Dict[str, Any],
        after: Dict[str, Any],
        new_record_id: str,
        new_fact_ids: Sequence[str],
    ) -> List[Dict[str, Any]]:
        filled = []
        before_gap_keys = set()
        for gap in before.get("gaps", []):
            key = (gap.get("type"), gap.get("requirement_id"), tuple(gap.get("keys", [])))
            before_gap_keys.add(key)
            # A missing requirement is filled if the after report has a candidate
            # from the supplement for that requirement.
            if gap.get("type") == "missing_requirement":
                req_id = gap.get("requirement_id")
                for alt in after.get("alternatives", []):
                    report = next((r for r in alt["requirement_reports"] if r["requirement_id"] == req_id), None)
                    if report and any(fid in new_fact_ids for fid in report.get("candidate_fact_ids", [])):
                        filled.append(
                            {
                                "gap_type": "missing_requirement",
                                "requirement_id": req_id,
                                "label": gap.get("label"),
                                "filled_by_fact_ids": [
                                    fid for fid in report["candidate_fact_ids"] if fid in new_fact_ids
                                ],
                                "record_id": new_record_id,
                            }
                        )
        if not before.get("feasible") and after.get("feasible"):
            for combo in after.get("valid_combinations", []):
                if new_record_id in combo.get("record_ids", []):
                    for gap in before.get("gaps", []):
                        if gap.get("type") == "distinct_operation":
                            filled.append(
                                {
                                    "gap_type": "distinct_operation",
                                    "keys": gap.get("keys") or gap.get("requirement_keys"),
                                    "message": gap.get("message"),
                                    "resolved_by_combination": combo,
                                    "record_id": new_record_id,
                                }
                            )
                    filled.append(
                        {
                            "gap_type": "full_standard_combination",
                            "standard_id": after.get("standard_id"),
                            "alternative_id": combo.get("alternative_id"),
                            "combination": combo,
                        }
                    )
                    break
        return filled

    # ---------------------------------------------------------- notices
    def _notice(
        self,
        kind: str,
        recipient: str,
        signoff_id: str,
        message: str,
        *,
        review_id: Optional[str] = None,
        pending_conclusions: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        notice = {
            "id": self._next("notice"),
            "type": kind,
            "recipient": recipient,
            "signoff_id": signoff_id,
            "review_id": review_id,
            "pending_conclusions": pending_conclusions or [],
            "message": message,
        }
        self.state["notices"].append(notice)
        return notice

    def notifications_for(self, recipient: str) -> List[Dict[str, Any]]:
        return [n for n in self.state["notices"] if n.get("recipient") == recipient]

    def outbox(self) -> List[Dict[str, Any]]:
        return list(self.state["notices"])

    # ---------------------------------------------------------- batch API
    def make_decision(self, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch a role-checked decision from a JSON-friendly mapping."""
        dtype = decision.get("decision_type") or decision.get("type") or decision.get("kind")
        issuer = (
            decision.get("issuer")
            or decision.get("decided_by")
            or decision.get("actor")
            or {"id": decision.get("issuer_id"), "role": decision.get("role") or decision.get("issuer_role")}
        )
        issued_at = decision.get("issued_at") or decision.get("decided_at") or decision.get("at")
        if dtype in {"attribution", "attribution_clarification", "clarify_attribution"}:
            return self.clarify_attribution(
                decision_id=decision.get("id") or decision.get("decision_id"),
                issuer=issuer,
                issuer_role=decision.get("issuer_role") or decision.get("role"),
                issued_at=issued_at,
                target=decision["target"],
                attribution=decision.get("attribution") or decision.get("changes") or {},
                reason=decision.get("reason", ""),
                source_text=decision.get("source_text", ""),
                create_reviews=decision.get("create_reviews", True),
                review_signoff_ids=decision.get("review_signoff_ids"),
            )
        if dtype in {"interpretation", "standard_interpretation", "issue_interpretation"}:
            return self.issue_interpretation(
                interpretation_id=decision.get("id") or decision.get("decision_id"),
                issuer=issuer,
                issuer_role=decision.get("issuer_role") or decision.get("role"),
                issued_at=issued_at,
                text=decision.get("text", ""),
                rule=decision.get("rule") or {},
                scope=decision.get("scope") or {},
                standard_id=decision.get("standard_id"),
            )
        raise ValidationError(f"unknown decision type: {dtype}")

    def handle(self, action: Dict[str, Any]) -> Any:
        """Dispatch one JSON-friendly action; useful for a CLI/request queue."""
        kind = action.get("action") or action.get("type")
        if kind in {"make_decision", "decide", "add_decision"}:
            return self.make_decision(action.get("decision", action))
        if kind == "add_round":
            return self.add_round(**{k: v for k, v in action.items() if k not in {"action", "type"}})
        if kind == "add_record":
            return self.add_record(action["record"], **action.get("options", {}))
        if kind == "add_standard":
            return self.add_standard(action["standard"])
        if kind == "evaluate":
            return self.evaluate(**{k: v for k, v in action.items() if k not in {"action", "type"}})
        if kind == "evaluate_group":
            return self.evaluate_group(**{k: v for k, v in action.items() if k not in {"action", "type"}})
        if kind == "sign_off":
            return self.sign_off(**{k: v for k, v in action.items() if k not in {"action", "type"}})
        if kind == "sign_capability":
            return self.sign_capability(**{k: v for k, v in action.items() if k not in {"action", "type"}})
        if kind in {"clarify_attribution", "attribution_clarification"}:
            payload = {k: v for k, v in action.items() if k not in {"action", "type"}}
            return self.clarify_attribution(**payload)
        if kind in {"issue_interpretation", "standard_interpretation"}:
            return self.issue_interpretation(**{k: v for k, v in action.items() if k not in {"action", "type"}})
        if kind == "review_signoff":
            return self.review_signoff(**{k: v for k, v in action.items() if k not in {"action", "type"}})
        if kind == "complete_review":
            return self.complete_review(**{k: v for k, v in action.items() if k not in {"action", "type"}})
        if kind == "submit_supplement":
            return self.submit_supplement(action["record"], **action.get("options", {}))
        raise ValidationError(f"unknown action: {kind}")

    # Discoverable semantic aliases.
    assess = evaluate
    gap_analysis = evaluate
    sign = sign_off
    add_comment = add_record
    correct_attribution = clarify_attribution
    interpret_standard = issue_interpretation
