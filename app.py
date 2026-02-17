import concurrent.futures
import os
import random
import sqlite3
import subprocess
import threading
import time
import re
import shutil
import ssl
import urllib.error
import urllib.request
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, redirect, render_template, request, url_for

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "pingtool.db"

app = Flask(__name__)
PING_WORKERS = 64
BACKGROUND_STARTED = False
BACKGROUND_LOCK = threading.Lock()
PING_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=PING_WORKERS)
MANUFACTURERS = ["Draytek", "Mikrotik", "TP-Link"]
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "123456")
ALERT_STATE = {"last_event": None, "objects": {}}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def status_class_from_text(raw_status):
    value = str(raw_status or "").lower()
    if any(word in value for word in ["red", "critical", "down", "offline", "disconnected", "not responding"]):
        return "down"
    if any(word in value for word in ["green", "up", "online", "connected", "ok", "normal", "clear"]):
        return "up"
    if any(word in value for word in ["yellow", "warning", "degraded", "amber", "orange"]):
        return "neutral"
    return "neutral"


def vmware_dashboard_status(raw_status):
    value = str(raw_status or "").strip().lower()
    if any(word in value for word in ["red", "critical", "down", "offline", "disconnected", "not responding"]):
        return "Red"
    if any(word in value for word in ["yellow", "warning", "degraded", "amber", "orange"]):
        return "Yellow"
    if any(word in value for word in ["green", "up", "online", "connected", "ok", "normal", "clear"]):
        return "Green"
    return "Unknown"


def parse_vmware_alarm_string(text):
    content = str(text or "").strip()
    compact = re.sub(r"\s+", " ", content)

    lead_match = re.match(
        r"^Alarm\s+(.+?)\s+on\s+([^:]+):\s*(.+)$",
        compact,
        flags=re.IGNORECASE,
    )

    if not lead_match:
        return {
            "key": "VMware Alarm",
            "status": "Unknown",
            "message": content or "VMware alarm received (unparsed format)",
            "status_class": "neutral",
        }

    alarm_name = lead_match.group(1).strip()
    tail = lead_match.group(3).strip()
    status_match = re.search(r"\bstatus\s*[=:]\s*([^,;]+)", tail, flags=re.IGNORECASE)
    raw_status = status_match.group(1).strip() if status_match else "Unknown"

    key = re.sub(r"\s+status\s*[=:].*$", "", tail, flags=re.IGNORECASE).strip() or "VMware Alarm"
    dashboard_status = vmware_dashboard_status(raw_status)
    status_class = status_class_from_text(f"{dashboard_status} {raw_status}")

    return {
        "key": key,
        "status": dashboard_status,
        "message": f"{alarm_name} on {key}: {dashboard_status}",
        "status_class": status_class,
    }


def parse_vmware_payload(req):
    payload = req.get_json(silent=True)
    if isinstance(payload, dict):
        return str(payload.get("text") or payload.get("message") or payload.get("body") or "")

    form_body = req.form.get("body") if req.form else ""
    if form_body:
        return str(form_body)

    return req.get_data(as_text=True)


def require_webhook_token(req):
    token = (
        req.args.get("token")
        or req.headers.get("x-webhook-token")
        or req.headers.get("authorization", "").replace("Bearer ", "").strip()
    )
    return bool(token) and token == WEBHOOK_TOKEN


def broadcast_update(key, status, message, source, status_class):
    event = {
        "key": key,
        "status": status,
        "message": message,
        "source": source,
        "timestamp": now_iso(),
        "statusClass": status_class,
    }
    ALERT_STATE["last_event"] = event
    ALERT_STATE["objects"][key] = {
        "status": status,
        "lastChange": event["timestamp"],
        "message": message,
        "source": source,
        "statusClass": status_class,
    }


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
                supplier TEXT,
                host_type TEXT,
                post_code TEXT,
                web_url TEXT,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
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
        if "supplier" not in columns:
            connection.execute("ALTER TABLE hosts ADD COLUMN supplier TEXT;")
        if "host_type" not in columns:
            connection.execute("ALTER TABLE hosts ADD COLUMN host_type TEXT;")
        if "post_code" not in columns:
            connection.execute("ALTER TABLE hosts ADD COLUMN post_code TEXT;")
        if "failed_attempts" not in columns:
            connection.execute(
                "ALTER TABLE hosts ADD COLUMN failed_attempts INTEGER NOT NULL DEFAULT 0;"
            )
        connection.execute("UPDATE hosts SET failed_attempts = 0 WHERE failed_attempts IS NULL")
        connection.commit()


def resolve_command(binary_name, candidates):
    found = shutil.which(binary_name)
    if found:
        return found
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


