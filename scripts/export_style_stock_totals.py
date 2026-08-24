"""One-off: look up stock totals for a list of styles and export Excel."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pyodbc
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.page import PageMargins
from openpyxl.worksheet.pagebreak import Break
from py.utilities.setting import Config
from py.utilities.ocean_qty import PLOG_OCEAN_JOIN, ocean_qty_sql_sum

STYLES_RAW = """
BT8514
BTC9350
BX1785
EL0961
EM1152
EM1153
EN1177
EN1321
EN1346
EN1636
EN1654
EN1789
EN1817
EN1824
EN1849
EN1873
EN1876
EN1881
EN2004
EN2007
EN2045
EN2060
EN2153
EN2159
EN2323
EN2326
EN2327
EN2329
EN2330
EN2334
ES1123
ES1147
ES1215
ES1272
ES1279
ES1323
ES1374
ES1377
ES1398
ES1404
ES1422
ES1447
ES1449
ES1462
ES1463
ES1467
ES1468
ES1470
ES1499
ES1501
ES1502
ET2020
ET3102
ET3114
ET3136
ET3283
ET3284
ET3314
ET3422
et3423
ET3426
ET3437
ET3439
ET3440
ET3443
ET3444
ET3459
ET3462
ET3464
ET8840
GM1175
GN2248
GN2367
GN2374
GN2417
GN2418
GN2501
GN2592
GN2597
GN2598
GN2599
GN2600
GN2636
GN2670
GN2674
GN2715
GN2716
GN2717
GN2718
GN2719
GN2720
GN2721
GN2804
GN2805
GN2806
GN2807
GN2808
GN2809
GN2810
GN2811
GN2812
GN2813
GN2814
GN2815
GN2816
GN2817
GN2818
GN2819
GN2820
GN2821
GS1529
GT3543
GT3544
GT3721
GT3722
GT3723
GT3724
GT3725
GT3726
GT3757
GT3758
GT3791
GT3795
GT3830
GT3854
GT3961
L0635
L0665
L1518
L1521
L1522
L1522
L1577
L1627
L1629
L1648
L1648
L1849
L1850
L1851
L1864
L1871
L1876
L1884
L1886
L1889
L1890
L1892
L1893
L1895
L1896
L1897
L1898
L1899
L1900
L1901
L2004
L2005
L2006
L2007
L2008
L2009
L2010
L2011
M1068
M1129
M1137
M1141
M1147
M1149
M1151
M1161
M1163
M1165
M1168
M1169
M1171
M1172
M1173
M1178
M1179
M1182
M1183
M1186
M1187
N0233
N0316
N0680
N0702
N0720
N0723
N0731
N0735
N0813
N0815
N1036B
N1116
N1151
N1223
N1265
N1271
N1283
N1324
N1512
N1546
N1547
N1644
N1675
N1685
N1696
N1724
N1738
N1745
N1771
N1780
N1782
N1790
N1798
N1799
N1811
N1812
N1814
N1819
N1822
N1834
N1842
N1845
N1851
N1854
N1856
N1861
N1866
N1867
N1868
N1869
N1914
N1919
N1922
N1926
N1928
N1929
N1930
N1939
N1941
N1959
N1970
N1971
N1976
N1979
N1985
N1987
N1996
N1997
N1999
N2011
N2012
N2014
N2027
N2071
N2086
N2087
N2111
N2114
N2121
N2136
N2137
N2139
N2141
N2147
N2150
N2191
N2222
N2234
N2262
N2266
N2267
N2272
N2273
N2274
N2275
N2280
N2298
N2304
N2310
N2311
N2314
N2317
N2318
N2319
N2337
N2345
N2352
N2362
N2376
N2388
N2390
N2391C
N2394A
N2395
N2405
N2436
N2438
N2441
N2442
N2443
N2444
N2449
N2451
N2457
N2461
N2464
N2465
N2467
N2468
N2479
N2482
N2483
N2488
N2493
N2494
N2496DA
N2497
N2497
N2503
N2513
N2526
N2534
N2537
N2539
N2549
N2550
N2553
N2559
N2566
N2567
N2579
N2581
N2582
N2583
N2585
N2587
N2588
N2589
N2601
N2602
N2604
N2609
N2611
N2613
N2616
N2626
N2630
N2632
N2633
N2634
N2635
N2637
N2640
N2641
N2642
N2643
N2645
N2649
N2651
N2653
N2654
N2655
N2656
N2657
N2658
N2659
N2660
N2662
N2663
N2667
N2668
N2669
N2682
N2684
N2685
N2686
N2687
N2688
N2697
N2698
N2700
N2702
N2703
N2710
N2711
N2712
N2713
N2714
N2722
N2723
N2725
N2727
N2729
N2731
N2732
N2733
N2734
N2735
N2736
N2737
N2738
N2739
N2740
N2741
N2742
N2743
N2745
N2746
N2748
N2751
N2752
N2753
N2754
N2755
N2756
N2758
N2759
N2760
N2761
N2762
N2763
N2764
N2765
N2766
N2767
N2768
N2769
N2770
N2771
N2772
N2773
N2774
N2775
N2776
N2777
N2778
N2779
N2780
N2781
N2782
N2783
N2784
N2785
N2786
N2787
N2788
N2789
N2790
N2791
N2792
N2793
N2794
N2795
N2796
N2797
N2798
N2799
N2800
N2801
N2802
N2803
N2822
N2823
N2824
N2825
N2826
N2827
N2828
N2829
N2830
N2831
N2832
N2833
N2834
N2837
N2838
N3962
N5010
S1186
S1193
S1198
S1207
S1208
S1209
S1212
S1229
S1231
S1235
S1240
S1242
S1245
S1253
S1275
S1283
S1285
S1291
S1292
S1293
S1294
S1322
S1327
S1333
S1334
S1342
S1343
S1345
S1346
S1347
S1349
S1351
S1361
S1395
S1399
S1400
S1401
S1402
S1405
S1407
S1408
S1412
S1424
S1425
S1426
S1431
S1434
S1435
S1437
S1439
S1441
S1457
S1458
S1459
S1460
S1472
S1473
S1476
S1482
S1484
S1487
S1488
S1489
S1491
S1505
S1506
S1510
S1512
S1514
S1517
S1518
S1520
S1521
S1522
S1523
S1530
S1532
S1533
S1537
S1538
S1544
S1547
S1548
S1549
S1550
T2777
T2777
T2907 B
T2975
T3001
T3067
T3071
T3085
T3096
T3097
T3101
T3112
T3116
T3117
T3129
T3132
T3133
T3134
T3142
T3146
T3147
T3156
T3159
T3161
T3164
T3170
T3171
T3184
T3185
T3186
T3187
T3193
T3217
T3222
T3230
T3242
T3247
T3258
T3259
T3262
T3266
T3272
T3273
T3274
T3275
T3285
T3291
T3298
T3299
T3303
T3305
T3321
T3323SA
T3324
T3328
T3343
T3345
T3349
T3353
T3355
T3356
T3362
T3364
T3365
t3367
T3375
T3376
T3377
T3378
T3380
T3386
T3388
T3393
T3398
T3417
T3433
T3434
T3435
T3436
T3445
T3446
T3449
T3451
T3453
T3454
T3455
T3456
T3469
T3480
T3481
T3483
T3484
T3485
T3506
T3507
T3511
T3515
T3524
T3525
T3528
T3532
T3545
T3547
T3552
T3554
T3562
T3566
T3567
T3569
T3572
T3574
T3575
T3576
T3577
T3592
T3595
T3596
T3599
T3600
T3602
T3606
T3607A
T3613
T3616
T3620
T3621
T3626
T3628
T3631
T3632
T3633
T3641
T3645
T3646
T3647
T3649
T3651
T3661
T3669
T3686
T3687
T3692
T3693
T3693W
T3698
T3701
T3702
T3705
T3706
T3710
T3713
T3720
T3727
T3735
T3736
T3737
T3739
T3741
T3742
T3743
T3745
T3746
T3747
T3748
T3749
T3751
T3754
T3755
T3756
T3761
T3763
T3767
T3774
T3775
T3785
T3786
T3796
T3797
T3798
T3799
T3803
T3807
T3815
T3816
T3819
T3821
T3823
T3824
T3825
T3831
T3833
T3834
T3835
T3836
T3839
T3841
T3842
T3844
T3845
T3847
T3848
T3849
T3850
T3851
T3852
T3853
T3860
T3861
T3864
T3872
T3873
T3875
T3876
T3878
T3879
T3880
T3883
T3884
T3885
T3886
T3888
T3889
T3890
T3892
T3893
T3894
T3896
T3897
T3899
T3901
T3902
T3906
T3910
T3911
T3912
T3917
T3918
T3919
T3921
T3922
T3923
T3924
T3925
T3927
T3928
T3929
T3930
T3931
T3932
T3935
T3936
T3937
T3938
T3939
T3940
T3941
T3943
T3944
T3945
T3946
T3947
T3952
T3953
T3954
T3955
T3956
T3957
T3958
T3959
T3960
T3963
T3964
T3965
T3966
T3967
T3968
T3969
T3970
T3973
T3974
T3975
T3979
T3980
"""

cfg = Config.SQL_SERVER_CONFIG
OUT_PATH = ROOT / "style_stock_totals.xlsx"
BATCH = 400


def unique_styles(raw: str):
    seen = set()
    ordered = []
    for line in raw.splitlines():
        style = line.strip()
        if not style:
            continue
        key = " ".join(style.upper().split())
        if key in seen:
            continue
        seen.add(key)
        ordered.append(style.strip())
    return ordered


def connect(database: str):
    return pyodbc.connect(
        f'DRIVER={cfg["driver"]};'
        f'SERVER={cfg["server"]};'
        f'DATABASE={database};'
        f'UID={cfg["username"]};'
        f'PWD={cfg["password"]}'
    )


def _add_qty(totals, key, stock, ocean):
    if key not in totals:
        totals[key] = {"stock": 0, "ocean": 0}
    totals[key]["stock"] += int(stock or 0)
    totals[key]["ocean"] += int(ocean or 0)


def format_stock_total(stock, ocean):
    stock = int(stock)
    ocean = int(ocean or 0)
    if ocean:
        return f"{stock} ({ocean})"
    return stock


def fetch_stock(conn, styles):
    """Return {STYLE_KEY: {stock, ocean}} using ALT_CD, then PROD_CD fallback."""
    totals = {}
    cursor = conn.cursor()
    ocean_sum = ocean_qty_sql_sum()
    for i in range(0, len(styles), BATCH):
        chunk = styles[i : i + BATCH]
        keys = [" ".join(s.upper().split()) for s in chunk]
        placeholders = ",".join("?" for _ in keys)
        cursor.execute(
            f"""
            SELECT
                RTRIM(LTRIM(UPPER(i.ALT_CD))) AS style,
                SUM(CAST(ISNULL(id.in_stock, 0) AS INT)) AS stock_total,
                {ocean_sum} AS total_ocean
            FROM dbo.inv AS i
            LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
            {PLOG_OCEAN_JOIN}
            WHERE RTRIM(LTRIM(UPPER(i.ALT_CD))) IN ({placeholders})
            GROUP BY RTRIM(LTRIM(UPPER(i.ALT_CD)))
            """,
            keys,
        )
        for style, stock, ocean in cursor.fetchall():
            if not style:
                continue
            key = " ".join(str(style).upper().split())
            _add_qty(totals, key, stock, ocean)

        # Also match exact PROD_CD (covers entries like "T2907 B")
        cursor.execute(
            f"""
            SELECT
                RTRIM(LTRIM(UPPER(i.PROD_CD))) AS sku,
                SUM(CAST(ISNULL(id.in_stock, 0) AS INT)) AS stock_total,
                {ocean_sum} AS total_ocean
            FROM dbo.inv AS i
            LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
            {PLOG_OCEAN_JOIN}
            WHERE RTRIM(LTRIM(UPPER(i.PROD_CD))) IN ({placeholders})
            GROUP BY RTRIM(LTRIM(UPPER(i.PROD_CD)))
            """,
            keys,
        )
        for sku, stock, ocean in cursor.fetchall():
            if not sku:
                continue
            key = " ".join(str(sku).upper().split())
            # Don't double-count SKUs already rolled up via ALT_CD.
            if key not in totals:
                _add_qty(totals, key, stock, ocean)
    cursor.close()
    return totals


def fetch_unmatched_by_prefix(conn, unmatched_keys):
    """For styles not found as ALT_CD/PROD_CD, sum stock where PROD_CD starts with 'STYLE '."""
    if not unmatched_keys:
        return {}
    totals = {}
    cursor = conn.cursor()
    ocean_sum = ocean_qty_sql_sum()
    for i in range(0, len(unmatched_keys), BATCH):
        chunk = unmatched_keys[i : i + BATCH]
        likes = [f"{k} %" for k in chunk]
        clauses = " OR ".join(
            ["RTRIM(LTRIM(UPPER(i.PROD_CD))) LIKE ?" for _ in likes]
        )
        cursor.execute(
            f"""
            SELECT
                RTRIM(LTRIM(UPPER(i.PROD_CD))) AS sku,
                SUM(CAST(ISNULL(id.in_stock, 0) AS INT)) AS stock_total,
                {ocean_sum} AS total_ocean
            FROM dbo.inv AS i
            LEFT JOIN dbo.inv_data AS id ON i.PROD_CD = id.Prod_cd
            {PLOG_OCEAN_JOIN}
            WHERE {clauses}
            GROUP BY RTRIM(LTRIM(UPPER(i.PROD_CD)))
            """,
            likes,
        )
        for sku, stock, ocean in cursor.fetchall():
            if not sku:
                continue
            sku_key = " ".join(str(sku).upper().split())
            style_key = sku_key.split(" ", 1)[0]
            _add_qty(totals, style_key, stock, ocean)
    cursor.close()
    return totals


# 4 Style/Stock pairs across US Letter portrait.
PRINT_SETS = 4
ROWS_PER_PAGE = 45  # header + 45 rows + margins fill a US Letter page


def write_letter_print_sheet(ws, rows, sets=PRINT_SETS, rows_per_page=ROWS_PER_PAGE):
    """Lay out Style | Stock Total in newspaper columns that fill a US Letter page."""
    ws.title = "Style Stock"
    header_font = Font(bold=True, color="FFFFFF", size=9, name="Calibri")
    cell_font = Font(size=9, name="Calibri")
    header_fill = PatternFill("solid", fgColor="1F4E79")
    no_qty_fill = PatternFill("solid", fgColor="FCE4D6")
    alt_fill = PatternFill("solid", fgColor="F2F2F2")
    thin = Border(
        left=Side(style="thin", color="BFBFBF"),
        right=Side(style="thin", color="BFBFBF"),
        top=Side(style="thin", color="BFBFBF"),
        bottom=Side(style="thin", color="BFBFBF"),
    )
    center = Alignment(horizontal="center", vertical="center")
    left = Alignment(horizontal="left", vertical="center")

    # Each set: Style, Stock; 1 spacer column between sets.
    def set_origin(set_i):
        return 1 + set_i * 3

    for set_i in range(sets):
        origin = set_origin(set_i)
        for offset, title in enumerate(("Style", "Stock Total")):
            cell = ws.cell(1, origin + offset, title)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center
            cell.border = thin
        ws.column_dimensions[get_column_letter(origin)].width = 12
        ws.column_dimensions[get_column_letter(origin + 1)].width = 14
        if set_i < sets - 1:
            ws.column_dimensions[get_column_letter(origin + 2)].width = 2.2

    per_page = sets * rows_per_page
    last_data_row = 1
    for page_i, page_start in enumerate(range(0, len(rows), per_page)):
        page_rows = rows[page_start : page_start + per_page]
        sheet_row_base = 2 + page_i * rows_per_page
        for set_i in range(sets):
            origin = set_origin(set_i)
            chunk = page_rows[set_i * rows_per_page : (set_i + 1) * rows_per_page]
            for offset, (style, qty) in enumerate(chunk):
                r = sheet_row_base + offset
                last_data_row = max(last_data_row, r)
                style_cell = ws.cell(r, origin, style)
                qty_cell = ws.cell(r, origin + 1, qty)
                stripe = alt_fill if offset % 2 else None
                for cell, align in ((style_cell, left), (qty_cell, center)):
                    cell.font = cell_font
                    cell.alignment = align
                    cell.border = thin
                    if stripe:
                        cell.fill = stripe
                if qty == "no qty":
                    qty_cell.fill = no_qty_fill
                ws.row_dimensions[r].height = 14.5
        if page_i > 0:
            ws.row_breaks.append(Break(id=sheet_row_base - 1))

    last_col = set_origin(sets - 1) + 1
    ws.freeze_panes = "A2"
    ws.print_title_rows = "1:1"
    ws.print_area = f"A1:{get_column_letter(last_col)}{last_data_row}"
    ws.page_setup.paperSize = ws.PAPERSIZE_LETTER
    ws.page_setup.orientation = "portrait"
    ws.page_setup.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.horizontalCentered = True
    ws.page_margins = PageMargins(
        left=0.35, right=0.35, top=0.4, bottom=0.4, header=0.2, footer=0.2
    )
    ws.oddHeader.center.text = "Style Stock Totals"
    ws.oddFooter.center.text = "Page &P of &N"


def main():
    styles = unique_styles(STYLES_RAW)
    print(f"Unique styles: {len(styles)}")

    merged = {}
    for db_name in (cfg["database_fdc"], cfg["database_oms_ef"]):
        print(f"Querying {db_name}...")
        conn = connect(db_name)
        try:
            part = fetch_stock(conn, styles)
            for k, v in part.items():
                _add_qty(merged, k, v["stock"], v["ocean"])
        finally:
            conn.close()

    unmatched = []
    for s in styles:
        key = " ".join(s.upper().split())
        if key not in merged:
            unmatched.append(key)

    if unmatched:
        print(f"Prefix fallback for {len(unmatched)} unmatched styles...")
        for db_name in (cfg["database_fdc"], cfg["database_oms_ef"]):
            conn = connect(db_name)
            try:
                part = fetch_unmatched_by_prefix(conn, unmatched)
                for k, v in part.items():
                    _add_qty(merged, k, v["stock"], v["ocean"])
            finally:
                conn.close()

    rows = []
    found = 0
    missing = 0
    for style in styles:
        key = " ".join(style.upper().split())
        label = style.upper() if style.islower() else style
        if key in merged:
            found += 1
            rows.append((label, format_stock_total(merged[key]["stock"], merged[key]["ocean"])))
        else:
            missing += 1
            rows.append((label, "no qty"))

    wb = Workbook()
    write_letter_print_sheet(wb.active, rows)
    wb.save(OUT_PATH)
    print(f"Found: {found}  No qty: {missing}")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
