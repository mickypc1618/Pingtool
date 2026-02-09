import concurrent.futures
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, redirect, render_template, request, url_for

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "pingtool.db"

app = Flask(__name__)


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
                last_status INTEGER NOT NULL DEFAULT 0,
                last_checked_at TEXT,
                last_success_at TEXT
            );
            """
        )
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


def ping_host_task(payload):
    host_id, ip_address = payload
    return host_id, ping_host(ip_address)


def update_host_status(host_id, is_up):
    timestamp = datetime.utcnow().isoformat(timespec="seconds")
    with get_db_connection() as connection:
        if is_up:
            connection.execute(
                """
                UPDATE hosts
                SET last_status = 1,
                    last_checked_at = ?,
                    last_success_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, host_id),
            )
        else:
            connection.execute(
                """
                UPDATE hosts
                SET last_status = 0,
                    last_checked_at = ?
                WHERE id = ?
                """,
                (timestamp, host_id),
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


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        hostname = request.form.get("hostname", "").strip()
        ip_address = request.form.get("ip_address", "").strip()
        if hostname and ip_address:
            with get_db_connection() as connection:
                connection.execute(
                    "INSERT INTO hosts (hostname, ip_address) VALUES (?, ?)",
                    (hostname, ip_address),
                )
                connection.commit()
        return redirect(url_for("index"))

    with get_db_connection() as connection:
        hosts = connection.execute("SELECT * FROM hosts ORDER BY hostname").fetchall()
    return render_template("index.html", hosts=hosts)


@app.route("/bulk_upload", methods=["POST"])
def bulk_upload():
    bulk_text = request.form.get("bulk_hosts", "")
    hosts = parse_bulk_hosts(bulk_text)
    if hosts:
        with get_db_connection() as connection:
            connection.executemany(
                "INSERT INTO hosts (hostname, ip_address) VALUES (?, ?)", hosts
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
        is_up = ping_host(host["ip_address"])
        update_host_status(host_id, is_up)
    return redirect(url_for("index"))


@app.route("/hosts/<int:host_id>/delete", methods=["POST"])
def delete_host(host_id):
    with get_db_connection() as connection:
        connection.execute("DELETE FROM hosts WHERE id = ?", (host_id,))
        connection.commit()
    return redirect(url_for("index"))


def ping_all_hosts():
    with get_db_connection() as connection:
        hosts = connection.execute(
            "SELECT id, ip_address FROM hosts"
        ).fetchall()
    if not hosts:
        return
    payloads = [(host["id"], host["ip_address"]) for host in hosts]
    max_workers = min(4, os.cpu_count() or 1, len(payloads))
    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
        for host_id, is_up in executor.map(ping_host_task, payloads):
            update_host_status(host_id, is_up)


@app.route("/ping_all")
def ping_all():
    ping_all_hosts()
    return redirect(url_for("index"))


@app.route("/dashboard")
def dashboard():
    with get_db_connection() as connection:
        down_hosts = connection.execute(
            """
            SELECT * FROM hosts
            WHERE last_status = 0
            ORDER BY hostname
            """
        ).fetchall()
    return render_template("dashboard.html", hosts=down_hosts)


def background_ping_loop():
    while True:
        ping_all_hosts()
        time.sleep(60)


def start_background_pinger():
    thread = threading.Thread(target=background_ping_loop, daemon=True)
    thread.start()


if __name__ == "__main__":
    init_db()
    debug = True
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not debug:
        start_background_pinger()
    app.run(host="0.0.0.0", port=5000, debug=debug)
