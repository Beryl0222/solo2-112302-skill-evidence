"""Dual-group competency accreditation toolkit."""
from .engine import (
    enumerate_combinations,
    evaluate_standard,
    find_capability_evidence,
    normalize_standard,
)
from .errors import (
    AccreditationError,
    PermissionDeniedError,
    QualificationError,
    ValidationError,
)
from .demo import run_demo
from .parsing import parse_comment, parse_standard_text
from .service import AccreditationService, new_state, process_round

# A concise alias for callers who model the workflow as an “office”.
AccreditationOffice = AccreditationService

__all__ = [
    "AccreditationService",
    "AccreditationOffice",
    "AccreditationError",
    "PermissionDeniedError",
    "QualificationError",
    "ValidationError",
    "enumerate_combinations",
    "evaluate_standard",
    "find_capability_evidence",
    "normalize_standard",
    "parse_comment",
    "parse_standard_text",
    "new_state",
    "process_round",
    "run_demo",
]
