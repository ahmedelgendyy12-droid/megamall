import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "mega_mall.db"))

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    # Companies table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        name_ar TEXT NOT NULL,
        name_en TEXT NOT NULL,
        brand_color TEXT NOT NULL,
        gradient TEXT NOT NULL,
        card_bg TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Users table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL, -- 'admin' or 'company'
        company_id INTEGER REFERENCES companies(id),
        display_name TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Units table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS units (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        unit_code TEXT UNIQUE NOT NULL,
        floor TEXT NOT NULL,
        unit_type TEXT NOT NULL,
        area REAL NOT NULL,
        price_m REAL NOT NULL,
        catalog_price REAL NOT NULL,
        sug REAL,
        status TEXT NOT NULL DEFAULT 'AVAILABLE', -- 'AVAILABLE', 'SOLD', 'RESERVED'
        sold_to_company_id INTEGER REFERENCES companies(id),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Sales table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        serial_no INTEGER,
        unit_id INTEGER NOT NULL REFERENCES units(id),
        company_id INTEGER NOT NULL REFERENCES companies(id),
        client_name TEXT NOT NULL,
        client_phone TEXT,
        client_national_id TEXT,
        sale_date TEXT NOT NULL,
        catalog_price REAL NOT NULL,
        sale_price REAL NOT NULL,
        discount_amount REAL NOT NULL,
        discount_pct REAL NOT NULL,
        down_payment REAL NOT NULL DEFAULT 0,
        installments_count INTEGER NOT NULL DEFAULT 1,
        installment_frequency TEXT NOT NULL DEFAULT 'quarterly', -- 'monthly', 'quarterly', 'semi-annual', 'annual'
        notes TEXT,
        approval_status TEXT NOT NULL DEFAULT 'approved', -- 'approved', 'pending', 'rejected'
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Installments table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS installments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
        installment_no INTEGER NOT NULL,
        installment_type TEXT NOT NULL DEFAULT 'installment', -- 'down_payment', 'installment', 'maintenance', 'delivery'
        due_date TEXT NOT NULL,
        amount REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', -- 'paid', 'pending', 'overdue'
        paid_amount REAL DEFAULT 0,
        payment_date TEXT,
        payment_method TEXT, -- 'cash', 'bank_transfer', 'cheque'
        receipt_no TEXT,
        bank_deposited INTEGER NOT NULL DEFAULT 0,
        bank_name TEXT NOT NULL DEFAULT '',
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Activity Logs
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS activity_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        action TEXT NOT NULL,
        details TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Migrations for existing databases to ensure columns exist
    cursor.execute("PRAGMA table_info(sales)")
    sales_cols = [r[1] for r in cursor.fetchall()]
    if "approval_status" not in sales_cols:
        cursor.execute("ALTER TABLE sales ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'approved'")
    if "last_edited_by" not in sales_cols:
        cursor.execute("ALTER TABLE sales ADD COLUMN last_edited_by TEXT DEFAULT NULL")
    if "last_edited_at" not in sales_cols:
        cursor.execute("ALTER TABLE sales ADD COLUMN last_edited_at TIMESTAMP DEFAULT NULL")

    cursor.execute("PRAGMA table_info(installments)")
    inst_cols = [r[1] for r in cursor.fetchall()]
    if "bank_deposited" not in inst_cols:
        cursor.execute("ALTER TABLE installments ADD COLUMN bank_deposited INTEGER NOT NULL DEFAULT 0")
    if "bank_name" not in inst_cols:
        cursor.execute("ALTER TABLE installments ADD COLUMN bank_name TEXT NOT NULL DEFAULT ''")

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("Database schema created.")
