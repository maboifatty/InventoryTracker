#!/usr/bin/env python3
"""Inventory Master - a dependency-free three-tier inventory application."""

import json
import hashlib
import hmac
import os
import secrets
import smtplib
import sqlite3
import uuid
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr
from html import escape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "inventory.db"
RESTOCK_EMAIL_TO = "communitysevainventory@gmail.com"
SEVA_NAMES = {"Oakland", "Santa Clara", "Fremont"}
INVENTORY_UNITS = {"crates", "cases", "bottles", "cans"}


def load_dotenv(path=ROOT / ".env"):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv()


def connect():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def initialize_database():
    with connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT NOT NULL UNIQUE COLLATE NOCASE,
                name TEXT NOT NULL,
                unit TEXT NOT NULL DEFAULT 'crates',
                category TEXT NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL DEFAULT 0 CHECK(quantity >= 0),
                unit_price REAL NOT NULL DEFAULT 0 CHECK(unit_price >= 0),
                reorder_level INTEGER NOT NULL DEFAULT 0 CHECK(reorder_level >= 0),
                location TEXT NOT NULL DEFAULT '',
                supplier TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(inventory)")
        }
        if "unit" not in columns:
            db.execute("ALTER TABLE inventory ADD COLUMN unit TEXT NOT NULL DEFAULT 'crates'")
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS password_resets (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS sevas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '',
                seva_type TEXT NOT NULL DEFAULT '',
                volunteers INTEGER NOT NULL DEFAULT 1 CHECK(volunteers > 0),
                seva_date TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(sevas)")
        }
        if "location" not in columns:
            db.execute("ALTER TABLE sevas ADD COLUMN location TEXT NOT NULL DEFAULT ''")
        if "seva_type" not in columns:
            db.execute("ALTER TABLE sevas ADD COLUMN seva_type TEXT NOT NULL DEFAULT ''")
        if "volunteers" not in columns:
            db.execute("ALTER TABLE sevas ADD COLUMN volunteers INTEGER NOT NULL DEFAULT 1")
        db.execute("""
            CREATE TABLE IF NOT EXISTS seva_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seva_id INTEGER NOT NULL,
                inventory_id INTEGER,
                item_name TEXT NOT NULL DEFAULT '',
                item_unit TEXT NOT NULL DEFAULT '',
                quantity_used INTEGER NOT NULL CHECK(quantity_used > 0),
                FOREIGN KEY(seva_id) REFERENCES sevas(id) ON DELETE CASCADE,
                FOREIGN KEY(inventory_id) REFERENCES inventory(id) ON DELETE SET NULL
            )
        """)
        migrate_seva_items_history(db)
        db.execute("""
            CREATE TABLE IF NOT EXISTS low_stock_alerts (
                inventory_id INTEGER PRIMARY KEY,
                notified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(inventory_id) REFERENCES inventory(id) ON DELETE CASCADE
            )
        """)


def migrate_seva_items_history(db):
    columns = {
        row["name"]
        for row in db.execute("PRAGMA table_info(seva_items)")
    }
    inventory_fk = [
        dict(row)
        for row in db.execute("PRAGMA foreign_key_list(seva_items)")
        if row["table"] == "inventory"
    ]
    needs_migration = (
        "item_name" not in columns
        or "item_unit" not in columns
        or any(row.get("on_delete", "").upper() != "SET NULL" for row in inventory_fk)
    )
    if not needs_migration:
        return

    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    has_item_name = "item_name" in columns
    has_item_unit = "item_unit" in columns
    db.execute("""
        CREATE TABLE seva_items_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seva_id INTEGER NOT NULL,
            inventory_id INTEGER,
            item_name TEXT NOT NULL DEFAULT '',
            item_unit TEXT NOT NULL DEFAULT '',
            quantity_used INTEGER NOT NULL CHECK(quantity_used > 0),
            FOREIGN KEY(seva_id) REFERENCES sevas(id) ON DELETE CASCADE,
            FOREIGN KEY(inventory_id) REFERENCES inventory(id) ON DELETE SET NULL
        )
    """)
    item_name_expr = "NULLIF(seva_items.item_name, '')" if has_item_name else "NULL"
    item_unit_expr = "NULLIF(seva_items.item_unit, '')" if has_item_unit else "NULL"
    db.execute(f"""
        INSERT INTO seva_items_new (id, seva_id, inventory_id, item_name, item_unit, quantity_used)
        SELECT seva_items.id,
               seva_items.seva_id,
               seva_items.inventory_id,
               COALESCE({item_name_expr}, inventory.name, 'Deleted item'),
               COALESCE({item_unit_expr}, inventory.unit, ''),
               seva_items.quantity_used
        FROM seva_items
        LEFT JOIN inventory ON inventory.id = seva_items.inventory_id
    """)
    db.execute("DROP TABLE seva_items")
    db.execute("ALTER TABLE seva_items_new RENAME TO seva_items")
    db.commit()
    db.execute("PRAGMA foreign_keys = ON")


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return salt, digest.hex()


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def validate_user_payload(payload):
    errors = {}
    username = str(payload.get("username", "")).strip().lower()
    password = str(payload.get("password", ""))
    if not username:
        errors["username"] = "Email is required."
    elif not is_valid_email(username):
        errors["username"] = "Enter a valid email address."
    if not password:
        errors["password"] = "Password is required."
    return username, password, errors


