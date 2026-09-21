import os
import sqlite3
import hashlib
import json
import shutil
import io
import uuid
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Header, Depends, Query, Request, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import openpyxl

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "mega_mall.db"))
SECRET_KEY = os.environ.get("MEGA_MALL_SECRET_KEY", "MEGA_MALL_SECRET_KEY_2026")

app = FastAPI(title="Mega Mall Management System", version="2.0.0")

cors_origins_env = os.environ.get("ALLOWED_ORIGINS")
if cors_origins_env and cors_origins_env.strip() and cors_origins_env.strip() != "*":
    allowed_origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
else:
    allowed_origins = [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Start automated background backup scheduler (runs every 12 hours automatically)
try:
    if os.environ.get("DISABLE_AUTO_BACKUP") != "1" and not os.environ.get("PYTHONANYWHERE_SITE"):
        from auto_backup import start_background_backup_scheduler
        start_background_backup_scheduler(interval_hours=12)
except Exception:
    pass

def ensure_db_migrations():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("PRAGMA table_info(sales)")
        cols = [r[1] for r in c.fetchall()]
        if "last_edited_by" not in cols:
            c.execute("ALTER TABLE sales ADD COLUMN last_edited_by TEXT DEFAULT NULL")
        if "last_edited_at" not in cols:
            c.execute("ALTER TABLE sales ADD COLUMN last_edited_at TIMESTAMP DEFAULT NULL")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Migration error: {e}")

ensure_db_migrations()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()

def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode('utf-8')).hexdigest()

# Simple secure token mechanism for local/hosted deployment
def make_token(user_id: int, username: str, role: str, company_id: Optional[int]) -> str:
    payload = f"{user_id}:{username}:{role}:{company_id or 'none'}:{datetime.now().strftime('%Y%m%d')}"
    sig = hashlib.sha256((payload + SECRET_KEY).encode()).hexdigest()[:16]
    return f"{payload}:{sig}"

def verify_token(token: str):
    if not token:
        return None
    try:
        parts = token.split(":")
        if len(parts) != 6:
            return None
        uid, uname, role, comp_id_str, dt, sig = parts
        expected_sig = hashlib.sha256(f"{uid}:{uname}:{role}:{comp_id_str}:{dt}{SECRET_KEY}".encode()).hexdigest()[:16]
        if sig != expected_sig:
            return None
        return {
            "user_id": int(uid),
            "username": uname,
            "role": role,
            "company_id": int(comp_id_str) if comp_id_str != 'none' else None
        }
    except Exception:
        return None

def get_current_user(authorization: Optional[str] = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="يرجى تسجيل الدخول أولاً")
    token = authorization.replace("Bearer ", "").strip()
    user = verify_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="جلسة غير صالحة أو منتهية")
    return user

def get_current_user_flexible(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None)
):
    raw_token = None
    if authorization:
        raw_token = authorization.replace("Bearer ", "").strip()
    elif token:
        raw_token = token.strip()

    if not raw_token:
        raise HTTPException(status_code=401, detail="يرجى تسجيل الدخول أولاً")
    user = verify_token(raw_token)
    if not user:
        raise HTTPException(status_code=401, detail="جلسة غير صالحة أو منتهية")
    return user

# Helper function for exact calendar month additions without date drifting
def add_months_to_date(dt: datetime, months: int) -> datetime:
    total_month = dt.month - 1 + months
    year = dt.year + total_month // 12
    month = total_month % 12 + 1
    is_leap = (year % 4 == 0) and (year % 100 != 0 or year % 400 == 0)
    days_in_month = [31, 29 if is_leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    day = min(dt.day, days_in_month)
    return datetime(year, month, day)

# Pydantic models
class LoginRequest(BaseModel):
    username: str
    password: str

class CustomInstallmentItem(BaseModel):
    installment_no: Optional[int] = None
    installment_type: Optional[str] = "installment" # down_payment, installment, delivery, annual, maintenance
    due_date: str
    amount: float
    notes: Optional[str] = ""
    bank_deposited: Optional[bool] = False
    bank_name: Optional[str] = ""

class SaleCreateRequest(BaseModel):
    unit_id: int
    client_name: str
    client_phone: Optional[str] = ""
    client_national_id: Optional[str] = ""
    sale_date: str
    sale_price: float
    down_payment: float = 0.0
    down_payment_method: Optional[str] = "bank_transfer" # bank_transfer, cheque, cash
    down_payment_deposited: Optional[bool] = True
    down_payment_bank: Optional[str] = "حساب الشركة البنكي"
    installments_count: Optional[int] = 1
    installment_frequency: Optional[str] = "custom"
    first_due_date: Optional[str] = None
    company_id: Optional[int] = None
    notes: Optional[str] = ""
    custom_installments: Optional[List[CustomInstallmentItem]] = None

class PaymentRequest(BaseModel):
    paid_amount: float
    payment_date: str
    payment_method: str = "cheque"
    receipt_no: Optional[str] = ""
    notes: Optional[str] = ""
    bank_deposited: Optional[bool] = False
    bank_name: Optional[str] = ""

class BankStatusRequest(BaseModel):
    bank_deposited: bool
    bank_name: Optional[str] = ""

class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str

class EmailSettingsRequest(BaseModel):
    smtp_server: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    sender_email: str
    sender_name: str

class TelegramSettingsRequest(BaseModel):
    bot_token: str
    chat_id: str
    enabled: bool = True

class UpdateSaleRequest(BaseModel):
    client_name: str
    client_phone: Optional[str] = ""
    client_national_id: Optional[str] = ""
    sale_date: str
    notes: Optional[str] = ""
    sale_price: Optional[float] = None
    down_payment: Optional[float] = None
    installments_count: Optional[int] = None
    installment_frequency: Optional[str] = None

# --- AUTH ENDPOINTS ---

@app.post("/api/login")
def login(req: LoginRequest):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT u.*, c.name_ar as company_name, c.code as company_code, c.brand_color
        FROM users u
        LEFT JOIN companies c ON u.company_id = c.id
        WHERE u.username = ? AND u.password_hash = ?
    """, (req.username, hash_pw(req.password)))
    user = c.fetchone()

    if not user:
        # Log failed login attempt
        c.execute("""
            INSERT INTO activity_logs (username, action, details)
            VALUES (?, 'LOGIN_FAILED', ?)
        """, (req.username, f"محاولة تسجيل دخول فاشلة للمستخدم {req.username}"))
        conn.commit()
        conn.close()
        raise HTTPException(status_code=400, detail="اسم المستخدم أو كلمة المرور غير صحيحة")

    # Log successful login
    c.execute("""
        INSERT INTO activity_logs (username, action, details)
        VALUES (?, 'LOGIN_SUCCESS', ?)
    """, (user["username"], f"تسجيل دخول ناجح - {user['display_name']} ({user['role']})"))
    conn.commit()
    conn.close()

    token = make_token(user["id"], user["username"], user["role"], user["company_id"])
    return {
        "success": True,
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "role": user["role"],
            "company_id": user["company_id"],
            "company_name": user["company_name"],
            "company_code": user["company_code"],
            "brand_color": user["brand_color"]
        }
    }

@app.get("/api/me")
def me(user: dict = Depends(get_current_user)):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT u.*, c.name_ar as company_name, c.code as company_code, c.brand_color
        FROM users u
        LEFT JOIN companies c ON u.company_id = c.id
        WHERE u.id = ?
    """, (user["user_id"],))
    row = c.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود")
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "company_id": row["company_id"],
        "company_name": row["company_name"],
        "company_code": row["company_code"],
        "brand_color": row["brand_color"]
    }

# --- DASHBOARD & STATS API ---

@app.get("/api/dashboard")
def get_dashboard(
    company_id: Optional[int] = None,
    period: Optional[str] = "all", # all, monthly, quarterly, annual
    floor: Optional[str] = None,
    user: dict = Depends(get_current_user)
):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Auto-sync overdue status for past due pending installments
    c.execute("UPDATE installments SET status = 'overdue' WHERE status = 'pending' AND due_date < date('now')")
    conn.commit()

    # Company filtering: Only filter if explicitly requested via company_id param
    # This allows all partner companies to see the full mall portfolio and what enters the parent company!
    effective_company_id = company_id

    # 1. Total units in mall & available/sold breakdown
    c.execute("SELECT COUNT(*) as total, SUM(CASE WHEN status='SOLD' THEN 1 ELSE 0 END) as sold, SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) as pending, SUM(CASE WHEN status='AVAILABLE' THEN 1 ELSE 0 END) as available FROM units")
    unit_stats = c.fetchone()

    # 2. Company Cards Stats (Cards Center)
    c.execute("SELECT * FROM companies ORDER BY id")
    companies = c.fetchall()
    cards_data = []

    for comp in companies:
        c.execute("""
            SELECT 
                COUNT(s.id) as units_sold,
                COALESCE(SUM(s.sale_price), 0) as total_sales,
                COALESCE(SUM(s.catalog_price), 0) as total_catalog,
                COALESCE(SUM(s.discount_amount), 0) as total_discount
            FROM sales s
            WHERE s.company_id = ?
        """, (comp["id"],))
        cs = c.fetchone()

        # Payments for this company
        c.execute("""
            SELECT 
                COALESCE(SUM(CASE WHEN i.status = 'paid' THEN i.paid_amount ELSE 0 END), 0) as collected,
                COALESCE(SUM(CASE WHEN i.status = 'overdue' THEN i.amount ELSE 0 END), 0) as overdue,
                COALESCE(SUM(CASE WHEN i.status = 'pending' THEN i.amount ELSE 0 END), 0) as pending,
                COUNT(CASE WHEN i.status = 'paid' THEN 1 END) as paid_count,
                COUNT(CASE WHEN i.status = 'overdue' THEN 1 END) as overdue_count,
                COUNT(CASE WHEN i.status = 'pending' THEN 1 END) as pending_count,
                COUNT(CASE WHEN i.bank_deposited = 1 THEN 1 END) as bank_deposited_count
            FROM installments i
            JOIN sales s ON i.sale_id = s.id
            WHERE s.company_id = ?
        """, (comp["id"],))
        ps = c.fetchone()

        # Next upcoming installment for this company
        c.execute("""
            SELECT i.due_date, i.amount, s.client_name, u.unit_code
            FROM installments i
            JOIN sales s ON i.sale_id = s.id
            JOIN units u ON s.unit_id = u.id
            WHERE s.company_id = ? AND i.status IN ('pending', 'overdue')
            ORDER BY i.due_date ASC
            LIMIT 1
        """, (comp["id"],))
        next_inst_row = c.fetchone()
        next_installment = dict(next_inst_row) if next_inst_row else None

        tot_sales = cs["total_sales"]
        collected = ps["collected"]
        remaining = tot_sales - collected

        cards_data.append({
            "id": comp["id"],
            "code": comp["code"],
            "name_ar": comp["name_ar"],
            "name_en": comp["name_en"],
            "brand_color": comp["brand_color"],
            "gradient": comp["gradient"],
            "card_bg": comp["card_bg"],
            "units_sold": cs["units_sold"],
            "total_sales": tot_sales,
            "total_catalog": cs["total_catalog"],
            "total_discount": cs["total_discount"],
            "collected": collected,
            "overdue": ps["overdue"],
            "pending": ps["pending"],
            "remaining": remaining,
            "collection_pct": round((collected / tot_sales * 100) if tot_sales > 0 else 0, 1),
            "paid_count": ps["paid_count"] or 0,
            "pending_count": ps["pending_count"] or 0,
            "overdue_count": ps["overdue_count"] or 0,
            "bank_deposited_count": ps["bank_deposited_count"] or 0,
            "total_installments_count": (ps["paid_count"] or 0) + (ps["pending_count"] or 0) + (ps["overdue_count"] or 0),
            "next_installment": next_installment
        })

    # 3. Overall Financial KPIs (filtered if effective_company_id is set)
    where_sale = "WHERE 1=1"
    params = []
    if effective_company_id:
        where_sale += " AND s.company_id = ?"
        params.append(effective_company_id)

    c.execute(f"""
        SELECT 
            COUNT(s.id) as sales_count,
            COALESCE(SUM(s.sale_price), 0) as total_sales,
            COALESCE(SUM(s.catalog_price), 0) as total_catalog,
            COALESCE(SUM(s.discount_amount), 0) as total_discount
        FROM sales s
        {where_sale}
    """, params)
    kpis = c.fetchone()

    # Installment totals
    c.execute(f"""
        SELECT 
            COALESCE(SUM(CASE WHEN i.status = 'paid' THEN i.paid_amount ELSE 0 END), 0) as total_collected,
            COALESCE(SUM(CASE WHEN i.status = 'overdue' THEN i.amount ELSE 0 END), 0) as total_overdue,
            COALESCE(SUM(CASE WHEN i.status = 'pending' THEN i.amount ELSE 0 END), 0) as total_pending,
            COUNT(CASE WHEN i.status = 'overdue' THEN 1 END) as overdue_count,
            COUNT(CASE WHEN i.status = 'pending' THEN 1 END) as pending_count,
            COUNT(CASE WHEN i.status = 'paid' THEN 1 END) as paid_count
        FROM installments i
        JOIN sales s ON i.sale_id = s.id
        {where_sale}
    """, params)
    inst_kpis = c.fetchone()

    # 4. Cash Flow by Month / Quarter for trend chart
    # Group installments by Year-Month or Year-Quarter
    c.execute(f"""
        SELECT 
            substr(i.due_date, 1, 7) as year_month,
            SUM(i.amount) as due_amount,
            SUM(CASE WHEN i.status = 'paid' THEN i.paid_amount ELSE 0 END) as collected_amount,
            SUM(CASE WHEN i.status = 'overdue' THEN i.amount ELSE 0 END) as overdue_amount
        FROM installments i
        JOIN sales s ON i.sale_id = s.id
        {where_sale}
        GROUP BY substr(i.due_date, 1, 7)
        ORDER BY year_month ASC
        LIMIT 24
    """, params)
    timeline_rows = c.fetchall()

    timeline = [{
        "period": r["year_month"],
        "due": r["due_amount"],
        "collected": r["collected_amount"],
        "overdue": r["overdue_amount"]
    } for r in timeline_rows]

    # 5. Floor distribution
    c.execute("""
        SELECT u.floor, COUNT(u.id) as total_units,
               SUM(CASE WHEN u.status = 'SOLD' THEN 1 ELSE 0 END) as sold_units,
               SUM(u.catalog_price) as catalog_value
        FROM units u
        GROUP BY u.floor
    """)
    floor_dist = [dict(r) for r in c.fetchall()]

    # 6. Upcoming Installments list (Top 10)
    c.execute(f"""
        SELECT 
            i.id, i.installment_no, i.installment_type, i.due_date, i.amount, i.status,
            s.client_name, s.client_phone, u.unit_code, u.floor,
            c.name_ar as company_name, c.brand_color, c.code as company_code
        FROM installments i
        JOIN sales s ON i.sale_id = s.id
        JOIN units u ON s.unit_id = u.id
        JOIN companies c ON s.company_id = c.id
        {where_sale} AND i.status IN ('pending', 'overdue')
        ORDER BY i.due_date ASC
        LIMIT 10
    """, params)
    upcoming_installments = [dict(r) for r in c.fetchall()]



    user_company = None
    if user.get("company_id"):
        for cd in cards_data:
            if cd["id"] == user["company_id"]:
                user_company = cd
                break

    conn.close()

    total_sales = kpis["total_sales"]
    collected = inst_kpis["total_collected"]
    remaining = total_sales - collected

    return {
        "kpis": {
            "total_sales": total_sales,
            "total_catalog": kpis["total_catalog"],
            "total_discount": kpis["total_discount"],
            "total_collected": collected,
            "total_overdue": inst_kpis["total_overdue"],
            "total_pending": inst_kpis["total_pending"],
            "remaining_balance": remaining,
            "collection_rate": round((collected / total_sales * 100) if total_sales > 0 else 0, 1),
            "overdue_count": inst_kpis["overdue_count"],
            "pending_count": inst_kpis["pending_count"],
            "paid_count": inst_kpis["paid_count"],
            "total_installments_count": (inst_kpis["overdue_count"] or 0) + (inst_kpis["pending_count"] or 0) + (inst_kpis["paid_count"] or 0),
            "sales_count": kpis["sales_count"],
            "units_total": unit_stats["total"],
            "units_sold": unit_stats["sold"],
            "units_available": unit_stats["available"]
        },
        "cards": cards_data,
        "timeline": timeline,
        "floor_distribution": floor_dist,
        "upcoming_installments": upcoming_installments,
        "user_company": user_company
    }