def extract_received_packets(output):
    linux_match = re.search(
        r"(\d+)\s+packets transmitted,\s*(\d+)\s+(?:packets )?received",
        output,
        re.IGNORECASE,
    )
    if linux_match:
        return int(linux_match.group(2))

    windows_match = re.search(r"Received\s*=\s*(\d+)", output, re.IGNORECASE)
    if windows_match:
        return int(windows_match.group(1))

    return None


def ping_host(ip_address, count=4):
    ping_binary = resolve_command("ping", ["/bin/ping", "/usr/bin/ping", "/sbin/ping"])
    if not ping_binary:
        return False

    if os.name == "nt":
        command = [ping_binary, "-n", str(count), "-w", "1000", ip_address]
    else:
        command = [ping_binary, "-c", str(count), "-W", "2", ip_address]

    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError:
        return False

    output = result.stdout + result.stderr
    received = extract_received_packets(output)
    if received is None:
        # Fallback for uncommon ping output formats.
        return result.returncode == 0

    return received / count >= 0.5


def check_web_proof_of_life(web_url):
    if not web_url:
        return False

    # Prefer Python stdlib HTTP check so we do not depend on curl being installed.
    try:
        request = urllib.request.Request(
            web_url,
            method="HEAD",
            headers={"User-Agent": "Pingtool/1.0"},
        )
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(request, timeout=10, context=context) as response:
            return response.status == 200
    except urllib.error.HTTPError as error:
        return error.code == 200
    except Exception:
        pass

    curl_binary = resolve_command("curl", ["/usr/bin/curl", "/bin/curl"])
    if not curl_binary:
        return False

    command = [
        curl_binary,
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
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError:
        return False
    if result.returncode != 0:
        return False
    return result.stdout.strip() == "200"


def evaluate_host(host):
    ping_ok = ping_host(host["ip_address"])
    if ping_ok:
        return host["id"], True
    proof_ok = check_web_proof_of_life(host["web_url"])
    return host["id"], proof_ok


def schedule_next_ping(last_success_at, is_up, failed_attempts):
    if is_up:
        base_interval = 60
    elif last_success_at or failed_attempts < 4:
        base_interval = 60
    else:
        base_interval = 3600
    jitter = random.randint(0, max(10, base_interval // 10))
    return (datetime.now(timezone.utc) + timedelta(seconds=base_interval + jitter)).isoformat(
        timespec="seconds"
    )


def update_host_status(host_id, is_up, last_success_at, failed_attempts):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    safe_failed_attempts = failed_attempts or 0
    new_failed_attempts = 0 if is_up else safe_failed_attempts + (0 if last_success_at else 1)
    next_ping_at = schedule_next_ping(last_success_at, is_up, new_failed_attempts)
    with get_db_connection() as connection:
        if is_up:
            connection.execute(
                """
                UPDATE hosts
                SET last_status = 1,
                    failed_attempts = 0,
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
                    failed_attempts = ?,
                    last_checked_at = ?,
                    next_ping_at = ?
                WHERE id = ?
                """,
                (new_failed_attempts, timestamp, next_ping_at, host_id),
            )
        connection.commit()


def parse_bulk_hosts(raw_text):
    hosts = []
    for line in raw_text.splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue

        # CSV format: hostname,ip_address[,post_code]
        if "," in cleaned:
            parts = [part.strip() for part in cleaned.split(",")]
            if len(parts) < 2:
                continue
            hostname = parts[0]
            ip_address = parts[1]
            post_code = parts[2] if len(parts) >= 3 and parts[2] else None
        else:
            # Fallback format: "hostname ip_address" (no per-host postcode)
            parts = cleaned.split(None, 1)
            if len(parts) != 2:
                continue
            hostname, ip_address = parts
            post_code = None

        if hostname and ip_address:
            hosts.append((hostname, ip_address, post_code))
    return hosts


def schedule_new_host():
    jitter = random.randint(5, 20)
    return (datetime.now(timezone.utc) + timedelta(seconds=jitter)).isoformat(
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
        supplier = request.form.get("supplier", "").strip() or None
        host_type = request.form.get("host_type", "").strip() or None
        post_code = request.form.get("post_code", "").strip() or None
        web_url = request.form.get("web_url", "").strip() or None
        if hostname and ip_address:
            with get_db_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO hosts (
                        hostname,
                        ip_address,
                        manufacturer,
                        supplier,
                        host_type,
                        post_code,
                        web_url,
                        failed_attempts,
                        next_ping_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        hostname,
                        ip_address,
                        manufacturer,
                        supplier,
                        host_type,
                        post_code,
                        web_url,
                        schedule_new_host(),
                    ),
                )
                host_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                connection.commit()
            with get_db_connection() as connection:
                host = connection.execute(
                    "SELECT * FROM hosts WHERE id = ?", (host_id,)
                ).fetchone()
            if host:
                _, is_up = evaluate_host(host)
                update_host_status(host_id, is_up, host["last_success_at"], host["failed_attempts"] or 0)
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
    supplier = request.form.get("bulk_supplier", "").strip() or None
    host_type = request.form.get("bulk_host_type", "").strip() or None
    post_code = request.form.get("bulk_post_code", "").strip() or None
    hosts = parse_bulk_hosts(bulk_text)
    if hosts:
        scheduled_hosts = [
            (hostname, ip_address, manufacturer, supplier, host_type, line_post_code or post_code, None, schedule_new_host())
            for hostname, ip_address, line_post_code in hosts
        ]
        with get_db_connection() as connection:
            start_id = connection.execute("SELECT COALESCE(MAX(id), 0) FROM hosts").fetchone()[0]
            connection.executemany(
                """
                INSERT INTO hosts (
                    hostname,
                    ip_address,
                    manufacturer,
                    supplier,
                    host_type,
                    post_code,
                    web_url,
                    failed_attempts,
                    next_ping_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                scheduled_hosts,
            )
            connection.commit()

        with get_db_connection() as connection:
            new_hosts = connection.execute(
                "SELECT * FROM hosts WHERE id > ? ORDER BY id",
                (start_id,),
            ).fetchall()

        for host in new_hosts:
            _, is_up = evaluate_host(host)
            update_host_status(
                host["id"],
                is_up,
                host["last_success_at"],
                host["failed_attempts"],
            )
    return redirect(url_for("index"))


@app.route("/ping/<int:host_id>")
def ping_single(host_id):
    with get_db_connection() as connection:
        host = connection.execute(
            "SELECT * FROM hosts WHERE id = ?", (host_id,)
        ).fetchone()
    if host:
        _, is_up = evaluate_host(host)
        update_host_status(host_id, is_up, host["last_success_at"], host["failed_attempts"] or 0)
    return redirect(url_for("index"))




@app.route("/hosts/<int:host_id>/edit", methods=["POST"])
def edit_host(host_id):
    hostname = request.form.get("hostname", "").strip()
    ip_address = request.form.get("ip_address", "").strip()
    manufacturer = request.form.get("manufacturer", "Draytek")
    supplier = request.form.get("supplier", "").strip() or None
    host_type = request.form.get("host_type", "").strip() or None
    post_code = request.form.get("post_code", "").strip() or None
    web_url = request.form.get("web_url", "").strip() or None
    if hostname and ip_address:
        with get_db_connection() as connection:
            connection.execute(
                """
                UPDATE hosts
                SET hostname = ?,
                    ip_address = ?,
                    manufacturer = ?,
                    supplier = ?,
                    host_type = ?,
                    post_code = ?,
                    web_url = ?
                WHERE id = ?
                """,
                (
                    hostname,
                    ip_address,
                    manufacturer,
                    supplier,
                    host_type,
                    post_code,
                    web_url,
                    host_id,
                ),
            )
            connection.commit()
    return redirect(url_for("index"))


@app.route("/hosts/bulk_edit", methods=["POST"])
def bulk_edit_hosts():
    selected_ids = [
        int(host_id)
        for host_id in request.form.getlist("host_ids")
        if host_id.isdigit()
    ]
    if not selected_ids:
        return redirect(url_for("index"))

    apply_all = request.form.get("apply_all") == "1"
    manufacturer = request.form.get("bulk_edit_manufacturer", "").strip()
    supplier = request.form.get("bulk_edit_supplier", "").strip()
    host_type = request.form.get("bulk_edit_host_type", "").strip()

    if apply_all:
        where_clause = ""
        params = []
    else:
        placeholders = ",".join(["?"] * len(selected_ids))
        where_clause = f" WHERE id IN ({placeholders})"
        params = selected_ids

    updates = []
    values = []
    if manufacturer:
        updates.append("manufacturer = ?")
        values.append(manufacturer)
    if supplier:
        updates.append("supplier = ?")
        values.append(supplier)
    if host_type:
        updates.append("host_type = ?")
        values.append(host_type)

    if updates:
        query = "UPDATE hosts SET " + ", ".join(updates) + where_clause
        with get_db_connection() as connection:
            connection.execute(query, tuple(values + params))
            connection.commit()

    return redirect(url_for("index"))




@app.route("/hosts/bulk_delete", methods=["POST"])
def bulk_delete_hosts():
    selected_ids = [
        int(host_id)
        for host_id in request.form.getlist("host_ids")
        if host_id.isdigit()
    ]
    apply_all = request.form.get("apply_all") == "1"

    with get_db_connection() as connection:
        if apply_all:
            connection.execute("DELETE FROM hosts")
        elif selected_ids:
            placeholders = ",".join(["?"] * len(selected_ids))
            connection.execute(
                f"DELETE FROM hosts WHERE id IN ({placeholders})",
                selected_ids,
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
            SELECT id, hostname, ip_address, manufacturer, supplier, host_type, post_code, web_url, failed_attempts, last_success_at, next_ping_at
            FROM hosts
            WHERE next_ping_at IS NULL OR next_ping_at <= ?
            """,
            (now,),
        ).fetchall()
    if not hosts:
        return
    futures = [PING_EXECUTOR.submit(evaluate_host, host) for host in hosts]
    last_success_map = {host["id"]: host["last_success_at"] for host in hosts}
    failed_attempts_map = {host["id"]: (host["failed_attempts"] or 0) for host in hosts}
    for future in concurrent.futures.as_completed(futures):
        try:
            host_id, is_up = future.result()
        except Exception:
            continue
        update_host_status(
            host_id,
            is_up,
            last_success_map.get(host_id),
            failed_attempts_map.get(host_id, 0),
        )


@app.route("/ping_all")
def ping_all():
    ping_all_hosts()
    return redirect(url_for("index"))


def get_down_host_cards(show_unknown):
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
                "supplier": host["supplier"],
                "post_code": host["post_code"],
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
    return formatted_hosts


@app.route("/dashboard")
def dashboard():
    show_unknown = request.args.get("show_unknown") == "1"
    return render_template(
        "dashboard.html",
        hosts=get_down_host_cards(show_unknown),
        show_unknown=show_unknown,
    )


@app.route("/dashboard/plain")
def dashboard_plain():
    show_unknown = request.args.get("show_unknown") == "1"
    return render_template(
        "dashboard_plain.html",
        hosts=get_down_host_cards(show_unknown),
        show_unknown=show_unknown,
    )


@app.route("/alerts")
def alerts_page():
    objects = [
        {"key": key, **value}
        for key, value in ALERT_STATE["objects"].items()
    ]
    objects.sort(key=lambda item: item.get("lastChange", ""), reverse=True)
    return render_template(
        "alerts.html",
        last_event=ALERT_STATE["last_event"],
        objects=objects,
    )


@app.route("/api/alerts/state")
def alerts_state():
    objects = [
        {"key": key, **value}
        for key, value in ALERT_STATE["objects"].items()
    ]
    objects.sort(key=lambda item: item.get("lastChange", ""), reverse=True)
    return {
        "lastEvent": ALERT_STATE["last_event"],
        "objects": objects,
    }


@app.route("/webhook/vmware", methods=["POST"])
def webhook_vmware():
    if not require_webhook_token(request):
        return {"ok": False, "error": "Unauthorized"}, 401

    text_body = parse_vmware_payload(request)
    parsed = parse_vmware_alarm_string(text_body)

    broadcast_update(
        parsed["key"],
        parsed["status"],
        parsed["message"],
        "VMware",
        parsed["status_class"],
    )
    return {"ok": True}


@app.route("/webhook/omada", methods=["POST"])
def webhook_omada():
    if not require_webhook_token(request):
        return {"ok": False, "error": "Unauthorized"}, 401

    payload = request.get_json(silent=True) or {}
    key = str(payload.get("Controller") or "Omada")
    status = str(payload.get("operation") or "Omada Event")
    message = json.dumps(payload, ensure_ascii=False)
    status_class = status_class_from_text(status)
    broadcast_update(key, status, message, "Omada", status_class)
    return {"ok": True}


@app.route("/webhook/zabbix", methods=["POST"])
def webhook_zabbix():
    if not require_webhook_token(request):
        return {"ok": False, "error": "Unauthorized"}, 401

    payload = request.get_json(silent=True) or {}
    key = str(payload.get("host") or payload.get("Host") or "Zabbix Host")
    status = str(payload.get("severity") or payload.get("event_status") or "Zabbix Event")
    message = str(payload.get("subject") or payload.get("message") or "")
    status_class = status_class_from_text(status + " " + message)
    broadcast_update(key, status, message, "Zabbix", status_class)
    return {"ok": True}


def background_ping_loop():
    while True:
        started_at = time.monotonic()
        ping_all_hosts()
        elapsed = time.monotonic() - started_at
        time.sleep(max(0, 60 - elapsed))


def start_background_pinger():
    global BACKGROUND_STARTED
    with BACKGROUND_LOCK:
        if BACKGROUND_STARTED:
            return
        thread = threading.Thread(target=background_ping_loop, daemon=True)
        thread.start()
        BACKGROUND_STARTED = True


def boot_application(start_background=True):
    init_db()
    if start_background:
        start_background_pinger()


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    boot_application(start_background=(os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not debug))
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=debug)