def is_valid_email(value):
    parsed_name, parsed_email = parseaddr(value)
    if parsed_name or parsed_email != value:
        return False
    local, separator, domain = value.partition("@")
    return bool(local and separator and "." in domain and " " not in value)


def smtp_settings():
    host = os.environ.get("SMTP_HOST", "").strip()
    username = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", username or RESTOCK_EMAIL_TO).strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() != "false"
    return host, username, password, sender, port, use_tls


def validate(payload, existing=None):
    errors = {}
    values = {}
    existing = existing or {}
    values["sku"] = str(payload.get("sku") or existing.get("sku") or f"INV-{uuid.uuid4().hex[:8].upper()}").strip()
    values["name"] = str(payload.get("name", "")).strip()
    if not values["name"]:
        errors["name"] = "Product name is required."
    values["unit"] = str(payload.get("unit", existing.get("unit", ""))).strip().lower()
    if values["unit"] not in INVENTORY_UNITS:
        errors["unit"] = "Choose crates, cases, bottles, or cans."
    for field in ("category", "location", "supplier"):
        values[field] = str(payload.get(field, existing.get(field, ""))).strip()
    values["description"] = ""
    for field in ("quantity", "reorder_level"):
        try:
            values[field] = int(payload.get(field, 0))
            if values[field] < 0:
                raise ValueError
        except (TypeError, ValueError):
            errors[field] = "Must be a whole number of 0 or more."
    try:
        values["unit_price"] = round(float(payload.get("unit_price", 0)), 2)
        if values["unit_price"] < 0:
            raise ValueError
    except (TypeError, ValueError):
        errors["unit_price"] = "Must be a number of 0 or more."
    return values, errors


def validate_seva(payload):
    errors = {}
    name = str(payload.get("name", "")).strip()
    location = str(payload.get("location", "")).strip()
    seva_type = str(payload.get("seva_type", "")).strip().lower()
    seva_date = str(payload.get("seva_date", "")).strip()
    raw_items = payload.get("items", [])
    items = []
    if name not in SEVA_NAMES:
        errors["name"] = "Choose Oakland, Santa Clara, or Fremont."
    if not seva_date:
        errors["seva_date"] = "Seva date is required."
    if seva_type not in {"breakfast", "lunch"}:
        errors["seva_type"] = "Choose breakfast or lunch."
    try:
        volunteers = int(payload.get("volunteers", 0))
        if volunteers <= 0:
            raise ValueError
    except (TypeError, ValueError):
        volunteers = 0
        errors["volunteers"] = "Enter at least 1 volunteer."
    if not isinstance(raw_items, list):
        errors["items"] = "Item quantities are required."
        raw_items = []
    for raw in raw_items:
        try:
            item_id = int(raw.get("id"))
            quantity = int(raw.get("quantity", 0))
        except (AttributeError, TypeError, ValueError):
            errors["items"] = "Item quantities must be whole numbers."
            continue
        if quantity < 0:
            errors[f"item_{item_id}"] = "Quantity cannot be negative."
        elif quantity > 0:
            items.append({"id": item_id, "quantity": quantity})
    if "items" not in errors and not items:
        errors["items"] = "Enter a quantity for at least one item."
    return {"name": name, "location": location, "seva_type": seva_type, "volunteers": volunteers, "seva_date": seva_date, "items": items}, errors


