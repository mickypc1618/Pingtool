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

Host records now include additional metadata: `Supplier`, `Type`, and `Post Code` (available in both single-add and bulk upload forms).

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
