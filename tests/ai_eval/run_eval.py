"""Run the AI SQL evaluation suite.

    python -m tests.ai_eval.run_eval                 # run every case
    python -m tests.ai_eval.run_eval --tags safety   # only safety cases
    python -m tests.ai_eval.run_eval --dry-run       # list cases, call nothing
    python -m tests.ai_eval.run_eval --label v2-prompt

Each run writes a JSON report to tests/ai_eval/reports/. Two reports can be
compared to see whether a prompt or retriever change actually helped — which
is the whole point of having this.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
REPORTS_DIR = HERE / "reports"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.ai_eval.checks import CaseResult, check_case, summarize  # noqa: E402


def load_cases(path: Path, tags: list[str] | None = None) -> list[dict[str, Any]]:
    try:
        import yaml
    except ImportError:
        sys.exit("PyYAML is required: pip install pyyaml")

    with path.open(encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}

    cases = doc.get("cases") or []
    if not cases:
        sys.exit(f"no cases found in {path}")

    seen: set[str] = set()
    for case in cases:
        case_id = str(case.get("id") or "")
        if not case_id:
            sys.exit(f"case without an id: {case}")
        if case_id in seen:
            sys.exit(f"duplicate case id: {case_id}")
        seen.add(case_id)

    if tags:
        wanted = {t.lower() for t in tags}
        cases = [
            c for c in cases
            if wanted & {str(t).lower() for t in (c.get("tags") or [])}
        ]
        if not cases:
            sys.exit(f"no cases matched tags: {', '.join(tags)}")

    return cases


def run_one(case: dict[str, Any], *, username: str) -> CaseResult:
    from py.ai.ai_sql_agent import run_ai_query

    question = str(case.get("question") or "")
    try:
        outcome = run_ai_query(question, username=username)
    except Exception as exc:  # the agent itself blew up
        return CaseResult(
            case_id=str(case.get("id")),
            question=question,
            passed=False,
            error=f"{type(exc).__name__}: {exc}",
            failures=[f"agent raised: {type(exc).__name__}: {exc}"],
        )
    return check_case(case, outcome)


def print_line(result: CaseResult) -> None:
    mark = {"PASS": "ok  ", "FAIL": "FAIL", "ERROR": "ERR ", "SKIP": "skip"}[result.status]
    print(f"  {mark}  {result.case_id}")
    for failure in result.failures:
        print(f"          - {failure}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=str(HERE / "cases.yaml"))
    parser.add_argument("--tags", nargs="*", help="only run cases with these tags")
    parser.add_argument("--label", default="", help="name this run in the report")
    parser.add_argument("--username", default="eval")
    parser.add_argument("--dry-run", action="store_true", help="list cases, call nothing")
    parser.add_argument("--no-report", action="store_true")
    args = parser.parse_args()

    cases = load_cases(Path(args.cases), args.tags)

    if args.dry_run:
        print(f"{len(cases)} case(s):")
        for case in cases:
            tags = ",".join(str(t) for t in (case.get("tags") or []))
            print(f"  {case['id']:<28} [{tags}]  {case.get('question', '')}")
        return 0

    print(f"running {len(cases)} case(s)\n")
    started = time.perf_counter()
    results: list[CaseResult] = []
    for case in cases:
        result = run_one(case, username=args.username)
        results.append(result)
        print_line(result)

    elapsed = time.perf_counter() - started
    stats = summarize(results)

    print(
        f"\n{stats['passed']}/{stats['graded']} passed "
        f"({stats['pass_rate']:.0%})"
        + (f", {stats['errored']} errored" if stats["errored"] else "")
        + f"  —  {elapsed:.1f}s"
    )

    if stats["failure_histogram"]:
        print("\nfailures by kind:")
        for kind, count in stats["failure_histogram"].items():
            print(f"  {count:>3}  {kind}")

    if not args.no_report:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = f"-{args.label}" if args.label else ""
        report_path = REPORTS_DIR / f"{stamp}{suffix}.json"
        report_path.write_text(
            json.dumps(
                {
                    "label": args.label,
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "tags_filter": args.tags,
                    "elapsed_s": round(elapsed, 2),
                    "summary": stats,
                    "results": [
                        {
                            "id": r.case_id,
                            "status": r.status,
                            "question": r.question,
                            "sql": r.sql,
                            "tables_used": r.tables_used,
                            "failures": r.failures,
                            "error": r.error,
                            "timings": r.timings,
                        }
                        for r in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nreport: {report_path.relative_to(REPO_ROOT)}")

    return 0 if stats["failed"] == 0 and stats["errored"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