def low_stock_items(db):
    return [dict(row) for row in db.execute("""
        SELECT id, name, quantity, reorder_level
        FROM inventory
        WHERE quantity <= reorder_level
        ORDER BY name COLLATE NOCASE
    """)]


def send_restock_email(items):
    host, username, password, sender, port, use_tls = smtp_settings()
    if not host or not username or not password:
        return {"sent": False, "configured": False, "message": "SMTP email is not configured."}

    rows = "".join(
        "<tr>"
        f"<td>{escape(item['name'])}</td>"
        f"<td>{item['quantity']}</td>"
        f"<td>{item['reorder_level']}</td>"
        "</tr>"
        for item in items
    )
    html = f"""
    <p>The following inventory items require restock attention:</p>
    <table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;">
      <thead>
        <tr>
          <th>Item required</th>
          <th>Current quantity</th>
          <th>Minimum inventory quantity</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    """
    text = "The following inventory items require restock attention:\n\n"
    text += "Item required | Current quantity | Minimum inventory quantity\n"
    text += "\n".join(f"{item['name']} | {item['quantity']} | {item['reorder_level']}" for item in items)

    message = EmailMessage()
    message["Subject"] = "Inventory restock attention required"
    message["From"] = sender
    message["To"] = RESTOCK_EMAIL_TO
    message.set_content(text)
    message.add_alternative(html, subtype="html")

    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            if use_tls:
                smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)
        return {"sent": True, "configured": True, "message": f"Email sent to {RESTOCK_EMAIL_TO}."}
    except Exception as exc:
        return {"sent": False, "configured": True, "message": f"Email could not be sent: {exc}"}


def send_password_reset_email(recipient, reset_url):
    host, username, password, sender, port, use_tls = smtp_settings()
    if not host or not username or not password:
        return {"sent": False, "configured": False, "message": "SMTP email is not configured."}

    message = EmailMessage()
    message["Subject"] = "Inventory Master password reset"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(
        "We received a request to reset your Inventory Master password.\n\n"
        f"Reset your password here: {reset_url}\n\n"
        "This link expires in 1 hour. If you did not request this, you can ignore this email."
    )
    message.add_alternative(
        f"""
        <p>We received a request to reset your Inventory Master password.</p>
        <p><a href="{escape(reset_url)}">Reset your password</a></p>
        <p>This link expires in 1 hour. If you did not request this, you can ignore this email.</p>
        """,
        subtype="html",
    )

    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            if use_tls:
                smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)
        return {"sent": True, "configured": True, "message": f"Password reset email sent to {recipient}."}
    except Exception as exc:
        return {"sent": False, "configured": True, "message": f"Password reset email could not be sent: {exc}"}


def process_low_stock_notifications(db):
    current_items = low_stock_items(db)
    current_ids = {item["id"] for item in current_items}
    if current_ids:
        db.execute(
            "DELETE FROM low_stock_alerts WHERE inventory_id NOT IN ({})".format(",".join("?" for _ in current_ids)),
            list(current_ids),
        )
    else:
        db.execute("DELETE FROM low_stock_alerts")
        return {"sent": False, "configured": True, "message": ""}

    already_notified = {
        row["inventory_id"]
        for row in db.execute("SELECT inventory_id FROM low_stock_alerts")
    }
    new_attention_items = [item for item in current_items if item["id"] not in already_notified]
    if not new_attention_items:
        return {"sent": False, "configured": True, "message": ""}

    result = send_restock_email(current_items)
    if result["sent"]:
        db.executemany(
            "INSERT OR REPLACE INTO low_stock_alerts (inventory_id, notified_at) VALUES (?, CURRENT_TIMESTAMP)",
            [(item["id"],) for item in current_items],
        )
    return result


def existing_seva_items(db, seva_id):
    return {
        row["inventory_id"]: row["quantity_used"]
        for row in db.execute(
            "SELECT inventory_id, quantity_used FROM seva_items WHERE seva_id = ? AND inventory_id IS NOT NULL",
            (seva_id,),
        )
    }


