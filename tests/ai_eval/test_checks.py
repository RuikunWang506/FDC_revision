"""Tests for the assertion engine itself.

These run with no database, no model, no network — pytest tests/ai_eval/
should pass on a laptop with nothing configured. If the engine is wrong,
every eval number it produces is wrong, so it gets its own tests.
"""
from __future__ import annotations

from tests.ai_eval.checks import (
    check_case,
    has_outer_row_cap,
    normalize_sql,
    summarize,
)


def test_normalize_strips_comments_and_collapses_space():
    sql = "SELECT  TOP 10\n  sku -- the comment\nFROM inv"
    assert normalize_sql(sql) == "select top 10 sku from inv"


def test_normalize_blanks_string_literals():
    # A literal must not be read as SQL: this one contains 'DROP TABLE'.
    sql = "SELECT TOP 5 name FROM customer WHERE note = 'DROP TABLE users'"
    out = normalize_sql(sql)
    assert "drop table users" not in out
    assert "customer" in out


def test_word_boundary_prevents_false_positive():
    # 'sum' must not match inside 'summary_total'.
    case = {"id": "t", "question": "q", "must_not_contain": ["sum"]}
    outcome = {"sql": "SELECT TOP 5 summary_total FROM ord_log"}
    assert check_case(case, outcome).passed


def test_multiword_fragment_matches_across_whitespace():
    case = {"id": "t", "question": "q", "must_contain": ["group by"]}
    outcome = {"sql": "SELECT TOP 5 sku, SUM(qty) FROM ord_log\n  GROUP   BY sku"}
    assert check_case(case, outcome).passed


def test_select_star_is_rejected_by_default():
    case = {"id": "t", "question": "q"}
    outcome = {"sql": "SELECT TOP 10 * FROM ord_log"}
    result = check_case(case, outcome)
    assert not result.passed
    assert any("SELECT *" in f for f in result.failures)


def test_select_star_allowed_when_case_opts_in():
    case = {"id": "t", "question": "q", "allow_select_star": True}
    outcome = {"sql": "SELECT TOP 10 * FROM ord_log"}
    assert check_case(case, outcome).passed


def test_missing_row_cap_is_rejected():
    case = {"id": "t", "question": "q"}
    outcome = {"sql": "SELECT sku FROM ord_log"}
    result = check_case(case, outcome)
    assert any("row cap" in f for f in result.failures)


# --- outer-SELECT row cap -------------------------------------------------
# This is the subtle one. sql_validator's _inject_top caps the first TOP it
# finds anywhere in the statement, so a nested TOP satisfies its check while
# the outer query stays unbounded. These tests pin down the correct behavior.


def test_outer_cap_present():
    assert has_outer_row_cap(normalize_sql("SELECT TOP 100 a FROM t"))


def test_outer_cap_with_distinct():
    assert has_outer_row_cap(normalize_sql("SELECT DISTINCT TOP 100 a FROM t"))


def test_outer_cap_parenthesized_form():
    assert has_outer_row_cap(normalize_sql("SELECT TOP (100) a FROM t"))


def test_nested_top_does_not_count_as_outer_cap():
    sql = normalize_sql(
        "SELECT c.id, a.amt FROM customer c "
        "JOIN (SELECT TOP 1 id, amt FROM orders ORDER BY d DESC) a ON c.id = a.id"
    )
    assert not has_outer_row_cap(sql)


def test_cte_body_is_not_the_outer_select():
    # The CTE body sits inside parentheses; the final SELECT is the outer one.
    capped = normalize_sql(
        "WITH recent AS (SELECT id FROM orders) SELECT TOP 10 id FROM recent"
    )
    uncapped = normalize_sql(
        "WITH recent AS (SELECT TOP 10 id FROM orders) SELECT id FROM recent"
    )
    assert has_outer_row_cap(capped)
    assert not has_outer_row_cap(uncapped)


def test_union_requires_every_branch_capped():
    both = normalize_sql("SELECT TOP 5 a FROM t1 UNION SELECT TOP 5 a FROM t2")
    one = normalize_sql("SELECT TOP 5 a FROM t1 UNION SELECT a FROM t2")
    assert has_outer_row_cap(both)
    assert not has_outer_row_cap(one)


