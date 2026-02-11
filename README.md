# Pingtool

Pingtool is a lightweight Flask application that stores hosts in a SQLite database and provides a dashboard for down hosts with their last successful ping time.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Development run

```bash
python app.py
```

Then visit:
- `http://localhost:5000/` to add hosts and trigger pings.
- `http://localhost:5000/dashboard` to view down hosts and last successful ping times.

Host records now include additional metadata: `Supplier`, `Type`, and `Post Code` (available in both single-add and bulk upload forms).

## Production run (WSGI)

Use Gunicorn with the provided WSGI entrypoint:

```bash
gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 8 wsgi:application
```

### Optional systemd service

Create `/etc/systemd/system/pingtool.service`:

```ini
[Unit]
Description=Pingtool Gunicorn Service
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/opt/Pingtool
Environment="PATH=/opt/Pingtool/.venv/bin"
ExecStart=/opt/Pingtool/.venv/bin/gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 8 wsgi:application
Restart=always

[Install]
WantedBy=multi-user.target
```

Then enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable pingtool
sudo systemctl start pingtool
```

## Notes

- Ping uses the system `ping` command, so ensure it is available on your host.
- Hosts have four lifecycle statuses:
  - `New`: newly added, never successful yet, and fewer than 4 failed checks.
  - `Up`: responds to ping (or proof-of-life fallback if ping fails).
  - `Down`: no response now, but has a historical successful ping.
  - `Unknown`: never had a successful ping after 4 failed checks.
- If ping fails, Pingtool will optionally use a web proof-of-life check (`curl -k`) against the configured `Web URL` and treat HTTP `200` as up.
- New hosts are checked as soon as possible after creation; if they fail 4 checks they become Unknown.
- Unknown hosts are scheduled every hour; known hosts are scheduled every minute, with jitter so checks are staggered.
- Timestamps are stored in UTC ISO-8601 format.

## Manufacturer notes

- **Draytek**: Use `Web URL` proof of life (for example: `https://203.0.113.10:4433/`).
- **Mikrotik**: Suggested alternative is RouterOS API health checks (API/REST endpoint if enabled), since web login endpoints may not always return deterministic `200` health responses.
- **TP-Link (Omada)**: Suggested approach is integrating with Omada Controller API for device status rather than only checking a per-device web page.
