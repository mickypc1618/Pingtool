import concurrent.futures
import os
import random
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, redirect, render_template, request, url_for

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "pingtool.db"

app = Flask(__name__)
PING_WORKERS = 64
PING_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=PING_WORKERS)
MANUFACTURERS = ["Draytek", "Mikrotik", "TP-Link"]


def get_db_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    DATA_DIR.mkdir(exist_ok=True)
    with get_db_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS hosts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hostname TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                manufacturer TEXT NOT NULL DEFAULT 'Draytek',
                web_url TEXT,
                last_status INTEGER NOT NULL DEFAULT 0,
                last_checked_at TEXT,
                last_success_at TEXT,
                next_ping_at TEXT
            );
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(hosts)").fetchall()
        }
        if "next_ping_at" not in columns:
            connection.execute("ALTER TABLE hosts ADD COLUMN next_ping_at TEXT;")
        if "manufacturer" not in columns:
            connection.execute(
                "ALTER TABLE hosts ADD COLUMN manufacturer TEXT NOT NULL DEFAULT 'Draytek';"
            )
        if "web_url" not in columns:
            connection.execute("ALTER TABLE hosts ADD COLUMN web_url TEXT;")
        connection.commit()


def ping_host(ip_address, count=4):
    if os.name == "nt":
        command = ["ping", "-n", str(count), "-w", "1000", ip_address]
    else:
        command = ["ping", "-c", str(count), "-W", "1", ip_address]
    result = subprocess.run(command, capture_output=True, text=True)
    output = result.stdout + result.stderr
    received = 0
    for line in output.splitlines():
        if "received" in line and "packets transmitted" in line:
            parts = line.split(",")
            for part in parts:
                if "received" in part:
                    received = int(part.strip().split(" ")[0])
            break
        if "Received = " in line:
            for part in line.split(","):
                if "Received =" in part:
                    received = int(part.split("=")[1].strip())
            break
    return received / count >= 0.5