# --- INSTALLMENTS LIST & PAY ---

@app.get("/api/installments")
def get_installments(
    status: Optional[str] = "all", # all, paid, pending, overdue
    company_id: Optional[int] = None,
    search: Optional[str] = None,
    period: Optional[str] = "all", # all, this_month, next_month, this_year, next_year, year_2028, future
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    months: Optional[str] = None,
    user: dict = Depends(get_current_user)
):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Auto-sync overdue status for past due pending installments
    c.execute("UPDATE installments SET status = 'overdue' WHERE status = 'pending' AND due_date < date('now')")
    conn.commit()

    # Allow viewing all installments for full mall visibility, or filter if company_id is provided
    effective_company_id = company_id

    query = """
        SELECT 
            i.*,
            s.client_name, s.client_phone, s.sale_date, s.sale_price,
            u.unit_code, u.floor, u.unit_type, u.area,
            c.name_ar as company_name, c.brand_color, c.code as company_code
        FROM installments i
        JOIN sales s ON i.sale_id = s.id
        JOIN units u ON s.unit_id = u.id
        JOIN companies c ON s.company_id = c.id
        WHERE 1=1
    """
    params = []

    if effective_company_id:
        query += " AND s.company_id = ?"
        params.append(effective_company_id)

    if status and status != "all":
        query += " AND i.status = ?"
        params.append(status)

    if search:
        query += " AND (s.client_name LIKE ? OR u.unit_code LIKE ? OR i.receipt_no LIKE ?)"
        s_param = f"%{search}%"
        params.extend([s_param, s_param, s_param])

    # Date Range filtering
    if start_date:
        query += " AND i.due_date >= ?"
        params.append(start_date)

    if end_date:
        query += " AND i.due_date <= ?"
        params.append(end_date)

    # Specific year & month(s)
    if year and months:
        m_list = [int(m.strip()) for m in months.split(",") if m.strip().isdigit()]
        if m_list:
            placeholders = ",".join(["?"] * len(m_list))
            query += f" AND substr(i.due_date, 1, 4) = ? AND cast(substr(i.due_date, 6, 2) as integer) IN ({placeholders})"
            params.append(str(year))
            params.extend(m_list)
        else:
            query += " AND substr(i.due_date, 1, 4) = ?"
            params.append(str(year))
    elif year and month:
        m_str = f"{int(month):02d}"
        query += " AND substr(i.due_date, 1, 7) = ?"
        params.append(f"{year}-{m_str}")
    elif year:
        query += " AND substr(i.due_date, 1, 4) = ?"
        params.append(str(year))
    elif month:
        m_str = f"{int(month):02d}"
        query += " AND substr(i.due_date, 6, 2) = ?"
        params.append(m_str)

    # Period presets (when explicit start/end dates are not given)
    if not start_date and not end_date and not year and not month and period and period != "all":
        if period == "this_month":
            query += " AND substr(i.due_date, 1, 7) = strftime('%Y-%m', 'now')"
        elif period == "next_month":
            query += " AND substr(i.due_date, 1, 7) = strftime('%Y-%m', date('now', '+1 month'))"
        elif period == "this_year":
            query += " AND substr(i.due_date, 1, 4) = strftime('%Y', 'now')"
        elif period == "next_year":
            query += " AND substr(i.due_date, 1, 4) = cast(cast(strftime('%Y', 'now') as integer) + 1 as text)"
        elif period == "year_2028":
            query += " AND substr(i.due_date, 1, 4) = '2028'"
        elif period == "future":
            query += " AND substr(i.due_date, 1, 4) >= '2029'"

    query += " ORDER BY i.due_date ASC, i.id ASC"

    c.execute(query, params)
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

@app.get("/api/installments/years-summary")
def get_installments_years_summary(
    company_id: Optional[int] = None,
    user: dict = Depends(get_current_user)
):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    effective_company_id = company_id

    query = """
        SELECT 
            substr(i.due_date, 1, 4) as year,
            COUNT(*) as count,
            COALESCE(SUM(i.amount), 0) as total_amount,
            COALESCE(SUM(CASE WHEN i.status = 'paid' THEN i.paid_amount ELSE 0 END), 0) as collected_amount,
            COALESCE(SUM(CASE WHEN i.status = 'overdue' THEN i.amount ELSE 0 END), 0) as overdue_amount,
            COALESCE(SUM(CASE WHEN i.status = 'pending' THEN i.amount ELSE 0 END), 0) as pending_amount,
            COUNT(CASE WHEN i.status = 'paid' THEN 1 END) as paid_count,
            COUNT(CASE WHEN i.status = 'overdue' THEN 1 END) as overdue_count,
            COUNT(CASE WHEN i.status = 'pending' THEN 1 END) as pending_count
        FROM installments i
        JOIN sales s ON i.sale_id = s.id
        WHERE 1=1
    """
    params = []
    if effective_company_id:
        query += " AND s.company_id = ?"
        params.append(effective_company_id)
    query += " GROUP BY substr(i.due_date, 1, 4) ORDER BY year ASC"

    c.execute(query, params)
    rows = [dict(r) for r in c.fetchall()]
    for r in rows:
        r["remaining_amount"] = r["total_amount"] - r["collected_amount"]
    conn.close()
    return rows

@app.post("/api/installments/{installment_id}/pay")
def pay_installment(
    installment_id: int,
    req: PaymentRequest,
    user: dict = Depends(get_current_user)
):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
        SELECT i.*, s.company_id, u.unit_code, s.client_name 
        FROM installments i
        JOIN sales s ON i.sale_id = s.id
        JOIN units u ON s.unit_id = u.id
        WHERE i.id = ?
    """, (installment_id,))
    inst = c.fetchone()
    if not inst:
        conn.close()
        raise HTTPException(status_code=404, detail="القسط غير موجود")

    # Prevent double payment
    if inst["status"] == "paid":
        conn.close()
        raise HTTPException(status_code=400, detail="هذا القسط مسدد بالفعل ولا يمكن تحصيله مرة أخرى")

    # Authorization check
    if user["role"] == "company" and user["company_id"] != inst["company_id"]:
        conn.close()
        raise HTTPException(status_code=403, detail="غير مصرح لك بسداد قسط لشركة أخرى")

    if req.paid_amount <= 0:
        conn.close()
        raise HTTPException(status_code=400, detail="قيمة المبلغ المسدد يجب أن تكون أكبر من صفر")

    # Validate payment_date format
    try:
        datetime.strptime(req.payment_date, "%Y-%m-%d")
    except (ValueError, TypeError):
        conn.close()
        raise HTTPException(status_code=400, detail="تاريخ السداد غير صحيح (الصيغة المطلوبة: YYYY-MM-DD)")

    original_amount = round(inst["amount"], 2)
    paid_now = round(req.paid_amount, 2)

    if paid_now > original_amount:
        conn.close()
        raise HTTPException(
            status_code=400,
            detail=f"المبلغ المسدد ({paid_now:,.2f} ج.م) لا يمكن أن يتجاوز قيمة القسط المستحقة ({original_amount:,.2f} ج.م)"
        )

    receipt = req.receipt_no or f"REC-{datetime.now().strftime('%Y%m%d')}-{installment_id}"
    bank_dep = 1 if req.bank_deposited or req.payment_method == "bank_transfer" else 0
    b_name = req.bank_name if req.bank_name else ("تحويل بنكي" if req.payment_method == "bank_transfer" else "خزينة الشركة")

    remainder = round(original_amount - paid_now, 2)

    if remainder > 0:
        # Partial payment: Split installment to preserve strict contract balance
        pay_notes = req.notes or ""
        partial_note = f"سداد جزئي: {paid_now:,.2f} ج.م من أصل {original_amount:,.2f} ج.م"
        final_notes = f"{pay_notes} | {partial_note}".strip(" |")

        c.execute("""
            UPDATE installments
            SET status = 'paid',
                amount = ?,
                paid_amount = ?,
                payment_date = ?,
                payment_method = ?,
                receipt_no = ?,
                bank_deposited = ?,
                bank_name = ?,
                notes = ?
            WHERE id = ?
        """, (paid_now, paid_now, req.payment_date, req.payment_method, receipt, bank_dep, b_name, final_notes, installment_id))

        today_str = datetime.now().strftime("%Y-%m-%d")
        rem_status = 'overdue' if inst["due_date"] < today_str else 'pending'
        rem_note = f"متبقي القسط #{inst['installment_no']} بعد سداد جزئي ({paid_now:,.2f} ج.م)"

        c.execute("""
            INSERT INTO installments (
                sale_id, installment_no, installment_type, due_date, amount, status,
                paid_amount, bank_deposited, bank_name, notes
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, '', ?)
        """, (inst["sale_id"], inst["installment_no"], inst["installment_type"], inst["due_date"], remainder, rem_status, rem_note))

        action_detail = f"سداد جزئي بقيمة {paid_now:,.2f} ج.م للوحدة {inst['unit_code']} (متبقي {remainder:,.2f} ج.م)"
        msg = f"تم تسجيل السداد الجزئي بنجاح بمبلغ {paid_now:,.2f} ج.م (وتم جدولة المتبقي {remainder:,.2f} ج.م تلقائياً)"
    else:
        # Full payment
        c.execute("""
            UPDATE installments
            SET status = 'paid',
                paid_amount = ?,
                payment_date = ?,
                payment_method = ?,
                receipt_no = ?,
                bank_deposited = ?,
                bank_name = ?,
                notes = COALESCE(?, notes)
            WHERE id = ?
        """, (paid_now, req.payment_date, req.payment_method, receipt, bank_dep, b_name, req.notes, installment_id))

        action_detail = f"تحصيل قسط بالكامل بمبلغ {paid_now:,.2f} ج.م للوحدة {inst['unit_code']} - العميل {inst['client_name']}"
        msg = "تم تسجيل السداد بالكامل بنجاح"

    # Log action
    c.execute("""
        INSERT INTO activity_logs (username, action, details)
        VALUES (?, 'PAY_INSTALLMENT', ?)
    """, (user["username"], action_detail))

    if bank_dep == 1:
        c.execute("""
            INSERT INTO activity_logs (username, action, details)
            VALUES (?, 'BANK_DEPOSIT', ?)
        """, (user["username"], f"دخول الفلوس البنك ({b_name}) بمبلغ {paid_now:,.2f} ج.م - قسط الوحدة {inst['unit_code']} (العميل: {inst['client_name']})"))

    conn.commit()
    conn.close()
    return {"success": True, "message": msg, "receipt_no": receipt, "remainder": remainder}

@app.post("/api/installments/{installment_id}/bank-status")
def toggle_bank_status(
    installment_id: int,
    req: BankStatusRequest,
    user: dict = Depends(get_current_user)
):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
        SELECT i.*, u.unit_code, s.client_name
        FROM installments i
        JOIN sales s ON i.sale_id = s.id
        JOIN units u ON s.unit_id = u.id
        WHERE i.id = ?
    """, (installment_id,))
    inst = c.fetchone()

    dep_val = 1 if req.bank_deposited else 0
    b_name = req.bank_name or (inst["bank_name"] if inst and inst["bank_name"] else "حساب الشركة البنكي")

    c.execute("""
        UPDATE installments
        SET bank_deposited = ?,
            bank_name = ?
        WHERE id = ?
    """, (dep_val, b_name if dep_val else "", installment_id))

    if inst:
        amount_val = inst["paid_amount"] if (inst["paid_amount"] and inst["paid_amount"] > 0) else inst["amount"]
        if dep_val == 1:
            log_detail = f"تأكيد دخول الفلوس البنك ({b_name}) بمبلغ {amount_val:,.2f} ج.م - قسط #{inst['installment_no']} للوحدة {inst['unit_code']} (العميل: {inst['client_name']})"
        else:
            log_detail = f"إلغاء تأكيد الإيداع البنكي لقسط #{inst['installment_no']} للوحدة {inst['unit_code']} بمبلغ {amount_val:,.2f} ج.م"

        c.execute("""
            INSERT INTO activity_logs (username, action, details)
            VALUES (?, 'BANK_DEPOSIT', ?)
        """, (user["username"], log_detail))

    conn.commit()
    conn.close()
    return {"success": True, "bank_deposited": dep_val, "bank_name": b_name if dep_val else ""}

# --- UNITS API ---

@app.get("/api/units")
def get_units(
    floor: Optional[str] = None,
    status: Optional[str] = "all", # all, AVAILABLE, SOLD
    search: Optional[str] = None,
    user: dict = Depends(get_current_user)
):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    query = """
        SELECT 
            u.*,
            s.client_name, s.sale_price, s.sale_date, s.id as sale_id,
            c.name_ar as company_name, c.brand_color, c.code as company_code
        FROM units u
        LEFT JOIN sales s ON s.unit_id = u.id
        LEFT JOIN companies c ON s.company_id = c.id
        WHERE 1=1
    """
    params = []

    if status and status != "all":
        query += " AND u.status = ?"
        params.append(status)

    if floor and floor != "all":
        query += " AND u.floor LIKE ?"
        params.append(f"%{floor}%")

    if search:
        query += " AND (u.unit_code LIKE ? OR s.client_name LIKE ?)"
        s_param = f"%{search}%"
        params.extend([s_param, s_param])

    query += " ORDER BY u.id ASC"

    c.execute(query, params)
    units = [dict(r) for r in c.fetchall()]
    conn.close()
    return units

# --- RECORD NEW SALE API ---

