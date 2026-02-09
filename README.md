# Pingtool

Pingtool is a lightweight Flask application that stores hosts in a SQLite database and provides a dashboard for down hosts with their last successful ping time.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python app.py
```

Then visit:
- `http://localhost:5000/` to add hosts and trigger pings.
- `http://localhost:5000/dashboard` to view down hosts and last successful ping times.

## Notes

- Ping uses the system `ping` command, so ensure it is available on your host.
- A background job pings every minute with 4 packets; hosts are marked down if fewer than half respond.
- Timestamps are stored in UTC ISO-8601 format.
