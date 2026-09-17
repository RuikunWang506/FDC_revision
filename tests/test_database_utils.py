import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from mysql.connector.errors import PoolError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from py.utilities import database_utils


class MysqlPoolRetryTests(unittest.TestCase):
    def setUp(self):
        database_utils._checkouts.clear()
        database_utils._mysql_pool = None
        database_utils._last_reset_at = 0.0

    def test_retries_pool_exhaustion_then_succeeds(self):
        pool = MagicMock()
        good = MagicMock()
        good.is_connected.return_value = True
        pool.get_connection.side_effect = [PoolError("pool exhausted"), good]
        with patch.object(database_utils, "_get_mysql_pool", return_value=pool), patch(
            "py.utilities.database_utils.time.sleep"
        ) as slept:
            conn = database_utils.get_mysql_connection()
        self.assertIs(conn, good)
        self.assertEqual(pool.get_connection.call_count, 2)
        slept.assert_called_once()

    def test_returns_none_after_retries_exhausted(self):
        pool = MagicMock()
        pool.get_connection.side_effect = PoolError("pool exhausted")
        with patch.object(database_utils, "_get_mysql_pool", return_value=pool), patch(
            "py.utilities.database_utils.time.sleep"
        ), patch.object(database_utils, "reset_mysql_pool", return_value=False):
            conn = database_utils.get_mysql_connection(retries=3)
        self.assertIsNone(conn)
        self.assertEqual(pool.get_connection.call_count, 3)

    def test_unpooled_skips_app_pool(self):
        pool = MagicMock()
        direct = MagicMock()
        direct.is_connected.return_value = True
        with patch.object(database_utils, "_get_mysql_pool", return_value=pool), patch(
            "py.utilities.database_utils.mysql.connector.connect", return_value=direct
        ) as connect:
            conn = database_utils.get_mysql_connection_unpooled()
        self.assertIs(conn, direct)
        pool.get_connection.assert_not_called()
        connect.assert_called_once()

    def test_exhausted_pool_resets_then_succeeds(self):
        pool = MagicMock()
        good = MagicMock()
        good.is_connected.return_value = True
        pool.get_connection.side_effect = [
            PoolError("pool exhausted"),
            PoolError("pool exhausted"),
            PoolError("pool exhausted"),
            good,
        ]
        with patch.object(database_utils, "_get_mysql_pool", return_value=pool), patch(
            "py.utilities.database_utils.time.sleep"
        ), patch.object(database_utils, "_log"):
            conn = database_utils.get_mysql_connection(retries=3)
        self.assertIs(conn, good)
        self.assertGreaterEqual(database_utils._pool_resets, 1)

    def test_cleanup_force_releases_stale_checkout(self):
        conn = MagicMock()
        database_utils._checkouts[id(conn)] = {
            "conn": conn,
            "t": time.time() - 500,
            "thread": "stale-thread",
        }
        with patch.object(database_utils, "_log"):
            stats = database_utils.cleanup_mysql_pool(max_hold_sec=60)
        conn.close.assert_called()
        self.assertEqual(stats["checked_out"], 0)


if __name__ == "__main__":
    unittest.main()
