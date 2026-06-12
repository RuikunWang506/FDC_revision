import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from product_detail_module.product_detail import get_db_connections


def main():
    conn, _ = get_db_connections()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT t.name, OBJECT_NAME(t.parent_id)
        FROM sys.triggers t
        WHERE OBJECT_NAME(t.parent_id) = 'wm_binfile'
        """
    )
    print("=== Triggers on wm_binfile ===")
    for row in cur.fetchall():
        print(row)
        cur.execute("SELECT OBJECT_DEFINITION(OBJECT_ID(?))", (row[0],))
        print(cur.fetchone()[0][:3000])

    cur.execute(
        "SELECT OBJECT_DEFINITION(OBJECT_ID('proc_wm_update_wm_binfile_from_wm_log'))"
    )
    txt = cur.fetchone()[0] or ""
    # print end of proc after ADJ
    idx = txt.upper().find("'ADJ'")
    print("\n=== End of proc after ADJ ===")
    print(txt[idx : idx + 2500])

    conn.close()


if __name__ == "__main__":
    main()