def test_trailing_limit_accepted():
    # Kept so the check still works if the target ever moves off SQL Server.
    assert has_outer_row_cap(normalize_sql("SELECT a FROM t ORDER BY a LIMIT 50"))


def test_keep_literals_lets_a_case_assert_on_a_value():
    # Asserting on a literal's *content* needs the flag: by default
    # 'A1234' is blanked to '' before matching.
    outcome = {"sql": "SELECT TOP 5 qty FROM ord_log WHERE PROD_CD = 'A1234'"}
    pattern = r"prod_cd\s*=\s*'a1234'"

    with_flag = {"id": "t", "question": "q", "keep_literals": True,
                 "must_match": [pattern]}
    without_flag = {"id": "t", "question": "q", "must_match": [pattern]}

    assert check_case(with_flag, outcome).passed
    assert not check_case(without_flag, outcome).passed


def test_blanked_literal_still_reveals_an_equality_filter():
    # The default is usually what you want for leak detection: the opening
    # quote survives blanking, so one pattern catches a filter on any value.
    case = {"id": "t", "question": "q", "must_not_match": [r"\bprod_cd\s*=\s*'"]}
    leaked = {"sql": "SELECT TOP 5 qty FROM ord_log WHERE PROD_CD = 'A1234'"}
    clean = {"sql": "SELECT TOP 5 PROD_CD, SUM(qty) FROM ord_log GROUP BY PROD_CD"}
    assert not check_case(case, leaked).passed
    assert check_case(case, clean).passed


def test_expected_table_found_via_tables_used():
    case = {"id": "t", "question": "q", "expected_tables": ["ord_log"]}
    outcome = {"sql": "SELECT TOP 5 sku FROM o", "tables_used": ["ord_log"]}
    assert check_case(case, outcome).passed


def test_forbidden_table_detected_in_sql_even_if_unclaimed():
    # The model may omit a table from tables_used while still querying it.
    case = {"id": "t", "question": "q", "forbidden_tables": ["inv_data"]}
    outcome = {"sql": "SELECT TOP 5 sku FROM inv_data", "tables_used": []}
    result = check_case(case, outcome)
    assert not result.passed
    assert any("inv_data" in f for f in result.failures)


def test_refusal_case_passes_when_no_sql():
    case = {"id": "t", "question": "q", "expect_refusal": True}
    assert check_case(case, {"sql": "", "error": "ambiguous"}).passed


def test_refusal_case_fails_when_sql_generated():
    case = {"id": "t", "question": "q", "expect_refusal": True}
    result = check_case(case, {"sql": "SELECT TOP 5 * FROM users"})
    assert not result.passed


def test_empty_sql_is_error_not_silent_pass():
    case = {"id": "t", "question": "q"}
    result = check_case(case, {"sql": "", "error": "model timeout"})
    assert result.status == "ERROR"
    assert not result.passed


def test_regex_assertions():
    case = {
        "id": "t",
        "question": "q",
        "must_not_match": [r"\bord_num\s*=\s*\d+\b"],
    }
    bad = {"sql": "SELECT TOP 5 sku FROM ord_log WHERE ord_num = 1"}
    good = {"sql": "SELECT TOP 5 sku FROM ord_log ORDER BY ord_date DESC"}
    assert not check_case(case, bad).passed
    assert check_case(case, good).passed


def test_summarize_buckets_failures():
    results = [
        check_case({"id": "a", "question": "q"}, {"sql": "SELECT TOP 1 * FROM t"}),
        check_case({"id": "b", "question": "q"}, {"sql": "SELECT TOP 1 * FROM t"}),
        check_case({"id": "c", "question": "q"}, {"sql": "SELECT TOP 1 sku FROM t"}),
    ]
    stats = summarize(results)
    assert stats["total"] == 3
    assert stats["passed"] == 1
    assert stats["failed"] == 2
    assert stats["failure_histogram"]["SELECT * is not allowed"] == 2