@app.post("/api/sales")
def create_sale(
    req: SaleCreateRequest,
    user: dict = Depends(get_current_user)
):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Basic Validations
    if not req.client_name or not req.client_name.strip():
        conn.close()
        raise HTTPException(status_code=400, detail="اسم العميل مطلوب")

    if req.client_national_id and req.client_national_id.strip():
        nid = req.client_national_id.strip()
        if not nid.isdigit() or len(nid) != 14:
            conn.close()
            raise HTTPException(status_code=400, detail="الرقم القومي يجب أن يتكون من 14 رقماً")

    if req.sale_price <= 0:
        conn.close()
        raise HTTPException(status_code=400, detail="سعر البيع الفعلي يجب أن يكون أكبر من صفر")

    if req.down_payment < 0:
        conn.close()
        raise HTTPException(status_code=400, detail="قيمة الدفعة المقدمة لا يمكن أن تكون سالبة")

    if req.down_payment > req.sale_price:
        conn.close()
        raise HTTPException(status_code=400, detail="قيمة الدفعة المقدمة لا يمكن أن تتجاوز إجمالي سعر البيع")

    # Validate sale_date format
    try:
        datetime.strptime(req.sale_date, "%Y-%m-%d")
    except (ValueError, TypeError):
        conn.close()
        raise HTTPException(status_code=400, detail="تاريخ التعاقد غير صحيح (الصيغة المطلوبة: YYYY-MM-DD)")

    # Allow 100% cash sale (down_payment == sale_price, no installments needed)
    is_full_cash_sale = abs(req.down_payment - req.sale_price) < 1.0 and (not req.custom_installments or len(req.custom_installments) == 0)

    # Financial Balance Check: Ensure sum(installments) + down_payment == sale_price
    if req.custom_installments and len(req.custom_installments) > 0:
        has_dp = any(x.installment_type == "down_payment" for x in req.custom_installments)
        inst_sum = sum(x.amount for x in req.custom_installments)
        total_contract_sum = inst_sum if has_dp else round(inst_sum + req.down_payment, 2)

        for item in req.custom_installments:
            if item.amount <= 0:
                conn.close()
                raise HTTPException(status_code=400, detail=f"قيمة القسط رقم {item.installment_no or 'المحدد'} يجب أن تكون أكبر من صفر")
            if not item.due_date or not item.due_date.strip():
                conn.close()
                raise HTTPException(status_code=400, detail=f"تاريخ استحقاق القسط رقم {item.installment_no or 'المحدد'} مطلوب")

        if abs(total_contract_sum - req.sale_price) > 1.0:
            conn.close()
            raise HTTPException(
                status_code=400,
                detail=f"عدم اتزان مالي: إجمالي الأقساط والمقدم ({total_contract_sum:,.2f} ج.م) لا يتطابق مع سعر البيع المطلوب ({req.sale_price:,.2f} ج.م)"
            )

    # Determine seller company
    if user["role"] == "company":
        company_id = user["company_id"]
    else:
        # Admin can choose which company is making the sale
        company_id = req.company_id if req.company_id else 1

    # Check unit availability
    c.execute("SELECT * FROM units WHERE id = ?", (req.unit_id,))
    unit = c.fetchone()
    if not unit:
        conn.close()
        raise HTTPException(status_code=404, detail="الوحدة غير موجودة")

    if unit["status"] == "SOLD":
        conn.close()
        raise HTTPException(status_code=400, detail="هذه الوحدة مباعة بالفعل وغير متاحة")

    approval_status = "approved"
    unit_status = "SOLD"

    catalog_price = unit["catalog_price"]
    discount_amount = round(catalog_price - req.sale_price, 2)
    discount_pct = round((discount_amount / catalog_price * 100) if catalog_price > 0 else 0, 2)

    if req.custom_installments and len(req.custom_installments) > 0:
        inst_count_save = len([x for x in req.custom_installments if x.installment_type != "down_payment"])
        freq_save = "custom"
    else:
        inst_count_save = req.installments_count or 1
        freq_save = req.installment_frequency or "quarterly"

    # Insert Sale
    c.execute("""
        INSERT INTO sales (
            unit_id, company_id, client_name, client_phone, client_national_id,
            sale_date, catalog_price, sale_price, discount_amount, discount_pct,
            down_payment, installments_count, installment_frequency, notes, approval_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        req.unit_id, company_id, req.client_name, req.client_phone, req.client_national_id,
        req.sale_date, catalog_price, req.sale_price, discount_amount, discount_pct,
        req.down_payment, inst_count_save, freq_save, req.notes, approval_status
    ))
    sale_id = cursor_sale_id = c.lastrowid

    # Update unit status to SOLD or PENDING
    c.execute("UPDATE units SET status = ?, sold_to_company_id = ? WHERE id = ?", (unit_status, company_id, req.unit_id))

    # Down payment payment configuration
    dp_method = req.down_payment_method or "bank_transfer"
    dp_dep = 1 if (req.down_payment_deposited or dp_method == "bank_transfer") else 0
    dp_bank = req.down_payment_bank if req.down_payment_bank else ("حساب الشركة البنكي" if dp_dep else "خزينة الشركة")

    # If user specified custom installments manually:
    if is_full_cash_sale:
        # 100% cash sale: only record the down payment, no installments needed
        c.execute("""
            INSERT INTO installments (
                sale_id, installment_no, installment_type, due_date, amount, status,
                paid_amount, payment_date, payment_method, receipt_no, bank_deposited, bank_name, notes
            ) VALUES (?, 0, 'down_payment', ?, ?, 'paid', ?, ?, ?, ?, ?, ?, 'بيع كاش - دفع كامل المبلغ عند التعاقد')
        """, (sale_id, req.sale_date, req.down_payment, req.down_payment, req.sale_date, dp_method, f"REC-DP-{sale_id:04d}", dp_dep, dp_bank))
    elif req.custom_installments and len(req.custom_installments) > 0:
        # Insert Down payment if not included in custom_installments and > 0
        has_dp = any(x.installment_type == "down_payment" for x in req.custom_installments)
        if not has_dp and req.down_payment > 0:
            c.execute("""
                INSERT INTO installments (
                    sale_id, installment_no, installment_type, due_date, amount, status,
                    paid_amount, payment_date, payment_method, receipt_no, bank_deposited, bank_name, notes
                ) VALUES (?, 0, 'down_payment', ?, ?, 'paid', ?, ?, ?, ?, ?, ?, 'دفعة مقدمة عند التعاقد')
            """, (sale_id, req.sale_date, req.down_payment, req.down_payment, req.sale_date, dp_method, f"REC-DP-{sale_id:04d}", dp_dep, dp_bank))

        seq = 1
        for item in req.custom_installments:
            b_dep = 1 if item.bank_deposited else 0
            b_name = item.bank_name or ""
            if item.installment_type == "down_payment":
                c.execute("""
                    INSERT INTO installments (
                        sale_id, installment_no, installment_type, due_date, amount, status,
                        paid_amount, payment_date, payment_method, receipt_no, bank_deposited, bank_name, notes
                    ) VALUES (?, 0, 'down_payment', ?, ?, 'paid', ?, ?, ?, ?, ?, ?, 'دفعة مقدمة عند التعاقد')
                """, (sale_id, item.due_date, item.amount, item.amount, item.due_date, dp_method, f"REC-DP-{sale_id:04d}", dp_dep, dp_bank))
            else:
                num = item.installment_no if item.installment_no else seq
                c.execute("""
                    INSERT INTO installments (
                        sale_id, installment_no, installment_type, due_date, amount, status, paid_amount, bank_deposited, bank_name, notes
                    ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """, (sale_id, num, item.installment_type or "installment", item.due_date, item.amount, b_dep, b_name, item.notes or ""))
                seq += 1
    else:
        # Fallback to automated generation using exact calendar month additions
        if req.down_payment > 0:
            c.execute("""
                INSERT INTO installments (
                    sale_id, installment_no, installment_type, due_date, amount, status,
                    paid_amount, payment_date, payment_method, receipt_no, bank_deposited, bank_name, notes
                ) VALUES (?, 0, 'down_payment', ?, ?, 'paid', ?, ?, ?, ?, ?, ?, 'دفعة مقدمة عند التعاقد')
            """, (sale_id, req.sale_date, req.down_payment, req.down_payment, req.sale_date, dp_method, f"REC-DP-{sale_id:04d}", dp_dep, dp_bank))

        inst_cnt = req.installments_count if (req.installments_count and req.installments_count > 0) else 1
        remaining_balance = round(req.sale_price - req.down_payment, 2)
        part = round(remaining_balance / inst_cnt, 2)
        remainder = round(remaining_balance - (part * inst_cnt), 2)

        freq_months = 3
        if req.installment_frequency == "monthly":
            freq_months = 1
        elif req.installment_frequency in ("semi-annual", "semi_annual"):
            freq_months = 6
        elif req.installment_frequency == "annual":
            freq_months = 12

        first_date = req.first_due_date if req.first_due_date else req.sale_date
        start_due_dt = datetime.strptime(first_date, "%Y-%m-%d")

        for i in range(1, inst_cnt + 1):
            due_dt = add_months_to_date(start_due_dt, freq_months * (i - 1))
            due_str = due_dt.strftime("%Y-%m-%d")
            amt = round(part + remainder, 2) if i == inst_cnt else part
            c.execute("""
                INSERT INTO installments (
                    sale_id, installment_no, installment_type, due_date, amount, status, paid_amount, bank_deposited, bank_name
                ) VALUES (?, ?, 'installment', ?, ?, 'pending', 0, 0, '')
            """, (sale_id, i, due_str, amt))

    # Log action
    c.execute("""
        INSERT INTO activity_logs (username, action, details)
        VALUES (?, 'CREATE_SALE', ?)
    """, (user["username"], f"تسجيل بيع الوحدة {unit['unit_code']} للعميل {req.client_name} بقيمة {req.sale_price:,.2f} ج.م"))

    if dp_dep == 1 and req.down_payment > 0:
        c.execute("""
            INSERT INTO activity_logs (username, action, details)
            VALUES (?, 'BANK_DEPOSIT', ?)
        """, (user["username"], f"دخول الفلوس البنك ({dp_bank}) بمبلغ {req.down_payment:,.2f} ج.م - مقدم تعاقد الوحدة {unit['unit_code']} للعميل {req.client_name}"))

    conn.commit()
    conn.close()
    msg = "تم تسجيل التعاقد وجدولة الأقساط بنجاح"
    return {
        "success": True,
        "sale_id": sale_id,
        "message": msg,
        "installments_count": req.installments_count + (1 if req.down_payment > 0 else 0)
    }

# --- DELETE / CANCEL SALE (Admin or Owning Company) ---

@app.delete("/api/sales/{sale_id}")
@app.post("/api/sales/{sale_id}/reject")  # backward-compat alias
def delete_sale(sale_id: int, user: dict = Depends(get_current_user)):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT s.*, u.unit_code, c.name_ar as company_name, c.name_en as company_en
        FROM sales s
        JOIN units u ON s.unit_id = u.id
        JOIN companies c ON s.company_id = c.id
        WHERE s.id = ?
    """, (sale_id,))
    sale = c.fetchone()
    if not sale:
        conn.close()
        raise HTTPException(status_code=404, detail="العقد غير موجود")
    
    # Permission check: Admin or the owning company
    if user["role"] != "admin":
        if user.get("company_id") != sale["company_id"]:
            conn.close()
            raise HTTPException(status_code=403, detail="ليس لديك صلاحية لحذف هذا العقد (مقتصر على الشركة صاحبة العقد أو المدير العام)")

    unit_code = sale["unit_code"]
    client_name = sale["client_name"]
    company_name = sale["company_name"]
    sale_price = sale["sale_price"]

    c.execute("DELETE FROM installments WHERE sale_id = ?", (sale_id,))
    c.execute("DELETE FROM sales WHERE id = ?", (sale_id,))
    c.execute("UPDATE units SET status = 'AVAILABLE', sold_to_company_id = NULL WHERE id = ?", (sale["unit_id"],))

    if user["role"] == "company":
        details = f"قامت شركة {company_name} ({user['username']}) بحذف عقد الوحدة {unit_code} للعميل '{client_name}' (قيمة العقد: {sale_price:,.0f} ج.م) وإعادة الوحدة للمتاح"
    else:
        details = f"قام المدير العام ({user['username']}) بحذف تعاقد البيع #{sale_id} للوحدة {unit_code} للعميل '{client_name}' التابع لشركة {company_name} وإعادة الوحدة للمتاح"

    c.execute("""
        INSERT INTO activity_logs (username, action, details)
        VALUES (?, 'DELETE_SALE', ?)
    """, (user["username"], details))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"تم حذف العقد بنجاح وإعادة الوحدة {unit_code} للمتاح للبيع"}

# --- UPDATE / EDIT SALE (Admin or Owning Company) ---

