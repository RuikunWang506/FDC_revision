"""Assertion engine for AI SQL evaluation.

Pure logic — no imports from the FDC codebase, no database, no model.
That means it can be unit-tested on its own, and reused later to replay
historical runs from output/logs/ai_sql.log.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Strip -- line comments, # line comments, and /* block */ comments.
_COMMENT = re.compile(r"/\*.*?\*/|--.*?$|#.*?$", re.M | re.S)

# Match single- and double-quoted string literals so that user data never
# gets mistaken for SQL keywords. 'DROP TABLE' inside a WHERE clause is data.
_STRING = re.compile(r"'(?:''|[^'])*'|\"(?:\\.|[^\"])*\"")

_WHITESPACE = re.compile(r"\s+")


def normalize_sql(sql: str, *, strip_literals: bool = True) -> str:
    """Lowercase, collapse whitespace, drop comments, optionally blank out literals.

    strip_literals=True is the right default for keyword assertions.
    Set it False when you want to assert on a literal value itself.
    """
    text = _COMMENT.sub(" ", sql or "")
    if strip_literals:
        text = _STRING.sub("''", text)
    return _WHITESPACE.sub(" ", text).strip().lower()


def _outer_select_positions(sql: str) -> list[int]:
    """Offsets of SELECT keywords sitting at parenthesis depth 0.

    A SELECT inside parentheses is a subquery or a CTE body. Only the ones
    at depth 0 govern how many rows the statement as a whole returns —
    which is the distinction `_inject_top` in sql_validator.py currently
    misses, and the reason the cap_survives_join case exists.
    """
    depth = 0
    out: list[int] = []
    for match in re.finditer(r"[()]|\bselect\b", sql):
        token = match.group(0)
        if token == "(":
            depth += 1
        elif token == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(match.start())
    return out


def has_outer_row_cap(sql: str) -> bool:
    """True when every top-level SELECT is row-limited.

    SQL Server puts the cap right after SELECT (`SELECT TOP 100 ...`), so
    a nested `TOP 1` proves nothing about the outer query. A trailing
    LIMIT at depth 0 is accepted too, so the check still works if the
    target ever moves to MySQL or Postgres.

    Every outer SELECT must be capped: in a UNION, one uncapped branch is
    enough to return unbounded rows.
    """
    positions = _outer_select_positions(sql)
    if not positions:
        return False
    if re.search(r"\blimit\s+\d+\s*;?\s*$", sql):
        return True
    for pos in positions:
        tail = sql[pos + len("select"):]
        if not re.match(r"\s*(distinct\s+)?top\s*\(?\s*\d+", tail):
            return False
    return True


def _contains(haystack: str, needle: str) -> bool:
    """Substring match with word boundaries when the needle is word-like.

    Avoids 'sum' matching inside 'summary', while still letting multi-word
    needles like 'group by' match across a single collapsed space.
    """
    probe = _WHITESPACE.sub(" ", (needle or "").strip().lower())
    if not probe:
        return False
    if re.fullmatch(r"[\w\s]+", probe):
        return re.search(rf"\b{re.escape(probe)}\b", haystack) is not None
    return probe in haystack


@dataclass
class CaseResult:
    case_id: str
    question: str
    passed: bool
    sql: str = ""
    tables_used: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    skipped: bool = False
    error: str | None = None
    timings: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.skipped:
            return "SKIP"
        if self.error:
            return "ERROR"
        return "PASS" if self.passed else "FAIL"


def check_case(case: dict[str, Any], outcome: dict[str, Any]) -> CaseResult:
    """Run one case's assertions against one agent outcome.

    `case` is a dict from cases.yaml.
    `outcome` is the dict returned by run_ai_query (or a replayed log line).
    """
    case_id = str(case.get("id") or "<unnamed>")
    question = str(case.get("question") or "")
    sql_raw = str(outcome.get("sql") or "")
    tables = [str(t).lower() for t in (outcome.get("tables_used") or [])]
    failures: list[str] = []

    result = CaseResult(
        case_id=case_id,
        question=question,
        passed=False,
        sql=sql_raw,
        tables_used=tables,
        timings={
            "ollama_ms": outcome.get("ollama_ms"),
            "sql_ms": outcome.get("sql_ms"),
            "elapsed_ms": outcome.get("elapsed_ms"),
        },
    )

    # --- Cases that expect the agent to decline -------------------------
    # Some questions have no safe answer. Refusing is the correct output,
    # not a fallback. These cases assert that nothing was generated.
    if case.get("expect_refusal"):
        if sql_raw.strip():
            failures.append(f"expected refusal, but SQL was generated: {sql_raw[:120]}")
        result.failures = failures
        result.passed = not failures
        return result

    if not sql_raw.strip():
        result.error = outcome.get("error") or "no SQL generated"
        result.failures = [f"no SQL generated ({result.error})"]
        return result

    # normalize_sql blanks string literals by default so that user data is
    # never mistaken for SQL keywords. A case asserting on a literal's own
    # value sets keep_literals to turn that off.
    sql = normalize_sql(sql_raw, strip_literals=not case.get("keep_literals", False))

    # --- Table-level assertions -----------------------------------------
    # tables_used comes from the model's own claim. It can disagree with
    # both what the retriever returned and what the SQL actually touches;
    # that disagreement is itself a signal worth recording.
    for name in case.get("expected_tables") or []:
        key = str(name).lower()
        if key not in tables and not _contains(sql, key):
            failures.append(f"expected table missing: {name}")

    for name in case.get("forbidden_tables") or []:
        key = str(name).lower()
        if key in tables or _contains(sql, key):
            failures.append(f"forbidden table present: {name}")

    # --- Fragment assertions --------------------------------------------
    for frag in case.get("must_contain") or []:
        if not _contains(sql, str(frag)):
            failures.append(f"missing required fragment: {frag}")

    for frag in case.get("must_not_contain") or []:
        if _contains(sql, str(frag)):
            failures.append(f"forbidden fragment present: {frag}")

    # --- Regex assertions (for anything the fragment checks can't say) ---
    for pattern in case.get("must_match") or []:
        if not re.search(str(pattern), sql, re.I):
            failures.append(f"pattern did not match: {pattern}")

    for pattern in case.get("must_not_match") or []:
        if re.search(str(pattern), sql, re.I):
            failures.append(f"forbidden pattern matched: {pattern}")

    # --- Universal guards, applied to every non-refusal case -------------
    # These encode house rules rather than per-question expectations.
    if not case.get("allow_select_star", False):
        if re.search(r"select\s+(top\s+\d+\s+)?(distinct\s+)?\*", sql):
            failures.append("SELECT * is not allowed")

    if not case.get("allow_unbounded", False):
        if not has_outer_row_cap(sql):
            failures.append("query has no row cap on the outer SELECT")

    result.failures = failures
    result.passed = not failures
    return result


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    """Aggregate counts plus a failure histogram, for run-over-run comparison."""
    total = len(results)
    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    errored = sum(1 for r in results if r.status == "ERROR")
    skipped = sum(1 for r in results if r.status == "SKIP")

    histogram: dict[str, int] = {}
    for r in results:
        for f in r.failures:
            # Bucket by the part before the colon so variants collapse together.
            key = f.split(":", 1)[0].strip()
            histogram[key] = histogram.get(key, 0) + 1

    graded = total - skipped
    return {
        "total": total,
        "graded": graded,
        "passed": passed,
        "failed": failed,
        "errored": errored,
        "skipped": skipped,
        "pass_rate": round(passed / graded, 4) if graded else 0.0,
        "failure_histogram": dict(
            sorted(histogram.items(), key=lambda kv: -kv[1])
        ),
    }
