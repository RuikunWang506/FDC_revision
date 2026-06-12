import pyodbc

cs = (
    "DRIVER={SQL Server};SERVER=THSERVER4;DATABASE=master;"
    "Trusted_Connection=yes;"
)
c = pyodbc.connect(cs, timeout=10)
cur = c.cursor()

cur.execute("SELECT SYSTEM_USER, SUSER_SNAME(), ORIGINAL_LOGIN()")
print("Identity:", cur.fetchone())

cur.execute("SELECT HAS_DBACCESS('master'), HAS_DBACCESS('omsdata_FDC')")
print("DB access:", cur.fetchone())

for db in ("master", "omsdata_FDC"):
    try:
        cur.execute(f"USE [{db}]")
        cur.execute("SELECT COUNT(*) FROM dbo.omssys") if db == "master" else cur.execute("SELECT TOP 1 1")
        print(f"{db}: OK")
    except Exception as e:
        print(f"{db}: FAIL -> {e}")

cur.execute(
    "SELECT dp.name, dp.type_desc FROM omsdata_FDC.sys.database_principals dp "
    "WHERE dp.name LIKE '%stream%' OR dp.name LIKE '%LIVEROOM%' OR dp.name LIKE '%OMS%'"
)
print("omsdata_FDC principals:", cur.fetchall())
