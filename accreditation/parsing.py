"""Parsing helpers for Chinese workshop comments and standard wording.

The parser is intentionally rule based and conservative.  It extracts the
business dimensions used by :mod:`accreditation.engine`; callers can always
provide structured facts when their source language is outside these rules.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

WEEKDAYS = {
    "周一": 1,
    "礼拜一": 1,
    "星期二": 2,
    "周二": 2,
    "礼拜二": 2,
    "周三": 3,
    "星期三": 3,
    "礼拜三": 3,
    "周四": 4,
    "星期四": 4,
    "礼拜四": 4,
    "周五": 5,
    "星期五": 5,
    "礼拜五": 5,
    "周六": 6,
    "星期六": 6,
    "周日": 7,
    "周天": 7,
    "星期日": 7,
    "星期天": 7,
}

WEEKDAY_EN = {
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
    "sunday": 7,
}

CHINESE_NUMBERS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
}

# Order is useful for diagnostics and for the standard parser.
TASK_LIBRARY: Dict[str, Dict[str, Any]] = {
    "missing_value_check": {
        "label": "缺值核对",
        "patterns": [
            r"核对缺失值",
            r"核对缺值",
            r"检查缺值",
            r"缺值核对",
            r"核过缺值",
            r"核缺值",
        ],
    },
    "explain_basis": {
        "label": "解释回填依据",
        "patterns": [
            r"解释回填依据",
            r"说明回填依据",
            r"逐条解释[^，,。；;]{0,8}依据",
            r"解释了?[^，,。；;]{0,8}依据",
            r"说明[^，,。；;]{0,8}依据",
        ],
    },
    "backfill_rule_correction": {
        "label": "改正回填规则",
        "patterns": [r"改正了?回填规则", r"纠正了?回填规则", r"修改了?回填规则"],
    },
    "independent_rerun": {
        "label": "独立复跑",
        "patterns": [r"独立复跑", r"重新跑了?一遍", r"重跑一遍", r"复跑"],
    },
    "exception_backfill": {
        "label": "异常回填",
        # Do not treat mention of the backfill rule/basis as performing backfill.
        "patterns": [r"异常回填", r"例外回填", r"复填", r"回填(?!依据|规则|方法|口径|条件)"],
    },
}

INDEPENDENT_CUES = [
    " independently",
    "自行",
    "独立",
    "没请人提示",
    "没有请人提示",
    "未请人提示",
    "没请旁人提示",
    "不问旁人",
    "不请人提示",
    "无提示",
    "无人提示",
    "单独接",
    "独自",
]

PROMPTED_CUES = [
    "同伴提示下",
    "队友提示下",
    "旁人提示下",
    "老师提示下",
    "经同伴提示",
    "在同伴提示",
    "在队友提示",
    "同伴演示",
    "队友演示",
    "照着",
    "照做",
    "仿照",
    "模仿",
    "提示后",
    "提醒下",
]

NEGATED_PROMPT_CUES = ["没请人提示", "没有请人提示", "未请人提示", "没请旁人提示", "不问旁人"]

QUOTE_RE = re.compile(r"[“\"']([^”\"']+)[”\"']")
SESSION_RE = re.compile(r"第([一二两三四五六七八九1-9])\s*(?:场|次|轮|节)")
WEEKDAY_RE = re.compile("|".join(sorted(WEEKDAYS, key=len, reverse=True)))


def weekday_index(value: Any) -> Optional[int]:
    """Return an ordering index for Chinese/English weekdays or integers."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text in WEEKDAYS:
        return WEEKDAYS[text]
    low = text.lower()
    for name, idx in WEEKDAY_EN.items():
        if name in low:
            return idx
    match = WEEKDAY_RE.search(text)
    if match:
        return WEEKDAYS[match.group(0)]
    try:
        # ISO dates are ordered by their ordinal; callers should not mix them
        # with bare weekday indexes in the same case.
        from datetime import date

        return date.fromisoformat(text[:10]).toordinal()
    except (ValueError, TypeError):
        return None


