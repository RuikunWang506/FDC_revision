import importlib.util
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "fdc_sync_guard", ROOT / "py/dam/sync_guard.py"
)
sync_guard = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(sync_guard)


class SyncGuardTests(unittest.TestCase):
    def test_second_claim_fails_until_release(self):
        status = {"is_running": False}
        self.assertTrue(sync_guard.claim_sync_run(status))
        self.assertTrue(status["is_running"])
        self.assertFalse(sync_guard.claim_sync_run(status))
        sync_guard.release_sync_run(status)
        self.assertFalse(status["is_running"])
        self.assertTrue(sync_guard.claim_sync_run(status))

    def test_concurrent_claims_only_one_wins(self):
        status = {"is_running": False}
        wins = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            if sync_guard.claim_sync_run(status):
                wins.append(1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(wins), 1)
        self.assertTrue(status["is_running"])


if __name__ == "__main__":
    unittest.main()