def check_web_proof_of_life(web_url):
    if not web_url:
        return False
    command = [
        "curl",
        "-k",
        "-L",
        "--max-time",
        "10",
        "--silent",
        "--output",
        "/dev/null",
        "--write-out",
        "%{http_code}",
        web_url,
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        return False
    return result.stdout.strip() == "200"


def evaluate_host(host):
    ping_ok = ping_host(host["ip_address"])
    if ping_ok:
        return host["id"], True
    proof_ok = check_web_proof_of_life(host["web_url"])
    return host["id"], proof_ok


def schedule_next_ping(last_success_at, is_up):
    if is_up:
        base_interval = 60
    elif last_success_at:
        base_interval = 60
    else:
        base_interval = 3600
    jitter = random.randint(0, max(10, base_interval // 10))
    return (datetime.now(timezone.utc) + timedelta(seconds=base_interval + jitter)).isoformat(
        timespec="seconds"
    )


def update_host_status(host_id, is_up, last_success_at):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    next_ping_at = schedule_next_ping(last_success_at, is_up)
    with get_db_connection() as connection:
        if is_up:
            connection.execute(
                """
                UPDATE hosts
                SET last_status = 1,
                    last_checked_at = ?,
                    last_success_at = ?,
                    next_ping_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, next_ping_at, host_id),
            )
        else:
            connection.execute(
                """
                UPDATE hosts
                SET last_status = 0,
                    last_checked_at = ?,
                    next_ping_at = ?
                WHERE id = ?
                """,
                (timestamp, next_ping_at, host_id),
            )
        connection.commit()


def parse_bulk_hosts(raw_text):
    hosts = []
    for line in raw_text.splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        if "," in cleaned:
            parts = [part.strip() for part in cleaned.split(",", 1)]
        else:
            parts = cleaned.split(None, 1)
        if len(parts) != 2:
            continue
        hostname, ip_address = parts
        if hostname and ip_address:
            hosts.append((hostname, ip_address))
    return hosts


def schedule_new_host():
    jitter = random.randint(0, 600)
    return (datetime.now(timezone.utc) + timedelta(seconds=3600 + jitter)).isoformat(
        timespec="seconds"
    )


def parse_db_timestamp(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_duration(seconds):
    if seconds is None:
        return "No successful ping yet"
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remainder}s"
    hours, remainder = divmod(minutes, 60)
    return f"{hours}h {remainder}m"


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        hostname = request.form.get("hostname", "").strip()
        ip_address = request.form.get("ip_address", "").strip()
        manufacturer = request.form.get("manufacturer", "Draytek")
        web_url = request.form.get("web_url", "").strip() or None
        if hostname and ip_address:
            with get_db_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO hosts (hostname, ip_address, manufacturer, web_url, next_ping_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (hostname, ip_address, manufacturer, web_url, schedule_new_host()),
                )
                connection.commit()
        return redirect(url_for("index"))

    with get_db_connection() as connection:
        hosts = connection.execute("SELECT * FROM hosts ORDER BY hostname").fetchall()
    edit_id = request.args.get("edit_id", type=int)
    return render_template(
        "index.html",
        hosts=hosts,
        manufacturers=MANUFACTURERS,
        edit_id=edit_id,
    )


@app.route("/bulk_upload", methods=["POST"])
def bulk_upload():
    bulk_text = request.form.get("bulk_hosts", "")
    manufacturer = request.form.get("bulk_manufacturer", "Draytek")
    hosts = parse_bulk_hosts(bulk_text)
    if hosts:
        scheduled_hosts = [
            (hostname, ip_address, manufacturer, None, schedule_new_host())
            for hostname, ip_address in hosts
        ]
        with get_db_connection() as connection:
            connection.executemany(
                """
                INSERT INTO hosts (hostname, ip_address, manufacturer, web_url, next_ping_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                scheduled_hosts,
            )
            connection.commit()
    return redirect(url_for("index"))


@app.route("/ping/<int:host_id>")
def ping_single(host_id):
    with get_db_connection() as connection:
        host = connection.execute(
            "SELECT * FROM hosts WHERE id = ?", (host_id,)
        ).fetchone()
    if host:
        _, is_up = evaluate_host(host)
        update_host_status(host_id, is_up, host["last_success_at"])
    return redirect(url_for("index"))




@app.route("/hosts/<int:host_id>/edit", methods=["POST"])
def edit_host(host_id):
    hostname = request.form.get("hostname", "").strip()
    ip_address = request.form.get("ip_address", "").strip()
    manufacturer = request.form.get("manufacturer", "Draytek")
    web_url = request.form.get("web_url", "").strip() or None
    if hostname and ip_address:
        with get_db_connection() as connection:
            connection.execute(
                """
                UPDATE hosts
                SET hostname = ?,
                    ip_address = ?,
                    manufacturer = ?,
                    web_url = ?
                WHERE id = ?
                """,
                (hostname, ip_address, manufacturer, web_url, host_id),
            )
            connection.commit()
    return redirect(url_for("index"))


@app.route("/hosts/<int:host_id>/delete", methods=["POST"])
def delete_host(host_id):
    with get_db_connection() as connection:
        connection.execute("DELETE FROM hosts WHERE id = ?", (host_id,))
        connection.commit()
    return redirect(url_for("index"))


def ping_all_hosts():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_db_connection() as connection:
        hosts = connection.execute(
            """
            SELECT id, hostname, ip_address, manufacturer, web_url, last_success_at, next_ping_at
            FROM hosts
            WHERE next_ping_at IS NULL OR next_ping_at <= ?
            """,
            (now,),
        ).fetchall()
    if not hosts:
        return
    futures = [PING_EXECUTOR.submit(evaluate_host, host) for host in hosts]
    last_success_map = {host["id"]: host["last_success_at"] for host in hosts}
    for future in concurrent.futures.as_completed(futures):
        host_id, is_up = future.result()
        update_host_status(host_id, is_up, last_success_map.get(host_id))


@app.route("/ping_all")
def ping_all():
    ping_all_hosts()
    return redirect(url_for("index"))


@app.route("/dashboard")
def dashboard():
    show_unknown = request.args.get("show_unknown") == "1"
    with get_db_connection() as connection:
        down_hosts = connection.execute(
            """
            SELECT * FROM hosts
            WHERE last_status = 0
            """
        ).fetchall()
    now = datetime.now(timezone.utc)
    formatted_hosts = []
    for host in down_hosts:
        last_success_at = host["last_success_at"]
        reference_time = last_success_at
        downtime_seconds = None
        if reference_time:
            try:
                parsed_time = parse_db_timestamp(reference_time)
                if parsed_time is not None:
                    downtime_seconds = int((now - parsed_time).total_seconds())
            except ValueError:
                downtime_seconds = None
        if downtime_seconds is None and not show_unknown:
            continue
        formatted_hosts.append(
            {
                "hostname": host["hostname"],
                "ip_address": host["ip_address"],
                "last_checked_at": host["last_checked_at"],
                "last_success_at": host["last_success_at"],
                "downtime_seconds": downtime_seconds,
                "downtime_display": format_duration(downtime_seconds),
            }
        )
    formatted_hosts.sort(
        key=lambda item: (
            item["downtime_seconds"] is None,
            item["downtime_seconds"] or 0,
        )
    )
    return render_template(
        "dashboard.html", hosts=formatted_hosts, show_unknown=show_unknown
    )


def background_ping_loop():
    while True:
        started_at = time.monotonic()
        ping_all_hosts()
        elapsed = time.monotonic() - started_at
        time.sleep(max(0, 60 - elapsed))


def start_background_pinger():
    thread = threading.Thread(target=background_ping_loop, daemon=True)
    thread.start()


if __name__ == "__main__":
    init_db()
    debug = True
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not debug:
        start_background_pinger()
    app.run(host="0.0.0.0", port=5000, debug=debug)
