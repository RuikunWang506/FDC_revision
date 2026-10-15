"""Regression tests for sql_validator's row-cap and table-allowlist checks.

validate_sql is the last thing standing between a generated query and the
production database. Three guarantees are pinned here:

  1. Whatever the model wrote, the statement that runs is row-bounded.
  2. The tables it reads are identified correctly -- aliases, CTE names and
     subquery aliases are not tables, which is why this needs a parser.
  3. Enforcement of the allowlist is off by default and the verdict is
     reported either way, so the rule can be measured before it blocks.

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
    site-packages/py.py wins. Adding __init__.py inverts the collision and
    breaks pytest's own `import py`. The collision is the package name
    itself, so loading by path sidesteps resolution entirely. Two existing
    tests in this directory already do the same thing.
    """
    spec = importlib.util.spec_from_file_location(alias, _ROOT / relative)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {relative}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_validator = _load_by_path("fdc_sql_validator", "py/ai/sql_validator.py")
validate_sql = _validator.validate_sql
referenced_tables = _validator.referenced_tables

CAP = 100


def _capped(sql: str) -> bool:
    return has_outer_row_cap(normalize_sql(sql))


def _top_values(sql: str) -> list[int]:
    return [int(m.group(1)) for m in re.finditer(r"\btop\s*\(?\s*(\d+)", sql, re.I)]


def _run(sql: str) -> dict:
    """Default path: allowlist computed but not enforced."""
    return validate_sql(sql, default_limit=CAP, enforce_allowlist=False)


def _enforced(sql: str, allowed=None) -> dict:
    return validate_sql(
        sql, default_limit=CAP, allowed_tables=allowed, enforce_allowlist=True
    )


# --- row cap: behavior that already worked, and must keep working --------


def test_plain_select_gets_a_cap():
    out = _run("SELECT CUS_ID, CUS_NAME FROM customer")
    assert out["ok"], out["error"]
    assert _capped(out["sql"])


def test_existing_cap_below_the_limit_is_left_alone():
    out = _run("SELECT TOP 5 PROD_CD FROM inv ORDER BY PROD_CD")
    assert out["ok"], out["error"]
    assert _top_values(out["sql"]) == [5]


def test_existing_cap_above_the_limit_is_clamped_down():
    out = _run("SELECT TOP 9999 PROD_CD FROM inv")
    assert out["ok"], out["error"]
    assert all(v <= CAP for v in _top_values(out["sql"]))


def test_distinct_keeps_its_place():
    out = _run("SELECT DISTINCT PROD_CD FROM ord_log")
    assert out["ok"], out["error"]
    assert re.search(r"select\s+distinct\s+top\s*\(?\s*\d+", out["sql"], re.I)


def test_write_statement_still_rejected():
    assert not validate_sql("DELETE FROM ord_log WHERE ORD_NUM < 1000")["ok"]


def test_multiple_statements_still_rejected():
    assert not validate_sql("SELECT TOP 5 a FROM ord_log; DROP TABLE ord_log")["ok"]


# --- row cap: the five bugs fixed in the outer-SELECT change -------------


def test_nested_top_does_not_satisfy_the_outer_cap():
    out = _run(
        "SELECT c.CUS_ID, c.CUS_NAME, a.ORD_AMT "
        "FROM customer c "
        "JOIN (SELECT TOP 1 CUS_ID, ORD_AMT FROM orders ORDER BY ORD_DATE DESC) a "
        "ON c.CUS_ID = a.CUS_ID"
    )
    assert out["ok"], out["error"]
    assert _capped(out["sql"]), f"outer SELECT left unbounded: {out['sql']}"


def test_union_caps_every_branch():
    out = _run("SELECT PROD_CD FROM inv UNION ALL SELECT PROD_CD FROM inv_data")
    assert out["ok"], out["error"]
    assert _capped(out["sql"]), f"a UNION branch is unbounded: {out['sql']}"


def test_cte_caps_the_final_select_not_the_body():
    out = _run(
        "WITH recent AS (SELECT ORD_NUM, CUS_ID FROM orders) "
        "SELECT CUS_ID, COUNT(*) AS n FROM recent GROUP BY CUS_ID"
    )
    assert out["ok"], out["error"]
    assert _capped(out["sql"]), f"final SELECT left unbounded: {out['sql']}"


def test_cte_with_a_trailing_subquery_still_caps_the_outer_select():
    out = _run(
        "WITH recent AS (SELECT ORD_NUM, CUS_ID FROM orders) "
        "SELECT r.CUS_ID FROM recent r "
        "WHERE r.CUS_ID IN (SELECT CUS_ID FROM customer WHERE ACTIVE = 1)"
    )
    assert out["ok"], out["error"]
    assert _capped(out["sql"]), f"outer SELECT left unbounded: {out['sql']}"


def test_model_cannot_raise_the_cap_with_limit():
    out = _run("SELECT PROD_CD FROM inv LIMIT 5000")
    assert out["ok"], out["error"]
    assert all(v <= CAP for v in _top_values(out["sql"]))


def test_smaller_limit_is_honored():
    out = _run("SELECT PROD_CD FROM inv LIMIT 10")
    assert out["ok"], out["error"]
    assert _top_values(out["sql"]) == [10]


def test_limit_is_removed_for_sql_server():
    out = _run("SELECT PROD_CD FROM inv LIMIT 10")
    assert out["ok"], out["error"]
    assert not re.search(r"\blimit\b", out["sql"], re.I)