def _split_header(text: str) -> Tuple[str, str]:
    parts = re.split(r"[|｜]", text, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", text.strip()


def _time_context(*texts: str) -> Dict[str, Any]:
    joined = " ".join(t for t in texts if t)
    day_match = WEEKDAY_RE.search(joined)
    time_index = WEEKDAYS[day_match.group(0)] if day_match else None
    time_label = day_match.group(0) if day_match else None

    session_number = None
    session_match = SESSION_RE.search(joined)
    if session_match:
        session_number = CHINESE_NUMBERS.get(session_match.group(1))
    return {
        "time_index": time_index,
        "time_label": time_label,
        "session_number": session_number,
    }


def _reporter(header: str, body: str) -> Optional[str]:
    text = header or body[:40]
    match = re.search(r"([一-龥]{2,4}?)(?:转述|复述|说道?|称)", text)
    if match:
        name = match.group(1)
        # Avoid common time/title prefixes captured by the lazy expression.
        for day in WEEKDAYS:
            if name.startswith(day):
                name = name[len(day) :]
        name = name.strip("，,：: ")
        if name and name not in {"老师", "教务", "企业", "周三", "周二", "周一", "周四"}:
            return name
    if "罗闻转述" in header:
        return "罗闻"
    return None


def _operation_ref(context: Dict[str, Any], *segments: str) -> Tuple[str, Optional[str]]:
    """Return (operation_id, textual reference).

    Sentence splitting alone never mints a new operation.  A new operation is
    only recognized from an explicit session/round marker.
    """
    text = " ".join(segments)
    ctx = _time_context(text)
    day = ctx["time_index"] or context.get("time_index")
    session = ctx["session_number"] or context.get("session_number")

    if session:
        prefix = f"op-day{day or 'x'}-session{session}"
        return prefix, f"第{session}场/次操作"
    if "同一轮" in text or "同一次操作" in text or "同一操作" in text:
        return f"op-day{day or 'x'}-same-round", "同一轮/同一次操作"
    if "另一次" in text or "另一轮" in text or "不同实训操作" in text:
        return "", "explicit-other-operation"
    return "", None


def split_actions(text: str) -> List[str]:
    """Split prose into action-sized spans without creating operation identity."""
    # Parenthetical cue phrases are retained in each resulting span where useful.
    parts = re.split(
        r"[。！？!?；;]+|，但|,但|但|但是|，|,|、|：|:|(?<=复填)后|(?<=跑完)后|并(?=解释|说明)",
        text,
    )
    return [p.strip(" 　\t\r\n“”\"'") for p in parts if p and p.strip(" 　\t\r\n“”\"'")]


def _find_tasks(span: str) -> List[str]:
    tasks: List[str] = []
    for task, spec in TASK_LIBRARY.items():
        if any(re.search(pattern, span) for pattern in spec["patterns"]):
            tasks.append(task)
    # Explain phrase can include the noun 回填; do not count that mention alone.
    if "explain_basis" in tasks and "exception_backfill" in tasks:
        if not re.search(r"复填|异常回填|(?<!依据)回填(?!依据|规则)", span):
            tasks.remove("exception_backfill")
    return tasks


def _is_peer_actor(span: str, peers: Iterable[str]) -> Tuple[bool, Optional[str]]:
    peer_names = list(peers) + ["同伴", "队友", "旁人"]
    for peer in peer_names:
        # “由同伴演示回填” means the peer performs the action.
        if re.search(rf"由{re.escape(peer)}(?:来)?(?:演示|示范|操作|做|完成|回填|核)", span):
            return True, peer
        if re.search(rf"^{re.escape(peer)}(?:演示|示范|操作|做|完成|回填|核对|核)", span):
            return True, peer
        if re.search(rf"{re.escape(peer)}(?:演示|示范)了?[^，,。]*(?:回填|核对|操作)", span):
            return True, peer
    return False, None


def _actor(
    span: str,
    learner: str,
    peers: Iterable[str],
    reporter: Optional[str],
    quoted: bool,
    inherited_actor: Optional[str] = None,
) -> Tuple[str, str]:
    is_peer, peer_name = _is_peer_actor(span, peers)
    if is_peer:
        assert peer_name is not None
        return peer_name, "peer"

    peer_set = set(peers) | {"同伴", "队友", "旁人"}
    for name in peer_set:
        if name and name in span and not re.search(rf"在{re.escape(name)}(?:提示|提醒)", span):
            # A bare peer mention can be the actor unless it is only a prompt cue.
            if re.search(
                rf"{re.escape(name)}(?:刚才)?(?:独立|自行|亲自|单独|当场)?(?:来)?(?:完成|回填|核对|核|做|跑|操作|说|复述|演示|示范)",
                span,
            ):
                return name, "peer"

    if learner and learner in span:
        return learner, "learner"
    if "他" in span or "她" in span:
        return learner, "learner"
    if quoted and reporter and span.startswith("我"):
        # “我看他…” is perception; if no task is in this span it yields no fact.
        return reporter, "reporter"
    if inherited_actor:
        kind = "learner" if inherited_actor == learner else "peer"
        return inherited_actor, kind
    # Observation records are about the named learner by default.
    return learner, "learner"


def _independence(span: str, context_span: str = "") -> str:
    text = f"{context_span} {span}"
    has_prompt = any(cue in text for cue in PROMPTED_CUES)
    has_independent = any(cue in text for cue in INDEPENDENT_CUES)
    # “没请人提示” is an explicit negation of prompting, not a negative outcome.
    if any(cue in text for cue in NEGATED_PROMPT_CUES):
        return "independent"
    if has_prompt and not re.search(r"没请人提示|不问旁人", text):
        return "prompted"
    if has_independent:
        return "independent"
    return "unknown"


def _completed(span: str) -> bool:
    cleaned = span
    for cue in NEGATED_PROMPT_CUES:
        cleaned = cleaned.replace(cue, "")
    # Negated task performance.  “没请人提示” has already been removed.
    if re.search(r"(?:未|没有|没|不曾|未能)\s*(?:完成|回填|核对|核|跑|提交|解释|做)", cleaned):
        return False
    return True


def _quoted_spans(body: str) -> List[Tuple[str, int, int]]:
    return [(m.group(1), m.start(), m.end()) for m in QUOTE_RE.finditer(body)]


def parse_comment(
    record: Any,
    *,
    learner_name: str = "学员",
    peer_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Parse one observation/comment record into typed evidence facts.

    The returned dictionary always contains ``facts``.  Parsing never mutates
    the input and never drops the original text.
    """
    if isinstance(record, str):
        record_id = None
        text = record
        extra: Dict[str, Any] = {}
    elif isinstance(record, dict):
        record_id = record.get("id") or record.get("record_id")
        text = record.get("text") or record.get("content") or ""
        extra = {k: v for k, v in record.items() if k not in {"text", "content"}}
    else:
        raise TypeError("record must be a string or mapping")

    peers = list(peer_names or [])
    if "罗闻" in text and "罗闻" not in peers:
        peers.append("罗闻")

    header, body = _split_header(text)
    base_context = _time_context(header, body)
    reporter = _reporter(header, body)
    quoted_matches = _quoted_spans(body)

    parse_units: List[Tuple[str, bool]] = []
    if quoted_matches:
        for quote, _, _ in quoted_matches:
            parse_units.append((quote, True))
        outside = QUOTE_RE.sub(" ", body)
        if outside.strip():
            parse_units.append((outside, False))
    else:
        parse_units.append((body, False))

    facts: List[Dict[str, Any]] = []
    fact_number = 0
    inherited_actor: Optional[str] = None
    running_context = dict(base_context)

    for unit_text, is_quoted in parse_units:
        # “但/但是” marks a contrast boundary: an independence cue on one
        # side must never be distributed to the action on the other side.
        contrast_clauses = [c for c in re.split(r"但(?:是)?", unit_text) if c.strip()]
        for contrast_clause in contrast_clauses:
            segments = split_actions(contrast_clause)
            previous_segment = ""
            for segment_index, segment in enumerate(segments):
                # Update running time/session even for a time-only lead-in
                # segment such as “周二第二场：”.  Only the segment itself is
                # used here so a later time switch cannot rewrite earlier facts.
                local_time = _time_context(segment)
                if local_time.get("time_index") is not None:
                    running_context["time_index"] = local_time["time_index"]
                    running_context["time_label"] = local_time["time_label"]
                if local_time.get("session_number") is not None:
                    running_context["session_number"] = local_time["session_number"]

                tasks = _find_tasks(segment)

                # A “follows the demonstration and finishes the rerun” span also
                # shows guided performance of the demonstrated task object.
                propagated_tasks: List[str] = []
                if any(cue in segment for cue in ["照着", "照做", "仿照", "模仿"]):
                    propagated_tasks = [
                        t for t in _find_tasks(previous_segment) if t != "independent_rerun"
                    ]

                all_tasks = tasks + [t for t in propagated_tasks if t not in tasks]
                if not all_tasks:
                    previous_segment = segment
                    continue

                op_id, op_ref = _operation_ref(running_context, segment)
                if not op_id:
                    # An explicit session marker already produced a canonical
                    # cross-record id above.  Otherwise keep the id local to
                    # this record while still separating explicit day switches.
                    day = running_context.get("time_index")
                    suffix = f"op-day{day}" if day is not None else "operation-1"
                    op_id = f"{record_id or 'comment'}:{suffix}"

                actor, actor_kind = _actor(
                    segment, learner_name, peers, reporter, is_quoted, inherited_actor
                )
                if actor_kind in {"learner", "peer"}:
                    inherited_actor = actor

                context_span = previous_segment
                next_segment = segments[segment_index + 1] if segment_index + 1 < len(segments) else ""
                # A leading negated-prompt cue can describe the whole predicate
                # chain (“重新跑了一遍，没请人提示就改正…”).  Do not import an
                # arbitrary later clause, especially a contrastive one.
                if next_segment and any(next_segment.startswith(cue) for cue in NEGATED_PROMPT_CUES):
                    context_span += " " + next_segment
                independence = _independence(segment, context_span)
                completed = _completed(segment)
                time_index = running_context.get("time_index")
                time_label = running_context.get("time_label")
                session_number = running_context.get("session_number")

                for task in all_tasks:
                    fact_number += 1
                    # Propagated guided work is necessarily prompted even if the
                    # rerun span does not itself contain the word “prompt”.
                    fact_independence = independence
                    if task in propagated_tasks and fact_independence == "unknown":
                        fact_independence = "prompted"

                    facts.append(
                        {
                            "local_fact_id": f"{record_id or 'comment'}:fact-{fact_number}",
                            "record_id": record_id,
                            "task": task,
                            "task_label": TASK_LIBRARY[task]["label"],
                            "actor": actor,
                            "actor_kind": actor_kind,
                            "qualifies_learner": actor_kind == "learner",
                            "completed": completed,
                            "independence": fact_independence,
                            "mode": "hearsay" if is_quoted or reporter else "direct",
                            "reporter": reporter if is_quoted or reporter else None,
                            "is_quoted": is_quoted,
                            "time_index": time_index,
                            "time_label": time_label,
                            "session_number": session_number,
                            "operation_id": op_id,
                            "operation_ref": op_ref,
                            "span": segment,
                            "raw_text": text,
                            "status": "active",
                            "origin": "parsed",
                        }
                    )
                previous_segment = segment

    return {
        "record_id": record_id,
        "learner_name": learner_name,
        "reporter": reporter,
        "time": base_context,
        "facts": facts,
        "raw_text": text,
        **({"extra": extra} if extra else {}),
    }


def parse_comments(records: Iterable[Any], **kwargs: Any) -> List[Dict[str, Any]]:
    return [parse_comment(record, **kwargs) for record in records]


def parse_standard_text(text: str, *, standard_id: Optional[str] = None) -> Dict[str, Any]:
    """Parse the supported Chinese “A 和 B，或 C 并 D” standard syntax.

    More elaborate standards should be supplied as structured requirement
    sets; unknown wording is returned with an empty ``alternatives`` list and
    the original text preserved.
    """
    raw = text
    text = re.split(r"[|｜]", text, maxsplit=1)[-1] if re.search(r"[|｜]", text) else text
    text = text.strip().rstrip("。")

    # Split top-level alternatives.  The supported synthetic wording uses
    # “，或” / “；或者”.
    alt_texts = [p.strip() for p in re.split(r"[，,；;]\s*(?:或者|或)|(?:^|\s)或者", text) if p.strip()]
    if len(alt_texts) == 1 and "或" in text:
        alt_texts = [p.strip() for p in re.split(r"或者|或", text) if p.strip()]

    alternatives: List[Dict[str, Any]] = []
    distinct_pairs: List[List[str]] = []
    all_task_order: List[str] = []

    for alt_index, alt in enumerate(alt_texts, start=1):
        found: List[Tuple[int, str, re.Match[str]]] = []
        for task, spec in TASK_LIBRARY.items():
            earliest: Optional[re.Match[str]] = None
            for pattern in spec["patterns"]:
                match = re.search(pattern, alt)
                if match and (earliest is None or match.start() < earliest.start()):
                    earliest = match
            if earliest:
                found.append((earliest.start(), task, earliest))
        found.sort(key=lambda item: item[0])

        tasks: List[str] = []
        spans: Dict[str, str] = {}
        for _, task, match in found:
            tasks.append(task)
            start = max(0, match.start() - 12)
            end = min(len(alt), match.end() + 12)
            spans[task] = alt[start:end]
            if task not in all_task_order:
                all_task_order.append(task)

        coordination = bool(re.search(r"和|与|、", alt))
        pre_distributed_independent = bool(
            re.search(r"独立(?:完成)?[^，,。]*(?:和|与|、)", alt)
        )
        post_distributed_independent = coordination and bool(
            re.search(r"(?:都|均|须|需|应).{0,8}独立|独立完成", alt)
        )
        global_independent = "独立" in alt and (
            pre_distributed_independent or post_distributed_independent
        )
        requirements = []
        for req_index, task in enumerate(tasks, start=1):
            local = spans.get(task, alt)
            independent = task == "independent_rerun" or global_independent or ("独立" in local and task != "explain_basis")
            requirements.append(
                {
                    "id": f"a{alt_index}-r{req_index}",
                    "key": task,
                    "task": task,
                    "label": TASK_LIBRARY[task]["label"],
                    "competency_id": task,
                    "independent": bool(independent),
                    "completed": True,
                    "actor_kind": "learner",
                }
            )
        alternatives.append({"id": f"a{alt_index}", "requirements": requirements})

    if re.search(r"不同(?:的)?(?:实训)?操作|来自不同操作", text):
        pair = [t for t in ["missing_value_check", "exception_backfill"] if t in all_task_order]
        if len(pair) >= 2:
            distinct_pairs.append(pair)

    return {
        "id": standard_id,
        "text": raw,
        "alternatives": alternatives,
        "distinct_operation_keys": distinct_pairs,
        "parsing": "rule-based-v1",
    }
