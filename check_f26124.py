#!/usr/bin/env python
import sys
sys.path.insert(0, r'c:\FDC_FLASK_APP')

from py.utilities.setting import Config
import pyodbc

config = Config.SQL_SERVER_CONFIG

try:
    # Connect to FDC
    conn_fdc = pyodbc.connect(
        f'DRIVER={config["driver"]};SERVER={config["server"]};DATABASE={config["database_fdc"]};UID={config["username"]};PWD={config["password"]}'
    )
    
    print("=" * 80)
    print("CHECKING PLOG FOR F26124")
    print("=" * 80)
    
    cursor = conn_fdc.cursor()
    
    # Check if F26124 exists in plog
    cursor.execute("SELECT COUNT(*) FROM plog WHERE pur_num = 'F26124'")
    count = cursor.fetchone()[0]
    print(f"\nTotal records in plog for F26124: {count}")
    
    if count > 0:
        # Get sample records with all columns to find ocean indicator
        cursor.execute("""
            SELECT TOP 1 
                LOG_DATE, LOG_TIME, VEN_ID, INVS_NUM, PUR_NUM, PUR_CD, PROD_CD, 
                LOG_QTY, BASE_COST, HDL_CHG, SHP_CHG, 
                CAN_QTY, EST_DT, DISCOUNT, PUR_LNT, NT_NUM, COMM_LN, 
                UT_DESC, PROD_DUTY, WT_QTY, WHS_NUM, BATCH_NUM, CAL_SEL, UT_SHP, UT_DUTY, 
                EXCH_RT, PC_UNIT, DEF_UT, CLASS_ITEM, SO_NUM, SO_DT, TYPE_CD, SO_NT_NUM, 
                CURRENCY, EXC_RATE, CURRENCY_AMT, DIV_CD, PRJ_CD, JOB_CD, FINAL_CUS, 
                LOT_NUM, ALLOC_QTY, UT_MISC, ITMSHP_DT, CFMSHP_DT, AVG_COST, 
                INV_MK, INV_PCK, INV_MFR, ERR_CODE, PROD_DT, ESTSHP_DT, PROD_REF
            FROM plog 
            WHERE pur_num = 'F26124'
        """)
        
        columns = [column[0] for column in cursor.description]
        rows = cursor.fetchall()
        
        print(f"\nColumns in plog table:")
        for i, col in enumerate(columns):
            print(f"  {i}: {col}")
        
        if rows:
            print(f"\nSample record for F26124:")
            row = rows[0]
            row_dict = dict(zip(columns, row))
            for key, val in row_dict.items():
                print(f"  {key}: {val}")
    
    conn_fdc.close()
    
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
