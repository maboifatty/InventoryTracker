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
from email.message import EmailMessage
from html import escape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "inventory.db"
RESTOCK_EMAIL_TO = "communitysevainventory@gmail.com"


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
            CREATE TABLE IF NOT EXISTS sevas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '',
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
        db.execute("""
            CREATE TABLE IF NOT EXISTS seva_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seva_id INTEGER NOT NULL,
                inventory_id INTEGER NOT NULL,
                quantity_used INTEGER NOT NULL CHECK(quantity_used > 0),
                FOREIGN KEY(seva_id) REFERENCES sevas(id) ON DELETE CASCADE,
                FOREIGN KEY(inventory_id) REFERENCES inventory(id) ON DELETE CASCADE
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS low_stock_alerts (
                inventory_id INTEGER PRIMARY KEY,
                notified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(inventory_id) REFERENCES inventory(id) ON DELETE CASCADE
            )
        """)


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return salt, digest.hex()


def validate_user_payload(payload):
    errors = {}
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    if not username:
        errors["username"] = "User name is required."
    if not password:
        errors["password"] = "Password is required."
    return username, password, errors


def validate(payload, existing=None):
    errors = {}
    values = {}
    existing = existing or {}
    values["sku"] = str(payload.get("sku") or existing.get("sku") or f"INV-{uuid.uuid4().hex[:8].upper()}").strip()
    values["name"] = str(payload.get("name", "")).strip()
    if not values["name"]:
        errors["name"] = "Product name is required."
    for field in ("category", "location", "supplier", "description"):
        values[field] = str(payload.get(field, existing.get(field, ""))).strip()
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
    seva_date = str(payload.get("seva_date", "")).strip()
    raw_items = payload.get("items", [])
    items = []
    if not name:
        errors["name"] = "Seva name is required."
    if not seva_date:
        errors["seva_date"] = "Seva date is required."
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
    return {"name": name, "location": location, "seva_date": seva_date, "items": items}, errors


def low_stock_items(db):
    return [dict(row) for row in db.execute("""
        SELECT id, name, quantity, reorder_level
        FROM inventory
        WHERE quantity <= reorder_level
        ORDER BY name COLLATE NOCASE
    """)]


def send_restock_email(items):
    host = os.environ.get("SMTP_HOST", "").strip()
    username = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", username or RESTOCK_EMAIL_TO).strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() != "false"
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
            "SELECT inventory_id, quantity_used FROM seva_items WHERE seva_id = ?",
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
    for item_id in sorted(set(old_items) | set(new_by_id)):
        old_quantity = old_items.get(item_id, 0)
        new_quantity = new_by_id.get(item_id, 0)
        db.execute(
            "UPDATE inventory SET quantity = quantity + ? - ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (old_quantity, new_quantity, item_id),
        )
    db.execute("DELETE FROM seva_items WHERE seva_id = ?", (seva_id,))
    db.executemany(
        "INSERT INTO seva_items (seva_id, inventory_id, quantity_used) VALUES (?, ?, ?)",
        [(seva_id, item["id"], item["quantity"]) for item in new_items],
    )


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

    def do_GET(self):
        parsed = urlparse(self.path)
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
                    SELECT id, name, location, seva_date, created_at
                    FROM sevas
                    ORDER BY seva_date DESC, created_at DESC, id DESC
                """)]
                for seva in sevas:
                    seva["items"] = [dict(row) for row in db.execute("""
                        SELECT seva_items.inventory_id AS id,
                               COALESCE(inventory.name, 'Deleted item') AS name,
                               seva_items.quantity_used
                        FROM seva_items
                        LEFT JOIN inventory ON inventory.id = seva_items.inventory_id
                        WHERE seva_items.seva_id = ?
                        ORDER BY inventory.name COLLATE NOCASE
                    """, (seva["id"],))]
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
                self.json_response({"error": "That user name already exists.", "fields": {"username": "Choose another user name."}}, 409)
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
            self.json_response({"error": "Invalid user name or password."}, 401)
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
                    "INSERT INTO sevas (name, location, seva_date) VALUES (?, ?, ?)",
                    (values["name"], values["location"], values["seva_date"]),
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
                    "UPDATE sevas SET name = ?, location = ?, seva_date = ? WHERE id = ?",
                    (values["name"], values["location"], values["seva_date"], seva_id),
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
            cursor = db.execute("DELETE FROM inventory WHERE id = ?", (item_id,))
        if cursor.rowcount == 0:
            self.json_response({"error": "Item not found."}, 404)
        else:
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


if __name__ == "__main__":
    initialize_database()
    server = ThreadingHTTPServer(("127.0.0.1", 8000), InventoryHandler)
    print("Inventory Master is running at http://127.0.0.1:8000")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
