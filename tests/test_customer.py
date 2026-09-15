import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from py.other.customer import (
    customer_column_for_field,
    format_oms_invoice_number,
    normalize_customer_field_value,
    order_lookup_amount,
    sort_customer_rows,
)


class CustomerOrderHelperTests(unittest.TestCase):
    def test_invoice_blank_or_zero_is_not_invoiced(self):
        self.assertEqual(format_oms_invoice_number(None), "not invoiced")
        self.assertEqual(format_oms_invoice_number(0), "not invoiced")
        self.assertEqual(format_oms_invoice_number("0"), "not invoiced")
        self.assertEqual(format_oms_invoice_number("not invoiced"), "not invoiced")

    def test_invoice_number_is_plain_digits(self):
        self.assertEqual(format_oms_invoice_number(790), "790")
        self.assertEqual(format_oms_invoice_number(861913), "861913")
        self.assertEqual(format_oms_invoice_number(33623), "33623")
        self.assertEqual(format_oms_invoice_number(33623.0), "33623")
        self.assertEqual(format_oms_invoice_number(" 33623 "), "33623")

    def test_order_amount_matches_order_lookup(self):
        self.assertEqual(order_lookup_amount(100, 5.5), 105.5)
        self.assertEqual(order_lookup_amount(None, None), 0.0)
        self.assertEqual(order_lookup_amount("12.50", "2.50"), 15.0)


class InvoiceLookupRoutingTests(unittest.TestCase):
    def test_short_and_long_ef_invoices_prefer_ef(self):
        from py.order.invoice_lookup import invoice_database_candidates

        self.assertEqual(
            invoice_database_candidates("790")[0],
            "oms_EF",
        )
        self.assertEqual(
            invoice_database_candidates("861913")[0],
            "oms_EF",
        )
        self.assertEqual(
            invoice_database_candidates("33623")[0],
            "omsdata_FDC",
        )

    def test_preferred_db_is_searched_first(self):
        from py.order.invoice_lookup import invoice_database_candidates

        self.assertEqual(
            invoice_database_candidates("790", preferred_db="oms_EF")[0],
            "oms_EF",
        )
        self.assertEqual(
            invoice_database_candidates("790", preferred_db="omsdata_FDC")[0],
            "omsdata_FDC",
        )


class CustomerFieldEditTests(unittest.TestCase):
    def test_editable_fields_map_to_oms_columns(self):
        self.assertEqual(customer_column_for_field('phone_1'), 'PHONE')
        self.assertEqual(customer_column_for_field('customer_name'), 'CUS_NM')
        self.assertEqual(customer_column_for_field('billing_zip'), 'ZIP')
        self.assertEqual(customer_column_for_field('terms_description'), 'TERM_DESC')

    def test_readonly_and_unknown_fields_are_rejected(self):
        self.assertIsNone(customer_column_for_field('customer_id'))
        self.assertIsNone(customer_column_for_field('status'))
        self.assertIsNone(customer_column_for_field('database'))
        self.assertIsNone(customer_column_for_field('CUS_NM; DROP TABLE customer'))

    def test_field_value_is_trimmed_and_length_capped(self):
        self.assertEqual(normalize_customer_field_value('  ERWAN  '), 'ERWAN')
        self.assertEqual(normalize_customer_field_value(None), '')
        with self.assertRaises(ValueError):
            normalize_customer_field_value('x' * 81)


class CustomerSearchHelperTests(unittest.TestCase):
    def test_sort_customer_rows_by_name(self):
        rows = sort_customer_rows([
            {'cus_nm': 'Zeta', 'cus_id': '2', 'database_origin': 'oms_EF'},
            {'cus_nm': 'alpha', 'cus_id': '9', 'database_origin': 'omsdata_FDC'},
            {'cus_nm': 'Alpha', 'cus_id': '1', 'database_origin': 'oms_EF'},
        ])
        self.assertEqual([row['cus_id'] for row in rows], ['1', '9', '2'])


if __name__ == "__main__":
    unittest.main()
