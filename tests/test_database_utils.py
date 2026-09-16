import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from mysql.connector.errors import PoolError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from py.utilities import database_utils


class MysqlPoolRetryTests(unittest.TestCase):
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
        ):
            conn = database_utils.get_mysql_connection(retries=3)
        self.assertIsNone(conn)
        self.assertEqual(pool.get_connection.call_count, 3)


if __name__ == "__main__":
    unittest.main()
