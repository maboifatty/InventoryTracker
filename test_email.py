#!/usr/bin/env python3
"""Send a test email using the same SMTP settings as the inventory app."""

from email.message import EmailMessage
import os
import smtplib
from pathlib import Path


TO_ADDRESS = "communitysevainventory@gmail.com"
ROOT = Path(__file__).resolve().parent


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


def main():
    load_dotenv()
    host = os.environ.get("SMTP_HOST", "").strip()
    username = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", username or TO_ADDRESS).strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    use_tls = os.environ.get("SMTP_USE_TLS", "true").lower() != "false"

    missing = [
        name
        for name, value in {
            "SMTP_HOST": host,
            "SMTP_USER": username,
            "SMTP_PASSWORD": password,
            "SMTP_FROM": sender,
        }.items()
        if not value
    ]
    if missing:
        raise SystemExit(f"Missing SMTP setting(s): {', '.join(missing)}")

    message = EmailMessage()
    message["Subject"] = "InventoryTracker SMTP test"
    message["From"] = sender
    message["To"] = TO_ADDRESS
    message.set_content("This is a test email from InventoryTracker SMTP settings.")

    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if use_tls:
            smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(message)

    print(f"Test email sent to {TO_ADDRESS}.")


if __name__ == "__main__":
    main()
