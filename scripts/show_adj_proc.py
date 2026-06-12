import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections


def main():
    conn, _ = get_db_connections()
    cur = conn.cursor()
    cur.execute(
        "SELECT OBJECT_DEFINITION(OBJECT_ID('proc_wm_update_wm_binfile_from_wm_log'))"
    )
    txt = cur.fetchone()[0] or ""
    # Find ADJ section
    idx = txt.upper().find("'ADJ'")
    if idx >= 0:
        print(txt[max(0, idx - 200) : idx + 800])
    else:
        print("ADJ not found in proc")
        print(txt[-3000:])

    # Check inv_data update path - search for inv_data in all wm procs
    cur.execute(
        """
        SELECT name FROM sys.procedures
        WHERE OBJECT_DEFINITION(object_id) LIKE '%inv_data%'
           OR OBJECT_DEFINITION(object_id) LIKE '%INV_DATA%'
        """
    )
    print("\n=== Procs referencing inv_data ===")
    for row in cur.fetchall():
        print(row[0])

    conn.close()


if __name__ == "__main__":
    main()
