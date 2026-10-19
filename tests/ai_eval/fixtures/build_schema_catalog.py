"""Build a synthetic schema_catalog.json for running the eval offline.

    python tests/ai_eval/fixtures/build_schema_catalog.py

Why this exists
---------------
The real catalog is produced by schema_scanner.scan_schema(), which needs
live connections to omsdata_FDC, oms_EF and onlinedb. Without one, the
agent cannot be run at all. This file is the schema the agent sees,
reconstructed from the application's own SQL.

It is a fixture, not a stand-in. The real catalog lives under output/ and
is gitignored -- it carries production structure and sample rows, so it
can never be committed and can never run in CI. A synthetic catalog is
the only way the eval chain runs anywhere but one laptop, so both files
have a permanent job:

    tests/ai_eval/fixtures/schema_catalog.json   committed, CI, this file
    output/ai_schema/schema_catalog.json         local only, real data

To use it, put it where load_catalog() looks:

    mkdir -p output/ai_schema
    cp tests/ai_eval/fixtures/schema_catalog.json output/ai_schema/

load_catalog() reads that path when the file exists and only falls back
to scanning when it does not, so nothing in py/ needs to change.

Table names are real
--------------------
The first version of this fixture held 19 tables, nine of which were
relevant to any given question. Production has 518. Retrieval picking 6
of 19 is not the same problem as picking 6 of 518, and measuring the
easy one produces numbers that are optimistic for a reason that has
nothing to do with the model.

production_table_names.txt holds all 518 names, read from the app's own
/api/ai/schema endpoint. Every name there that is not defined with
columns below is injected as a bare name. Retrieval scores against table
names, descriptions and column names, so a name with no columns still
competes -- which is the point.

Columns are only defined for the ten core tables, and every one of them
was read out of production SQL or schema_overrides.json. Where a column
could not be confirmed it is left out: an absent column is a known gap,
a fabricated one is a silent wrong answer, and these fixtures already
made that mistake once.

Sample rows ARE invented, and look it. They have to exist, because the
prompt carries one sample row per table and the no_sample_*_leak cases
are about whether the model copies those values into a WHERE clause it
was never asked for.

Dates
-----
OLG_DT, ORD_DT, INVS_DT and LOG_DATE are an OMS day count, not datetimes
and not YYYYMMDD. 81088 is 2023-01-01. Sample values are generated from
real dates through oms_day() so they stay internally consistent.
"""
from __future__ import annotations

import importlib.util
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
OUT = HERE / "schema_catalog.json"
NAMES_FILE = HERE / "production_table_names.txt"

OMS_EPOCH_DAY = 81088
OMS_EPOCH_DATE = date(2023, 1, 1)

CORE = (
    "inv", "inv_data", "ord_log", "orders", "customer",
    "invoice", "invt_log", "plog", "bin_pur", "pur_ord",
)


def oms_day(when: date) -> int:
    """Real date -> the integer the OMS stores.

    Inverse of the conversion the app uses:
        DATEADD(DAY, DATEDIFF(DAY, '19000101', col) - 81088, '20230101')
    """
    return OMS_EPOCH_DAY + (when - OMS_EPOCH_DATE).days


def _load_by_path(alias: str, relative: str):
    """Load a project module by file path.

    The project's top-level package is named `py`, which collides with
    the py.py shim pytest ships, so a plain import cannot resolve it.
    """
    path = REPO_ROOT / relative
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def production_table_names() -> list[str]:
    names = []
    for line in NAMES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line)
    return names


