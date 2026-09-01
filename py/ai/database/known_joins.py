"""Curated table/column connections from live FDC SQL (not guessed FKs).

OMS FDC and OMS EF share the same table names. MySQL onlinedb is the app DB.
Cross-db links are logical only (no FK).
"""
from __future__ import annotations

from typing import Any

# engine: sqlserver applies to both omsdata_FDC and oms_EF
OMS_JOINS: list[dict[str, str]] = [
    {
        'left': 'inv',
        'left_col': 'PROD_CD',
        'right': 'inv_data',
        'right_col': 'PROD_CD',
        'source': 'get_sku_inventory / product detail',
        'note': 'SKU master → warehouse qty (stock / ordered / available / incoming)',
    },
    {
        'left': 'ord_log',
        'left_col': 'PROD_CD',
        'right': 'inv',
        'right_col': 'PROD_CD',
        'source': 'who-bought / customer purchases',
        'note': 'Sales line SKU → item master',
    },
    {
        'left': 'ord_log',
        'left_col': 'CUS_ID',
        'right': 'customer',
        'right_col': 'CUS_ID',
        'source': 'get_sku_customers / get_order',
        'note': 'Order line → customer name',
    },
    {
        'left': 'ord_log',
        'left_col': 'ORD_NUM',
        'right': 'orders',
        'right_col': 'ORD_NUM',
        'source': 'get_order',
        'note': 'Order lines → order header (5-digit FDC / 4-digit EF)',
    },
    {
        'left': 'orders',
        'left_col': 'CUS_ID',
        'right': 'customer',
        'right_col': 'CUS_ID',
        'source': 'get_order / customer page',
        'note': 'Order header → customer',
    },
    {
        'left': 'orders',
        'left_col': 'INVS_NUM',
        'right': 'invoice',
        'right_col': 'INVS_NUM',
        'source': 'get_invoice',
        'note': 'Order header ↔ invoice number',
    },
    {
        'left': 'invoice',
        'left_col': 'CUS_ID',
        'right': 'customer',
        'right_col': 'CUS_ID',
        'source': 'get_invoice / invoice_lookup',
        'note': 'Invoice header → customer',
    },
    {
        'left': 'invt_log',
        'left_col': 'INVS_NUM',
        'right': 'invoice',
        'right_col': 'INVS_NUM',
        'source': 'get_invoice',
        'note': 'Invoice lines → invoice header',
    },
    {
        'left': 'invt_log',
        'left_col': 'PROD_CD',
        'right': 'inv',
        'right_col': 'PROD_CD',
        'source': 'invoice_lookup lines',
        'note': 'Invoiced SKU → item master',
    },
    {
        'left': 'plog',
        'left_col': 'PROD_CD',
        'right': 'inv',
        'right_col': 'PROD_CD',
        'source': 'get_sku_ocean / list_received_styles / boat',
        'note': 'PO/container lines. hdl_chg=0 ocean, hdl_chg>0 received',
    },
    {
        'left': 'plog',
        'left_col': 'PUR_NUM',
        'right': 'pur_ord',
        'right_col': 'PUR_NUM',
        'source': 'boat / container hub',
        'note': 'PO lines → purchase order header',
    },
    {
        'left': 'bin_pur',
        'left_col': 'PROD_CD',
        'right': 'inv',
        'right_col': 'PROD_CD',
        'source': 'ocean qty / receiving',
        'note': 'PO bin lines (PO_QTY / RCVD_QTY)',
    },
    {
        'left': 'bin_pur',
        'left_col': 'PUR_NUM',
        'right': 'pur_ord',
        'right_col': 'PUR_NUM',
        'source': 'ocean qty / receiving',
        'note': 'PO bin lines → PO header',
    },
]

MYSQL_JOINS: list[dict[str, str]] = [
    {
        'left': 'products',
        'left_col': 'SKU',
        'right': 'products',
        'right_col': 'Location',
        'source': 'get_sku_locations / location_service',
        'note': 'One row per SKU+bin. Same SKU can have many Location values.',
    },
]

CROSS_DB_JOINS: list[dict[str, str]] = [
    {
        'left_db': 'omsdata_FDC',
        'left': 'inv',
        'left_col': 'PROD_CD',
        'right_db': 'onlinedb',
        'right': 'products',
        'right_col': 'SKU',
        'source': 'product detail / location_service',
        'note': 'OMS SKU (PROD_CD) matches MySQL warehouse bin SKU. Same key on oms_EF.inv.',
    },
]

CORE_OMS_TABLES = (
    'inv',
    'inv_data',
    'ord_log',
    'orders',
    'customer',
    'invoice',
    'invt_log',
    'plog',
    'bin_pur',
    'pur_ord',
)

JOIN_KEYS = ('PROD_CD', 'CUS_ID', 'ORD_NUM', 'INVS_NUM', 'PUR_NUM', 'SKU')


def joins_prompt_text() -> str:
    lines = [
        'Known joins from FDC app SQL (use these; OMS often has no declared FKs):',
    ]
    for row in OMS_JOINS:
        lines.append(
            f"- {row['left']}.{row['left_col']} = {row['right']}.{row['right_col']}  ({row['note']})"
        )
    lines.append(
        '- MySQL onlinedb.products.SKU matches OMS inv.PROD_CD (warehouse bins; NL-SQL stays on SQL Server).'
    )
    return '\n'.join(lines)


def joins_for_tables(table_names: list[str]) -> list[dict[str, str]]:
    want = {str(n or '').lower() for n in table_names}
    out = []
    for row in OMS_JOINS:
        if row['left'].lower() in want or row['right'].lower() in want:
            out.append(row)
    return out


def connections_for_table(table_name: str, *, include_cross: bool = True) -> list[dict[str, Any]]:
    key = (table_name or '').lower()
    out: list[dict[str, Any]] = []
    for row in OMS_JOINS:
        if row['left'].lower() == key:
            out.append({**row, 'direction': 'out', 'kind': 'known'})
        elif row['right'].lower() == key:
            out.append({**row, 'direction': 'in', 'kind': 'known'})
    if include_cross:
        for row in CROSS_DB_JOINS:
            if row['left'].lower() == key or row['right'].lower() == key:
                out.append({**row, 'kind': 'cross-db'})
        for row in MYSQL_JOINS:
            if row['left'].lower() == key or row['right'].lower() == key:
                out.append({**row, 'kind': 'known-mysql'})
    return out
