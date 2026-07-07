#!/usr/bin/env python3
"""Inventory Master - a dependency-free three-tier inventory application."""

import json
import hashlib
import hmac
import secrets
import sqlite3
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "inventory.db"


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
                items = [dict(row) for row in db.execute(sql, args)]
                stats = dict(db.execute("""
                    SELECT COUNT(*) total_items,
                           COALESCE(SUM(quantity), 0) total_units,
                           COALESCE(SUM(quantity * unit_price), 0) total_value,
                           COALESCE(SUM(CASE WHEN quantity <= reorder_level THEN 1 ELSE 0 END), 0) low_stock
                    FROM inventory
                """).fetchone())
            self.json_response({"items": items, "stats": stats})
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
            self.json_response(item, 201)
        except sqlite3.IntegrityError:
            self.json_response({"error": "That SKU already exists.", "fields": {"sku": "SKU must be unique."}}, 409)

    def do_PUT(self):
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
            self.json_response(item)
        except sqlite3.IntegrityError:
            self.json_response({"error": "That SKU already exists.", "fields": {"sku": "SKU must be unique."}}, 409)

    def do_DELETE(self):
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


if __name__ == "__main__":
    initialize_database()
    server = ThreadingHTTPServer(("127.0.0.1", 8000), InventoryHandler)
    print("Inventory Master is running at http://127.0.0.1:8000")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