@app.put("/api/sales/{sale_id}")
def update_sale(sale_id: int, req: UpdateSaleRequest, user: dict = Depends(get_current_user)):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
        SELECT s.*, u.unit_code, c.name_ar as company_name, c.name_en as company_en
        FROM sales s
        JOIN units u ON s.unit_id = u.id
        JOIN companies c ON s.company_id = c.id
        WHERE s.id = ?
    """, (sale_id,))
    sale = c.fetchone()
    if not sale:
        conn.close()
        raise HTTPException(status_code=404, detail="العقد غير موجود")

    # Permission check: Admin or the owning company
    if user["role"] != "admin":
        if user.get("company_id") != sale["company_id"]:
            conn.close()
            raise HTTPException(status_code=403, detail="ليس لديك صلاحية لتعديل هذا العقد (مقتصر على الشركة صاحبة العقد أو المدير العام)")

    # Check for paid non-downpayment installments
    c.execute("""
        SELECT COUNT(*) as paid_insts_count, COALESCE(SUM(paid_amount), 0) as paid_sum 
        FROM installments 
        WHERE sale_id = ? AND status = 'paid' AND installment_type != 'down_payment'
    """, (sale_id,))
    paid_info = c.fetchone()
    paid_insts = paid_info["paid_insts_count"]

    # Check if financial numbers are being changed
    financials_changed = False
    new_price = float(req.sale_price) if req.sale_price is not None else float(sale["sale_price"])
    new_down = float(req.down_payment) if req.down_payment is not None else float(sale["down_payment"])
    new_count = int(req.installments_count) if req.installments_count is not None else int(sale["installments_count"])
    new_freq = req.installment_frequency if req.installment_frequency else sale["installment_frequency"]

    if abs(new_price - float(sale["sale_price"])) > 0.01 or abs(new_down - float(sale["down_payment"])) > 0.01 or new_count != int(sale["installments_count"]) or new_freq != sale["installment_frequency"]:
        financials_changed = True

    if paid_insts > 0 and financials_changed:
        conn.close()
        raise HTTPException(status_code=400, detail="لا يمكن تعديل المبالغ المالية لوجود أقساط محصلة بالفعل. يمكنك تعديل بيانات العميل والملاحظات فقط.")

    changes = []
    if req.client_name.strip() != sale["client_name"]:
        changes.append(f"اسم العميل: '{sale['client_name']}' ➔ '{req.client_name.strip()}'")
    if (req.client_phone or "").strip() != (sale["client_phone"] or "").strip():
        changes.append(f"الهاتف: '{sale['client_phone'] or '-'}' ➔ '{req.client_phone.strip()}'")
    if (req.client_national_id or "").strip() != (sale["client_national_id"] or "").strip():
        changes.append(f"الرقم القومي: '{sale['client_national_id'] or '-'}' ➔ '{req.client_national_id.strip()}'")
    if req.sale_date.strip() != (sale["sale_date"] or "").strip():
        changes.append(f"تاريخ التعاقد: '{sale['sale_date']}' ➔ '{req.sale_date.strip()}'")
    if (req.notes or "").strip() != (sale["notes"] or "").strip():
        changes.append("الملاحظات")
    if financials_changed:
        changes.append(f"المبالغ: السعر {float(sale['sale_price']):,.0f} ➔ {new_price:,.0f} ج.م، المقدم {float(sale['down_payment']):,.0f} ➔ {new_down:,.0f} ج.م، الأقساط: {new_count}")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    editor_display = f"شركة {sale['company_name']} ({user['username']})" if user["role"] == "company" else f"المدير العام ({user['username']})"

    # If financials changed and no subsequent installments paid, re-generate installments
    if financials_changed:
        # Recalculate discount
        cat_price = float(sale["catalog_price"])
        disc_amt = max(0.0, cat_price - new_price)
        disc_pct = round((disc_amt / cat_price) * 100, 2) if cat_price > 0 else 0.0

        c.execute("""
            UPDATE sales SET
                client_name = ?,
                client_phone = ?,
                client_national_id = ?,
                sale_date = ?,
                notes = ?,
                sale_price = ?,
                discount_amount = ?,
                discount_pct = ?,
                down_payment = ?,
                installments_count = ?,
                installment_frequency = ?,
                last_edited_by = ?,
                last_edited_at = ?
            WHERE id = ?
        """, (
            req.client_name.strip(),
            (req.client_phone or "").strip(),
            (req.client_national_id or "").strip(),
            req.sale_date.strip(),
            (req.notes or "").strip(),
            new_price,
            disc_amt,
            disc_pct,
            new_down,
            new_count,
            new_freq,
            editor_display,
            now_str,
            sale_id
        ))

        # Delete existing installments and recreate
        c.execute("DELETE FROM installments WHERE sale_id = ?", (sale_id,))
        
        # Recreate down payment if > 0
        if new_down > 0:
            c.execute("""
                INSERT INTO installments (
                    sale_id, installment_no, installment_type, due_date, amount, status,
                    paid_amount, payment_date, payment_method, receipt_no, bank_deposited, bank_name, notes
                ) VALUES (?, 0, 'down_payment', ?, ?, 'paid', ?, ?, 'bank_transfer', ?, 1, 'حساب الشركة البنكي', 'دفعة مقدمة عند التعاقد (معدلة)')
            """, (sale_id, req.sale_date.strip(), new_down, new_down, req.sale_date.strip(), f"REC-DP-{sale_id:04d}"))

        remaining_balance = round(new_price - new_down, 2)
        inst_cnt = max(1, new_count)
        part = round(remaining_balance / inst_cnt, 2)
        remainder = round(remaining_balance - (part * inst_cnt), 2)

        freq_months = 3
        if new_freq == "monthly":
            freq_months = 1
        elif new_freq in ("semi-annual", "semi_annual"):
            freq_months = 6
        elif new_freq == "annual":
            freq_months = 12

        start_due_dt = datetime.strptime(req.sale_date.strip(), "%Y-%m-%d")
        for i in range(1, inst_cnt + 1):
            due_dt = add_months_to_date(start_due_dt, freq_months * (i - 1))
            due_str = due_dt.strftime("%Y-%m-%d")
            amt = round(part + remainder, 2) if i == inst_cnt else part
            c.execute("""
                INSERT INTO installments (
                    sale_id, installment_no, installment_type, due_date, amount, status, paid_amount, bank_deposited, bank_name
                ) VALUES (?, ?, 'installment', ?, ?, 'pending', 0, 0, '')
            """, (sale_id, i, due_str, amt))
    else:
        # Only client details and notes updated
        c.execute("""
            UPDATE sales SET
                client_name = ?,
                client_phone = ?,
                client_national_id = ?,
                sale_date = ?,
                notes = ?,
                last_edited_by = ?,
                last_edited_at = ?
            WHERE id = ?
        """, (
            req.client_name.strip(),
            (req.client_phone or "").strip(),
            (req.client_national_id or "").strip(),
            req.sale_date.strip(),
            (req.notes or "").strip(),
            editor_display,
            now_str,
            sale_id
        ))

    changes_str = "، ".join(changes) if changes else "تحديث البيانات بدون تغييرات جوهرية"
    audit_details = f"قام {editor_display} بتعديل عقد الوحدة {sale['unit_code']} للعميل '{req.client_name.strip()}' - التعديلات: [{changes_str}]"
    
    c.execute("""
        INSERT INTO activity_logs (username, action, details)
        VALUES (?, 'UPDATE_SALE', ?)
    """, (user["username"], audit_details))

    conn.commit()
    conn.close()

    return {
        "success": True,
        "message": f"تم تعديل العقد بنجاح وتوثيق التغييرات في سجل نشاطات الإدارة",
        "sale_id": sale_id,
        "last_edited_by": editor_display,
        "last_edited_at": now_str
    }

# --- SALE DETAILS MODAL API ---

@app.get("/api/sales/{sale_id}")
def get_sale_details(sale_id: int, user: dict = Depends(get_current_user)):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
        SELECT 
            s.*,
            u.unit_code, u.floor, u.unit_type, u.area, u.price_m, u.sug,
            c.name_ar as company_name, c.name_en as company_en, c.brand_color, c.code as company_code
        FROM sales s
        JOIN units u ON s.unit_id = u.id
        JOIN companies c ON s.company_id = c.id
        WHERE s.id = ?
    """, (sale_id,))
    sale = c.fetchone()
    if not sale:
        conn.close()
        raise HTTPException(status_code=404, detail="البيانات غير موجودة")

    c.execute("""
        SELECT * FROM installments WHERE sale_id = ? ORDER BY installment_no ASC
    """, (sale_id,))
    installments = [dict(r) for r in c.fetchall()]

    conn.close()
    return {
        "sale": dict(sale),
        "installments": installments
    }

@app.get("/api/sales")
def get_sales(
    company_id: Optional[int] = None,
    user: dict = Depends(get_current_user)
):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    query = """
        SELECT 
            s.*,
            u.unit_code, u.floor, u.unit_type, u.area,
            c.name_ar as company_name, c.name_en as company_en, c.brand_color, c.code as company_code,
            COALESCE((SELECT SUM(paid_amount) FROM installments WHERE sale_id = s.id AND status = 'paid'), 0) as paid_amount,
            COALESCE((SELECT COUNT(*) FROM installments WHERE sale_id = s.id AND status = 'paid'), 0) as paid_count,
            COALESCE((SELECT COUNT(*) FROM installments WHERE sale_id = s.id), 0) as installments_count,
            COALESCE((SELECT SUM(amount) FROM installments WHERE sale_id = s.id AND status = 'overdue'), 0) as overdue_amount
        FROM sales s
        JOIN units u ON s.unit_id = u.id
        JOIN companies c ON s.company_id = c.id
        WHERE 1=1
    """
    params = []
    if company_id:
        query += " AND s.company_id = ?"
        params.append(company_id)
    query += " ORDER BY s.sale_date DESC, s.id DESC"

    c.execute(query, params)
    rows = [dict(r) for r in c.fetchall()]
    for r in rows:
        r["remaining_balance"] = r["sale_price"] - r["paid_amount"]
        r["collection_pct"] = round((r["paid_amount"] / r["sale_price"] * 100) if r["sale_price"] > 0 else 0, 1)
    conn.close()
    return rows




# --- STATIC PWA FILES ---

@app.get("/manifest.json")
def serve_manifest():
    manifest_file = os.path.join(BASE_DIR, "manifest.json")
    if os.path.exists(manifest_file):
        with open(manifest_file, "r", encoding="utf-8") as f:
            return JSONResponse(content=json.load(f), media_type="application/manifest+json")
    return JSONResponse(content={})

@app.get("/sw.js")
def serve_sw():
    sw_file = os.path.join(BASE_DIR, "sw.js")
    if os.path.exists(sw_file):
        with open(sw_file, "r", encoding="utf-8") as f:
            from starlette.responses import Response
            return Response(content=f.read(), media_type="application/javascript")
    return Response(content="", media_type="application/javascript")

@app.get("/icon-{size}.png")
def serve_icon(size: str):
    icon_file = os.path.join(BASE_DIR, f"icon-{size}.png")
    if os.path.exists(icon_file):
        from starlette.responses import FileResponse
        return FileResponse(icon_file, media_type="image/png")
    raise HTTPException(status_code=404, detail="Icon not found")

@app.get("/favicon.ico")
def serve_favicon():
    icon_file = os.path.join(BASE_DIR, "icon-192.png")
    if os.path.exists(icon_file):
        from starlette.responses import FileResponse
        return FileResponse(icon_file, media_type="image/png")
    from starlette.responses import Response
    return Response(status_code=204)

# --- SERVE FRONTEND SPA ---

@app.get("/", response_class=HTMLResponse)
def serve_index():
    index_file = os.path.join(BASE_DIR, "templates", "index.html")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})
    return HTMLResponse(content="<h1>Mega Mall API is running. Index file not found.</h1>")

# --- ARABIC TAFQEET HELPER ---
def tafqeet_arabic(amount) -> str:
    try:
        val = int(round(float(amount)))
        if val == 0:
            return "صفر جنيهاً مصرياً"
        ones = ['', 'واحد', 'اثنان', 'ثلاثة', 'أربعة', 'خمسة', 'ستة', 'سبعة', 'ثمانية', 'تسعة', 'عشرة']
        teens = ['أحد عشر', 'اثنا عشر', 'ثلاثة عشر', 'أربعة عشر', 'خمسة عشر', 'ستة عشر', 'سبعة عشر', 'ثمانية عشر', 'تسعة عشر']
        tens = ['', '', 'عشرون', 'ثلاثون', 'أربعون', 'خمسون', 'ستون', 'سبعون', 'ثمانون', 'تسعون']
        hundreds = ['', 'مائة', 'مائتان', 'ثلاثمائة', 'أربعمائة', 'خمسمائة', 'ستمائة', 'سبعمائة', 'ثمانمائة', 'تسعمائة']
        
        parts = []
        millions = val // 1000000
        rem = val % 1000000
        if millions > 0:
            if millions == 1: parts.append('مليون')
            elif millions == 2: parts.append('مليونان')
            elif 3 <= millions <= 10: parts.append(f'{ones[millions]} ملايين')
            else: parts.append(f'{millions} مليون')
            
        thousands = rem // 1000
        rem = rem % 1000
        if thousands > 0:
            if thousands == 1: parts.append('ألف')
            elif thousands == 2: parts.append('ألفان')
            elif 3 <= thousands <= 10: parts.append(f'{ones[thousands]} آلاف')
            else:
                h = thousands // 100
                t = thousands % 100
                t_str = ''
                if h > 0: t_str += hundreds[h]
                if t > 0:
                    if t_str: t_str += ' و '
                    if t <= 10: t_str += ones[t]
                    elif t < 20: t_str += teens[t-11]
                    else:
                        o = t % 10
                        tn = t // 10
                        if o > 0: t_str += f'{ones[o]} و {tens[tn]}'
                        else: t_str += tens[tn]
                parts.append(f'{t_str} ألف')
                
        if rem > 0:
            h = rem // 100
            t = rem % 100
            t_str = ''
            if h > 0: t_str += hundreds[h]
            if t > 0:
                if t_str: t_str += ' و '
                if t <= 10: t_str += ones[t]
                elif t < 20: t_str += teens[t-11]
                else:
                    o = t % 10
                    tn = t // 10
                    if o > 0: t_str += f'{ones[o]} و {tens[tn]}'
                    else: t_str += tens[tn]
            parts.append(t_str)
            
        return "فقط " + " و ".join(parts) + " جنيهاً مصرياً لا غير"
    except Exception:
        return f"{amount:,.2f} ج.م"


# --- PDF REPORTS & RECEIPTS ---

