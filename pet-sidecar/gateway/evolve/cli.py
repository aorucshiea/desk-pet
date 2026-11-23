"""Command-line access to the evolution workbench.

Usage (from ``pet-sidecar``)::

    python -m gateway.evolve.cli inspect
    python -m gateway.evolve.cli propose --goal "..." --plan "..." --edits e.json
    python -m gateway.evolve.cli evaluate ev_...
    python -m gateway.evolve.cli promote ev_... --approved-by 用户名
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .models import EvolveError
from .service import EvolveService


def _find_repo(start: Path) -> Path:
    current = Path(start).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return Path(__file__).resolve().parents[3]


def _service(args: argparse.Namespace) -> EvolveService:
    repo = Path(args.repo).expanduser().resolve() if args.repo else _find_repo(Path.cwd())
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else None
    plugin_dir = Path(args.plugin_dir).expanduser() if args.plugin_dir else None
    return EvolveService(repo, data_dir=data_dir, plugin_dir=plugin_dir)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=None, help="repository root (default: auto-detect)")
    parser.add_argument("--data-dir", default=None, help="candidate store (default: PET_EVOLVE_DIR or ~/.pet/evolve)")
    parser.add_argument("--plugin-dir", default=None, help="live plugin directory")


def _print_json(value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    sys.stdout.write(text + "\n")


def _load_edits(path: str) -> list[dict]:
    if path == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("edits JSON must be a list of {path, old, new}")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evolve", description="Desk Pet self-evolution workbench (M1)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("inspect", help="print a compact repo/plugin map")
    _add_common(p)
    p.add_argument("--max-files", type=int, default=400)

    p = sub.add_parser("read", help="read numbered source lines")
    _add_common(p)
    p.add_argument("path")
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--end", type=int, default=None)

    p = sub.add_parser("propose", help="create an isolated candidate")
    _add_common(p)
    p.add_argument("--goal", required=True)
    p.add_argument("--plan", default="")
    p.add_argument("--kind", choices=("core", "plugin"), default="core")
    p.add_argument("--title", default="")
    p.add_argument("--edits", required=True, help="JSON file (or - for stdin)")

    p = sub.add_parser("evaluate", help="compile + focused tests (or full suite)")
    _add_common(p)
    p.add_argument("candidate_id")
    p.add_argument("--full", action="store_true")

    p = sub.add_parser("list", help="list recent candidates")
    _add_common(p)
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("show", help="show one candidate record")
    _add_common(p)
    p.add_argument("candidate_id")

    p = sub.add_parser("discard", help="discard a candidate and remove its worktree")
    _add_common(p)
    p.add_argument("candidate_id")

    p = sub.add_parser("promote", help="promote a verified plugin (human approval)")
    _add_common(p)
    p.add_argument("candidate_id")
    p.add_argument("--approved-by", required=True)

    p = sub.add_parser("rollback", help="restore the plugin backup for a promotion")
    _add_common(p)
    p.add_argument("promotion_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        service = _service(args)
        if args.command == "inspect":
            sys.stdout.write(service.inspect(max_files=args.max_files))
        elif args.command == "read":
            sys.stdout.write(service.read_source(args.path, start=args.start, end=args.end))
        elif args.command == "propose":
            record = service.propose(
                goal=args.goal,
                plan=args.plan,
                edits=_load_edits(args.edits),
                kind=args.kind,
                title=args.title,
            )
            _print_json({"id": record["id"], "status": record["status"],
                         "files_changed": record["files_changed"],
                         "diff_sha256": record["diff_sha256"]})
        elif args.command == "evaluate":
            _print_json(service.evaluate(args.candidate_id, full=args.full))
        elif args.command == "list":
            _print_json(service.list_candidates(args.limit))
        elif args.command == "show":
            _print_json(service.show(args.candidate_id))
        elif args.command == "discard":
            _print_json(service.discard(args.candidate_id))
        elif args.command == "promote":
            _print_json(service.promote(args.candidate_id, approved_by=args.approved_by))
        elif args.command == "rollback":
            _print_json(service.rollback(args.promotion_id))
        return 0
    except EvolveError as exc:
        sys.stderr.write(f"错误: {exc}\n")
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"错误: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
