"""Ask the retriever about tables nobody wrote an alias for.

    python probe_retrieval.py

No model, no database -- this calls schema_retriever.retrieve_schema
directly, so it runs in seconds.

Why
---
Three eval runs scored exactly 14/26 against fixtures holding 19 tables,
518 tables, and 518 tables with seven dated snapshots carrying their base
table's columns. Identical score, identical failure histogram, and not
one dated table in any retrieval result.

Reading the scoring explains it. _tokens() splits on
`[a-z0-9_]+|[\\u4e00-\\u9fff]+`, so a run of Chinese becomes ONE token:
"列出所有客户" tokenises to ['列出所有客户'], which cannot appear in any
table's blob because blobs are ASCII column names. That leaves two ways
for a table to score on a Chinese question:

    +12  an entry in schema_overrides.json's _table_aliases
    +15  the table's name appearing literally in the question

and the selection threshold is 4. For Chinese questions the alias map is
therefore the whole retriever. For English, a near-miss like
orders121112 scores +2 from the substring, still under the bar.

That is good news for snapshot contamination and bad news for coverage:
the alias map lists about ten tables and the database has 518. This
script measures which it is.

What to look for
----------------
Questions marked FALLBACK got the _CORE six because nothing scored ≥ 4.
The agent then writes confident SQL against tables that have nothing to
do with the question, and nothing downstream marks the answer as a
guess.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _ensure_py_package() -> None:
    target = str(REPO_ROOT / "py")
    existing = sys.modules.get("py")
    if existing is not None and target in list(getattr(existing, "__path__", [])):
        return
    package = types.ModuleType("py")
    package.__path__ = [target]
    sys.modules["py"] = package


_ensure_py_package()

from py.ai.schema_retriever import _CORE, _tokens, retrieve_schema  # noqa: E402

CORE_SET = {t.lower() for t in _CORE}

# Questions about real production tables that no alias covers, paired
# with the table a person would expect. Table names come from the
# production catalog listing.
CASES: list[tuple[str, str]] = [
    ("最近有哪些收货记录", "wm_rcvlog"),
    ("What was received into the warehouse recently?", "wm_rcvlog"),
    ("供应商列表", "venfile"),
    ("List all vendors", "venfile"),
    ("哪些箱子还没发出", "wm_casefile"),
    ("Which cases have not shipped?", "wm_casefile"),
    ("运费是多少", "shp_log"),
    ("What are the shipping charges?", "shp_log"),
    ("银行付款记录", "bnk_pmt"),
    ("Show bank payments", "bnk_pmt"),
    ("业务员的销售额", "salesman"),
    ("Sales by salesman", "salesman"),
    ("退货记录", "rma_file"),
    ("Show returns", "rma_file"),
    ("哪些库位有货", "bin_file"),
    ("Which bins hold stock?", "bin_file"),
    # Controls: these ARE in the alias map and should retrieve properly.
    ("列出所有客户", "customer"),
    ("哪些 SKU 有在途库存", "inv_data"),
]


def main() -> int:
    width = max(len(q) for q, _ in CASES)
    fallbacks = 0
    hits = 0

    print(f"{'question':<{width}}  {'expected':<14} retrieved")
    print("-" * (width + 60))

    for question, expected in CASES:
        got = list(retrieve_schema(question).get("table_names") or [])
        lowered = {t.lower() for t in got}
        is_fallback = lowered == CORE_SET or (
            len(lowered) == len(CORE_SET) and lowered <= CORE_SET
        )
        found = expected.lower() in lowered
        if is_fallback:
            fallbacks += 1
        if found:
            hits += 1
        mark = "FALLBACK" if is_fallback else ("hit " if found else "miss")
        print(f"{question:<{width}}  {expected:<14} [{mark}] {','.join(got)}")

    print()
    print(f"expected table retrieved: {hits} / {len(CASES)}")
    print(f"fell back to _CORE:       {fallbacks} / {len(CASES)}")
    print()
    print("tokenisation of a Chinese question:")
    for q in ("列出所有客户", "最近有哪些收货记录", "Which cases have not shipped?"):
        print(f"  {q!r} -> {_tokens(q)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
