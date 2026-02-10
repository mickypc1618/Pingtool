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
- Hosts have three statuses:
  - `Up`: responds to ping (or proof-of-life fallback if ping fails).
  - `Down`: no ping response now, but has a historical successful ping.
  - `Unknown`: has never had a successful ping.
- If ping fails, Pingtool will optionally use a web proof-of-life check (`curl -k`) against the configured `Web URL` and treat HTTP `200` as up.
- Unknown hosts are scheduled every hour; known hosts are scheduled every minute, with jitter so checks are staggered.
- Timestamps are stored in UTC ISO-8601 format.

## Manufacturer notes

- **Draytek**: Use `Web URL` proof of life (for example: `https://138.248.139.155:4433/`).
- **Mikrotik**: Suggested alternative is RouterOS API health checks (API/REST endpoint if enabled), since web login endpoints may not always return deterministic `200` health responses.
- **TP-Link (Omada)**: Suggested approach is integrating with Omada Controller API for device status rather than only checking a per-device web page.