def validate_seva_stock_changes(db, new_items, old_items=None):
    old_items = old_items or {}
    errors = {}
    ids = sorted(set(old_items) | {item["id"] for item in new_items})
    if not ids:
        return errors
    existing = {
        row["id"]: dict(row)
        for row in db.execute(
            "SELECT id, name, quantity FROM inventory WHERE id IN ({})".format(",".join("?" for _ in ids)),
            ids,
        )
    }
    for item_id in ids:
        current = existing.get(item_id)
        if current is None:
            errors[f"item_{item_id}"] = "This item no longer exists."
            continue
        old_quantity = old_items.get(item_id, 0)
        new_quantity = next((item["quantity"] for item in new_items if item["id"] == item_id), 0)
        available = current["quantity"] + old_quantity
        if new_quantity > available:
            errors[f"item_{item_id}"] = f"Only {available} available."
    return errors


def apply_seva_items(db, seva_id, new_items, old_items=None):
    old_items = old_items or {}
    new_by_id = {item["id"]: item["quantity"] for item in new_items}
    item_names = {
        row["id"]: {"name": row["name"], "unit": row["unit"]}
        for row in db.execute(
            "SELECT id, name, unit FROM inventory WHERE id IN ({})".format(",".join("?" for _ in new_by_id)),
            list(new_by_id),
        )
    } if new_by_id else {}
    for item_id in sorted(set(old_items) | set(new_by_id)):
        old_quantity = old_items.get(item_id, 0)
        new_quantity = new_by_id.get(item_id, 0)
        db.execute(
            "UPDATE inventory SET quantity = quantity + ? - ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (old_quantity, new_quantity, item_id),
        )
    db.execute("DELETE FROM seva_items WHERE seva_id = ? AND inventory_id IS NOT NULL", (seva_id,))
    db.executemany(
        "INSERT INTO seva_items (seva_id, inventory_id, item_name, item_unit, quantity_used) VALUES (?, ?, ?, ?, ?)",
        [
            (
                seva_id,
                item["id"],
                item_names.get(item["id"], {}).get("name", "Deleted item"),
                item_names.get(item["id"], {}).get("unit", ""),
                item["quantity"],
            )
            for item in new_items
        ],
    )


def seva_items_for_response(db, seva_id):
    return [dict(row) for row in db.execute("""
        SELECT seva_items.inventory_id AS id,
               COALESCE(NULLIF(seva_items.item_name, ''), inventory.name, 'Deleted item') AS name,
               COALESCE(NULLIF(seva_items.item_unit, ''), inventory.unit, '') AS unit,
               seva_items.quantity_used
        FROM seva_items
        LEFT JOIN inventory ON inventory.id = seva_items.inventory_id
        WHERE seva_items.seva_id = ?
        ORDER BY name COLLATE NOCASE
    """, (seva_id,))]


def pdf_text(value):
    return str(value).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf_line(content, x, y, size=11, bold=False):
    font = "F2" if bold else "F1"
    return f"BT /{font} {size} Tf {x} {y} Td ({pdf_text(content)}) Tj ET"


def pdf_rule(x1, y1, x2, y2):
    return f"{x1} {y1} m {x2} {y2} l S"


