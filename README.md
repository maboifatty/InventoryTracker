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
- Stock status and reorder alerts
- Persistent local SQLite storage

## API

- `POST /api/users`
- `POST /api/login`
- `POST /api/logout`
- `GET /api/items` (supports `search` and `status` query parameters)
- `POST /api/items`
- `PUT /api/items/{id}`
- `DELETE /api/items/{id}`
