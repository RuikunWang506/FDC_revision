"""A stand-in for the Ollama client, so agent logic can be tested without a model.

Two uses:

1. Offline eval — feed the pipeline canned responses and check that the
   assertions in cases.yaml behave as intended.
2. Future unit tests of run_ai_query itself — monkeypatch ollama_client.chat
   with one of these and assert on control flow (retry fired? validator
   rejected? summarizer skipped on empty rows?) with no network and no GPU.

Everything here is deterministic: same input, same output, every run.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class Call:
    """One recorded chat() invocation, so tests can assert on what was sent."""
    messages: list[dict[str, str]]
    json_mode: bool
    timeout: int

    @property
    def system(self) -> str:
        for m in self.messages:
            if m.get("role") == "system":
                return str(m.get("content") or "")
        return ""

    @property
    def user_turns(self) -> list[str]:
        return [str(m.get("content") or "") for m in self.messages if m.get("role") == "user"]

    @property
    def is_retry(self) -> bool:
        """run_ai_query's retry sends a second user turn with correction text."""
        return len(self.user_turns) > 1


class FakeOllama:
    """Replays canned responses in place of a real model.

    responses maps a lookup key to either a literal string or a callable
    taking the messages list. The key is matched against the first user
    turn by substring, so a case id or a distinctive phrase works.

    on_missing controls what happens when nothing matches:
      "raise"   surface it — the default, so gaps in the fixture are loud
      "empty"   return '{}', exercising the no-SQL-generated path
      a string  return it verbatim
    """

    def __init__(
        self,
        responses: dict[str, str | Callable[[list[dict[str, str]]], str]] | None = None,
        *,
        on_missing: str = "raise",
    ) -> None:
        self.responses = responses or {}
        self.on_missing = on_missing
        self.calls: list[Call] = []

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        timeout: int = 120,
        json_mode: bool = False,
    ) -> str:
        self.calls.append(Call(messages=messages, json_mode=json_mode, timeout=timeout))

        # The summarizer call is not JSON mode; it just needs prose back.
        if not json_mode:
            return "（摘要占位）"

        haystack = " ".join(
            str(m.get("content") or "") for m in messages if m.get("role") == "user"
        )
        for key, value in self.responses.items():
            if key in haystack:
                return value(messages) if callable(value) else value

        if self.on_missing == "raise":
            raise KeyError(
                f"FakeOllama has no canned response matching: {haystack[:160]!r}"
            )
        if self.on_missing == "empty":
            return "{}"
        return self.on_missing

    # -- helpers for asserting on control flow ---------------------------

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def retried(self) -> bool:
        return any(c.is_retry for c in self.calls)

    def reset(self) -> None:
        self.calls.clear()


def sql_payload(
    sql: str,
    *,
    tables: list[str] | None = None,
    reason: str = "",
    wrap_in_fence: bool = False,
    trailing_prose: str = "",
) -> str:
    """Build a response in the shape run_ai_query's parser expects.

    wrap_in_fence and trailing_prose reproduce real model misbehavior —
    ```json fences and chatter around the object. _parse_sql_payload is
    supposed to survive both; these let a test prove it.
    """
    body = json.dumps(
        {"sql": sql, "tables_used": tables or [], "reason": reason},
        ensure_ascii=False,
    )
    if wrap_in_fence:
        body = f"```json\n{body}\n```"
    if trailing_prose:
        body = f"{body}\n\n{trailing_prose}"
    return body


def load_scenarios(path: str | Path) -> dict[str, dict[str, Any]]:
    """Read scenarios.yaml — canned outputs keyed by eval case id."""
    import yaml

    with Path(path).open(encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}
    return doc.get("scenarios") or {}


def outcome_from_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    """Turn a scenario entry into the dict shape check_case expects.

    Lets the assertions in cases.yaml be exercised against known-good and
    known-bad SQL without running the agent at all.
    """
    return {
        "sql": scenario.get("sql") or "",
        "tables_used": scenario.get("tables_used") or [],
        "error": scenario.get("error"),
        "ollama_ms": scenario.get("ollama_ms", 0),
        "sql_ms": scenario.get("sql_ms", 0),
        "elapsed_ms": scenario.get("elapsed_ms", 0),
    }