def table(
    name: str,
    columns: dict[str, str],
    *,
    primary_keys: list[str] | None = None,
    indexes: list[str] | None = None,
    samples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One table, shaped exactly as schema_scanner._empty_table builds it.

    `columns` maps column name -> SQL type. The scanner stores the column
    name as its own description and lets schema_overrides.json replace
    it, so this mirrors that: descriptions are not written here.
    """
    return {
        "name": name,
        "description": "",
        "columns": {col: col for col in columns},
        "column_types": dict(columns),
        "primary_keys": primary_keys or [],
        "foreign_keys": [],
        "indexes": indexes or [],
        "samples": samples or [],
    }


def core_tables(*, with_samples: bool) -> dict[str, Any]:
    """The ten tables the eval cases actually query, with real columns."""
    d1 = oms_day(date(2026, 9, 28))
    d2 = oms_day(date(2026, 10, 2))
    d3 = oms_day(date(2026, 10, 9))

    tables = {
        "inv": table(
            "inv",
            {
                "PROD_CD": "varchar",
                "DESCRIP": "varchar null",
                "UNIT_COLOR": "varchar null",
                "ALT_CD": "varchar null",
                "CLASS_CD": "varchar null",
                "RETAIL_PRS": "decimal null",
            },
            primary_keys=["PROD_CD"],
            indexes=["PK_inv (PROD_CD)", "IX_inv_class (CLASS_CD)"],
            samples=[{
                "PROD_CD": "M1141 BK",
                "DESCRIP": "SAMPLE ROW - NOT REAL DATA",
                "UNIT_COLOR": "BK",
                "ALT_CD": "",
                "CLASS_CD": "HB",
                "RETAIL_PRS": 39.99,
            }],
        ),
        "inv_data": table(
            "inv_data",
            {
                "PROD_CD": "varchar",
                "WHS_NUM": "int",
                "IN_STOCK": "int null",
                "AVAIL_QTY": "int null",
                "ON_ORDER_QTY": "int null",
            },
            primary_keys=["PROD_CD", "WHS_NUM"],
            indexes=["PK_inv_data (PROD_CD, WHS_NUM)"],
            samples=[{
                "PROD_CD": "M1141 BK", "WHS_NUM": 827,
                "IN_STOCK": 144, "AVAIL_QTY": 96, "ON_ORDER_QTY": 240,
            }],
        ),
        "ord_log": table(
            "ord_log",
            {
                "ORD_NUM": "int",
                "COMM_LN": "int",
                "CUS_ID": "int null",
                "PROD_CD": "varchar null",
                "UT_DESC": "varchar null",
                "ORDER_QTY": "int null",
                "UNIT_PRS": "decimal null",
                "PC_UNIT": "int null",
                "OLG_DT": "int null",
            },
            primary_keys=["ORD_NUM", "COMM_LN"],
            indexes=[
                "PK_ord_log (ORD_NUM, COMM_LN)",
                "IX_ord_log_prod (PROD_CD)",
                "IX_ord_log_cus (CUS_ID)",
            ],
            samples=[{
                "ORD_NUM": 88123, "COMM_LN": 1, "CUS_ID": 1,
                "PROD_CD": "M1141 BK",
                "UT_DESC": "SAMPLE ROW - NOT REAL DATA",
                "ORDER_QTY": 12, "UNIT_PRS": 18.5, "PC_UNIT": 1,
                "OLG_DT": d1,
            }],
        ),
        "orders": table(
            "orders",
            {
                "ORD_NUM": "int",
                "CUS_ID": "int null",
                "ORD_DT": "int null",
                "SHIP_DT": "int null",
                "CAN_DT": "int null",
                "PO_NUM": "varchar null",
                "INVS_NUM": "int null",
                "ORD_AMT": "decimal null",
                "HANDL_FEE": "decimal null",
                "MISC_CHG": "decimal null",
                "DISCOUNT": "decimal null",
                "PAID_AMT": "decimal null",
                "STATUS": "varchar null",
                "ORD_TYPE": "varchar null",
                "TRK_NUM": "varchar null",
                "ATTN": "varchar null",
            },
            primary_keys=["ORD_NUM"],
            indexes=["PK_orders (ORD_NUM)", "IX_orders_cus (CUS_ID)"],
            samples=[{
                "ORD_NUM": 88123, "CUS_ID": 1, "ORD_DT": d1, "SHIP_DT": d2,
                "CAN_DT": None, "PO_NUM": "SAMPLE-PO", "INVS_NUM": 50011,
                "ORD_AMT": 222.0, "HANDL_FEE": 15.0, "MISC_CHG": 0.0,
                "DISCOUNT": 0.0, "PAID_AMT": 237.0, "STATUS": "OP",
                "ORD_TYPE": "R", "TRK_NUM": "",
                "ATTN": "SAMPLE ROW - NOT REAL DATA",
            }],
        ),
        "customer": table(
            "customer",
            {"CUS_ID": "int", "CUS_NM": "varchar null"},
            primary_keys=["CUS_ID"],
            indexes=["PK_customer (CUS_ID)", "IX_customer_nm (CUS_NM)"],
            samples=[{"CUS_ID": 1, "CUS_NM": "SAMPLE ROW - NOT REAL DATA"}],
        ),
        "invoice": table(
            "invoice",
            {
                "INVS_NUM": "int",
                "CUS_ID": "int null",
                "INVS_DT": "int null",
                "INVS_AMT": "decimal null",
                "HANDL_FEE": "decimal null",
                "MISC_CHG": "decimal null",
                "DISCOUNT": "decimal null",
                "INVS_TAX": "decimal null",
                "PAID_AMT": "decimal null",
                "PAID_BY": "varchar null",
                "CHK_NUM": "varchar null",
                "INVS_TYPE": "varchar null",
            },
            primary_keys=["INVS_NUM"],
            indexes=["PK_invoice (INVS_NUM)", "IX_invoice_cus (CUS_ID)"],
            samples=[{
                "INVS_NUM": 50011, "CUS_ID": 1, "INVS_DT": d2,
                "INVS_AMT": 237.0, "HANDL_FEE": 15.0, "MISC_CHG": 0.0,
                "DISCOUNT": 0.0, "INVS_TAX": 0.0, "PAID_AMT": 237.0,
                "PAID_BY": "CHK", "CHK_NUM": "SAMPLE", "INVS_TYPE": "I",
            }],
        ),
        "invt_log": table(
            "invt_log",
            {
                "INVS_NUM": "int", "COMM_LN": "int",
                "PROD_CD": "varchar null", "PROD_QTY": "int null",
            },
            primary_keys=["INVS_NUM", "COMM_LN"],
            indexes=["PK_invt_log (INVS_NUM, COMM_LN)"],
            samples=[{
                "INVS_NUM": 50011, "COMM_LN": 1,
                "PROD_CD": "M1141 BK", "PROD_QTY": 12,
            }],
        ),
        "plog": table(
            "plog",
            {
                "PUR_NUM": "varchar", "COMM_LN": "int",
                "PROD_CD": "varchar null", "LOG_QTY": "int null",
                "HDL_CHG": "int null", "LOG_DATE": "int null",
                "LOG_TIME": "int null",
            },
            primary_keys=["PUR_NUM", "COMM_LN"],
            indexes=["PK_plog (PUR_NUM, COMM_LN)", "IX_plog_prod (PROD_CD)"],
            samples=[{
                "PUR_NUM": "SAMPLE-CTR", "COMM_LN": 1,
                "PROD_CD": "M1141 BK", "LOG_QTY": 600, "HDL_CHG": 0,
                "LOG_DATE": d3, "LOG_TIME": 1430,
            }],
        ),
        "bin_pur": table(
            "bin_pur",
            {
                "PUR_NUM": "varchar", "PROD_CD": "varchar",
                "PO_QTY": "int null", "RCVD_QTY": "int null",
            },
            primary_keys=["PUR_NUM", "PROD_CD"],
            indexes=["PK_bin_pur (PUR_NUM, PROD_CD)"],
            samples=[{
                "PUR_NUM": "SAMPLE-CTR", "PROD_CD": "M1141 BK",
                "PO_QTY": 600, "RCVD_QTY": 0,
            }],
        ),
        "pur_ord": table(
            "pur_ord",
            {"PUR_NUM": "varchar", "VENDOR_CD": "varchar null"},
            primary_keys=["PUR_NUM"],
            indexes=["PK_pur_ord (PUR_NUM)"],
            samples=[{"PUR_NUM": "SAMPLE-CTR", "VENDOR_CD": "SAMPLE"}],
        ),
    }

    if not with_samples:
        for spec in tables.values():
            spec["samples"] = []
    return tables


# Dated copies of core tables that exist in production. A snapshot of a
# table has that table's columns -- that is what makes it a snapshot --
# so these are given the base table's column structure rather than being
# left bare.
#
# This matters for retrieval, not for realism. schema_retriever scores a
# table on a blob built from its name, description and column names, so a
# table with no columns scores on its name alone and effectively never
# clears the selection threshold. Leaving these bare was the first
# version of this fixture, and it measured nothing: 518 bare tables
# scored exactly the same as 19.
#
# With the base table's columns, customer021111 has nearly the same blob
# as customer and competes with it. Whether it wins is the question the
# fixture now actually asks.
SNAPSHOTS = {
    "customer021111": "customer",
    "customer123015": "customer",
    "inv021111": "inv",
    "inv021715": "inv",
    "inv_data022619": "inv_data",
    "inv_data_20130521": "inv_data",
    "orders121112": "orders",
}


def snapshot_tables(base: dict[str, Any]) -> dict[str, Any]:
    """Dated copies of core tables, carrying the base table's columns.

    Sample rows are dropped: these are stale copies, and inventing
    sample values for them would imply the fixture knows what is in
    them. The column structure is the part that drives retrieval.
    """
    out: dict[str, Any] = {}
    for name, source in SNAPSHOTS.items():
        spec = base.get(source)
        if not spec:
            continue
        out[name.lower()] = table(
            name,
            dict(spec["column_types"]),
            primary_keys=list(spec["primary_keys"]),
            indexes=[],
            samples=[],
        )
    return out


def oms_tables(*, with_samples: bool) -> dict[str, Any]:
    """Core tables with columns, plus every other production name bare."""
    tables = core_tables(with_samples=with_samples)
    tables.update(snapshot_tables(tables))
    for name in production_table_names():
        key = name.lower()
        if key not in tables:
            tables[key] = table(name, {})
    return tables


def mysql_tables() -> dict[str, Any]:
    """onlinedb. No listing for this one, so only what the app references."""
    return {
        "products": table(
            "products",
            {"SKU": "varchar", "Location": "varchar", "Update_Date": "datetime null"},
            primary_keys=["SKU", "Location"],
            indexes=["PRIMARY (SKU, Location)"],
            samples=[{
                "SKU": "M1141 BK", "Location": "A-01-02",
                "Update_Date": "2026-10-09 11:20:00",
            }],
        ),
        "users": table(
            "users",
            {"id": "int", "username": "varchar", "created_at": "datetime null"},
            primary_keys=["id"],
            indexes=["PRIMARY (id)"],
        ),
        "help_articles": table(
            "help_articles", {"id": "int", "title": "varchar null"},
            primary_keys=["id"],
        ),
        "fdc_ai_chats": table(
            "fdc_ai_chats", {"id": "int", "created_at": "datetime null"},
            primary_keys=["id"],
        ),
    }


def db_entry(label: str, name: str, engine: str, tables: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": label,
        "name": name,
        "engine": engine,
        "table_count": len(tables),
        "tables": tables,
        "error": None,
    }


def build() -> dict[str, Any]:
    joins = _load_by_path("fdc_known_joins", "py/ai/database/known_joins.py")

    fdc_tables = oms_tables(with_samples=True)
    # scan_schema() scans oms_EF with sample_rows=False. Keeping that
    # asymmetry matters: ord_num_digit_scheme is about the two databases
    # differing.
    ef_tables = oms_tables(with_samples=False)

    databases = {
        "omsdata_FDC": db_entry("FDC", "omsdata_FDC", "sqlserver", fdc_tables),
        "oms_EF": db_entry("E_FAS / EF", "oms_EF", "sqlserver", ef_tables),
        "onlinedb": db_entry("MySQL app (onlinedb)", "onlinedb", "mysql", mysql_tables()),
    }

    return {
        # Not datetime.now(): a fixture that changes every rebuild makes
        # a diff with no meaning in it.
        "generated_at": "synthetic",
        "database": "omsdata_FDC",
        "table_count": len(fdc_tables),
        "tables": fdc_tables,
        "databases": databases,
        "known_joins": {
            "oms": joins.OMS_JOINS,
            "mysql": joins.MYSQL_JOINS,
            "cross_db": joins.CROSS_DB_JOINS,
        },
        "error": None,
        "synthetic": True,
    }


def report_noise_coverage(names: list[str]) -> None:
    """How much of the real name list the retriever's noise filter catches.

    Not a side errand: schema_retriever._is_noise_table is the only thing
    standing between a question about customers and a 2011 snapshot of
    the customer table. Printing the gap here keeps it visible every time
    the fixture is rebuilt.
    """
    retriever = _load_by_path("fdc_schema_retriever", "py/ai/schema_retriever.py")
    is_noise = retriever._is_noise_table

    caught = [n for n in names if is_noise(n)]
    dated = [n for n in names if re.search(r"\d{6,}$", n)]
    uncaught_dated = [n for n in dated if not is_noise(n)]
    core_snapshots = sorted(
        n for n in uncaught_dated
        if any(n.lower().startswith(c) for c in CORE)
    )

    print(f"  noise filter catches: {len(caught)} / {len(names)}")
    print(f"  dated-looking names:  {len(dated)}, uncaught: {len(uncaught_dated)}")
    if core_snapshots:
        print(f"  UNCAUGHT dated snapshots of core tables ({len(core_snapshots)}):")
        for name in core_snapshots:
            print(f"    {name}")


def main() -> int:
    catalog = build()
    OUT.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    fdc = catalog["databases"]["omsdata_FDC"]["tables"]
    with_cols = [k for k, v in fdc.items() if v["columns"]]
    cols = sum(len(t["columns"]) for t in fdc.values())
    print(f"wrote {OUT.relative_to(REPO_ROOT)}")
    print(f"  databases: {len(catalog['databases'])}")
    print(f"  FDC tables: {len(fdc)}  ({len(with_cols)} with columns, {cols} columns)")
    print(f"  dated snapshots carrying base columns: {len(SNAPSHOTS)}")
    print(f"  known joins: {len(catalog['known_joins']['oms'])}")
    print(f"  OMS day count check: 2023-01-01 -> {oms_day(date(2023, 1, 1))}")
    report_noise_coverage(production_table_names())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
