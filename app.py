#!/usr/bin/env python3
"""
Expense Tracker Web  -  backend (pure Python standard library)

Run:  python app.py      ->  opens http://localhost:8000
REST API (JSON):
  GET    /api/expenses?month=YYYY-MM&category=&q=
  POST   /api/expenses
  PUT    /api/expenses/<id>
  DELETE /api/expenses/<id>
  GET    /api/summary?month=YYYY-MM
  GET    /api/budgets            POST /api/budgets        DELETE /api/budgets/<category>
  GET    /api/export.csv?month=YYYY-MM
"""

import calendar
import csv
import io
import json
import mimetypes
import re
import sqlite3
import threading
import webbrowser
from contextlib import closing
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
DB_PATH = BASE / "expenses.db"
HOST, PORT = "127.0.0.1", 8000

CATEGORIES = ["Food", "Travel", "Shopping", "Bills", "Education",
              "Health", "Entertainment", "Rent", "Other"]
PAYMENTS = ["Cash", "UPI", "Card", "Net Banking"]


# ------------------------------------------------------------------ database
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(connect()) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            amount REAL NOT NULL CHECK (amount > 0),
            category TEXT NOT NULL,
            description TEXT DEFAULT '',
            payment TEXT DEFAULT 'Cash')""")
        conn.execute("""CREATE TABLE IF NOT EXISTS budgets (
            category TEXT PRIMARY KEY,
            monthly_limit REAL NOT NULL CHECK (monthly_limit > 0))""")


def valid_month(value):
    try:
        return datetime.strptime(value, "%Y-%m").strftime("%Y-%m")
    except (TypeError, ValueError):
        return date.today().strftime("%Y-%m")


def clean_expense(data):
    """Validate JSON from the frontend. Returns (row, error)."""
    try:
        d = datetime.strptime(str(data.get("date", "")), "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None, "Date must be in YYYY-MM-DD format."
    try:
        amount = round(float(data.get("amount")), 2)
        if amount <= 0 or amount > 1e9:
            raise ValueError
    except (TypeError, ValueError):
        return None, "Amount must be a positive number."
    category = str(data.get("category", "")).strip().title()[:30]
    if not category:
        return None, "Category is required."
    payment = str(data.get("payment", "Cash")).strip()
    if payment not in PAYMENTS:
        payment = "Cash"
    desc = str(data.get("description", "")).strip()[:120]
    return (d, amount, category, desc, payment), None


def rows_to_list(rows):
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ queries
def query_expenses(month, category, q):
    sql, params = "SELECT * FROM expenses WHERE 1=1", []
    if month:
        sql += " AND substr(date,1,7)=?"
        params.append(month)
    if category and category != "All":
        sql += " AND category=?"
        params.append(category)
    if q:
        sql += " AND (description LIKE ? OR category LIKE ? OR payment LIKE ?)"
        params += [f"%{q}%"] * 3
    sql += " ORDER BY date DESC, id DESC"
    with closing(connect()) as conn:
        return rows_to_list(conn.execute(sql, params).fetchall())


def month_shift(month, delta):
    y, m = int(month[:4]), int(month[5:])
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12}-{idx % 12 + 1:02d}"


def build_summary(month):
    today = date.today()
    with closing(connect()) as conn:
        total, count = conn.execute(
            "SELECT COALESCE(SUM(amount),0), COUNT(*) FROM expenses "
            "WHERE substr(date,1,7)=?", (month,)).fetchone()
        by_cat = rows_to_list(conn.execute(
            "SELECT category, SUM(amount) AS total, COUNT(*) AS n FROM expenses "
            "WHERE substr(date,1,7)=? GROUP BY category ORDER BY total DESC", (month,)))
        by_pay = rows_to_list(conn.execute(
            "SELECT payment, SUM(amount) AS total FROM expenses "
            "WHERE substr(date,1,7)=? GROUP BY payment ORDER BY total DESC", (month,)))
        trend = []
        for i in range(5, -1, -1):
            mo = month_shift(month, -i)
            t = conn.execute("SELECT COALESCE(SUM(amount),0) FROM expenses "
                             "WHERE substr(date,1,7)=?", (mo,)).fetchone()[0]
            trend.append({"month": mo, "total": t})
        prev_total = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE substr(date,1,7)=?",
            (month_shift(month, -1),)).fetchone()[0]
        biggest = conn.execute(
            "SELECT * FROM expenses WHERE substr(date,1,7)=? "
            "ORDER BY amount DESC LIMIT 1", (month,)).fetchone()
        budgets = []
        for b in conn.execute("SELECT * FROM budgets ORDER BY category"):
            spent = conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM expenses "
                "WHERE substr(date,1,7)=? AND category=?",
                (month, b["category"])).fetchone()[0]
            budgets.append({"category": b["category"], "limit": b["monthly_limit"],
                            "spent": spent})
    year, mon = int(month[:4]), int(month[5:])
    days_in_month = calendar.monthrange(year, mon)[1]
    days = today.day if month == today.strftime("%Y-%m") else days_in_month
    return {
        "month": month, "total": total, "count": count,
        "avg_per_day": total / days if days else 0,
        "prev_total": prev_total,
        "by_category": by_cat, "by_payment": by_pay, "trend": trend,
        "biggest": dict(biggest) if biggest else None, "budgets": budgets,
    }


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "ExpenseTracker/1.0"

    def log_message(self, fmt, *args):
        print(f"  {self.command:<6} {self.path}")

    # helpers
    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def error(self, message, status=400):
        self.send_json({"error": message}, status)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return None

    # routing
    def do_GET(self):
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path

        if path == "/api/expenses":
            month = qs.get("month")
            return self.send_json(query_expenses(
                valid_month(month) if month else None,
                qs.get("category"), qs.get("q", "").strip()))
        if path == "/api/summary":
            return self.send_json(build_summary(valid_month(qs.get("month"))))
        if path == "/api/meta":
            return self.send_json({"categories": CATEGORIES, "payments": PAYMENTS})
        if path == "/api/export.csv":
            month = qs.get("month")
            rows = query_expenses(valid_month(month) if month else None, None, "")
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["ID", "Date", "Amount", "Category", "Payment", "Description"])
            for r in rows:
                w.writerow([r["id"], r["date"], r["amount"], r["category"],
                            r["payment"], r["description"]])
            body = buf.getvalue().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="expenses_{month or "all"}.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/api/"):
            return self.error("Not found", 404)
        self.serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        data = self.read_json()
        if data is None:
            return self.error("Invalid JSON")
        if path == "/api/expenses":
            row, err = clean_expense(data)
            if err:
                return self.error(err)
            with closing(connect()) as conn, conn:
                cur = conn.execute(
                    "INSERT INTO expenses (date, amount, category, description, payment) "
                    "VALUES (?,?,?,?,?)", row)
            return self.send_json({"id": cur.lastrowid}, 201)
        if path == "/api/budgets":
            category = str(data.get("category", "")).strip().title()
            try:
                limit = float(data.get("limit"))
                if limit <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return self.error("Budget limit must be a positive number.")
            if not category:
                return self.error("Category is required.")
            with closing(connect()) as conn, conn:
                conn.execute(
                    "INSERT INTO budgets (category, monthly_limit) VALUES (?,?) "
                    "ON CONFLICT(category) DO UPDATE SET "
                    "monthly_limit=excluded.monthly_limit", (category, limit))
            return self.send_json({"ok": True}, 201)
        self.error("Not found", 404)

    def do_PUT(self):
        m = re.fullmatch(r"/api/expenses/(\d+)", urlparse(self.path).path)
        if not m:
            return self.error("Not found", 404)
        data = self.read_json()
        if data is None:
            return self.error("Invalid JSON")
        row, err = clean_expense(data)
        if err:
            return self.error(err)
        with closing(connect()) as conn, conn:
            cur = conn.execute(
                "UPDATE expenses SET date=?, amount=?, category=?, description=?, "
                "payment=? WHERE id=?", (*row, int(m.group(1))))
        if cur.rowcount == 0:
            return self.error("Expense not found", 404)
        self.send_json({"ok": True})

    def do_DELETE(self):
        path = urlparse(self.path).path
        m = re.fullmatch(r"/api/expenses/(\d+)", path)
        b = re.fullmatch(r"/api/budgets/(.+)", path)
        with closing(connect()) as conn, conn:
            if m:
                cur = conn.execute("DELETE FROM expenses WHERE id=?", (int(m.group(1)),))
            elif b:
                cur = conn.execute("DELETE FROM budgets WHERE category=?",
                                   (unquote(b.group(1)),))
            else:
                return self.error("Not found", 404)
        if cur.rowcount == 0:
            return self.error("Not found", 404)
        self.send_json({"ok": True})

    def serve_static(self, path):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        file = (STATIC / rel).resolve()
        if STATIC.resolve() not in file.parents or not file.is_file():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404 Not Found")
            return
        body = file.read_bytes()
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8" if ctype.startswith("text") or ctype.endswith("javascript") else ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def main():
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://localhost:{PORT}"
    print("=" * 46)
    print("  EXPENSE TRACKER WEB")
    print(f"  Running at {url}   (Ctrl+C to stop)")
    print("=" * 46)
    threading.Timer(1, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped. Bye!")
        server.server_close()


if __name__ == "__main__":
    main()
