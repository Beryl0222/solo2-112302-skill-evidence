"""Command line entry point.

Examples:
    python -m accreditation demo
    python -m accreditation --state case.json evaluate --standard school-s1
    python -m accreditation --state case.json run --request request.json
    python -m accreditation --state case.json notifications --recipient 教务处
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

from .demo import run_demo
from .errors import AccreditationError
from .service import AccreditationService


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _load_request(path: str) -> Dict[str, Any]:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="accreditation", description=__doc__)
    parser.add_argument("--state", default=".accreditation-state.json", help="JSON state path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create an empty state file")
    sub.add_parser("demo", help="run the bundled synthetic corpus scenario")

    run = sub.add_parser("run", help="run one action or a request with actions[]")
    run.add_argument("--request", required=True, help="JSON request file or - for stdin")

    evaluate = sub.add_parser("evaluate", help="evaluate one adopted standard")
    evaluate.add_argument("--standard", required=True)
    evaluate.add_argument("--group")
    evaluate.add_argument("--purpose")
    evaluate.add_argument("--as-of", dest="as_of")

    group = sub.add_parser("evaluate-group", help="evaluate all standards adopted by a group")
    group.add_argument("--group-name", required=True)
    group.add_argument("--purpose")
    group.add_argument("--as-of", dest="as_of")

    notes = sub.add_parser("notifications", help="list notifications for a recipient")
    notes.add_argument("--recipient", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "demo":
            _print(run_demo(state_path=args.state if args.state != ".accreditation-state.json" else None))
            return 0

        service = AccreditationService.open(args.state)

        if args.command == "init":
            service.save()
            _print({"state": str(service.state_path), "case_id": service.state["case_id"]})
            return 0

        if args.command == "run":
            request = _load_request(args.request)
            if "actions" in request:
                results = [service.handle(action) for action in request["actions"]]
            else:
                results = service.handle(request)
            service.save()
            # Service objects are plain JSON-friendly dicts; return the result.
            _print(results)
            return 0

        if args.command == "evaluate":
            result = service.evaluate(
                args.standard, group=args.group, purpose=args.purpose, as_of=args.as_of
            )
            _print(result)
            return 0

        if args.command == "evaluate-group":
            result = service.evaluate_group(
                args.group_name, purpose=args.purpose, as_of=args.as_of
            )
            _print(result)
            return 0

        if args.command == "notifications":
            _print(service.notifications_for(args.recipient))
            return 0

        return 2
    except (AccreditationError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
