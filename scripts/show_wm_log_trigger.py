import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections


def main():
    conn, _ = get_db_connections()
    cur = conn.cursor()
    for name in (
        "TRIG_WM_LOG_BY_WMS_WEB",
        "add_wm_skufile",
    ):
        cur.execute("SELECT OBJECT_DEFINITION(OBJECT_ID(?))", (name,))
        txt = cur.fetchone()[0]
        print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
        print(txt or "(not found)")

    for proc in (
        "proc_wm_add_wm_binfile_from_wm_log",
        "proc_wm_add_wm_skufile_from_wm_log",
        "proc_wm_update_wm_binfile_from_wm_log",
    ):
        cur.execute("SELECT OBJECT_DEFINITION(OBJECT_ID(?))", (proc,))
        txt = cur.fetchone()[0]
        print(f"\n{'=' * 60}\n{proc}\n{'=' * 60}")
        print((txt or "(not found)")[:4000])

    conn.close()


if __name__ == "__main__":
    main()
