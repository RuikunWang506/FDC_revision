"""Meta-test: prove the assertions in cases.yaml actually catch things.

Without this, a mistyped regex in cases.yaml would pass everything
silently, and the eval would report a healthy pass rate while detecting
nothing. These tests replay known-good and known-bad SQL through the same
assertion engine the real eval uses.

No model, no database, no network.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.ai_eval.checks import check_case
from tests.ai_eval.fake_llm import outcome_from_scenario

HERE = Path(__file__).resolve().parent


def _load(name: str, key: str) -> dict:
    with (HERE / name).open(encoding="utf-8") as handle:
        return (yaml.safe_load(handle) or {}).get(key) or {}


CASES = {c["id"]: c for c in (_load("cases.yaml", "cases") or [])}
SCENARIOS = _load("scenarios.yaml", "scenarios")

GOOD = [(cid, s["good"]) for cid, s in SCENARIOS.items() if s.get("good") is not None]
BAD = [(cid, s["bad"]) for cid, s in SCENARIOS.items() if s.get("bad") is not None]


def test_every_scenario_names_a_real_case():
    """A scenario for a case id that no longer exists is dead weight."""
    unknown = sorted(set(SCENARIOS) - set(CASES))
    assert not unknown, f"scenarios reference unknown case ids: {unknown}"


@pytest.mark.parametrize("case_id,scenario", GOOD, ids=[c for c, _ in GOOD])
def test_good_sql_passes(case_id: str, scenario: dict):
    """Correct SQL must not trip any assertion.

    A failure here means the case is too strict — it would mark a working
    agent as broken, which is worse than missing a bug.
    """
    result = check_case(CASES[case_id], outcome_from_scenario(scenario))
    assert result.passed, (
        f"[{case_id}] good SQL was rejected: {result.failures}"
    )


@pytest.mark.parametrize("case_id,scenario", BAD, ids=[c for c, _ in BAD])
def test_bad_sql_is_caught(case_id: str, scenario: dict):
    """Known-bad SQL must fail, and fail for the stated reason.

    Checking *which* assertion fired matters: a case can reject bad SQL by
    accident (say, the row-cap guard) while the assertion it was written
    for is broken. That would look like coverage without being coverage.
    """
    result = check_case(CASES[case_id], outcome_from_scenario(scenario))
    assert not result.passed, f"[{case_id}] bad SQL slipped through"

    expected = scenario.get("catches")
    if expected:
        assert any(expected in f for f in result.failures), (
            f"[{case_id}] caught, but not for the stated reason.\n"
            f"  expected: {expected}\n"
            f"  actual:   {result.failures}"
        )


def test_scenario_coverage_is_visible():
    """Not a pass/fail gate — reports which cases have no scenario yet.

    Run with -s to see it. Cases without scenarios are untested
    assertions: they may be right, but nothing has confirmed it.
    """
    uncovered = sorted(set(CASES) - set(SCENARIOS))
    if uncovered:
        print(
            f"\n{len(uncovered)} of {len(CASES)} cases have no scenario yet:\n  "
            + "\n  ".join(uncovered)
        )