# --- the scanner must not be fooled by data ------------------------------


def test_literal_containing_sql_keywords_does_not_move_the_cap():
    sql = "SELECT CUS_NAME FROM customer WHERE NOTE = 'select (from) orders'"
    out = _run(sql)
    assert out["ok"], out["error"]
    assert _capped(out["sql"])
    assert "'select (from) orders'" in out["sql"], "literal was altered"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM ord_log WHERE note = 'a (b'",
        "SELECT a FROM ord_log WHERE note = 'top 1'",
    ],
)
def test_unbalanced_paren_inside_a_literal_is_harmless(sql: str):
    out = _run(sql)
    assert out["ok"], out["error"]
    assert _capped(out["sql"])


# --- table extraction ----------------------------------------------------
# The cases that motivated parsing rather than matching. In the raw text a
# table name, an alias and a CTE name are all just words.


def test_alias_is_not_a_table():
    assert referenced_tables("SELECT o.ORD_NUM FROM orders o") == {"orders"}


def test_both_sides_of_a_join_are_collected():
    tables = referenced_tables(
        "SELECT c.CUS_NAME FROM orders o JOIN customer c ON o.CUS_ID = c.CUS_ID"
    )
    assert tables == {"orders", "customer"}


def test_cte_name_is_not_a_table():
    tables = referenced_tables(
        "WITH recent AS (SELECT ORD_NUM FROM orders) SELECT ORD_NUM FROM recent"
    )
    assert tables == {"orders"}, "the CTE name leaked in as a table"


def test_subquery_alias_is_not_a_table():
    assert referenced_tables("SELECT t.PROD_CD FROM (SELECT PROD_CD FROM inv) t") == {"inv"}


def test_table_inside_a_subquery_is_collected():
    tables = referenced_tables(
        "SELECT CUS_ID FROM customer WHERE CUS_ID IN (SELECT CUS_ID FROM orders)"
    )
    assert tables == {"customer", "orders"}


def test_schema_qualified_name_resolves_to_the_table():
    assert referenced_tables("SELECT a FROM omsdata_FDC.dbo.orders") == {"orders"}


def test_table_named_in_a_string_literal_is_not_a_table():
    tables = referenced_tables(
        "SELECT CUS_NAME FROM customer WHERE NOTE = 'see the users table'"
    )
    assert tables == {"customer"}


def test_bracketed_identifier_resolves_to_the_table():
    """T-SQL lets a table name contain spaces and parentheses.

    This is the exact shape the parenthesis-depth scanner cannot handle,
    and the reason the row-cap check is still an approximation while this
    one is not.
    """
    assert referenced_tables("SELECT a FROM [Order Details]") == {"order details"}


# --- the allowlist, enforced ---------------------------------------------


def test_credentials_table_is_blocked_when_enforced():
    """The gap this change exists to close.

    Nothing in the keyword blacklist stops this: it is a plain, valid,
    read-only SELECT. The only thing wrong with it is the table.
    """
    out = _enforced("SELECT TOP 500 USERNAME, PASSWORD_HASH FROM users")
    assert not out["ok"]
    assert out["blocked_tables"] == ["users"]


def test_blocked_table_hidden_behind_a_join_is_still_caught():
    out = _enforced(
        "SELECT TOP 10 c.CUS_NAME, u.PASSWORD_HASH "
        "FROM customer c JOIN users u ON c.CUS_ID = u.CUS_ID"
    )
    assert not out["ok"]
    assert out["blocked_tables"] == ["users"]


def test_blocked_table_inside_a_subquery_is_still_caught():
    out = _enforced(
        "SELECT TOP 10 CUS_ID FROM customer WHERE CUS_ID IN (SELECT CUS_ID FROM users)"
    )
    assert not out["ok"]
    assert out["blocked_tables"] == ["users"]


def test_caller_supplied_allowlist_is_what_counts():
    """The production rule: the agent may only query what it was shown."""
    assert not _enforced("SELECT TOP 5 PROD_CD FROM inv", allowed=["ord_log"])["ok"]
    assert _enforced("SELECT TOP 5 PROD_CD FROM inv", allowed=["inv"])["ok"]


def test_unparseable_sql_is_refused_when_enforced():
    out = _enforced("SELECT TOP 5 FROM WHERE ORDER BY (((")
    assert not out["ok"]
    assert out["parse_error"]


# --- the allowlist, in shadow mode ---------------------------------------


def test_shadow_mode_reports_but_does_not_block():
    """Off by default: the verdict is recorded, the query still runs.

    This is what makes the change safe to deploy before anyone knows what
    the allowlist should contain.
    """
    out = _run("SELECT TOP 10 USERNAME FROM users")
    assert out["ok"], out["error"]
    assert out["blocked_tables"] == ["users"]
    assert out["allowlist_enforced"] is False


def test_shadow_mode_does_not_block_on_a_parse_failure():
    out = _run("SELECT TOP 5 FROM WHERE ORDER BY (((")
    assert out["parse_error"]
    assert out["blocked_tables"] == []


def test_verdict_is_present_on_an_allowed_query():
    out = _run("SELECT TOP 5 PROD_CD FROM inv")
    assert out["ok"], out["error"]
    assert out["tables"] == ["inv"]
    assert out["blocked_tables"] == []
