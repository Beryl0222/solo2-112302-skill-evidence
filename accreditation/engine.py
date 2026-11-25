"""Pure evaluation and combination-search logic."""
from __future__ import annotations

import copy
import itertools
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .parsing import TASK_LIBRARY, weekday_index


def normalize_standard(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Return a standard with structured alternatives.

    A structured standard may provide either ``alternatives`` directly or the
    simpler DNF fields ``requirement_sets``.  If only Chinese ``text`` is
    present, the conservative text parser is used.
    """
    std = copy.deepcopy(raw)
    if not std.get("id"):
        std["id"] = std.get("standard_id") or "standard"

    if "alternatives" not in std and std.get("requirement_sets"):
        alts = []
        for i, reqs in enumerate(std["requirement_sets"], start=1):
            norm_reqs = []
            for j, req in enumerate(reqs, start=1):
                if isinstance(req, str):
                    req = {"task": req}
                item = copy.deepcopy(req)
                item.setdefault("id", f"a{i}-r{j}")
                item.setdefault("key", item.get("task") or item["id"])
                item.setdefault("completed", True)
                item.setdefault("actor_kind", "learner")
                norm_reqs.append(item)
            alts.append({"id": f"a{i}", "requirements": norm_reqs})
        std["alternatives"] = alts

    if "alternatives" not in std and std.get("text"):
        from .parsing import parse_standard_text

        parsed = parse_standard_text(std["text"], standard_id=std["id"])
        std["alternatives"] = parsed["alternatives"]
        std.setdefault("distinct_operation_keys", parsed["distinct_operation_keys"])
        std["text_parser"] = parsed.get("parsing")

    std.setdefault("alternatives", [])
    std.setdefault("distinct_operation_keys", [])
    # Friendly aliases for callers modeling “these abilities need separate ops”.
    for alias in (
        "distinct_operation_groups",
        "distinct_operations",
        "different_operation_keys",
        "distinct_operation_competencies",
        "distinct_operation_competency_ids",
    ):
        if raw.get(alias) and not std.get("distinct_operation_keys"):
            std["distinct_operation_keys"] = raw[alias]
    std.setdefault("same_operation_keys", [])
    std.setdefault("competency_ids", [])
    return std


def fact_time_index(fact: Dict[str, Any]) -> Optional[float]:
    value = fact.get("time_index")
    if value is not None:
        return value
    observed = fact.get("observed_at")
    return weekday_index(observed) if observed else None


def _within_as_of(fact: Dict[str, Any], as_of: Any) -> bool:
    if as_of is None:
        return True

    # ISO date comparison when both sides carry ISO-ish dates.
    observed = fact.get("observed_at")
    if isinstance(as_of, str) and len(as_of) >= 10 and as_of[:4].isdigit() and observed:
        return str(observed)[:10] <= as_of[:10]

    cutoff = weekday_index(as_of) if not isinstance(as_of, (int, float)) else as_of
    fact_index = fact_time_index(fact)
    if cutoff is None or fact_index is None:
        # Unknown time is not known to be a later-progress record; callers can
        # require explicit time metadata for stricter historical replay.
        return True
    return fact_index <= cutoff


def requirement_rejection_reasons(
    fact: Dict[str, Any],
    req: Dict[str, Any],
    *,
    as_of: Any = None,
    include_hearsay: bool = True,
) -> List[str]:
    reasons: List[str] = []
    if fact.get("status", "active") != "active":
        reasons.append(f"fact_status:{fact.get('status')}")
    if not _within_as_of(fact, as_of):
        reasons.append("observed_after_cutoff")

    task = req.get("task") or req.get("key")
    tasks = req.get("tasks") or ([task] if task else [])
    if tasks and fact.get("task") not in tasks:
        reasons.append("task_mismatch")

    if req.get("completed", True) and not fact.get("completed", False):
        reasons.append("not_completed")

    actor_kind = req.get("actor_kind", "learner")
    if actor_kind == "learner":
        if not fact.get("qualifies_learner", False):
            reasons.append("actor_is_not_learner")
    elif fact.get("actor_kind") != actor_kind:
        reasons.append(f"actor_kind_mismatch:{fact.get('actor_kind')}")

    if req.get("independent") and fact.get("independence") != "independent":
        reasons.append(f"not_independent:{fact.get('independence', 'unknown')}")

    if req.get("direct_observation") and fact.get("mode") != "direct":
        reasons.append("not_direct_observation")
    if not include_hearsay and fact.get("mode") == "hearsay":
        reasons.append("hearsay_excluded")

    if req.get("operation_id") and fact.get("operation_id") != req["operation_id"]:
        reasons.append("wrong_operation")

    for key, expected in (req.get("fact_equals") or {}).items():
        if fact.get(key) != expected:
            reasons.append(f"fact_value_mismatch:{key}")

    return reasons


def candidate_facts(
    facts: Iterable[Dict[str, Any]],
    req: Dict[str, Any],
    *,
    as_of: Any = None,
    include_hearsay: bool = True,
) -> List[Dict[str, Any]]:
    candidates = []
    for fact in facts:
        reasons = requirement_rejection_reasons(
            fact, req, as_of=as_of, include_hearsay=include_hearsay
        )
        if not reasons:
            candidates.append(fact)
    return candidates


def _record_ids(facts: Sequence[Dict[str, Any]], fact_ids: Sequence[str]) -> List[str]:
    by_id = {f["fact_id"]: f for f in facts if "fact_id" in f}
    records: List[str] = []
    for fid in fact_ids:
        rid = by_id[fid].get("record_id")
        if rid and rid not in records:
            records.append(rid)
    return records


def _competency_ids_for_alternative(
    standard: Dict[str, Any], alt: Dict[str, Any], chosen_reqs: Sequence[Dict[str, Any]]
) -> List[str]:
    if alt.get("awards"):
        return list(alt["awards"])
    if standard.get("competency_ids"):
        return list(standard["competency_ids"])
    ids: List[str] = []
    for req in chosen_reqs:
        cid = req.get("competency_id")
        if cid and cid not in ids:
            ids.append(cid)
        for cid in req.get("competency_ids", []) or []:
            if cid not in ids:
                ids.append(cid)
    return ids


def _constraint_failures(
    assignment: Dict[str, Dict[str, Any]],
    keyed: Dict[str, Dict[str, Any]],
    standard: Dict[str, Any],
) -> List[Dict[str, Any]]:
    failures: List[Dict[str, Any]] = []

    for group in standard.get("distinct_operation_requirement_ids", []) or []:
        ops = {rid: assignment[rid]["operation_id"] for rid in group if rid in assignment}
        if len(ops) == len(group) and len(set(ops.values())) != len(group):
            failures.append({"type": "distinct_operation", "requirement_ids": group, "operations": ops})

    for key_group in standard.get("distinct_operation_keys", []) or []:
        chosen = [keyed[k] for k in key_group if k in keyed]
        if len(chosen) == len(key_group) and len({f.get("operation_id") for f in chosen}) < len(chosen):
            failures.append(
                {
                    "type": "distinct_operation",
                    "requirement_keys": key_group,
                    "operation_ids": [f.get("operation_id") for f in chosen],
                }
            )

    for key_group in standard.get("same_operation_keys", []) or []:
        chosen = [keyed[k] for k in key_group if k in keyed]
        if len(chosen) == len(key_group) and len({f.get("operation_id") for f in chosen}) != 1:
            failures.append(
                {
                    "type": "same_operation_required",
                    "requirement_keys": key_group,
                    "operation_ids": [f.get("operation_id") for f in chosen],
                }
            )
    return failures


def enumerate_combinations(
    facts: Iterable[Dict[str, Any]],
    standard: Dict[str, Any],
    *,
    as_of: Any = None,
    include_hearsay: bool = True,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Enumerate valid evidence combinations for a normalized standard."""
    facts = list(facts)
    standard = normalize_standard(standard)
    valid: List[Dict[str, Any]] = []
    seen: set[Tuple[str, ...]] = set()

    for alt in standard.get("alternatives", []):
        reqs = alt.get("requirements", [])
        candidate_lists = []
        for req in reqs:
            cand = candidate_facts(
                facts, req, as_of=as_of, include_hearsay=include_hearsay
            )
            # Multiple reports of the same operation are one piece of operation
            # evidence.  Keep all occurrences for provenance, but choose one
            # slot representative for combination search.
            by_operation: Dict[Any, List[Dict[str, Any]]] = {}
            for fact in cand:
                by_operation.setdefault(fact.get("operation_id"), []).append(fact)
            operation_slots = []
            for op_facts in by_operation.values():
                ordered = sorted(
                    op_facts,
                    key=lambda f: (
                        0 if f.get("mode") == "direct" else 1,
                        str(f.get("fact_id", "")),
                    ),
                )
                representative = copy.deepcopy(ordered[0])
                representative["_all_fact_ids"] = [f["fact_id"] for f in ordered]
                operation_slots.append(representative)
            candidate_lists.append(operation_slots)

        if not reqs or any(not c for c in candidate_lists):
            continue

        for chosen_facts in itertools.product(*candidate_lists):
            assignment = {req["id"]: fact for req, fact in zip(reqs, chosen_facts)}
            keyed = {req.get("key") or req["id"]: fact for req, fact in zip(reqs, chosen_facts)}
            failures = _constraint_failures(assignment, keyed, standard)
            if failures:
                continue

            primary_fact_ids = [f["fact_id"] for f in chosen_facts]
            all_fact_ids: List[str] = []
            for fact in chosen_facts:
                for fid in fact.get("_all_fact_ids", [fact["fact_id"]]):
                    if fid not in all_fact_ids:
                        all_fact_ids.append(fid)
            dedup_key = tuple(
                sorted((req["id"], fact.get("operation_id")) for req, fact in zip(reqs, chosen_facts))
            )
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            combo = {
                "standard_id": standard["id"],
                "alternative_id": alt["id"],
                "assignments": {rid: f["fact_id"] for rid, f in assignment.items()},
                "key_assignments": {k: f["fact_id"] for k, f in keyed.items()},
                "fact_ids": primary_fact_ids,
                "all_evidence_fact_ids": all_fact_ids,
                "corroborating_fact_ids": [fid for fid in all_fact_ids if fid not in primary_fact_ids],
                "record_ids": _record_ids(facts, all_fact_ids),
                "operation_ids": list(dict.fromkeys(f.get("operation_id") for f in chosen_facts)),
                "unique_operation_count": len({f.get("operation_id") for f in chosen_facts}),
                "competency_ids": _competency_ids_for_alternative(standard, alt, reqs),
            }
            valid.append(combo)
            if len(valid) >= limit:
                return valid
    return valid


def _near_misses(
    facts: List[Dict[str, Any]],
    req: Dict[str, Any],
    *,
    as_of: Any,
    include_hearsay: bool,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    task = req.get("task") or req.get("key")
    tasks = set(req.get("tasks") or ([task] if task else []))
    misses = []
    for fact in facts:
        if tasks and fact.get("task") not in tasks:
            continue
        reasons = requirement_rejection_reasons(
            fact, req, as_of=as_of, include_hearsay=include_hearsay
        )
        if reasons:
            misses.append(
                {
                    "fact_id": fact.get("fact_id"),
                    "record_id": fact.get("record_id"),
                    "operation_id": fact.get("operation_id"),
                    "actor": fact.get("actor"),
                    "independence": fact.get("independence"),
                    "mode": fact.get("mode"),
                    "span": fact.get("span"),
                    "reasons": reasons,
                }
            )
    return misses[:limit]


def _separation_gaps(
    facts: List[Dict[str, Any]],
    standard: Dict[str, Any],
    requirement_reports: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    gaps: List[Dict[str, Any]] = []
    for key_group in standard.get("distinct_operation_keys", []) or []:
        # Candidate operations for each key in this alternative.
        per_key = {}
        for key in key_group:
            report = next((r for r in requirement_reports if r.get("key") == key), None)
            candidate_ids = (report or {}).get("candidate_fact_ids", [])
            ops = []
            for fid in candidate_ids:
                fact = next((f for f in facts if f.get("fact_id") == fid), None)
                if fact and fact.get("operation_id") not in ops:
                    ops.append(fact.get("operation_id"))
            per_key[key] = ops

        if all(per_key.get(k) for k in key_group):
            # Pairwise distinct choice exists iff a Cartesian choice has all
            # operations distinct.  Groups are tiny in this domain.
            possible = False
            for ops in itertools.product(*[per_key[k] for k in key_group]):
                if len(set(ops)) == len(ops):
                    possible = True
                    break
            if not possible:
                gaps.append(
                    {
                        "type": "distinct_operation",
                        "keys": list(key_group),
                        "candidate_operations": per_key,
                        "message": "相关能力虽有证据，但都落在同一操作；拆句不能充作不同独立操作。",
                    }
                )
    return gaps


def evaluate_standard(
    facts: Iterable[Dict[str, Any]],
    standard: Dict[str, Any],
    *,
    as_of: Any = None,
    include_hearsay: bool = True,
    combination_limit: int = 100,
) -> Dict[str, Any]:
    """Evaluate one standard and explain every unmet alternative."""
    facts = list(facts)
    std = normalize_standard(standard)
    combos = enumerate_combinations(
        facts,
        std,
        as_of=as_of,
        include_hearsay=include_hearsay,
        limit=combination_limit,
    )

    alternative_reports = []
    all_gaps: List[Dict[str, Any]] = []
    req_by_id: Dict[str, Dict[str, Any]] = {}

    for alt in std.get("alternatives", []):
        req_reports = []
        reports_by_id: Dict[str, Dict[str, Any]] = {}
        missing_ids: List[str] = []
        for req in alt.get("requirements", []):
            req_by_id[req["id"]] = req
            candidates = candidate_facts(
                facts, req, as_of=as_of, include_hearsay=include_hearsay
            )
            candidate_ids = [f["fact_id"] for f in candidates]
            report = {
                "requirement_id": req["id"],
                "key": req.get("key") or req["id"],
                "label": req.get("label") or TASK_LIBRARY.get(req.get("task", ""), {}).get("label"),
                "competency_id": req.get("competency_id"),
                "required_independent": bool(req.get("independent")),
                "satisfied": bool(candidates),
                "candidate_fact_ids": candidate_ids,
                "near_misses": _near_misses(
                    facts, req, as_of=as_of, include_hearsay=include_hearsay
                ),
            }
            req_reports.append(report)
            reports_by_id[req["id"]] = report
            if not candidates:
                missing_ids.append(req["id"])
                all_gaps.append(
                    {
                        "type": "missing_requirement",
                        "alternative_id": alt["id"],
                        "requirement_id": req["id"],
                        "key": report["key"],
                        "label": report["label"],
                        "near_misses": report["near_misses"],
                    }
                )

        separation_gaps = _separation_gaps(facts, std, req_reports) if not missing_ids else []
        satisfied = not missing_ids and not separation_gaps and any(c["alternative_id"] == alt["id"] for c in combos)
        alternative_reports.append(
            {
                "alternative_id": alt["id"],
                "satisfied": satisfied,
                "requirement_reports": req_reports,
                "missing_requirement_ids": missing_ids,
                "separation_gaps": separation_gaps,
                "matched_combinations": [c for c in combos if c["alternative_id"] == alt["id"]],
            }
        )
        if not missing_ids and separation_gaps:
            all_gaps.extend(dict(g, alternative_id=alt["id"]) for g in separation_gaps)

    feasible = bool(combos)
    best = None
    if alternative_reports:
        best = min(
            alternative_reports,
            key=lambda a: (
                len(a["missing_requirement_ids"]) + len(a["separation_gaps"]),
                0 if a["satisfied"] else 1,
            ),
        )["alternative_id"]

    return {
        "standard_id": std["id"],
        "standard_text": std.get("text"),
        "feasible": feasible,
        "valid_combinations": combos,
        "alternatives": alternative_reports,
        "gaps": all_gaps,
        "best_alternative_id": best,
        "as_of": as_of,
        "include_hearsay": include_hearsay,
    }


def find_capability_evidence(
    facts: Iterable[Dict[str, Any]],
    standards: Iterable[Dict[str, Any]],
    competency_id: str,
    *,
    as_of: Any = None,
    include_hearsay: bool = True,
) -> List[Dict[str, Any]]:
    """Find direct requirement evidence for one exact competency id.

    There is deliberately no category-based inference here: a general
    competency id cannot satisfy a different professional competency id.
    """
    facts = list(facts)
    matches = []
    for raw_standard in standards:
        standard = normalize_standard(raw_standard)
        for alt in standard.get("alternatives", []):
            for req in alt.get("requirements", []):
                ids = [req.get("competency_id")] + list(req.get("competency_ids") or [])
                if competency_id not in ids:
                    continue
                for fact in candidate_facts(
                    facts, req, as_of=as_of, include_hearsay=include_hearsay
                ):
                    matches.append(
                        {
                            "standard_id": standard["id"],
                            "alternative_id": alt["id"],
                            "requirement_id": req["id"],
                            "competency_id": competency_id,
                            "fact_id": fact["fact_id"],
                            "record_id": fact.get("record_id"),
                            "operation_id": fact.get("operation_id"),
                        }
                    )
    return matches
