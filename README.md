# InventoryTracker

Inventory Tracking app which allows manual and automatic updating based on seva descriptions.

## Inventory Master

A dependency-free three-tier inventory management application:

- **Presentation tier:** responsive HTML, CSS, and JavaScript interface
- **Application tier:** Python HTTP and JSON API
- **Data tier:** SQLite database (`inventory.db`, created automatically)

## Run

```bash
python3 app.py
```

Open <http://127.0.0.1:8000> in a browser. Stop the server with `Ctrl+C`.

## Features

- Login page with create-user option
- Password visibility toggle
- User credentials stored locally in SQLite with password hashing
- Create, edit, search, filter, and delete inventory master records
- Bulk-correct inventory stock and minimum threshold values
- Record sevas and subtract used quantities from inventory stock
- View a read-only log of past sevas, dates, and items used
- Stock status and reorder alerts
- Restock email notification support for items needing attention
- Persistent local SQLite storage

## Email notifications

Restock emails are sent to `communitysevainventory@gmail.com` when an item newly enters the needs-attention state. Configure SMTP before starting the app:

```bash
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USER="communitysevainventory@gmail.com"
export SMTP_PASSWORD="your-gmail-app-password"
export SMTP_FROM="communitysevainventory@gmail.com"
python3 app.py
```

For Gmail, use an app password rather than the normal account password.

To test only the email settings, run:

```bash
python3 test_email.py
```

## API

- `POST /api/users`
- `POST /api/login`
- `POST /api/logout`
- `GET /api/sevas`
- `POST /api/sevas`
- `GET /api/items` (supports `search` and `status` query parameters)
- `POST /api/items`
- `PUT /api/items/{id}`
- `DELETE /api/items/{id}`
