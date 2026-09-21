import os
import openpyxl
import json
import re
from datetime import datetime, timedelta
import hashlib
from app.database import get_db_connection, init_db

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode('utf-8')).hexdigest()

def norm_unit(u: str) -> str:
    u = u.strip().upper().replace(' ', '')
    u = u.replace('1ST-', '1ST-').replace('1ST', '1ST-') if '1ST-' not in u and u.startswith('1ST') else u
    u = u.replace('1ST', '1ST-') if u.startswith('1ST') and not u.startswith('1ST-') else u
    u = u.replace('3ND-', '3RD-').replace('3ND', '3RD-')
    u = u.replace('GR-0', 'GR-')
    return u

def seed():
    init_db()
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. Seed Companies
    companies_data = [
        ("connect", "كونكريت (Concrete)", "Concrete CRD", "#0284c7", "linear-gradient(135deg, #0284c7 0%, #06b6d4 100%)", "bg-gradient-to-br from-cyan-500 to-blue-600"),
        ("trust", "ترست (Trust)", "Trust Developments", "#7c3aed", "linear-gradient(135deg, #7c3aed 0%, #a855f7 100%)", "bg-gradient-to-br from-purple-600 to-indigo-700"),
        ("mmd", "إم إم دي (MMD)", "MMD Group", "#059669", "linear-gradient(135deg, #059669 0%, #10b981 100%)", "bg-gradient-to-br from-emerald-500 to-teal-700"),
        ("td", "تي دي (TD)", "TD Developments", "#ea580c", "linear-gradient(135deg, #ea580c 0%, #f97316 100%)", "bg-gradient-to-br from-amber-500 to-orange-600"),
    ]

    comp_ids = {}
    for code, name_ar, name_en, color, grad, bg in companies_data:
        cursor.execute("SELECT id FROM companies WHERE code = ?", (code,))
        row = cursor.fetchone()
        if not row:
            cursor.execute("""
                INSERT INTO companies (code, name_ar, name_en, brand_color, gradient, card_bg)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (code, name_ar, name_en, color, grad, bg))
            comp_ids[code] = cursor.lastrowid
        else:
            comp_ids[code] = row["id"]

    # 2. Seed Users
    users_data = [
        ("admin", hash_pw("Adm$Dfm0379"), "admin", None, "المدير العام (إدارة التحالف)"),
        ("concrete", hash_pw("12341234"), "company", comp_ids["connect"], "مسؤول مبيعات شركة كونكريت"),
        ("trust", hash_pw("Trs$jLP3211"), "company", comp_ids["trust"], "مسؤول مبيعات شركة ترست"),
        ("mmd", hash_pw("Mmd!iEg2427"), "company", comp_ids["mmd"], "مسؤول مبيعات شركة MMD"),
        ("td", hash_pw("Td!DOB0296"), "company", comp_ids["td"], "مسؤول مبيعات شركة TD"),
    ]

    for uname, pwh, role, cid, dname in users_data:
        cursor.execute("SELECT id FROM users WHERE username = ?", (uname,))
        if not cursor.fetchone():
            cursor.execute("""
                INSERT INTO users (username, password_hash, role, company_id, display_name)
                VALUES (?, ?, ?, ?, ?)
            """, (uname, pwh, role, cid, dname))

    # 3. Seed Units from Excel
    excel_path = os.path.join(BASE_DIR, "حصص المشاع و الوحدات المباعة 30-6-2026.xlsx")
    wb = openpyxl.load_workbook(excel_path, data_only=True)
    ws = wb.active

    current_floor = "GROUND FLOOR"
    units_inserted = 0

    cursor.execute("SELECT COUNT(*) FROM units")
    if cursor.fetchone()[0] == 0:
        for r in range(1, ws.max_row + 1):
            c1 = ws.cell(r, 1).value
            c2 = ws.cell(r, 2).value
            c3 = ws.cell(r, 3).value
            c4 = ws.cell(r, 4).value
            c5 = ws.cell(r, 5).value
            c6 = ws.cell(r, 6).value

            c1_str = str(c1).strip() if c1 else ''
            c2_str = str(c2).strip() if c2 else ''

            if 'GROUND FLOOR' in c1_str.upper():
                current_floor = "الدور الأرضي تجاري (Ground)"
            elif 'FIRST FLOOR' in c1_str.upper():
                current_floor = "الدور الأول تجاري (1st Floor)"
            elif 'SECOND' in c1_str.upper():
                current_floor = "الدور الثاني إداري (2nd Floor)"
            elif 'THIRD' in c1_str.upper():
                current_floor = "الدور الثالث إداري (3rd Floor)"

            if c2_str and c2_str.upper() != 'UNIT' and c5 is not None:
                norm_code = norm_unit(c2_str)
                try:
                    area_val = float(c3) if c3 else 0.0
                    pm_val = float(c4) if c4 else 0.0
                    up_val = float(c5) if c5 else 0.0
                    sug_val = float(c6) if c6 else 0.0
                    cursor.execute("""
                        INSERT OR IGNORE INTO units (unit_code, floor, unit_type, area, price_m, catalog_price, sug, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'AVAILABLE')
                    """, (norm_code, current_floor, c1_str or 'Shop', area_val, pm_val, up_val, sug_val))
                    units_inserted += 1
                except Exception as e:
                    pass
        print(f"Inserted {units_inserted} units from Excel.")

    # 4. Seed Sales & Installments from PDF
    # Clean client name mapping
    client_names_clean = {
        "GR-47": "سحر علي علي سليمان",
        "GR-57": "سحر علي علي سليمان",
        "1ST-59": "السيد سعد علي سلام",
        "2ND-20": "عبد الله الشريف - محمد هلال حلواني",
        "2ND-21": "رضا سعيد الغندور فايد",
        "1ST-42": "نجلاء فتحي يوسف السعداوي",
        "1ST-32": "نورة رمضان شعبان الدسوقي",
        "1ST-58": "محمد السعيد البدراوي علي النفنوف",
        "GR-31": "عاطف عزت محمد عطية",
        "1ST-43": "محمود حامد عبد الحليم حتاتة",
        "GR-1": "وائل بركات حسن عبد الدايم",
        "2ND-49": "غازي عبده غازي يوسف عبد الله",
        "GR-64": "محمد محمود احمد زيد",
        "GR-37": "محمد علي عبده قاسم - احمد علي عبده قاسم",
        "GR-42": "عبد العليم عبد العليم علي قاسم",
        "GR-30": "أحمد الصياد - حسن الصياد - مؤمن صلاح",
        "2ND-13": "نجلاء فوزي عبد الحليم الطرانيسي",
        "1ST-50": "محمد محمد حسن مصطفى",
        "2ND-50": "أسامة احمد محمد شلبي سلام",
        "GR-55": "احمد احمد إبراهيم العيسوى",
        "GR-56": "احمد احمد إبراهيم العيسوى",
        "GR-24": "احمد احمد إبراهيم العيسوى",
        "GR-23": "احمد احمد إبراهيم العيسوى",
        "2ND-08": "احمد احمد إبراهيم العيسوى",
        "2ND-09": "احمد احمد إبراهيم العيسوى",
        "2ND-07": "أيمن السيد عبده طه",
        "2ND-06": "حازم السيد عبده طه",
        "GR-51": "السعيد السيد إبراهيم السيد",
        "2ND-16": "احمد مصطفى سلام محمود سلام",
        "2ND-30": "نادية حامد حمد الجوهري",
        "GR-18": "علاء محمود موسى فتح الله",
        "GR-72": "كريم محمد محمد الفطايري",
        "1ST-51": "علي عصام علي محمد إسكندر",
        "1ST-27": "إبراهيم فكري إبراهيم",
        "2ND-27": "إبراهيم فكري إبراهيم",
        "2ND-31": "طلعت شاهر الشحات إبراهيم النجار",
        "GR-35": "مجدي محمد محمد وهبة",
        "GR-15": "سعد محمد محمد الجمل",
        "GR-16": "محمد محمد محمد الحسيني",
        "GR-29": "محمد هاني عبد الفتاح السقعان",
        "2ND-36": "مها محمد محمد العطار",
        "GR-17": "ميسرة عبد الستار السيد إبراهيم",
        "3RD-36": "محمود محمد رأفت محمود البيومي",
    }

    # PDF Sales data: (serial, unit_raw, comp_code, sale_price, sale_date)
    pdf_sales_list = [
        (1, "GR-47", "connect", 6137000.0, "2026-04-07"),
        (2, "GR-57", "connect", 9702000.0, "2026-04-07"),
        (3, "1st-59", "connect", 2890000.0, "2026-04-15"),
        (4, "2nd-20", "connect", 2975000.0, "2026-04-18"),
        (5, "2nd-21", "connect", 2975000.0, "2026-03-15"),
        (6, "1st-42", "connect", 2890000.0, "2026-04-05"),
        (7, "1st-32", "connect", 4641000.0, "2026-03-05"),
        (8, "1st-58", "connect", 1800000.0, "2026-01-24"),
        (9, "GR-31", "connect", 10625000.0, "2026-01-03"),
        (10, "1st-43", "connect", 1900000.0, "2025-12-30"),
        (11, "GR-01", "connect", 10600000.0, "2025-11-30"),
        (12, "2nd-49", "connect", 3240000.0, "2025-11-25"),
        (13, "GR-64", "connect", 7068000.0, "2026-01-17"),
        (14, "GR-37", "connect", 17000000.0, "2026-01-22"),
        (15, "GR-42", "connect", 12100000.0, "2026-02-21"),
        (16, "GR-30", "connect", 4560000.0, "2026-03-03"),
        (17, "2nd-13", "connect", 2600000.0, "2026-06-18"),
        (18, "1st-50", "connect", 3584000.0, "2026-06-13"),
        (19, "2nd-50", "connect", 3240000.0, "2026-06-30"),
        (20, "GR-55", "connect", 11407000.0, "2026-04-14"),
        (21, "GR-56", "connect", 11407000.0, "2026-04-14"),
        (22, "GR-24", "connect", 5508000.0, "2026-04-14"),
        (23, "GR-23", "connect", 5814000.0, "2026-04-14"),
        (24, "2nd-08", "connect", 3323500.0, "2026-04-14"),
        (25, "2nd-09", "connect", 3323500.0, "2026-04-14"),
        (26, "2nd-07", "connect", 3323500.0, "2026-04-14"),
        (27, "2nd-06", "connect", 3323500.0, "2026-04-14"),
        (28, "GR-51", "connect", 6587500.0, "2026-04-23"),
        (29, "2nd-16", "connect", 4000000.0, "2026-04-20"),
        (30, "2nd-30", "connect", 2700000.0, "2026-05-13"),
        (31, "GR-18", "connect", 4896000.0, "2026-06-15"),
        (32, "GR-72", "connect", 7650000.0, "2026-06-11"),
        (33, "1st-51", "connect", 3584000.0, "2026-06-13"),
        (34, "1st-27", "trust", 5926000.0, "2025-12-04"),
        (35, "2nd-27", "trust", 3648000.0, "2025-12-04"),
        (36, "2nd-31", "trust", 6570000.0, "2025-12-08"),
        (37, "GR-35", "trust", 9579500.0, "2025-12-22"),
        (38, "GR-15", "trust", 3888000.0, "2026-02-28"),
        (39, "GR-16", "trust", 4420000.0, "2026-02-28"),
        (40, "GR-29", "mmd", 4641000.0, "2026-03-10"),
        (41, "2nd-36", "trust", 3150000.0, "2026-04-04"),
        (42, "GR-17", "trust", 6460000.0, "2026-04-08"),
        (43, "3nd-36", "td", 2983500.0, "2026-05-02"),
    ]

    cursor.execute("SELECT COUNT(*) FROM sales")
    if cursor.fetchone()[0] == 0:
        sales_count = 0
        installments_count = 0

        # Reference "today" date from environment metadata: 2026-09-06
        today_date = datetime(2026, 9, 6)

        for serial, raw_unit, comp_code, sale_price, sdate_str in pdf_sales_list:
            unit_code = norm_unit(raw_unit)
            cursor.execute("SELECT id, catalog_price FROM units WHERE unit_code = ?", (unit_code,))
            unit_row = cursor.fetchone()
            if not unit_row:
                print(f"Warning: Unit {unit_code} not found in DB!")
                continue

            unit_id = unit_row["id"]
            cat_price = unit_row["catalog_price"]
            cid = comp_ids[comp_code]
            cname = client_names_clean.get(unit_code, f"عميل وحدة {unit_code}")

            disc_amount = round(cat_price - sale_price, 2)
            disc_pct = round((disc_amount / cat_price * 100) if cat_price > 0 else 0, 2)

            # Standard installment plan:
            # 10% down payment
            down_payment = round(sale_price * 0.10, 2)
            rem_amount = sale_price - down_payment
            
            # 12 quarterly installments (3 years)
            n_installments = 12
            inst_amount = round(rem_amount / n_installments, 2)

            cursor.execute("""
                INSERT INTO sales (
                    serial_no, unit_id, company_id, client_name, client_phone, sale_date,
                    catalog_price, sale_price, discount_amount, discount_pct,
                    down_payment, installments_count, installment_frequency, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'quarterly', 'تم استيراد التعاقد من تقرير المبيعات')
            """, (
                serial, unit_id, cid, cname, f"010{10000000 + serial}", sdate_str,
                cat_price, sale_price, disc_amount, disc_pct,
                down_payment, n_installments
            ))
            sale_id = cursor.lastrowid
            sales_count += 1

            # Mark unit as SOLD
            cursor.execute("UPDATE units SET status = 'SOLD', sold_to_company_id = ? WHERE id = ?", (cid, unit_id))

            # Add Down payment installment record
            cursor.execute("""
                INSERT INTO installments (
                    sale_id, installment_no, installment_type, due_date, amount, status, paid_amount, payment_date, payment_method, receipt_no, notes
                ) VALUES (?, 0, 'down_payment', ?, ?, 'paid', ?, ?, 'bank_transfer', ?, 'دفعة التعاقد المقدمة')
            """, (sale_id, sdate_str, down_payment, down_payment, sdate_str, f"REC-DP-{sale_id:04d}"))
            installments_count += 1

            # Generate the 12 quarterly installments
            sale_dt = datetime.strptime(sdate_str, "%Y-%m-%d")
            for i in range(1, n_installments + 1):
                # 3 months intervals
                due_dt = sale_dt + timedelta(days=91 * i)
                due_str = due_dt.strftime("%Y-%m-%d")

                # Determine initial status based on due date vs 2026-09-06
                if due_dt < today_date:
                    # Past installments: 85% paid, 15% overdue
                    if (i + serial) % 5 == 0:
                        status = "overdue"
                        paid_amt = 0
                        pay_dt = None
                        rec_no = None
                    else:
                        status = "paid"
                        paid_amt = inst_amount
                        pay_dt = due_str
                        rec_no = f"REC-{sale_id:04d}-{i:02d}"
                else:
                    status = "pending"
                    paid_amt = 0
                    pay_dt = None
                    rec_no = None

                cursor.execute("""
                    INSERT INTO installments (
                        sale_id, installment_no, installment_type, due_date, amount, status, paid_amount, payment_date, payment_method, receipt_no
                    ) VALUES (?, ?, 'installment', ?, ?, ?, ?, ?, 'cheque', ?)
                """, (sale_id, i, due_str, inst_amount, status, paid_amt, pay_dt, rec_no))
                installments_count += 1

        print(f"Seeded {sales_count} sales and {installments_count} installment records.")

    conn.commit()
    conn.close()
    print("Database seeding completed successfully!")

if __name__ == "__main__":
    seed()
