"""Regression tests for sql_validator's row-cap enforcement.

validate_sql is the last thing standing between a generated query and the
production database. Its job is to guarantee that whatever the model wrote,
the statement that actually runs is bounded.

The cap check used to be "does the word TOP appear anywhere in this string",
which is satisfied by a TOP inside a subquery while the outer query stays
unbounded. These tests pin the real contract: every top-level SELECT is
row-limited, and the configured limit is a ceiling the model cannot raise.

No database, no model, no network.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from tests.ai_eval.checks import has_outer_row_cap, normalize_sql

_ROOT = Path(__file__).resolve().parents[1]


def _load_by_path(alias: str, relative: str):
    """Import a project module by file path instead of by package name.

    `from py.ai.sql_validator import validate_sql` cannot work here: the
    project's top-level package is named `py`, and pytest ships its own
    top-level `py.py` compatibility shim. A directory without __init__.py
    is only a namespace-package candidate, so the scan continues and
    site-packages/py.py wins -- the import fails. Adding __init__.py makes
    the project's `py` win instead, and then pytest's own `import py`
    breaks. There is no path-ordering fix; the collision is the package
    name itself.

    Loading by path sidesteps module resolution entirely. Two existing
    tests in this directory already do the same thing for the same reason.
    The alias is deliberately not 'py.ai.sql_validator' so that nothing is
    registered under the contested name.
    """
    spec = importlib.util.spec_from_file_location(alias, _ROOT / relative)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {relative}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_validator = _load_by_path("fdc_sql_validator", "py/ai/sql_validator.py")
validate_sql = _validator.validate_sql

CAP = 100


def _capped(sql: str) -> bool:
    """True when every depth-0 SELECT carries a row cap.

    Reuses the assertion engine from the eval harness so the definition of
    'capped' lives in exactly one place.
    """
    return has_outer_row_cap(normalize_sql(sql))


def _top_values(sql: str) -> list[int]:
    return [int(m.group(1)) for m in re.finditer(r"\btop\s*\(?\s*(\d+)", sql, re.I)]


def _run(sql: str) -> dict:
    return validate_sql(sql, default_limit=CAP)


# --- behavior that already worked, and must keep working ------------------


def test_plain_select_gets_a_cap():
    out = _run("SELECT CUS_ID, CUS_NAME FROM customer")
    assert out["ok"], out["error"]
    assert _capped(out["sql"])


def test_existing_cap_below_the_limit_is_left_alone():
    # The model asking for 5 rows is intent, not a violation.
    out = _run("SELECT TOP 5 PROD_CD FROM inv ORDER BY PROD_CD")
    assert out["ok"], out["error"]
    assert _top_values(out["sql"]) == [5]


def test_existing_cap_above_the_limit_is_clamped_down():
    out = _run("SELECT TOP 9999 PROD_CD FROM inv")
    assert out["ok"], out["error"]
    assert all(v <= CAP for v in _top_values(out["sql"]))


def test_distinct_keeps_its_place():
    # TOP must land after DISTINCT; 'SELECT TOP n DISTINCT' is a syntax error.
    out = _run("SELECT DISTINCT PROD_CD FROM ord_log")
    assert out["ok"], out["error"]
    assert re.search(r"select\s+distinct\s+top\s*\(?\s*\d+", out["sql"], re.I)


def test_write_statement_still_rejected():
    out = validate_sql("DELETE FROM ord_log WHERE ORD_NUM < 1000")
    assert not out["ok"]


def test_multiple_statements_still_rejected():
    out = validate_sql("SELECT TOP 5 a FROM t; DROP TABLE t")
    assert not out["ok"]


# --- the bugs -------------------------------------------------------------


def test_nested_top_does_not_satisfy_the_outer_cap():
    """A TOP inside a subquery says nothing about how many rows come back.

    This is the shape the model actually produces for 'show each customer
    with their latest order' -- the subquery picks one row per group, and
    the outer join over customer is unbounded.
    """
    out = _run(
        "SELECT c.CUS_ID, c.CUS_NAME, a.ORD_AMT "
        "FROM customer c "
        "JOIN (SELECT TOP 1 CUS_ID, ORD_AMT FROM orders ORDER BY ORD_DATE DESC) a "
        "ON c.CUS_ID = a.CUS_ID"
    )
    assert out["ok"], out["error"]
    assert _capped(out["sql"]), f"outer SELECT left unbounded: {out['sql']}"


def test_union_caps_every_branch():
    """One uncapped branch is enough to return unbounded rows."""
    out = _run("SELECT PROD_CD FROM inv UNION ALL SELECT PROD_CD FROM inv_data")
    assert out["ok"], out["error"]
    assert _capped(out["sql"]), f"a UNION branch is unbounded: {out['sql']}"


def test_cte_caps_the_final_select_not_the_body():
    """The CTE body sits in parentheses; capping it bounds nothing."""
    out = _run(
        "WITH recent AS (SELECT ORD_NUM, CUS_ID FROM orders) "
        "SELECT CUS_ID, COUNT(*) AS n FROM recent GROUP BY CUS_ID"
    )
    assert out["ok"], out["error"]
    assert _capped(out["sql"]), f"final SELECT left unbounded: {out['sql']}"


def test_cte_with_a_trailing_subquery_still_caps_the_outer_select():
    """'last SELECT in the string' is the wrong target when one is nested."""
    out = _run(
        "WITH recent AS (SELECT ORD_NUM, CUS_ID FROM orders) "
        "SELECT r.CUS_ID FROM recent r "
        "WHERE r.CUS_ID IN (SELECT CUS_ID FROM customer WHERE ACTIVE = 1)"
    )
    assert out["ok"], out["error"]
    assert _capped(out["sql"]), f"outer SELECT left unbounded: {out['sql']}"


def test_model_cannot_raise_the_cap_with_limit():
    """LIMIT is the model's request, not its authority.

    The configured maximum is a ceiling. A larger LIMIT gets clamped to it;
    a smaller one is honored.
    """
    out = _run("SELECT PROD_CD FROM inv LIMIT 5000")
    assert out["ok"], out["error"]
    assert all(v <= CAP for v in _top_values(out["sql"])), (
        f"model raised its own cap: {out['sql']}"
    )


def test_smaller_limit_is_honored():
    out = _run("SELECT PROD_CD FROM inv LIMIT 10")
    assert out["ok"], out["error"]
    assert _top_values(out["sql"]) == [10]


def test_limit_is_removed_for_sql_server():
    out = _run("SELECT PROD_CD FROM inv LIMIT 10")
    assert out["ok"], out["error"]
    assert not re.search(r"\blimit\b", out["sql"], re.I)


# --- the scanner must not be fooled by data -------------------------------


def test_literal_containing_sql_keywords_does_not_move_the_cap():
    """Parens and keywords inside a string are data, not structure."""
    sql = "SELECT CUS_NAME FROM customer WHERE NOTE = 'select (from) orders'"
    out = _run(sql)
    assert out["ok"], out["error"]
    assert _capped(out["sql"])
    assert "'select (from) orders'" in out["sql"], "literal was altered"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM t WHERE note = 'a (b'",
        "SELECT a FROM t WHERE note = 'top 1'",
    ],
)
def test_unbalanced_paren_inside_a_literal_is_harmless(sql: str):
    out = _run(sql)
    assert out["ok"], out["error"]
    assert _capped(out["sql"])
