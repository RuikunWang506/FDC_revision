import pyodbc

# Direct connection with correct credentials from Config
conn_str = 'DRIVER={ODBC Driver 17 for SQL Server};SERVER=192.168.254.204;DATABASE=omsdata_FDC;UID=tristen;PWD=Fdc2022!!'

try:
    conn = pyodbc.connect(conn_str)
    cursor = conn.cursor()
    
    order_number = '35449'
    
    # Test the exact query used in the Flask app
    print("=" * 80)
    print(f"Testing exact Flask query for order {order_number}")
    print("=" * 80)
    
    cursor.execute(
        """
        SELECT TOP 1 NOTES
        FROM dbo.ntfile
        WHERE NT_NUM = ?
          AND NOTES IS NOT NULL
          AND LTRIM(RTRIM(NOTES)) <> ''
        ORDER BY LEN(NOTES) DESC
        """,
        (order_number,)
    )
    
    note_row = cursor.fetchone()
    
    if note_row and note_row[0]:
        raw_notes = note_row[0]
        stripped_notes = str(raw_notes).strip()
        
        print(f"\nRaw notes found: {repr(raw_notes)}")
        print(f"\nStripped notes: {repr(stripped_notes)}")
        print(f"\nLength after strip: {len(stripped_notes)}")
        print(f"\nBoolean check (note_row and note_row[0]): {note_row and note_row[0]}")
        print(f"\nWhat Flask would assign: {repr(str(note_row[0]).strip() if note_row and note_row[0] else '')}")
    else:
        print("\nNo note_row returned or note_row[0] is falsy")
        print(f"note_row: {note_row}")
        if note_row:
            print(f"note_row[0]: {repr(note_row[0])}")
    
    cursor.close()
    conn.close()
    print("\n" + "=" * 80)
    print("Query completed")
    print("=" * 80)
    
except Exception as e:
    print(f"Error: {str(e)}")
    import traceback
    traceback.print_exc()