def build_seva_pdf(seva, items):
    pages = []
    commands = ["q", "1 1 1 RG", "0 0 0 rg", "0.7 w"]
    y = 742
    left, right = 72, 540
    row_h = 22
    col_name, col_qty, col_unit = 82, 340, 430

    def add_table_header(title="Items Used"):
        nonlocal y, commands
        commands.append(pdf_line(title, 72, y, 14, True))
        y -= 22
        commands.extend([
            pdf_rule(left, y + 8, right, y + 8),
            pdf_rule(left, y - row_h + 8, right, y - row_h + 8),
            pdf_line("Name", col_name, y - 7, 11, True),
            pdf_line("Quantity", col_qty, y - 7, 11, True),
            pdf_line("Units", col_unit, y - 7, 11, True),
        ])
        y -= row_h

    def finish_page():
        nonlocal commands
        commands.append("Q")
        pages.append(commands)

    def start_page():
        nonlocal commands, y
        commands = ["q", "1 1 1 RG", "0 0 0 rg", "0.7 w"]
        y = 742

    commands.append(pdf_line("Seva Details", 72, y, 20, True))
    y -= 38
    detail_rows = [
        ("Seva name", seva["name"]),
        ("Date", seva["seva_date"]),
        ("Type", seva["seva_type"].capitalize() if seva["seva_type"] else ""),
        ("Number of people served", seva["volunteers"]),
    ]
    for label, value in detail_rows:
        commands.append(pdf_line(f"{label}: {value}", 72, y, 12))
        y -= 20

    y -= 14
    add_table_header()
    if not items:
        commands.append(pdf_line("No items recorded.", col_name, y - 7, 11))
        commands.append(pdf_rule(left, y - row_h + 8, right, y - row_h + 8))
    else:
        for item in items:
            if y < 80:
                finish_page()
                start_page()
                add_table_header("Items Used (continued)")
            commands.extend([
                pdf_line(item["name"], col_name, y - 7, 10),
                pdf_line(item["quantity_used"], col_qty, y - 7, 10),
                pdf_line(item.get("unit", ""), col_unit, y - 7, 10),
                pdf_rule(left, y - row_h + 8, right, y - row_h + 8),
            ])
            y -= row_h
    finish_page()

    page_count = len(pages)
    page_ids = [3 + i for i in range(page_count)]
    font_regular_id = 3 + page_count
    font_bold_id = font_regular_id + 1
    content_start_id = font_bold_id + 1
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{page_id} 0 R' for page_id in page_ids)}] /Count {page_count} >>".encode("ascii"),
    ]
    for index, page_id in enumerate(page_ids):
        content_id = content_start_id + index
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_regular_id} 0 R /F2 {font_bold_id} 0 R >> >> /Contents {content_id} 0 R >>".encode("ascii")
        )
    objects.extend([
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    ])
    for page_commands in pages:
        stream = "\n".join(page_commands).encode("utf-8")
        objects.append(b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream")
    pdf = [b"%PDF-1.4\n"]
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(sum(len(part) for part in pdf))
        pdf.append(f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref = sum(len(part) for part in pdf)
    pdf.append(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        pdf.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.append(f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return b"".join(pdf)


class InventoryHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "static"), **kwargs)

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def pdf_response(self, body, filename):
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return None

    def current_user(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        token = header.removeprefix("Bearer ").strip()
        if not token:
            return None
        with connect() as db:
            row = db.execute("""
                SELECT users.id, users.username
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token = ?
            """, (token,)).fetchone()
        return dict(row) if row else None

    def public_url(self, path):
        host = self.headers.get("Host", "127.0.0.1:8000")
        scheme = self.headers.get("X-Forwarded-Proto", "http")
        return f"{scheme}://{host}{path}"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/reset-password":
            self.path = "/index.html"
            super().do_GET()
            return
        pdf_seva_id = self.pdf_seva_id()
        if pdf_seva_id is not None:
            if self.current_user() is None:
                self.json_response({"error": "Please log in."}, 401)
                return
            with connect() as db:
                seva = db.execute(
                    "SELECT id, name, location, seva_type, volunteers, seva_date, created_at FROM sevas WHERE id = ?",
                    (pdf_seva_id,),
                ).fetchone()
                if seva is None:
                    self.json_response({"error": "Seva not found."}, 404)
                    return
                items = seva_items_for_response(db, pdf_seva_id)
            filename = f"seva-{pdf_seva_id}-{seva['seva_date']}.pdf"
            self.pdf_response(build_seva_pdf(dict(seva), items), filename)
            return
        if parsed.path == "/api/items":
            if self.current_user() is None:
                self.json_response({"error": "Please log in."}, 401)
                return
            params = parse_qs(parsed.query)
            search = params.get("search", [""])[0].strip()
            status = params.get("status", ["all"])[0]
            sql = "SELECT * FROM inventory WHERE 1=1"
            args = []
            if search:
                sql += " AND (sku LIKE ? OR name LIKE ? OR category LIKE ? OR location LIKE ? OR supplier LIKE ?)"
                term = f"%{search}%"
                args.extend([term] * 5)
            if status == "low":
                sql += " AND quantity > 0 AND quantity <= reorder_level"
            elif status == "out":
                sql += " AND quantity = 0"
            elif status == "healthy":
                sql += " AND quantity > reorder_level"
            sql += " ORDER BY updated_at DESC, id DESC"
            with connect() as db:
                notification = process_low_stock_notifications(db)
                items = [dict(row) for row in db.execute(sql, args)]
                stats = dict(db.execute("""
                    SELECT COUNT(*) total_items,
                           COALESCE(SUM(quantity), 0) total_units,
                           COALESCE(SUM(quantity * unit_price), 0) total_value,
                           COALESCE(SUM(CASE WHEN quantity <= reorder_level THEN 1 ELSE 0 END), 0) low_stock
                    FROM inventory
                """).fetchone())
                notified_count = db.execute("SELECT COUNT(*) FROM low_stock_alerts").fetchone()[0]
                stats["restock_email_to"] = RESTOCK_EMAIL_TO
                stats["restock_email_sent"] = stats["low_stock"] > 0 and notified_count > 0
                stats["restock_email_message"] = notification.get("message", "")
            self.json_response({"items": items, "stats": stats})
            return
        if parsed.path == "/api/sevas":
            if self.current_user() is None:
                self.json_response({"error": "Please log in."}, 401)
                return
            with connect() as db:
                sevas = [dict(row) for row in db.execute("""
                    SELECT id, name, location, seva_type, volunteers, seva_date, created_at
                    FROM sevas
                    ORDER BY seva_date DESC, created_at DESC, id DESC
                """)]
                for seva in sevas:
                    seva["items"] = seva_items_for_response(db, seva["id"])
            self.json_response({"sevas": sevas})
            return
        if parsed.path.startswith("/api/"):
            self.json_response({"error": "Not found."}, 404)
            return
        super().do_GET()

    def do_POST(self):
        if self.path == "/api/users":
            payload = self.read_json()
            if payload is None:
                self.json_response({"error": "Invalid JSON."}, 400)
                return
            username, password, errors = validate_user_payload(payload)
            if errors:
                self.json_response({"error": "Please correct the highlighted fields.", "fields": errors}, 422)
                return
            salt, password_hash = hash_password(password)
            try:
                with connect() as db:
                    db.execute(
                        "INSERT INTO users (username, password_salt, password_hash) VALUES (?, ?, ?)",
                        (username, salt, password_hash),
                    )
                self.json_response({"message": "User created."}, 201)
            except sqlite3.IntegrityError:
                self.json_response({"error": "That email already exists.", "fields": {"username": "Choose another email."}}, 409)
            return

        if self.path == "/api/login":
            payload = self.read_json()
            if payload is None:
                self.json_response({"error": "Invalid JSON."}, 400)
                return
            username, password, errors = validate_user_payload(payload)
            if errors:
                self.json_response({"error": "Please correct the highlighted fields.", "fields": errors}, 422)
                return
            with connect() as db:
                row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
                if row:
                    _, password_hash = hash_password(password, row["password_salt"])
                    if hmac.compare_digest(password_hash, row["password_hash"]):
                        token = secrets.token_urlsafe(32)
                        db.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, row["id"]))
                        self.json_response({"token": token, "username": row["username"]})
                        return
            self.json_response({"error": "Invalid email or password."}, 401)
            return

        if self.path == "/api/forgot-password":
            payload = self.read_json()
            if payload is None:
                self.json_response({"error": "Invalid JSON."}, 400)
                return
            username = str(payload.get("username", "")).strip().lower()
            errors = {}
            if not username:
                errors["username"] = "Email is required."
            elif not is_valid_email(username):
                errors["username"] = "Enter a valid email address."
            if errors:
                self.json_response({"error": "Please correct the highlighted fields.", "fields": errors}, 422)
                return
            with connect() as db:
                row = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
                if not row:
                    self.json_response({"message": "If that email is registered, a password reset link has been sent."})
                    return
                token = secrets.token_urlsafe(32)
                token_hash = hash_token(token)
                expires_at = (datetime.utcnow() + timedelta(hours=1)).isoformat(timespec="seconds")
                reset_url = self.public_url(f"/reset-password?token={token}")
                db.execute("DELETE FROM password_resets WHERE user_id = ?", (row["id"],))
                db.execute(
                    "INSERT INTO password_resets (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                    (token_hash, row["id"], expires_at),
                )
                result = send_password_reset_email(username, reset_url)
                if not result["sent"]:
                    db.execute("DELETE FROM password_resets WHERE token_hash = ?", (token_hash,))
                    self.json_response({"error": result["message"]}, 503)
                    return
            self.json_response({"message": "If that email is registered, a password reset link has been sent."})
            return

        if self.path == "/api/reset-password":
            payload = self.read_json()
            if payload is None:
                self.json_response({"error": "Invalid JSON."}, 400)
                return
            token = str(payload.get("token", "")).strip()
            password = str(payload.get("password", ""))
            confirm_password = str(payload.get("confirm_password", ""))
            errors = {}
            if not token:
                errors["token"] = "Reset link is missing."
            if not password:
                errors["password"] = "New password is required."
            if not confirm_password:
                errors["confirm_password"] = "Please re-enter the new password."
            elif password and password != confirm_password:
                errors["confirm_password"] = "Passwords must match."
            if errors:
                self.json_response({"error": "Please correct the highlighted fields.", "fields": errors}, 422)
                return
            token_hash = hash_token(token)
            with connect() as db:
                row = db.execute("""
                    SELECT password_resets.*, users.id AS account_id
                    FROM password_resets
                    JOIN users ON users.id = password_resets.user_id
                    WHERE password_resets.token_hash = ?
                """, (token_hash,)).fetchone()
                if not row or row["used_at"]:
                    self.json_response({"error": "This reset link is invalid or has already been used."}, 400)
                    return
                if datetime.fromisoformat(row["expires_at"]) < datetime.utcnow():
                    db.execute("DELETE FROM password_resets WHERE token_hash = ?", (token_hash,))
                    self.json_response({"error": "This reset link has expired. Please request a new one."}, 400)
                    return
                salt, password_hash = hash_password(password)
                db.execute(
                    "UPDATE users SET password_salt = ?, password_hash = ? WHERE id = ?",
                    (salt, password_hash, row["account_id"]),
                )
                db.execute("DELETE FROM sessions WHERE user_id = ?", (row["account_id"],))
                db.execute(
                    "UPDATE password_resets SET used_at = CURRENT_TIMESTAMP WHERE token_hash = ?",
                    (token_hash,),
                )
            self.json_response({"message": "Password reset successfully. Please log in with your new password."})
            return

        if self.path == "/api/logout":
            header = self.headers.get("Authorization", "")
            token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
            if token:
                with connect() as db:
                    db.execute("DELETE FROM sessions WHERE token = ?", (token,))
            self.json_response({"message": "Logged out."})
            return

        if self.path == "/api/sevas":
            if self.current_user() is None:
                self.json_response({"error": "Please log in."}, 401)
                return
            payload = self.read_json()
            if payload is None:
                self.json_response({"error": "Invalid JSON."}, 400)
                return
            values, errors = validate_seva(payload)
            if errors:
                self.json_response({"error": "Please correct the highlighted fields.", "fields": errors}, 422)
                return
            with connect() as db:
                errors = validate_seva_stock_changes(db, values["items"])
                if errors:
                    self.json_response({"error": "Please correct the highlighted fields.", "fields": errors}, 422)
                    return
                cursor = db.execute(
                    "INSERT INTO sevas (name, location, seva_type, volunteers, seva_date) VALUES (?, ?, ?, ?, ?)",
                    (values["name"], values["location"], values["seva_type"], values["volunteers"], values["seva_date"]),
                )
                seva_id = cursor.lastrowid
                apply_seva_items(db, seva_id, values["items"])
                notification = process_low_stock_notifications(db)
            self.json_response({"message": "Seva saved.", "id": seva_id, "notification": notification}, 201)
            return

        if self.path != "/api/items":
            self.json_response({"error": "Not found."}, 404)
            return
        if self.current_user() is None:
            self.json_response({"error": "Please log in."}, 401)
            return
        payload = self.read_json()
        if payload is None:
            self.json_response({"error": "Invalid JSON."}, 400)
            return
        values, errors = validate(payload)
        if errors:
            self.json_response({"error": "Please correct the highlighted fields.", "fields": errors}, 422)
            return
        try:
            with connect() as db:
                columns = list(values)
                cursor = db.execute(
                    f"INSERT INTO inventory ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    [values[c] for c in columns],
                )
                item = dict(db.execute("SELECT * FROM inventory WHERE id = ?", (cursor.lastrowid,)).fetchone())
                notification = process_low_stock_notifications(db)
            item["notification"] = notification
            self.json_response(item, 201)
        except sqlite3.IntegrityError:
            self.json_response({"error": "That SKU already exists.", "fields": {"sku": "SKU must be unique."}}, 409)

    def do_PUT(self):
        seva_id = self.seva_id()
        if seva_id is not None:
            if self.current_user() is None:
                self.json_response({"error": "Please log in."}, 401)
                return
            payload = self.read_json()
            if payload is None:
                self.json_response({"error": "Invalid JSON."}, 400)
                return
            values, errors = validate_seva(payload)
            if errors:
                self.json_response({"error": "Please correct the highlighted fields.", "fields": errors}, 422)
                return
            with connect() as db:
                row = db.execute("SELECT * FROM sevas WHERE id = ?", (seva_id,)).fetchone()
                if row is None:
                    self.json_response({"error": "Seva not found."}, 404)
                    return
                old_items = existing_seva_items(db, seva_id)
                errors = validate_seva_stock_changes(db, values["items"], old_items)
                if errors:
                    self.json_response({"error": "Please correct the highlighted fields.", "fields": errors}, 422)
                    return
                db.execute(
                    "UPDATE sevas SET name = ?, location = ?, seva_type = ?, volunteers = ?, seva_date = ? WHERE id = ?",
                    (values["name"], values["location"], values["seva_type"], values["volunteers"], values["seva_date"], seva_id),
                )
                apply_seva_items(db, seva_id, values["items"], old_items)
                notification = process_low_stock_notifications(db)
            self.json_response({"message": "Seva updated.", "id": seva_id, "notification": notification})
            return

        item_id = self.item_id()
        if item_id is None:
            self.json_response({"error": "Not found."}, 404)
            return
        if self.current_user() is None:
            self.json_response({"error": "Please log in."}, 401)
            return
        payload = self.read_json()
        if payload is None:
            self.json_response({"error": "Invalid JSON."}, 400)
            return
        with connect() as db:
            row = db.execute("SELECT * FROM inventory WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            self.json_response({"error": "Item not found."}, 404)
            return
        values, errors = validate(payload, dict(row))
        if errors:
            self.json_response({"error": "Please correct the highlighted fields.", "fields": errors}, 422)
            return
        try:
            with connect() as db:
                assignments = ", ".join(f"{key} = ?" for key in values)
                cursor = db.execute(
                    f"UPDATE inventory SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    [*values.values(), item_id],
                )
                if cursor.rowcount == 0:
                    self.json_response({"error": "Item not found."}, 404)
                    return
                item = dict(db.execute("SELECT * FROM inventory WHERE id = ?", (item_id,)).fetchone())
                notification = process_low_stock_notifications(db)
            item["notification"] = notification
            self.json_response(item)
        except sqlite3.IntegrityError:
            self.json_response({"error": "That SKU already exists.", "fields": {"sku": "SKU must be unique."}}, 409)

    def do_DELETE(self):
        seva_id = self.seva_id()
        if seva_id is not None:
            if self.current_user() is None:
                self.json_response({"error": "Please log in."}, 401)
                return
            with connect() as db:
                row = db.execute("SELECT * FROM sevas WHERE id = ?", (seva_id,)).fetchone()
                if row is None:
                    self.json_response({"error": "Seva not found."}, 404)
                    return
                old_items = existing_seva_items(db, seva_id)
                for item_id, quantity in old_items.items():
                    db.execute(
                        "UPDATE inventory SET quantity = quantity + ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (quantity, item_id),
                    )
                db.execute("DELETE FROM sevas WHERE id = ?", (seva_id,))
                notification = process_low_stock_notifications(db)
            self.json_response({"message": "Seva deleted.", "notification": notification})
            return

        item_id = self.item_id()
        if item_id is None:
            self.json_response({"error": "Not found."}, 404)
            return
        if self.current_user() is None:
            self.json_response({"error": "Please log in."}, 401)
            return
        with connect() as db:
            item = db.execute("SELECT id, quantity FROM inventory WHERE id = ?", (item_id,)).fetchone()
            if item is None:
                self.json_response({"error": "Item not found."}, 404)
                return
            if item["quantity"] > 0:
                self.json_response({"error": "Set this item quantity to 0 before deleting it."}, 409)
                return
            db.execute("DELETE FROM inventory WHERE id = ?", (item_id,))
        self.json_response({"message": "Item deleted."})

    def item_id(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "items"]:
            try:
                return int(parts[2])
            except ValueError:
                pass
        return None

    def seva_id(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "sevas"]:
            try:
                return int(parts[2])
            except ValueError:
                pass
        return None

    def pdf_seva_id(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "sevas"] and parts[3] == "pdf":
            try:
                return int(parts[2])
            except ValueError:
                pass
        return None


if __name__ == "__main__":
    initialize_database()
    server = ThreadingHTTPServer(("127.0.0.1", 8000), InventoryHandler)
    print("Inventory Master is running at http://127.0.0.1:8000")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