@app.get("/api/sales/{sale_id}/pdf")
def generate_pdf_report(
    sale_id: int,
    user: dict = Depends(get_current_user_flexible)
):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
        SELECT 
            s.*,
            u.unit_code, u.floor, u.unit_type, u.area, u.price_m,
            c.name_ar as company_name, c.code as company_code
        FROM sales s
        JOIN units u ON s.unit_id = u.id
        JOIN companies c ON s.company_id = c.id
        WHERE s.id = ?
    """, (sale_id,))
    sale = c.fetchone()
    if not sale:
        conn.close()
        raise HTTPException(status_code=404, detail="بيانات العقد غير موجودة")

    if user["role"] != "admin" and user.get("company_id") and sale["company_id"] != user["company_id"]:
        conn.close()
        raise HTTPException(status_code=403, detail="غير مصرح لك بالاطلاع على هذا العقد")

    c.execute("SELECT * FROM installments WHERE sale_id = ? ORDER BY installment_no ASC", (sale_id,))
    installments = c.fetchall()
    conn.close()

    total_paid = sum(i['paid_amount'] or 0 for i in installments)
    remaining = max(0.0, sale['sale_price'] - total_paid)
    paid_pct = round((total_paid / sale['sale_price'] * 100), 1) if sale['sale_price'] > 0 else 0
    today_str = datetime.now().strftime("%Y-%m-%d")

    rows_html = ""
    for inst in installments:
        if inst['status'] == 'paid':
            status_badge = '<span class="badge badge-paid">مسدد</span>'
        elif inst['status'] == 'overdue':
            status_badge = '<span class="badge badge-overdue">متأخر</span>'
        else:
            status_badge = '<span class="badge badge-pending">متبقي</span>'

        type_label = "دفعة مقدمة" if inst['installment_type'] == "down_payment" else f"قسط #{inst['installment_no']}"
        bank_info = inst['bank_name'] if (inst['bank_deposited'] and inst['bank_name']) else ("تم الإيداع" if inst['bank_deposited'] else "—")
        
        rows_html += f"""
        <tr>
            <td class="text-center font-bold">{inst['installment_no']}</td>
            <td>{type_label}</td>
            <td class="text-center font-mono">{inst['due_date']}</td>
            <td class="text-left font-mono font-bold">{inst['amount']:,.2f}</td>
            <td class="text-center">{status_badge}</td>
            <td class="text-center font-mono">{inst['payment_date'] or '—'}</td>
            <td class="text-left font-mono font-bold text-success">{inst['paid_amount']:,.2f}</td>
            <td class="text-center font-mono text-xs">{inst['receipt_no'] or '—'}</td>
            <td class="text-center text-xs">{bank_info}</td>
        </tr>
        """

    html_content = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>كشف حساب تعاقد - {sale['client_name']} ({sale['unit_code']})</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800;900&family=Plus+Jakarta+Sans:wght@500;700;800&display=swap" rel="stylesheet">
    <style>
        @page {{ size: A4 portrait; margin: 12mm 15mm; }}
        * {{ box-sizing: border-box; -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }}
        body {{ font-family: 'Cairo', sans-serif; background: #f8fafc; color: #0f172a; margin: 0; padding: 24px; line-height: 1.5; font-size: 13px; }}
        .no-print {{ display: flex; justify-content: space-between; align-items: center; background: #1e293b; color: white; padding: 12px 20px; border-radius: 12px; margin-bottom: 24px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }}
        .btn {{ padding: 8px 18px; border-radius: 8px; font-weight: 700; font-family: 'Cairo'; cursor: pointer; border: none; font-size: 13px; text-decoration: none; display: inline-flex; align-items: center; gap: 8px; }}
        .btn-primary {{ background: #2563eb; color: white; }}
        .btn-primary:hover {{ background: #1d4ed8; }}
        .btn-secondary {{ background: #475569; color: white; }}
        .sheet {{ background: white; border-radius: 16px; padding: 32px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); border: 1px solid #e2e8f0; max-width: 900px; margin: 0 auto; }}
        .header {{ display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 2px solid #2563eb; padding-bottom: 18px; margin-bottom: 20px; }}
        .brand-title {{ margin: 0; font-size: 24px; font-weight: 900; color: #1e3a8a; }}
        .brand-sub {{ margin: 4px 0 0 0; color: #64748b; font-size: 12px; font-weight: 600; }}
        .alliance-badge {{ display: inline-block; background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: 700; margin-top: 6px; }}
        .doc-meta {{ text-align: left; }}
        .doc-title {{ margin: 0; font-size: 18px; font-weight: 800; color: #0f172a; }}
        .doc-no {{ margin: 4px 0; font-family: 'Plus Jakarta Sans', monospace; color: #64748b; font-size: 12px; }}
        .grid-info {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; padding: 16px; margin-bottom: 20px; }}
        .info-group h4 {{ margin: 0 0 8px 0; font-size: 12px; color: #64748b; font-weight: 700; border-bottom: 1px dashed #cbd5e1; padding-bottom: 4px; }}
        .info-row {{ display: flex; justify-content: space-between; margin-bottom: 5px; font-size: 12.5px; }}
        .info-label {{ color: #475569; font-weight: 600; }}
        .info-value {{ font-weight: 700; color: #0f172a; }}
        .kpi-cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 24px; }}
        .kpi-card {{ background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 12px; text-align: center; }}
        .kpi-card.highlight {{ background: #eff6ff; border-color: #bfdbfe; }}
        .kpi-title {{ font-size: 11px; color: #64748b; font-weight: 700; margin-bottom: 4px; }}
        .kpi-val {{ font-family: 'Plus Jakarta Sans', sans-serif; font-size: 16px; font-weight: 800; color: #0f172a; }}
        .kpi-card.highlight .kpi-val {{ color: #1d4ed8; }}
        .text-success {{ color: #059669 !important; }}
        .text-danger {{ color: #dc2626 !important; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 12px; }}
        th {{ background: #f1f5f9; color: #334155; font-weight: 800; padding: 10px 8px; border: 1px solid #cbd5e1; text-align: right; }}
        td {{ padding: 8px; border: 1px solid #e2e8f0; }}
        tr:nth-child(even) {{ background: #f8fafc; }}
        .text-center {{ text-align: center; }}
        .text-left {{ text-align: left; }}
        .font-mono {{ font-family: 'Plus Jakarta Sans', monospace; }}
        .font-bold {{ font-weight: 700; }}
        .badge {{ display: inline-block; padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 700; }}
        .badge-paid {{ background: #dcfce7; color: #15803d; border: 1px solid #bbf7d0; }}
        .badge-overdue {{ background: #ffe4e6; color: #b91c1c; border: 1px solid #fecdd3; }}
        .badge-pending {{ background: #fef3c7; color: #b45309; border: 1px solid #fde68a; }}
        .signatures {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-top: 40px; padding-top: 20px; border-top: 1px dashed #cbd5e1; }}
        .sign-box {{ text-align: center; }}
        .sign-title {{ font-size: 12px; font-weight: 700; color: #475569; margin-bottom: 45px; }}
        .sign-line {{ border-bottom: 1px solid #94a3b8; width: 80%; margin: 0 auto; }}
        .footer-note {{ margin-top: 30px; text-align: center; font-size: 10.5px; color: #94a3b8; border-top: 1px solid #f1f5f9; padding-top: 10px; }}
        @media print {{
            body {{ background: white; padding: 0; }}
            .sheet {{ border: none; box-shadow: none; padding: 0; max-width: 100%; }}
            .no-print {{ display: none !important; }}
        }}
    </style>
</head>
<body>
    <div class="no-print">
        <div style="font-weight: 700; font-size: 14px;">
            كشف حساب تعاقد وأقساط العميل • مشروع ميجا مول
        </div>
        <div style="display: flex; gap: 10px;">
            <button class="btn btn-primary" onclick="window.print()">
                <svg width="16" height="16" fill="currentColor" viewBox="0 0 24 24"><path d="M19 8H5c-1.66 0-3 1.34-3 3v6h4v4h12v-4h4v-6c0-1.66-1.34-3-3-3zm-3 11H8v-5h8v5zm3-7c-.55 0-1-.45-1-1s.45-1 1-1 1 .45 1 1-.45 1-1 1zm-1-9H6v4h12V3z"/></svg>
                طباعة / حفظ كـ PDF
            </button>
            <button class="btn btn-secondary" onclick="window.close()">إغلاق</button>
        </div>
    </div>

    <div class="sheet">
        <div class="header">
            <div>
                <h1 class="brand-title">ميجا مول • MEGA MALL</h1>
                <p class="brand-sub">مشروع ميجا مول التجاري الإداري • منطقة الداون تاون</p>
                <div class="alliance-badge">تحالف الشركات: كونكريت • ترست • MMD • TD</div>
            </div>
            <div class="doc-meta">
                <h2 class="doc-title">كشف حساب تعاقد</h2>
                <div class="doc-no">عقد رقم: #{sale['id']:04d}</div>
                <div class="doc-no">تاريخ التقرير: {today_str}</div>
            </div>
        </div>

        <div class="grid-info">
            <div class="info-group">
                <h4>بيانات العميل والتعاقد</h4>
                <div class="info-row"><span class="info-label">اسم العميل:</span><span class="info-value">{sale['client_name']}</span></div>
                <div class="info-row"><span class="info-label">الهاتف:</span><span class="info-value font-mono">{sale['client_phone'] or '—'}</span></div>
                <div class="info-row"><span class="info-label">الرقم القومي:</span><span class="info-value font-mono">{sale['client_national_id'] or '—'}</span></div>
                <div class="info-row"><span class="info-label">تاريخ التعاقد:</span><span class="info-value font-mono">{sale['sale_date']}</span></div>
            </div>
            <div class="info-group">
                <h4>بيانات الوحدة والشركة</h4>
                <div class="info-row"><span class="info-label">كود الوحدة:</span><span class="info-value font-mono font-bold" style="color: #2563eb;">{sale['unit_code']}</span></div>
                <div class="info-row"><span class="info-label">الدور والنوع:</span><span class="info-value">الدور {sale['floor']} • {sale['unit_type'] or 'تجاري'}</span></div>
                <div class="info-row"><span class="info-label">المساحة:</span><span class="info-value font-mono">{sale['area'] or '—'} م²</span></div>
                <div class="info-row"><span class="info-label">الشركة البائعة:</span><span class="info-value font-bold">{sale['company_name']}</span></div>
            </div>
        </div>

        <div class="kpi-cards">
            <div class="kpi-card">
                <div class="kpi-title">سعر البيع الإجمالي</div>
                <div class="kpi-val">{sale['sale_price']:,.2f} ج.م</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">الدفعة المقدمة</div>
                <div class="kpi-val">{sale['down_payment']:,.2f} ج.م</div>
            </div>
            <div class="kpi-card highlight">
                <div class="kpi-title">المسدد حتى الآن ({paid_pct}%)</div>
                <div class="kpi-val text-success">{total_paid:,.2f} ج.م</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">المتبقي المطلوب</div>
                <div class="kpi-val text-danger">{remaining:,.2f} ج.م</div>
            </div>
        </div>

        <h3 style="margin: 0 0 10px 0; font-size: 14px; font-weight: 800; color: #1e293b;">جدول سداد الأقساط والدفعات</h3>
        <table>
            <thead>
                <tr>
                    <th style="width: 35px;" class="text-center">#</th>
                    <th>نوع الدفعة</th>
                    <th class="text-center">تاريخ الاستحقاق</th>
                    <th class="text-left">المبلغ المطلوب</th>
                    <th class="text-center">الحالة</th>
                    <th class="text-center">تاريخ السداد</th>
                    <th class="text-left">المبلغ المسدد</th>
                    <th class="text-center">رقم الإيصال</th>
                    <th class="text-center">البنك</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
            <tfoot>
                <tr style="background: #f1f5f9; font-weight: 800;">
                    <td colspan="3" style="text-align: right; font-weight: 800;">الإجمالي الكلي</td>
                    <td class="text-left font-mono font-bold">{sale['sale_price']:,.2f}</td>
                    <td class="text-center">—</td>
                    <td class="text-center">—</td>
                    <td class="text-left font-mono font-bold text-success">{total_paid:,.2f}</td>
                    <td colspan="2" class="text-center text-xs text-danger font-mono">متبقي: {remaining:,.2f} ج.م</td>
                </tr>
            </tfoot>
        </table>

        <div class="signatures">
            <div class="sign-box">
                <div class="sign-title">إعداد ومراجعة الحسابات</div>
                <div class="sign-line"></div>
            </div>
            <div class="sign-box">
                <div class="sign-title">توقيع العميل بالموافقة</div>
                <div class="sign-line"></div>
            </div>
            <div class="sign-box">
                <div class="sign-title">اعتماد الإدارة العامة والختم</div>
                <div class="sign-line"></div>
            </div>
        </div>

        <div class="footer-note">
            تم استخراج هذا المستند إلكترونياً من نظام إدارة تحالف ميجا مول (Mega Mall Alliance Portal) • مستند محاسبي رسمي
        </div>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html_content)


@app.get("/api/installments/{installment_id}/receipt-pdf")
def generate_installment_receipt_pdf(
    installment_id: int,
    user: dict = Depends(get_current_user_flexible)
):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
        SELECT 
            i.*,
            s.client_name, s.client_phone, s.client_national_id, s.sale_price, s.down_payment, s.sale_date, s.id as sale_id,
            s.company_id,
            u.unit_code, u.floor, u.unit_type, u.area,
            c.name_ar as company_name, c.code as company_code
        FROM installments i
        JOIN sales s ON i.sale_id = s.id
        JOIN units u ON s.unit_id = u.id
        JOIN companies c ON s.company_id = c.id
        WHERE i.id = ?
    """, (installment_id,))
    inst = c.fetchone()
    if not inst:
        conn.close()
        raise HTTPException(status_code=404, detail="بيانات القسط غير موجودة")

    if user["role"] != "admin" and user.get("company_id") and inst["company_id"] != user["company_id"]:
        conn.close()
        raise HTTPException(status_code=403, detail="غير مصرح لك بطباعة هذا الإيصال")

    c.execute("SELECT SUM(paid_amount) as total_paid FROM installments WHERE sale_id = ?", (inst["sale_id"],))
    total_paid_row = c.fetchone()
    total_paid = total_paid_row["total_paid"] or 0.0
    remaining_balance = max(0.0, inst["sale_price"] - total_paid)
    conn.close()

    amount_val = inst["paid_amount"] if (inst["paid_amount"] and inst["paid_amount"] > 0) else inst["amount"]
    tafqeet_text = tafqeet_arabic(amount_val)
    receipt_no = inst["receipt_no"] if inst["receipt_no"] else f"REC-{inst['sale_id']:04d}-{inst['installment_no']}"
    payment_date = inst["payment_date"] if inst["payment_date"] else datetime.now().strftime("%Y-%m-%d")
    
    pay_method_ar = "نقدي (خزينة)" if inst["payment_method"] == "cash" else "تحويل بنكي / شيك" if inst["payment_method"] == "bank_transfer" else (inst["payment_method"] or "سداد معتمد")
    bank_ar = inst["bank_name"] if inst["bank_name"] else ("حساب الشركة البنكي" if inst["bank_deposited"] else "خزينة التحالف")
    inst_desc = "دفعة مقدمة عند التعاقد" if inst["installment_type"] == "down_payment" else f"القسط رقم ({inst['installment_no']}) المستحق بتاريخ {inst['due_date']}"

    html_content = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>إيصال سداد - {receipt_no} - {inst['client_name']}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800;900&family=Plus+Jakarta+Sans:wght@500;700;800&display=swap" rel="stylesheet">
    <style>
        @page {{ size: A4 portrait; margin: 12mm 15mm; }}
        * {{ box-sizing: border-box; -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }}
        body {{ font-family: 'Cairo', sans-serif; background: #f8fafc; color: #0f172a; margin: 0; padding: 24px; line-height: 1.5; font-size: 13px; }}
        .no-print {{ display: flex; justify-content: space-between; align-items: center; background: #1e293b; color: white; padding: 12px 20px; border-radius: 12px; margin-bottom: 24px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }}
        .btn {{ padding: 8px 18px; border-radius: 8px; font-weight: 700; font-family: 'Cairo'; cursor: pointer; border: none; font-size: 13px; display: inline-flex; align-items: center; gap: 8px; }}
        .btn-primary {{ background: #2563eb; color: white; }}
        .btn-secondary {{ background: #475569; color: white; }}
        .voucher {{ background: white; border-radius: 16px; padding: 32px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); border: 2px solid #0284c7; max-width: 800px; margin: 0 auto; position: relative; }}
        .voucher::before {{ content: ""; position: absolute; inset: 6px; border: 1px dashed #cbd5e1; border-radius: 12px; pointer-events: none; }}
        .header {{ display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 2px solid #0284c7; padding-bottom: 14px; margin-bottom: 20px; }}
        .brand-title {{ margin: 0; font-size: 22px; font-weight: 900; color: #0369a1; }}
        .brand-sub {{ margin: 2px 0 0 0; color: #64748b; font-size: 11.5px; font-weight: 600; }}
        .doc-badge {{ background: #e0f2fe; color: #0369a1; border: 1px solid #bae6fd; padding: 4px 12px; border-radius: 20px; font-weight: 800; font-size: 14px; display: inline-block; margin-top: 5px; }}
        .receipt-info {{ text-align: left; font-size: 12px; }}
        .receipt-info strong {{ color: #0f172a; }}
        .amount-box {{ background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%); border: 1.5px solid #86efac; border-radius: 12px; padding: 14px 20px; display: flex; justify-content: space-between; align-items: center; margin: 18px 0; }}
        .amount-num {{ font-family: 'Plus Jakarta Sans', sans-serif; font-size: 24px; font-weight: 900; color: #15803d; }}
        .amount-words {{ font-size: 13px; font-weight: 700; color: #166534; margin-top: 2px; }}
        .details-list {{ margin: 20px 0; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden; }}
        .detail-row {{ display: flex; border-bottom: 1px solid #f1f5f9; padding: 10px 16px; font-size: 13px; }}
        .detail-row:last-child {{ border-bottom: none; }}
        .detail-row:nth-child(odd) {{ background: #f8fafc; }}
        .detail-label {{ width: 170px; color: #64748b; font-weight: 700; shrink: 0; }}
        .detail-value {{ font-weight: 700; color: #0f172a; flex: 1; }}
        .unit-pill {{ background: #eff6ff; color: #1d4ed8; padding: 2px 8px; border-radius: 6px; font-family: 'Plus Jakarta Sans', monospace; }}
        .summary-band {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; padding: 12px; margin: 20px 0; text-align: center; }}
        .summary-item h5 {{ margin: 0 0 4px 0; font-size: 11px; color: #64748b; font-weight: 700; }}
        .summary-item p {{ margin: 0; font-family: 'Plus Jakarta Sans', sans-serif; font-size: 14px; font-weight: 800; color: #0f172a; }}
        .signatures {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-top: 40px; padding-top: 15px; border-top: 1px dashed #cbd5e1; }}
        .sign-box {{ text-align: center; }}
        .sign-title {{ font-size: 11.5px; font-weight: 700; color: #475569; margin-bottom: 45px; }}
        .sign-line {{ border-bottom: 1px solid #94a3b8; width: 85%; margin: 0 auto; }}
        .footer-note {{ margin-top: 24px; text-align: center; font-size: 10.5px; color: #94a3b8; border-top: 1px solid #f1f5f9; padding-top: 8px; }}
        @media print {{
            body {{ background: white; padding: 0; }}
            .voucher {{ border: 1.5px solid #0284c7; box-shadow: none; max-width: 100%; }}
            .no-print {{ display: none !important; }}
        }}
    </style>
</head>
<body>
    <div class="no-print">
        <div style="font-weight: 700; font-size: 14px;">
            إيصال استلام وسداد رسمي • {receipt_no}
        </div>
        <div style="display: flex; gap: 10px;">
            <button class="btn btn-primary" onclick="window.print()">
                <svg width="16" height="16" fill="currentColor" viewBox="0 0 24 24"><path d="M19 8H5c-1.66 0-3 1.34-3 3v6h4v4h12v-4h4v-6c0-1.66-1.34-3-3-3zm-3 11H8v-5h8v5zm3-7c-.55 0-1-.45-1-1s.45-1 1-1 1 .45 1 1-.45 1-1 1zm-1-9H6v4h12V3z"/></svg>
                طباعة الإيصال / حفظ PDF
            </button>
            <button class="btn btn-secondary" onclick="window.close()">إغلاق</button>
        </div>
    </div>

    <div class="voucher">
        <div class="header">
            <div>
                <h1 class="brand-title">ميجا مول • MEGA MALL ALLIANCE</h1>
                <p class="brand-sub">تحالف الشركات: كونكريت • ترست • MMD • TD</p>
                <div class="doc-badge">إيصال استلام نقدية وسداد قسط</div>
            </div>
            <div class="receipt-info">
                <div><strong>رقم الإيصال:</strong> <span style="font-family: monospace; font-size: 13px; color: #0284c7;">{receipt_no}</span></div>
                <div><strong>تاريخ السداد:</strong> {payment_date}</div>
                <div><strong>رقم العقد:</strong> #{inst['sale_id']:04d}</div>
            </div>
        </div>

        <div class="amount-box">
            <div>
                <div style="font-size: 11px; font-weight: 700; color: #166534;">المبلغ المستلم والمثبت:</div>
                <div class="amount-words">{tafqeet_text}</div>
            </div>
            <div class="amount-num">{amount_val:,.2f} ج.م</div>
        </div>

        <div class="details-list">
            <div class="detail-row">
                <div class="detail-label">وصلنا من السيد / السادة:</div>
                <div class="detail-value" style="font-size: 14px; color: #1e3a8a;">{inst['client_name']}</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">وذلك سداداً لـ:</div>
                <div class="detail-value">{inst_desc}</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">بيانات الوحدة:</div>
                <div class="detail-value">
                    <span class="unit-pill">{inst['unit_code']}</span>
                    <span>• الدور {inst['floor']} • المساحة: {inst['area'] or '—'} م²</span>
                    <span style="color: #64748b; margin-right: 8px;">({inst['company_name']})</span>
                </div>
            </div>
            <div class="detail-row">
                <div class="detail-label">طريقة الدفع:</div>
                <div class="detail-value">{pay_method_ar} • حساب: {bank_ar}</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">ملاحظات التحصيل:</div>
                <div class="detail-value" style="color: #64748b; font-weight: normal;">{inst['notes'] or 'تم السداد والتحصيل وفقاً لشروط العقد'}</div>
            </div>
        </div>

        <div class="summary-band">
            <div class="summary-item">
                <h5>سعر الوحدة الإجمالي</h5>
                <p>{inst['sale_price']:,.2f} ج.م</p>
            </div>
            <div class="summary-item">
                <h5>إجمالي المسدد حتى تاريخه</h5>
                <p style="color: #15803d;">{total_paid:,.2f} ج.م</p>
            </div>
            <div class="summary-item">
                <h5>الرصيد المتبقي على الوحدة</h5>
                <p style="color: #b91c1c;">{remaining_balance:,.2f} ج.م</p>
            </div>
        </div>

        <div class="signatures">
            <div class="sign-box">
                <div class="sign-title">المحصل / المستلم</div>
                <div class="sign-line"></div>
            </div>
            <div class="sign-box">
                <div class="sign-title">الحسابات والمراجعة</div>
                <div class="sign-line"></div>
            </div>
            <div class="sign-box">
                <div class="sign-title">اعتماد الإدارة والختم</div>
                <div class="sign-line"></div>
            </div>
        </div>

        <div class="footer-note">
            إيصال إلكتروني صادر ومعتمد من نظام إدارة تحالف ميجا مول • يُعتبر هذا الإيصال لاغياً في حال ارتداد الشيك أو التحويل البنكي
        </div>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html_content)


@app.get("/api/export/installments-pdf")
def export_installments_pdf(
    company_id: Optional[int] = None,
    status: Optional[str] = "all",
    period: Optional[str] = "all",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    months: Optional[str] = None,
    user: dict = Depends(get_current_user_flexible)
):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    if user["role"] != "admin" and user.get("company_id"):
        effective_company_id = user["company_id"]
    else:
        effective_company_id = company_id

    query = '''
        SELECT 
            u.unit_code, u.floor, s.client_name, c.name_ar as company_name,
            i.installment_no, i.installment_type, i.due_date, i.amount, i.status,
            i.paid_amount, i.payment_date, i.receipt_no, i.bank_deposited, i.bank_name
        FROM installments i
        JOIN sales s ON i.sale_id = s.id
        JOIN units u ON s.unit_id = u.id
        JOIN companies c ON s.company_id = c.id
        WHERE 1=1
    '''
    params = []
    if effective_company_id:
        query += " AND s.company_id = ?"
        params.append(effective_company_id)
    if status and status != "all":
        query += " AND i.status = ?"
        params.append(status)

    if start_date:
        query += " AND i.due_date >= ?"
        params.append(start_date)

    if end_date:
        query += " AND i.due_date <= ?"
        params.append(end_date)

    if year and months:
        m_list = [int(m.strip()) for m in months.split(",") if m.strip().isdigit()]
        if m_list:
            placeholders = ",".join(["?"] * len(m_list))
            query += f" AND substr(i.due_date, 1, 4) = ? AND cast(substr(i.due_date, 6, 2) as integer) IN ({placeholders})"
            params.append(str(year))
            params.extend(m_list)
        else:
            query += " AND substr(i.due_date, 1, 4) = ?"
            params.append(str(year))
    elif year and month:
        m_str = f"{int(month):02d}"
        query += " AND substr(i.due_date, 1, 7) = ?"
        params.append(f"{year}-{m_str}")
    elif year:
        query += " AND substr(i.due_date, 1, 4) = ?"
        params.append(str(year))
    elif month:
        m_str = f"{int(month):02d}"
        query += " AND substr(i.due_date, 6, 2) = ?"
        params.append(m_str)

    if not start_date and not end_date and not year and period and period != "all":
        if period == "this_month":
            query += " AND substr(i.due_date, 1, 7) = strftime('%Y-%m', 'now')"
        elif period == "next_month":
            query += " AND substr(i.due_date, 1, 7) = strftime('%Y-%m', date('now', '+1 month'))"
        elif period == "this_year":
            query += " AND substr(i.due_date, 1, 4) = strftime('%Y', 'now')"
        elif period == "next_year":
            query += " AND substr(i.due_date, 1, 4) = cast(cast(strftime('%Y', 'now') as integer) + 1 as text)"
        elif period == "year_2028":
            query += " AND substr(i.due_date, 1, 4) = '2028'"
        elif period == "future":
            query += " AND substr(i.due_date, 1, 4) >= '2029'"
        
    query += " ORDER BY i.due_date ASC"
    c.execute(query, params)
    rows = c.fetchall()

    company_filter_name = "مشروع المول ككل"
    if effective_company_id:
        c.execute("SELECT name_ar FROM companies WHERE id = ?", (effective_company_id,))
        comp_row = c.fetchone()
        if comp_row:
            company_filter_name = f"شركة {comp_row['name_ar']}"
    conn.close()

    total_amount = sum(r['amount'] or 0.0 for r in rows)
    total_paid = sum(r['paid_amount'] or 0.0 for r in rows)
    total_overdue = sum(r['amount'] or 0.0 for r in rows if r['status'] == 'overdue')
    total_pending = sum(r['amount'] or 0.0 for r in rows if r['status'] == 'pending')
    total_count = len(rows)

    filter_desc_parts = [f"النطاق: {company_filter_name}"]
    if year:
        filter_desc_parts.append(f"سنة: {year}")
    if status and status != 'all':
        st_label = "المسددة فقط" if status == 'paid' else "المتأخرة فقط" if status == 'overdue' else "المتبقية فقط"
        filter_desc_parts.append(f"الحالة: {st_label}")
    filter_desc = " • ".join(filter_desc_parts)

    tbody_rows = ""
    for idx, r in enumerate(rows, 1):
        if r['status'] == 'paid':
            status_badge = '<span class="badge badge-paid">مسدد</span>'
        elif r['status'] == 'overdue':
            status_badge = '<span class="badge badge-overdue">متأخر</span>'
        else:
            status_badge = '<span class="badge badge-pending">متبقي</span>'

        type_lbl = "مقدم" if r['installment_type'] == 'down_payment' else f"قسط #{r['installment_no']}"
        bank_lbl = "نعم" if r['bank_deposited'] else "لا"

        tbody_rows += f"""
        <tr>
            <td class="text-center font-mono">{idx}</td>
            <td class="font-bold font-mono text-center">{r['unit_code']}</td>
            <td class="text-center">{r['floor']}</td>
            <td>{r['client_name']}</td>
            <td>{r['company_name']}</td>
            <td class="text-center">{type_lbl}</td>
            <td class="text-center font-mono">{r['due_date']}</td>
            <td class="text-left font-mono font-bold">{r['amount']:,.2f}</td>
            <td class="text-center">{status_badge}</td>
            <td class="text-left font-mono font-bold text-success">{(r['paid_amount'] or 0):,.2f}</td>
            <td class="text-center font-mono text-xs">{r['payment_date'] or '—'}</td>
            <td class="text-center font-mono text-xs">{r['receipt_no'] or '—'}</td>
            <td class="text-center text-xs">{bank_lbl}</td>
        </tr>
        """

    today_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    html_content = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>تقرير الأقساط والتحصيلات - ميجا مول</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800;900&family=Plus+Jakarta+Sans:wght@500;700;800&display=swap" rel="stylesheet">
    <style>
        @page {{ size: A4 landscape; margin: 8mm 10mm; }}
        * {{ box-sizing: border-box; -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }}
        body {{ font-family: 'Cairo', sans-serif; background: #f8fafc; color: #0f172a; margin: 0; padding: 20px; line-height: 1.4; font-size: 11.5px; }}
        .no-print {{ display: flex; justify-content: space-between; align-items: center; background: #1e293b; color: white; padding: 10px 18px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }}
        .btn {{ padding: 7px 16px; border-radius: 8px; font-weight: 700; font-family: 'Cairo'; cursor: pointer; border: none; font-size: 12px; display: inline-flex; align-items: center; gap: 6px; }}
        .btn-primary {{ background: #2563eb; color: white; }}
        .btn-secondary {{ background: #475569; color: white; }}
        .report-wrapper {{ background: white; border-radius: 14px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; }}
        .header {{ display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 2px solid #2563eb; padding-bottom: 12px; margin-bottom: 14px; }}
        .brand-title {{ margin: 0; font-size: 20px; font-weight: 900; color: #1e3a8a; }}
        .brand-sub {{ margin: 2px 0 0 0; color: #64748b; font-size: 11px; }}
        .filter-bar {{ background: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 8px; padding: 8px 14px; margin-bottom: 14px; display: flex; justify-content: space-between; font-weight: 700; font-size: 11.5px; }}
        .kpi-row {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin-bottom: 16px; }}
        .kpi-card {{ background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 8px 12px; text-align: center; }}
        .kpi-title {{ font-size: 10px; color: #64748b; font-weight: 700; margin-bottom: 2px; }}
        .kpi-val {{ font-family: 'Plus Jakarta Sans', sans-serif; font-size: 14px; font-weight: 800; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 11px; }}
        th {{ background: #f1f5f9; color: #334155; font-weight: 800; padding: 8px 6px; border: 1px solid #cbd5e1; text-align: right; }}
        td {{ padding: 6px 6px; border: 1px solid #e2e8f0; }}
        tr:nth-child(even) {{ background: #f8fafc; }}
        .text-center {{ text-align: center; }}
        .text-left {{ text-align: left; }}
        .font-mono {{ font-family: 'Plus Jakarta Sans', monospace; }}
        .font-bold {{ font-weight: 700; }}
        .text-success {{ color: #059669 !important; }}
        .text-danger {{ color: #dc2626 !important; }}
        .badge {{ display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 10px; font-weight: 700; }}
        .badge-paid {{ background: #dcfce7; color: #15803d; }}
        .badge-overdue {{ background: #ffe4e6; color: #b91c1c; }}
        .badge-pending {{ background: #fef3c7; color: #b45309; }}
        .signatures {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-top: 30px; padding-top: 15px; border-top: 1px dashed #cbd5e1; }}
        .sign-box {{ text-align: center; }}
        .sign-title {{ font-size: 11px; font-weight: 700; color: #475569; margin-bottom: 35px; }}
        .sign-line {{ border-bottom: 1px solid #94a3b8; width: 80%; margin: 0 auto; }}
        @media print {{
            body {{ background: white; padding: 0; }}
            .report-wrapper {{ border: none; box-shadow: none; padding: 0; }}
            .no-print {{ display: none !important; }}
        }}
    </style>
</head>
<body>
    <div class="no-print">
        <div style="font-weight: 700; font-size: 13px;">
            تقرير جدول الأقساط والتحصيلات المعتمدة • ميجا مول
        </div>
        <div style="display: flex; gap: 8px;">
            <button class="btn btn-primary" onclick="window.print()">
                <svg width="15" height="15" fill="currentColor" viewBox="0 0 24 24"><path d="M19 8H5c-1.66 0-3 1.34-3 3v6h4v4h12v-4h4v-6c0-1.66-1.34-3-3-3zm-3 11H8v-5h8v5zm3-7c-.55 0-1-.45-1-1s.45-1 1-1 1 .45 1 1-.45 1-1 1zm-1-9H6v4h12V3z"/></svg>
                طباعة التقرير / حفظ PDF
            </button>
            <button class="btn btn-secondary" onclick="window.close()">إغلاق</button>
        </div>
    </div>

    <div class="report-wrapper">
        <div class="header">
            <div>
                <h1 class="brand-title">ميجا مول • MEGA MALL ALLIANCE</h1>
                <p class="brand-sub">تقرير الأقساط والتحصيلات المعتمدة (تحالف: كونكريت • ترست • MMD • TD)</p>
            </div>
            <div style="text-align: left; font-size: 11px; color: #64748b;">
                <div><strong>تاريخ الاستخراج:</strong> <span class="font-mono">{today_str}</span></div>
                <div><strong>المستخدم:</strong> {user.get('username')}</div>
            </div>
        </div>

        <div class="filter-bar">
            <div>{filter_desc}</div>
            <div>إجمالي النتائج: <span class="font-mono font-bold" style="color: #2563eb;">{total_count}</span> قسط</div>
        </div>

        <div class="kpi-row">
            <div class="kpi-card">
                <div class="kpi-title">عدد الأقساط</div>
                <div class="kpi-val font-mono">{total_count}</div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">إجمالي المبالغ المجدولة</div>
                <div class="kpi-val font-mono" style="color: #0f172a;">{total_amount:,.2f} ج.م</div>
            </div>
            <div class="kpi-card" style="background: #f0fdf4; border-color: #bbf7d0;">
                <div class="kpi-title" style="color: #166534;">إجمالي المحصل</div>
                <div class="kpi-val font-mono text-success">{total_paid:,.2f} ج.م</div>
            </div>
            <div class="kpi-card" style="background: #fff1f2; border-color: #fecdd3;">
                <div class="kpi-title" style="color: #9f1239;">إجمالي المتأخرات</div>
                <div class="kpi-val font-mono text-danger">{total_overdue:,.2f} ج.م</div>
            </div>
            <div class="kpi-card" style="background: #fefce8; border-color: #fef08a;">
                <div class="kpi-title" style="color: #854d0e;">المستحق القادم</div>
                <div class="kpi-val font-mono" style="color: #b45309;">{total_pending:,.2f} ج.م</div>
            </div>
        </div>

        <table>
            <thead>
                <tr>
                    <th style="width: 25px;" class="text-center">#</th>
                    <th class="text-center">الوحدة</th>
                    <th class="text-center">الدور</th>
                    <th>العميل</th>
                    <th>الشركة</th>
                    <th class="text-center">النوع</th>
                    <th class="text-center">الاستحقاق</th>
                    <th class="text-left">المبلغ</th>
                    <th class="text-center">الحالة</th>
                    <th class="text-left">المسدد</th>
                    <th class="text-center">تاريخ السداد</th>
                    <th class="text-center">الإيصال</th>
                    <th class="text-center">البنك</th>
                </tr>
            </thead>
            <tbody>
                {tbody_rows}
            </tbody>
            <tfoot>
                <tr style="background: #e2e8f0; font-weight: 800;">
                    <td colspan="7" style="text-align: right;">الإجماليات الكلية</td>
                    <td class="text-left font-mono">{total_amount:,.2f}</td>
                    <td class="text-center">—</td>
                    <td class="text-left font-mono text-success">{total_paid:,.2f}</td>
                    <td colspan="3" class="text-center text-xs">متأخر: {total_overdue:,.2f} | متبقي: {total_pending:,.2f}</td>
                </tr>
            </tfoot>
        </table>

        <div class="signatures">
            <div class="sign-box">
                <div class="sign-title">إعداد المراجع المالي</div>
                <div class="sign-line"></div>
            </div>
            <div class="sign-box">
                <div class="sign-title">مراجعة إدارة الحسابات</div>
                <div class="sign-line"></div>
            </div>
            <div class="sign-box">
                <div class="sign-title">اعتماد المدير العام للتحالف</div>
                <div class="sign-line"></div>
            </div>
        </div>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html_content)



# --- EXPORT EXCEL ---
@app.get("/api/export/installments")
def export_installments_excel(
    company_id: Optional[int] = None,
    status: Optional[str] = "all",
    period: Optional[str] = "all",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    months: Optional[str] = None,
    user: dict = Depends(get_current_user_flexible)
):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    if user["role"] != "admin" and user.get("company_id"):
        effective_company_id = user["company_id"]
    else:
        effective_company_id = company_id

    query = '''
        SELECT 
            u.unit_code, u.floor, s.client_name, c.name_ar as company_name,
            i.installment_no, i.installment_type, i.due_date, i.amount, i.status,
            i.paid_amount, i.payment_date, i.receipt_no, i.bank_deposited, i.bank_name
        FROM installments i
        JOIN sales s ON i.sale_id = s.id
        JOIN units u ON s.unit_id = u.id
        JOIN companies c ON s.company_id = c.id
        WHERE 1=1
    '''
    params = []
    if effective_company_id:
        query += " AND s.company_id = ?"
        params.append(effective_company_id)
    if status and status != "all":
        query += " AND i.status = ?"
        params.append(status)

    if start_date:
        query += " AND i.due_date >= ?"
        params.append(start_date)

    if end_date:
        query += " AND i.due_date <= ?"
        params.append(end_date)

    if year and months:
        m_list = [int(m.strip()) for m in months.split(",") if m.strip().isdigit()]
        if m_list:
            placeholders = ",".join(["?"] * len(m_list))
            query += f" AND substr(i.due_date, 1, 4) = ? AND cast(substr(i.due_date, 6, 2) as integer) IN ({placeholders})"
            params.append(str(year))
            params.extend(m_list)
        else:
            query += " AND substr(i.due_date, 1, 4) = ?"
            params.append(str(year))
    elif year and month:
        m_str = f"{int(month):02d}"
        query += " AND substr(i.due_date, 1, 7) = ?"
        params.append(f"{year}-{m_str}")
    elif year:
        query += " AND substr(i.due_date, 1, 4) = ?"
        params.append(str(year))
    elif month:
        m_str = f"{int(month):02d}"
        query += " AND substr(i.due_date, 6, 2) = ?"
        params.append(m_str)

    if not start_date and not end_date and not year and period and period != "all":
        if period == "this_month":
            query += " AND substr(i.due_date, 1, 7) = strftime('%Y-%m', 'now')"
        elif period == "next_month":
            query += " AND substr(i.due_date, 1, 7) = strftime('%Y-%m', date('now', '+1 month'))"
        elif period == "this_year":
            query += " AND substr(i.due_date, 1, 4) = strftime('%Y', 'now')"
        elif period == "next_year":
            query += " AND substr(i.due_date, 1, 4) = cast(cast(strftime('%Y', 'now') as integer) + 1 as text)"
        elif period == "year_2028":
            query += " AND substr(i.due_date, 1, 4) = '2028'"
        elif period == "future":
            query += " AND substr(i.due_date, 1, 4) >= '2029'"
        
    query += " ORDER BY i.due_date ASC"
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Installments"
    
    headers = ["Unit Code", "Floor", "Client Name", "Company Name", "Installment No", "Type", "Due Date", "Amount", "Status", "Paid Amount", "Payment Date", "Receipt No", "Bank Deposited", "Bank Name"]
    ws.append(headers)

    for r in rows:
        bank_status_str = "Yes" if r['bank_deposited'] else "No"
        ws.append([
            r['unit_code'], r['floor'], r['client_name'], r['company_name'],
            r['installment_no'], r['installment_type'], r['due_date'], r['amount'],
            r['status'], r['paid_amount'], r['payment_date'], r['receipt_no'],
            bank_status_str, r['bank_name'] or ''
        ])
        
    stream = io.BytesIO()
    wb.save(stream)
    stream.seek(0)
    
    return StreamingResponse(
        iter([stream.getvalue()]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=installments_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"}
    )

# --- CHANGE PASSWORD ---
@app.post("/api/change-password")
def change_password(req: PasswordChangeRequest, user: dict = Depends(get_current_user)):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT password_hash FROM users WHERE id = ?", (user["user_id"],))
    db_user = c.fetchone()
    
    if not db_user or db_user["password_hash"] != hash_pw(req.current_password):
        conn.close()
        raise HTTPException(status_code=400, detail="كلمة المرور الحالية غير صحيحة")
        
    c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_pw(req.new_password), user["user_id"]))
    c.execute("""
        INSERT INTO activity_logs (username, action, details)
        VALUES (?, 'CHANGE_PASSWORD', ?)
    """, (user["username"], f"تغيير كلمة المرور للمستخدم {user['username']}"))
    conn.commit()
    conn.close()
    return {"success": True, "message": "تم تغيير كلمة المرور بنجاح"}

# --- BACKUP DATABASE ---
@app.post("/api/backup")
def backup_database(user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="غير مصرح لك بعمل نسخة احتياطية")
        
    backup_dir = os.path.join(BASE_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"mega_mall_backup_{timestamp}.db"
    backup_path = os.path.join(backup_dir, backup_filename)
    
    shutil.copy2(DB_PATH, backup_path)
    
    # Log backup action
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO activity_logs (username, action, details)
        VALUES (?, 'CREATE_BACKUP', ?)
    """, (user["username"], f"إنشاء نسخة احتياطية: {backup_filename}"))
    conn.commit()
    conn.close()
    
    return {"success": True, "filename": backup_filename, "message": "تم إنشاء النسخة الاحتياطية بنجاح"}

# --- RESTORE DATABASE ---
@app.post("/api/restore")
async def restore_database(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="غير مصرح لك باسترجاع قاعدة البيانات (متاح للمدير فقط)")
    
    # 1. Validate file extension
    filename = file.filename or ""
    if not (filename.lower().endswith(".db") or filename.lower().endswith(".sqlite") or filename.lower().endswith(".sqlite3")):
        raise HTTPException(status_code=400, detail="نوع الملف غير مدعوم. يرجى رفع ملف قاعدة بيانات بصيغة .db أو .sqlite")
    
    # 2. Save to a temporary file
    backup_dir = os.path.join(BASE_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    unique_suffix = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    temp_filename = f"temp_restore_{unique_suffix}.db"
    temp_path = os.path.join(backup_dir, temp_filename)
    
    MAX_RESTORE_SIZE = 100 * 1024 * 1024  # 100 MB limit
    size = 0
    try:
        with open(temp_path, "wb") as f_out:
            while chunk := await file.read(1024 * 1024):  # 1MB chunks
                size += len(chunk)
                if size > MAX_RESTORE_SIZE:
                    raise HTTPException(status_code=400, detail="حجم الملف كبير جداً (الحد الأقصى 100 ميجابايت)")
                f_out.write(chunk)
    except HTTPException:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise
    except Exception as e:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=f"فشل في استلام الملف: {str(e)}")

    # 3. Validate SQLite integrity & essential schema
    test_conn = None
    try:
        try:
            test_conn = sqlite3.connect(temp_path)
            test_cur = test_conn.cursor()
            
            # Check integrity
            test_cur.execute("PRAGMA integrity_check;")
            res = test_cur.fetchone()
            if not res or str(res[0]).lower() != "ok":
                raise HTTPException(status_code=400, detail="الملف المرفوع تالف أو ليس قاعدة بيانات SQLite صالحة")
            
            # Check required tables
            test_cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = {row[0] for row in test_cur.fetchall()}
            required_tables = {"companies", "users", "units", "sales", "installments"}
            missing_tables = required_tables - tables
            if missing_tables:
                raise HTTPException(status_code=400, detail=f"قاعدة البيانات المرفوعة تفتقد جداول رئيسية لنظام ميجا مول: {', '.join(missing_tables)}")
            
            # Check admin user existence
            test_cur.execute("SELECT COUNT(*) FROM users WHERE role = 'admin';")
            admin_count = test_cur.fetchone()[0]
            if admin_count < 1:
                raise HTTPException(status_code=400, detail="قاعدة البيانات المرفوعة لا تحتوي على أي حساب مدير (Admin)")
        finally:
            if test_conn:
                try:
                    test_conn.close()
                except Exception:
                    pass
    except HTTPException:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise
    except Exception as e:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise HTTPException(status_code=400, detail=f"فشل فحص بنية الملف: {str(e)}")

    # 4. Create an automated pre-restore safety backup of active database
    pre_restore_filename = f"pre_restore_backup_{unique_suffix}.db"
    pre_restore_path = os.path.join(backup_dir, pre_restore_filename)
    try:
        if os.path.exists(DB_PATH):
            shutil.copy2(DB_PATH, pre_restore_path)
    except Exception as e:
        print(f"Warning: Could not create pre-restore backup: {e}")

    # 5. Perform the restoration using SQLite online backup API
    try:
        src = sqlite3.connect(temp_path)
        dst = sqlite3.connect(DB_PATH)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
    except Exception as e:
        try:
            shutil.copy2(temp_path, DB_PATH)
        except Exception as copy_err:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise HTTPException(status_code=500, detail=f"حدث خطأ أثناء استبدال قاعدة البيانات: {str(copy_err)}")
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    # 6. Run migrations to ensure any schema updates match current code
    ensure_db_migrations()

    # 7. Log restore action in activity logs
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""
            INSERT INTO activity_logs (username, action, details)
            VALUES (?, 'RESTORE_BACKUP', ?)
        """, (user["username"], f"تم استرجاع قاعدة البيانات من الملف: {filename} (تم حفظ نسخة احترازية: {pre_restore_filename})"))
        conn.commit()
        conn.close()
    except Exception as log_err:
        print(f"Could not log restore action: {log_err}")

    return {
        "success": True, 
        "message": "تم استرجاع قاعدة البيانات بنجاح!", 
        "restored_file": filename,
        "safety_backup": pre_restore_filename
    }


# --- EMAIL SETTINGS & NOTIFICATIONS ---
EMAIL_CONFIG_PATH = os.path.join(BASE_DIR, "email_config.json")

@app.post("/api/settings/email")
def save_email_settings(req: EmailSettingsRequest, user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="غير مصرح")
    with open(EMAIL_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(req.model_dump(), f, ensure_ascii=False, indent=4)
    return {"success": True, "message": "تم حفظ إعدادات البريد بنجاح"}

@app.post("/api/notifications/send-overdue")
def send_overdue_notifications(user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="غير مصرح")
        
    config = {}
    if os.environ.get("SMTP_SERVER"):
        config = {
            "smtp_server": os.environ.get("SMTP_SERVER"),
            "smtp_port": int(os.environ.get("SMTP_PORT", 587)),
            "smtp_username": os.environ.get("SMTP_USERNAME", ""),
            "smtp_password": os.environ.get("SMTP_PASSWORD", ""),
            "sender_email": os.environ.get("SMTP_SENDER_EMAIL", os.environ.get("SMTP_USERNAME", "")),
            "sender_name": os.environ.get("SMTP_SENDER_NAME", "إدارة ميجا مول المركزية")
        }
    elif os.path.exists(EMAIL_CONFIG_PATH):
        with open(EMAIL_CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
    else:
        raise HTTPException(status_code=400, detail="إعدادات البريد غير موجودة")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute('''
        SELECT s.client_name, s.client_phone, u.unit_code, i.amount, i.due_date 
        FROM installments i
        JOIN sales s ON i.sale_id = s.id
        JOIN units u ON s.unit_id = u.id
        WHERE i.status = 'overdue'
        ORDER BY i.due_date ASC
    ''')
    overdue_records = c.fetchall()
    conn.close()
    
    if not overdue_records:
        return {"success": True, "message": "لا توجد أقساط متأخرة حالياً لإرسال تنبيهات بشأنها"}

    try:
        server = smtplib.SMTP(config["smtp_server"], int(config["smtp_port"]))
        server.starttls()
        server.login(config["smtp_username"], config["smtp_password"])
        
        msg = MIMEMultipart()
        msg['From'] = f"{config['sender_name']} <{config['sender_email']}>"
        msg['To'] = config['sender_email']
        msg['Subject'] = f"تقرير الأقساط المتأخرة ({len(overdue_records)} قسط متأخر) - ميجا مول"
        
        lines = [
            f"إدارة ميجا مول المركزية،",
            f"فيما يلي كشف الأقساط المتأخرة حتى تاريخ اليوم:\n"
        ]
        total_overdue = 0
        for idx, r in enumerate(overdue_records, 1):
            total_overdue += r['amount']
            lines.append(f"{idx}. العميل: {r['client_name']} | الوحدة: {r['unit_code']} | المبلغ: {r['amount']:,.2f} ج.م | تاريخ الاستحقاق: {r['due_date']} | الهاتف: {r['client_phone'] or '-'}")
            
        lines.append(f"\nإجمالي المبالغ المتأخرة: {total_overdue:,.2f} ج.م")
        lines.append("\nتم إنشاء هذا التقرير تلقائياً من نظام إدارة ميجا مول.")
        
        body = "\n".join(lines)
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        
        server.send_message(msg)
        server.quit()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"فشل إرسال البريد: {str(e)}")
        
    return {"success": True, "message": f"تم إرسال تقرير الأقساط المتأخرة ({len(overdue_records)} قسط) إلى بريد الإدارة بنجاح"}

# --- ACTIVITY LOGS (Admin Audit Trail) ---

@app.get("/api/activity-logs")
def get_activity_logs(
    page: int = 1,
    limit: int = 50,
    action_filter: Optional[str] = None,
    username_filter: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    user: dict = Depends(get_current_user)
):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="سجل النشاطات متاح فقط للمدير العام")
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    where_clauses = []
    params = []
    
    if action_filter and action_filter != "all":
        where_clauses.append("action = ?")
        params.append(action_filter)
    if username_filter and username_filter != "all":
        where_clauses.append("username = ?")
        params.append(username_filter)
    if date_from:
        where_clauses.append("date(timestamp) >= ?")
        params.append(date_from)
    if date_to:
        where_clauses.append("date(timestamp) <= ?")
        params.append(date_to)
    
    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    
    # Get total count
    c.execute(f"SELECT COUNT(*) FROM activity_logs {where_sql}", params)
    total = c.fetchone()[0]
    
    # Get paginated results
    offset = (page - 1) * limit
    c.execute(f"""
        SELECT * FROM activity_logs
        {where_sql}
        ORDER BY timestamp DESC
        LIMIT ? OFFSET ?
    """, params + [limit, offset])
    logs = [dict(r) for r in c.fetchall()]
    
    # Get distinct action types for filter dropdown
    c.execute("SELECT DISTINCT action FROM activity_logs ORDER BY action")
    action_types = [r[0] for r in c.fetchall()]
    
    # Get distinct usernames for filter dropdown
    c.execute("SELECT DISTINCT username FROM activity_logs ORDER BY username")
    usernames = [r[0] for r in c.fetchall()]
    
    conn.close()
    
    return {
        "logs": logs,
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": max(1, (total + limit - 1) // limit),
        "action_types": action_types,
        "usernames": usernames
    }


@app.get("/api/export/activity-logs-pdf")
def export_activity_logs_pdf(
    action_filter: Optional[str] = None,
    username_filter: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    user: dict = Depends(get_current_user_flexible)
):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="سجل النشاطات متاح فقط للمدير العام")
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    where_clauses = []
    params = []
    
    if action_filter and action_filter != "all":
        where_clauses.append("action = ?")
        params.append(action_filter)
    if username_filter and username_filter != "all":
        where_clauses.append("username = ?")
        params.append(username_filter)
    if date_from:
        where_clauses.append("date(timestamp) >= ?")
        params.append(date_from)
    if date_to:
        where_clauses.append("date(timestamp) <= ?")
        params.append(date_to)
    
    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    
    c.execute(f"""
        SELECT * FROM activity_logs
        {where_sql}
        ORDER BY timestamp DESC
    """, params)
    logs = [dict(r) for r in c.fetchall()]
    conn.close()

    total_count = len(logs)
    count_sales = sum(1 for l in logs if l['action'] in ('CREATE_SALE', 'UPDATE_SALE'))
    count_pays = sum(1 for l in logs if l['action'] == 'PAY_INSTALLMENT')
    count_banks = sum(1 for l in logs if l['action'] == 'BANK_DEPOSIT')
    count_auth = sum(1 for l in logs if 'LOGIN' in l['action'] or l['action'] == 'CHANGE_PASSWORD')
    count_other = total_count - (count_sales + count_pays + count_banks + count_auth)

    action_meta = {
        'CREATE_SALE': {'label': 'تسجيل بيع جديد', 'color': '#1d4ed8', 'bg': '#eff6ff', 'border': '#bfdbfe'},
        'UPDATE_SALE': {'label': 'تعديل عقد بيع', 'color': '#b45309', 'bg': '#fffbeb', 'border': '#fde68a'},
        'PAY_INSTALLMENT': {'label': 'تحصيل قسط', 'color': '#047857', 'bg': '#ecfdf5', 'border': '#a7f3d0'},
        'BANK_DEPOSIT': {'label': 'إيداع بنكي', 'color': '#0f766e', 'bg': '#f0fdfa', 'border': '#99f6e4'},
        'DELETE_SALE': {'label': 'حذف عقد بيع', 'color': '#be123c', 'bg': '#fff1f2', 'border': '#fecdd3'},
        'LOGIN_SUCCESS': {'label': 'تسجيل دخول ناجح', 'color': '#15803d', 'bg': '#f0fdf4', 'border': '#bbf7d0'},
        'LOGIN_FAILED': {'label': 'تسجيل دخول فاشل', 'color': '#b91c1c', 'bg': '#fef2f2', 'border': '#fecaca'},
        'CHANGE_PASSWORD': {'label': 'تغيير كلمة المرور', 'color': '#6d28d9', 'bg': '#f5f3ff', 'border': '#ddd6fe'},
        'CREATE_BACKUP': {'label': 'نسخ احتياطي', 'color': '#4338ca', 'bg': '#eef2ff', 'border': '#c7d2fe'}
    }

    # Filter description text
    filter_parts = []
    if action_filter and action_filter != 'all':
        lbl = action_meta.get(action_filter, {}).get('label', action_filter)
        filter_parts.append(f"نوع الحركة: {lbl}")
    else:
        filter_parts.append("نوع الحركة: كل الحركات")

    if username_filter and username_filter != 'all':
        filter_parts.append(f"المستخدم: {username_filter}")
    else:
        filter_parts.append("المستخدم: كل المستخدمين")

    if date_from and date_to:
        filter_parts.append(f"الفترة: من {date_from} إلى {date_to}")
    elif date_from:
        filter_parts.append(f"من تاريخ: {date_from}")
    elif date_to:
        filter_parts.append(f"إلى تاريخ: {date_to}")
    
    filter_desc = " • ".join(filter_parts)
    today_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    tbody_rows = ""
    for idx, l in enumerate(logs, 1):
        meta = action_meta.get(l['action'], {'label': l['action'], 'color': '#475569', 'bg': '#f1f5f9', 'border': '#cbd5e1'})
        time_str = l['timestamp'] or '—'
        tbody_rows += f"""
        <tr>
            <td class="text-center font-mono">{idx}</td>
            <td class="text-center">
                <span class="badge" style="background: {meta['bg']}; color: {meta['color']}; border: 1px solid {meta['border']};">
                    {meta['label']}
                </span>
            </td>
            <td class="text-center font-bold font-mono text-slate-800">{l['username']}</td>
            <td style="font-weight: 500; color: #1e293b; line-height: 1.45;">{l['details'] or '—'}</td>
            <td class="text-center font-mono text-xs" dir="ltr">{time_str}</td>
        </tr>
        """

    html_content = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>تقرير سجل نشاطات وعمليات النظام - ميجا مول</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@400;600;700;800;900&family=Plus+Jakarta+Sans:wght@500;700;800&display=swap" rel="stylesheet">
    <style>
        @page {{ size: A4 landscape; margin: 8mm 10mm; }}
        * {{ box-sizing: border-box; -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }}
        body {{ font-family: 'Cairo', sans-serif; background: #f8fafc; color: #0f172a; margin: 0; padding: 20px; line-height: 1.4; font-size: 11.5px; }}
        .no-print {{ display: flex; justify-content: space-between; align-items: center; background: #1e293b; color: white; padding: 10px 18px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }}
        .btn {{ padding: 7px 16px; border-radius: 8px; font-weight: 700; font-family: 'Cairo'; cursor: pointer; border: none; font-size: 12px; display: inline-flex; align-items: center; gap: 6px; }}
        .btn-primary {{ background: #2563eb; color: white; }}
        .btn-secondary {{ background: #475569; color: white; }}
        .report-wrapper {{ background: white; border-radius: 14px; padding: 22px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; }}
        .header {{ display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 2px solid #4f46e5; padding-bottom: 12px; margin-bottom: 14px; }}
        .brand-title {{ margin: 0; font-size: 20px; font-weight: 900; color: #3730a3; }}
        .brand-sub {{ margin: 2px 0 0 0; color: #64748b; font-size: 11px; }}
        .filter-bar {{ background: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 8px; padding: 8px 14px; margin-bottom: 14px; display: flex; justify-content: space-between; font-weight: 700; font-size: 11.5px; }}
        .kpi-row {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin-bottom: 16px; }}
        .kpi-card {{ background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 8px 12px; text-align: center; }}
        .kpi-title {{ font-size: 10px; color: #64748b; font-weight: 700; margin-bottom: 2px; }}
        .kpi-val {{ font-family: 'Plus Jakarta Sans', sans-serif; font-size: 14px; font-weight: 800; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 11.5px; }}
        th {{ background: #f1f5f9; color: #334155; font-weight: 800; padding: 8px 8px; border: 1px solid #cbd5e1; text-align: right; }}
        td {{ padding: 7px 8px; border: 1px solid #e2e8f0; }}
        tr:nth-child(even) {{ background: #f8fafc; }}
        .text-center {{ text-align: center; }}
        .font-mono {{ font-family: 'Plus Jakarta Sans', monospace; }}
        .font-bold {{ font-weight: 700; }}
        .badge {{ display: inline-block; padding: 2px 8px; border-radius: 6px; font-size: 10px; font-weight: 700; white-space: nowrap; }}
        .signatures {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-top: 30px; padding-top: 15px; border-top: 1px dashed #cbd5e1; }}
        .sign-box {{ text-align: center; }}
        .sign-title {{ font-size: 11px; font-weight: 700; color: #475569; margin-bottom: 35px; }}
        .sign-line {{ border-bottom: 1px solid #94a3b8; width: 80%; margin: 0 auto; }}
        @media print {{
            body {{ background: white; padding: 0; }}
            .report-wrapper {{ border: none; box-shadow: none; padding: 0; }}
            .no-print {{ display: none !important; }}
        }}
    </style>
</head>
<body>
    <div class="no-print">
        <div style="font-weight: 700; font-size: 13px;">
            تقرير سجل نشاطات وعمليات النظام • ميجا مول (Admin Audit Trail)
        </div>
        <div style="display: flex; gap: 8px;">
            <button class="btn btn-primary" onclick="window.print()">
                <svg width="15" height="15" fill="currentColor" viewBox="0 0 24 24"><path d="M19 8H5c-1.66 0-3 1.34-3 3v6h4v4h12v-4h4v-6c0-1.66-1.34-3-3-3zm-3 11H8v-5h8v5zm3-7c-.55 0-1-.45-1-1s.45-1 1-1 1 .45 1 1-.45 1-1 1zm-1-9H6v4h12V3z"/></svg>
                طباعة التقرير / حفظ PDF
            </button>
            <button class="btn btn-secondary" onclick="window.close()">إغلاق</button>
        </div>
    </div>

    <div class="report-wrapper">
        <div class="header">
            <div>
                <h1 class="brand-title">ميجا مول • MEGA MALL ALLIANCE</h1>
                <p class="brand-sub">تقرير التدقيق والنشاطات الأمنية والتشغيلية (Audit Trail Report)</p>
            </div>
            <div style="text-align: left; font-size: 11px; color: #64748b;">
                <div><strong>تاريخ الاستخراج:</strong> <span class="font-mono">{today_str}</span></div>
                <div><strong>المسؤول:</strong> {user.get('username')} (المدير العام)</div>
            </div>
        </div>

        <div class="filter-bar">
            <div>{filter_desc}</div>
            <div>إجمالي العمليات المسجلة: <span class="font-mono font-bold" style="color: #4f46e5;">{total_count}</span> عملية</div>
        </div>

        <div class="kpi-row">
            <div class="kpi-card" style="background: #eef2ff; border-color: #c7d2fe;">
                <div class="kpi-title" style="color: #4338ca;">إجمالي العمليات</div>
                <div class="kpi-val font-mono" style="color: #3730a3;">{total_count}</div>
            </div>
            <div class="kpi-card" style="background: #eff6ff; border-color: #bfdbfe;">
                <div class="kpi-title" style="color: #1d4ed8;">عقود البيع</div>
                <div class="kpi-val font-mono" style="color: #1e40af;">{count_sales}</div>
            </div>
            <div class="kpi-card" style="background: #f0fdf4; border-color: #bbf7d0;">
                <div class="kpi-title" style="color: #166534;">تحصيلات الأقساط</div>
                <div class="kpi-val font-mono" style="color: #15803d;">{count_pays}</div>
            </div>
            <div class="kpi-card" style="background: #f0fdfa; border-color: #99f6e4;">
                <div class="kpi-title" style="color: #0f766e;">الإيداعات البنكية</div>
                <div class="kpi-val font-mono" style="color: #0d9488;">{count_banks}</div>
            </div>
            <div class="kpi-card" style="background: #faf5ff; border-color: #e9d5ff;">
                <div class="kpi-title" style="color: #7e22ce;">عمليات الأمان والدخول</div>
                <div class="kpi-val font-mono" style="color: #6b21a8;">{count_auth}</div>
            </div>
        </div>

        <table>
            <thead>
                <tr>
                    <th style="width: 30px;" class="text-center">#</th>
                    <th style="width: 140px;" class="text-center">نوع الحركة</th>
                    <th style="width: 90px;" class="text-center">المستخدم</th>
                    <th>تفاصيل العملية والبيان</th>
                    <th style="width: 140px;" class="text-center">التاريخ والوقت</th>
                </tr>
            </thead>
            <tbody>
                {tbody_rows if tbody_rows else '<tr><td colspan="5" class="text-center" style="padding: 20px; color: #94a3b8;">لا توجد عمليات مطابقة للفلاتر المحددة</td></tr>'}
            </tbody>
        </table>

        <div class="signatures">
            <div class="sign-box">
                <div class="sign-title">إعداد مسؤول التدقيق والمتابعة</div>
                <div class="sign-line"></div>
            </div>
            <div class="sign-box">
                <div class="sign-title">مراجعة إدارة أمن المعلومات (IT)</div>
                <div class="sign-line"></div>
            </div>
            <div class="sign-box">
                <div class="sign-title">اعتماد المدير العام للتحالف</div>
                <div class="sign-line"></div>
            </div>
        </div>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html_content)


# --- TELEGRAM BACKUP SETTINGS ---
TELEGRAM_CONFIG_PATH = os.path.join(BASE_DIR, "telegram_config.json")

@app.get("/api/settings/telegram")
def get_telegram_settings(user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="غير مصرح")
    if os.path.exists(TELEGRAM_CONFIG_PATH):
        with open(TELEGRAM_CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        # Mask the bot token for security (show only last 6 chars)
        token = config.get("bot_token", "")
        if len(token) > 10:
            config["bot_token_masked"] = "•" * (len(token) - 6) + token[-6:]
        else:
            config["bot_token_masked"] = token
        return {"success": True, "config": config}
    return {"success": True, "config": {"bot_token": "", "chat_id": "", "enabled": False}}

@app.post("/api/settings/telegram")
def save_telegram_settings(req: TelegramSettingsRequest, user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="غير مصرح")
    with open(TELEGRAM_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(req.model_dump(), f, ensure_ascii=False, indent=4)
    # Log the action
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO activity_logs (username, action, details)
        VALUES (?, 'UPDATE_TELEGRAM_SETTINGS', ?)
    """, (user["username"], "تحديث إعدادات بوت تليجرام للنسخ الاحتياطي"))
    conn.commit()
    conn.close()
    return {"success": True, "message": "تم حفظ إعدادات تليجرام بنجاح"}

@app.post("/api/settings/telegram/test")
def test_telegram_settings(req: TelegramSettingsRequest, user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="غير مصرح")
    try:
        from auto_backup import test_telegram_connection
        success = test_telegram_connection(req.bot_token, req.chat_id)
        if success:
            return {"success": True, "message": "تم الاتصال بنجاح! تحقق من رسالة البوت على تليجرام"}
        else:
            raise HTTPException(status_code=400, detail="فشل الاتصال بالبوت. تأكد من صحة الـ Token والـ Chat ID")
    except ImportError:
        raise HTTPException(status_code=500, detail="ملف auto_backup.py غير موجود")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"خطأ في الاتصال: {str(e)}")

@app.post("/api/settings/telegram/send-now")
def send_backup_now_telegram(user: dict = Depends(get_current_user)):
    """Manually trigger a backup and send it to Telegram immediately."""
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="غير مصرح")
    try:
        from auto_backup import create_backup, send_backup_to_telegram
        backup_path = create_backup()
        if not backup_path:
            raise HTTPException(status_code=500, detail="فشل إنشاء النسخة الاحتياطية")
        success = send_backup_to_telegram(backup_path)
        if success:
            # Log the action
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("""
                INSERT INTO activity_logs (username, action, details)
                VALUES (?, 'TELEGRAM_BACKUP_SENT', ?)
            """, (user["username"], f"إرسال نسخة احتياطية عبر تليجرام: {os.path.basename(backup_path)}"))
            conn.commit()
            conn.close()
            return {"success": True, "message": "تم إرسال النسخة الاحتياطية عبر تليجرام بنجاح"}
        else:
            raise HTTPException(status_code=400, detail="فشل إرسال النسخة عبر تليجرام. تأكد من الإعدادات")
    except ImportError:
        raise HTTPException(status_code=500, detail="ملف auto_backup.py غير موجود")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"Starting Mega Mall Management Server on http://{host}:{port} ...")
    uvicorn.run("server:app", host=host, port=port, reload=True)
