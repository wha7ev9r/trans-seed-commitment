#!/usr/bin/env python3
"""quota-guard: Transmission 月度流量配额守护 + 统一控制面板."""

from __future__ import annotations

import base64
import hmac
import json
import logging
import math
import os
import secrets
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from threading import RLock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, jsonify, request, stream_with_context

# ======================================================================
# Config from environment
# ======================================================================
TRANSMISSION_HOST = os.getenv("TRANSMISSION_HOST", "transmission")
TRANSMISSION_PORT = int(os.getenv("TRANSMISSION_PORT", "9091"))
TRANSMISSION_USER = os.getenv("TRANSMISSION_USER", "")
TRANSMISSION_PASS = os.getenv("TRANSMISSION_PASS", "")

QUOTA_USER = os.getenv("QUOTA_USER", "")
QUOTA_PASS = os.getenv("QUOTA_PASS", "")
MONTHLY_QUOTA_BYTES = int(os.getenv("MONTHLY_QUOTA_BYTES", "1099511627776"))

VNSTAT_HOST = os.getenv("VNSTAT_HOST", "vnstat-http")
VNSTAT_PORT = int(os.getenv("VNSTAT_PORT", "8685"))
VNSTAT_INTERFACE = os.getenv("VNSTAT_INTERFACE", "eth0")

TZ_NAME = os.getenv("TZ", "UTC")
TZ_ERROR = ""
try:
    ACCOUNTING_TZ = ZoneInfo(TZ_NAME)
except ZoneInfoNotFoundError:
    ACCOUNTING_TZ = UTC
    TZ_ERROR = f"unknown TZ value: {TZ_NAME}"

STATE_FILE = "/data/state.json"
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
QUOTA_SAFETY_MARGIN_BYTES = int(os.getenv("QUOTA_SAFETY_MARGIN_BYTES", "1073741824"))

RPC_URL = f"http://{TRANSMISSION_HOST}:{TRANSMISSION_PORT}/transmission/rpc"
VNSTAT_URL = f"http://{VNSTAT_HOST}:{VNSTAT_PORT}"
CSRF_TOKEN = secrets.token_urlsafe(32)
LOGGER = logging.getLogger("quota-guard")

# ======================================================================
# Persistent state
# ======================================================================
STATE_LOCK = RLock()
RPC_LOCK = RLock()


def _now() -> datetime:
    return datetime.now(ACCOUNTING_TZ)


def _this_month_key() -> str:
    n = _now()
    return f"{n.year}-{n.month:02d}"


def _quota_stop_threshold(quota_bytes: int) -> int:
    reserve = min(max(QUOTA_SAFETY_MARGIN_BYTES, 0), max(quota_bytes // 10, 0))
    return max(quota_bytes - reserve, 0)


def load_state() -> dict:
    default = {
        "month_key": _this_month_key(),
        "day_key": _now().strftime("%Y-%m-%d"),
        "monthly_uploaded_bytes": 0,
        "today_uploaded_bytes": 0,
        "cumulative_uploaded_bytes": 0,  # Transmission session start
        "manual_paused": False,
        "quota_paused": False,
        "quota_paused_torrent_ids": [],
        "is_paused": False,  # Derived compatibility field for the API/UI.
        "quota_bytes": MONTHLY_QUOTA_BYTES,
        "history": OrderedDict(),  # { "2025-07-01": {"uploaded": 123, "sessions": 45} }
    }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            raise ValueError("state root must be an object")
        for key, fallback in default.items():
            default[key] = saved.get(key, fallback)

        # Old state files had one ambiguous pause flag. Preserve that as a
        # manual pause so upgrading never starts torrents unexpectedly.
        if "manual_paused" not in saved and "quota_paused" not in saved:
            default["manual_paused"] = bool(saved.get("is_paused", False))

        history = default.get("history")
        default["history"] = OrderedDict(history if isinstance(history, dict) else {})
    except (OSError, ValueError, TypeError):
        pass

    for key in (
        "monthly_uploaded_bytes",
        "today_uploaded_bytes",
        "cumulative_uploaded_bytes",
    ):
        try:
            default[key] = max(int(default[key]), 0)
        except (TypeError, ValueError):
            default[key] = 0
    try:
        default["quota_bytes"] = max(int(default["quota_bytes"]), 1)
    except (TypeError, ValueError):
        default["quota_bytes"] = MONTHLY_QUOTA_BYTES

    normalized_history = OrderedDict()
    for date_key, entry in default["history"].items():
        if not isinstance(date_key, str):
            continue
        if isinstance(entry, dict):
            uploaded = entry.get("uploaded", 0)
            sessions = entry.get("sessions", 0)
        else:
            uploaded = entry
            sessions = 0
        try:
            normalized_history[date_key] = {
                "uploaded": max(int(uploaded), 0),
                "sessions": max(int(sessions), 0),
            }
        except (TypeError, ValueError):
            continue
    default["history"] = normalized_history

    paused_ids = default.get("quota_paused_torrent_ids")
    default["quota_paused_torrent_ids"] = (
        [torrent_id for torrent_id in paused_ids if isinstance(torrent_id, int)]
        if isinstance(paused_ids, list)
        else []
    )
    default["manual_paused"] = bool(default.get("manual_paused", False))
    default["quota_paused"] = bool(default.get("quota_paused", False))
    _refresh_pause_state(default)
    return default


def _refresh_pause_state(state: dict) -> None:
    state["is_paused"] = bool(
        state.get("manual_paused", False) or state.get("quota_paused", False)
    )


def save_state(state: dict) -> None:
    with STATE_LOCK:
        _refresh_pause_state(state)
        state["_updated_at"] = _now().isoformat()
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)


# ======================================================================
# Transmission RPC client
# ======================================================================
_session_id: str = ""
_last_rpc_error: str = ""


def _rpc_session() -> requests.Session:
    s = requests.Session()
    if TRANSMISSION_USER:
        s.auth = (TRANSMISSION_USER, TRANSMISSION_PASS)
    return s


def rpc_call(method: str, arguments: dict | None = None) -> dict | None:
    """Call a Transmission RPC method, return the `arguments` dict or None."""
    global _session_id, _last_rpc_error
    payload = {"method": method, "arguments": arguments or {}}
    headers = {"Content-Type": "application/json"}

    with RPC_LOCK:
        local_id = _session_id
    if local_id:
        headers["X-Transmission-Session-Id"] = local_id

    sess = _rpc_session()
    try:
        resp = sess.post(RPC_URL, json=payload, headers=headers, timeout=15)
    except requests.RequestException as exc:
        with RPC_LOCK:
            _last_rpc_error = str(exc)
        return None

    if resp.status_code == 409:
        new_sid = resp.headers.get("X-Transmission-Session-Id", "")
        with RPC_LOCK:
            _session_id = new_sid
        headers["X-Transmission-Session-Id"] = new_sid
        try:
            resp = sess.post(RPC_URL, json=payload, headers=headers, timeout=15)
        except requests.RequestException as exc:
            with RPC_LOCK:
                _last_rpc_error = str(exc)
            return None

    if not resp.ok:
        with RPC_LOCK:
            _last_rpc_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        return None

    try:
        data = resp.json()
    except ValueError as exc:
        with RPC_LOCK:
            _last_rpc_error = f"invalid RPC JSON response: {exc}"
        return None
    if data.get("result") != "success":
        with RPC_LOCK:
            _last_rpc_error = data.get("result", "unknown rpc error")
        return None

    with RPC_LOCK:
        _last_rpc_error = ""
    return data.get("arguments", {})


def get_session_stats() -> dict | None:
    return rpc_call("session-stats")


def get_torrents(ids: list[int] | None = None) -> list[dict] | None:
    """Get torrent list with limited fields for monitoring."""
    fields = [
        "id",
        "name",
        "status",
        "percentDone",
        "rateUpload",
        "rateDownload",
        "uploadedEver",
        "totalSize",
        "addedDate",
        "eta",
    ]
    args: dict = {"fields": fields}
    if ids:
        args["ids"] = ids
    result = rpc_call("torrent-get", args)
    if result is None:
        return None
    return result.get("torrents", [])


def torrent_stop_all() -> bool:
    result = rpc_call("torrent-stop", {})
    return result is not None


def torrent_start(ids: list[int] | None = None) -> bool:
    arguments = {} if ids is None else {"ids": ids}
    result = rpc_call("torrent-start", arguments)
    return result is not None


def torrent_start_all() -> bool:
    return torrent_start()


def session_set_speed(up_kb: int | None = None, down_kb: int | None = None) -> bool:
    args: dict = {}
    if up_kb is not None:
        args["speed-limit-up"] = up_kb
        args["speed-limit-up-enabled"] = True
    if down_kb is not None:
        args["speed-limit-down"] = down_kb
        args["speed-limit-down-enabled"] = down_kb > 0
    result = rpc_call("session-set", args)
    return result is not None


def _get_session_uploaded(stats: dict) -> int:
    val = stats.get("uploadedBytes")
    if val is not None:
        return int(val)
    current_stats = stats.get("current-stats", {})
    return int(current_stats.get("uploadedBytes", 0))


def _get_session_downloaded(stats: dict) -> int:
    val = stats.get("downloadedBytes")
    if val is not None:
        return int(val)
    current_stats = stats.get("current-stats", {})
    return int(current_stats.get("downloadedBytes", 0))


# ======================================================================
# Quota tracking logic
# ======================================================================
def check_quota() -> dict:
    """Poll Transmission stats, accumulate quota, auto-pause if exceeded."""
    stats = get_session_stats()
    if stats is None:
        return {"status": "rpc_error", "error": _last_rpc_error}

    current_total = max(_get_session_uploaded(stats), 0)
    torrents = get_torrents()
    if torrents is None:
        return {"status": "rpc_error", "error": _last_rpc_error}

    with STATE_LOCK:
        state = load_state()
        now = _now()
        today_str = now.strftime("%Y-%m-%d")
        month_key = f"{now.year}-{now.month:02d}"
        previous_total = max(int(state.get("cumulative_uploaded_bytes", 0)), 0)
        month_changed = state["month_key"] != month_key

        if month_changed:
            # Establish the new month's baseline at the current Transmission
            # session total; never charge uploads from the previous month.
            state["month_key"] = month_key
            state["monthly_uploaded_bytes"] = 0
            state["today_uploaded_bytes"] = 0
            state["cumulative_uploaded_bytes"] = current_total
            delta = 0
        else:
            # A lower session total means Transmission restarted while the
            # guard was running. Count the new session from zero.
            delta = (
                current_total
                if current_total < previous_total
                else current_total - previous_total
            )
            state["cumulative_uploaded_bytes"] = current_total

        if state.get("day_key") != today_str:
            state["day_key"] = today_str
            state["today_uploaded_bytes"] = 0

        state["monthly_uploaded_bytes"] += delta
        state["today_uploaded_bytes"] += delta

        if today_str not in state["history"]:
            state["history"][today_str] = {"uploaded": 0, "sessions": 0}
        state["history"][today_str]["uploaded"] = state["today_uploaded_bytes"]
        state["history"][today_str]["sessions"] = len(torrents)

        while len(state["history"]) > 90:
            state["history"].popitem(last=False)

        # A quota pause is distinct from a user pause. On month rollover,
        # resume only the torrents that were running before the quota stop.
        if month_changed and state["quota_paused"]:
            paused_ids = state.get("quota_paused_torrent_ids", [])
            if state["manual_paused"]:
                state["quota_paused"] = False
                state["quota_paused_torrent_ids"] = []
            elif not paused_ids or torrent_start(paused_ids):
                state["quota_paused"] = False
                state["quota_paused_torrent_ids"] = []

        if (
            state["monthly_uploaded_bytes"]
            >= _quota_stop_threshold(state["quota_bytes"])
            and not state["quota_paused"]
        ):
            running_ids = [
                torrent["id"]
                for torrent in torrents
                if torrent.get("status", 0) != 0 and isinstance(torrent.get("id"), int)
            ]
            if torrent_stop_all():
                state["quota_paused"] = True
                state["quota_paused_torrent_ids"] = running_ids
        elif (
            state["monthly_uploaded_bytes"]
            < _quota_stop_threshold(state["quota_bytes"])
            and state["quota_paused"]
            and not state["manual_paused"]
            and (
                not state.get("quota_paused_torrent_ids")
                or torrent_start(state["quota_paused_torrent_ids"])
            )
        ):
            state["quota_paused"] = False
            state["quota_paused_torrent_ids"] = []

        save_state(state)

        return {
            "status": "ok",
            "month_key": state["month_key"],
            "uploaded_bytes": state["monthly_uploaded_bytes"],
            "quota_bytes": state["quota_bytes"],
            "quota_stop_threshold_bytes": _quota_stop_threshold(state["quota_bytes"]),
            "is_paused": state["is_paused"],
            "today_uploaded_bytes": state["today_uploaded_bytes"],
            "session_uploaded_bytes": current_total,
        }


# ======================================================================
# vnstat helper
# ======================================================================
def get_vnstat_data() -> dict:
    """Pull vnstat JSON data from the vnstat container."""
    try:
        resp = requests.get(VNSTAT_URL + "/json.cgi", timeout=5)
        if resp.ok:
            return resp.json()
    except requests.RequestException:
        pass
    return {}


def get_vnstat_monthly_total(vnstat_data: dict) -> int | None:
    """Extract total (rx+tx) bytes for current month."""
    try:
        interfaces = vnstat_data.get("interfaces", [])
        if not interfaces:
            return None

        interface = next(
            (
                item
                for item in interfaces
                if item.get("name") == VNSTAT_INTERFACE
                or item.get("id") == VNSTAT_INTERFACE
            ),
            None,
        )
        if interface is None:
            return None

        traffic = interface.get("traffic", {})
        months = traffic.get("month", [])
        current_month = _this_month_key()
        for month in reversed(months):
            date = month.get("date", {})
            if (
                isinstance(date, dict)
                and f"{date.get('year')}-{int(date.get('month')):02d}" == current_month
            ):
                return int(month.get("rx", 0)) + int(month.get("tx", 0))
    except (IndexError, KeyError, TypeError, ValueError):
        pass
    return None


# ======================================================================
# Scheduler
# ======================================================================
_scheduler = BackgroundScheduler(timezone=ACCOUNTING_TZ, daemon=True)
_service_started_monotonic = time.monotonic()
_last_check_at: datetime | None = None
_last_check_status = "starting"
_last_check_error = ""


def _scheduled_check() -> None:
    global _last_check_at, _last_check_error, _last_check_status
    LOGGER.info("quota check running")
    try:
        result = check_quota()
    except Exception as exc:
        _last_check_at = _now()
        _last_check_status = "error"
        _last_check_error = str(exc)
        LOGGER.exception("quota check failed")
        return

    _last_check_at = _now()
    if result["status"] == "rpc_error":
        _last_check_status = "rpc_error"
        _last_check_error = result.get("error", "unknown")
        LOGGER.error("Transmission RPC error: %s", _last_check_error)
        return

    _last_check_status = "ok"
    _last_check_error = ""
    pct = result["uploaded_bytes"] / max(result["quota_bytes"], 1) * 100
    LOGGER.info(
        "uploaded %.2f GB / %.2f GB (%.1f%%), paused=%s",
        result["uploaded_bytes"] / 1e9,
        result["quota_bytes"] / 1e9,
        pct,
        result["is_paused"],
    )


_scheduler.add_job(
    _scheduled_check,
    "interval",
    seconds=CHECK_INTERVAL_SECONDS,
    next_run_time=_now() + timedelta(seconds=10),
)

# ======================================================================
# Flask app
# ======================================================================
app = Flask(__name__)
app.json.ensure_ascii = False
# Suppress static file routing in catch-all
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# ======================================================================
# Basic Auth (protects every path except /healthz for Docker healthcheck)
# ======================================================================


def check_basic_auth() -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        user, _, pwd = decoded.partition(":")
        return hmac.compare_digest(user, QUOTA_USER) and hmac.compare_digest(
            pwd, QUOTA_PASS
        )
    except Exception:
        return False


@app.before_request
def _auth_middleware():
    if request.path == "/healthz":
        return None
    if not check_basic_auth():
        return Response(
            "Authentication required",
            401,
            {"WWW-Authenticate": 'Basic realm="QuotaGuard"'},
        )
    if (
        request.path.startswith("/api/")
        and request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), CSRF_TOKEN)
    ):
        return jsonify({"error": "invalid CSRF token"}), 403


# ======================================================================
# API routes
# ======================================================================
@app.route("/healthz")
def healthz():
    uptime = time.monotonic() - _service_started_monotonic
    if _last_check_status == "starting":
        healthy = uptime <= CHECK_INTERVAL_SECONDS + 30
    else:
        age = (
            (_now() - _last_check_at).total_seconds()
            if _last_check_at is not None
            else float("inf")
        )
        healthy = _last_check_status == "ok" and age <= CHECK_INTERVAL_SECONDS * 3

    return (
        jsonify(
            {
                "ok": healthy,
                "quota_check_status": _last_check_status,
                "last_check_at": _last_check_at.isoformat() if _last_check_at else None,
                "error": _last_check_error,
            }
        ),
        200 if healthy else 503,
    )


@app.route("/api/status")
def api_status():
    stats = get_session_stats()
    vnstat_data = get_vnstat_data()
    vnstat_total = get_vnstat_monthly_total(vnstat_data)

    session_up = _get_session_uploaded(stats) if stats else 0
    session_down = _get_session_downloaded(stats) if stats else 0
    torrents_result = get_torrents() if stats is not None else None
    torrents = torrents_result or []
    active = sum(1 for t in torrents if t.get("rateUpload", 0) > 0)
    total_seeds = len(torrents)
    current_up = sum(t.get("rateUpload", 0) for t in torrents)
    current_down = sum(t.get("rateDownload", 0) for t in torrents)
    with STATE_LOCK:
        state = load_state()

    return jsonify(
        {
            "month_key": state["month_key"],
            "monthly_uploaded_bytes": state["monthly_uploaded_bytes"],
            "quota_bytes": state["quota_bytes"],
            "quota_stop_threshold_bytes": _quota_stop_threshold(state["quota_bytes"]),
            "is_paused": state["is_paused"],
            "manual_paused": state["manual_paused"],
            "quota_paused": state["quota_paused"],
            "today_uploaded_bytes": state["today_uploaded_bytes"],
            "session_uploaded_bytes_total": session_up,
            "session_downloaded_bytes_total": session_down,
            "total_torrents": total_seeds,
            "active_torrents": active,
            "current_upload_rate_bytes": current_up,
            "current_download_rate_bytes": current_down,
            "vnstat_monthly_total_bytes": vnstat_total,
            "vnstat_interface": VNSTAT_INTERFACE,
            "accounting_timezone": TZ_NAME,
            "rpc_ok": stats is not None and torrents_result is not None,
            "rpc_error": _last_rpc_error if torrents_result is None else "",
        }
    )


@app.route("/api/quota", methods=["GET", "POST"])
def api_quota():
    if request.method == "GET":
        with STATE_LOCK:
            state = load_state()
        return jsonify(
            {
                "quota_bytes": state["quota_bytes"],
                "monthly_uploaded_bytes": state["monthly_uploaded_bytes"],
            }
        )

    data = request.get_json(silent=True) or {}
    new_quota = data.get("bytes")
    if (
        isinstance(new_quota, bool)
        or not isinstance(new_quota, (int, float))
        or not math.isfinite(new_quota)
        or new_quota <= 0
    ):
        return jsonify({"error": "bytes must be a positive integer"}), 400

    with STATE_LOCK:
        state = load_state()
        state["quota_bytes"] = int(new_quota)
        save_state(state)
        return jsonify({"quota_bytes": state["quota_bytes"], "ok": True})


@app.route("/api/pause", methods=["POST"])
def api_pause():
    with STATE_LOCK:
        state = load_state()
        ok = torrent_stop_all()
        if ok:
            state["manual_paused"] = True
        save_state(state)
        return jsonify({"paused": state["is_paused"], "ok": ok})


@app.route("/api/resume", methods=["POST"])
def api_resume():
    with STATE_LOCK:
        state = load_state()
        if state["monthly_uploaded_bytes"] >= _quota_stop_threshold(
            state["quota_bytes"]
        ):
            return (
                jsonify(
                    {
                        "paused": True,
                        "ok": False,
                        "error": "monthly quota has been exceeded",
                    }
                ),
                409,
            )

        ok = torrent_start_all()
        if ok:
            state["manual_paused"] = False
            state["quota_paused"] = False
            state["quota_paused_torrent_ids"] = []
        save_state(state)
        return jsonify({"paused": state["is_paused"], "ok": ok})


@app.route("/api/history")
def api_history():
    with STATE_LOCK:
        state = load_state()
    history = state.get("history", {})
    # Return last 60 entries as [{date, uploaded}, ...]
    items = [
        {"date": k, "uploaded": v.get("uploaded", 0) if isinstance(v, dict) else v}
        for k, v in list(history.items())[-60:]
    ]
    return jsonify({"history": items})


# ======================================================================
# Reverse proxy: forward to Transmission
# ======================================================================
# All paths NOT handled by Flask are proxied to transmission:9091
#   /torrents/*  ->  /transmission/web/*   (Flood UI, mapped to Transmission web root)
#   /transmission/* -> /transmission/*      (RPC, forwarded as-is)
#   other paths     -> other paths          (catch-all)

PROXY_EXCLUDED_HEADERS = frozenset(
    {
        "host",
        "connection",
        "transfer-encoding",
        "keep-alive",
        "proxy-connection",
        "te",
        "trailer",
        "content-length",
        "accept-encoding",
        "authorization",
    }
)


def proxy_to_transmission(target_path: str) -> Response:
    """Forward the current request to Transmission and return the response."""
    global _session_id

    # Strip /torrents/ prefix for Flood UI static resources
    actual_path = target_path
    if target_path == "/torrents" or target_path == "/torrents/":
        actual_path = "/transmission/web/"
    elif target_path.startswith("/torrents/"):
        actual_path = "/transmission/web/" + target_path[len("/torrents/") :]
    upstream_url = f"http://{TRANSMISSION_HOST}:{TRANSMISSION_PORT}{actual_path}"

    # Build headers
    headers = {}
    for k, v in request.headers:
        if k.lower() not in PROXY_EXCLUDED_HEADERS:
            headers[k] = v
    # Requests may transparently decode upstream gzip responses. Request an
    # uncompressed body so response length and content remain consistent.
    headers["Accept-Encoding"] = "identity"

    # Forward session ID for Transmission RPC
    with RPC_LOCK:
        proxy_session_id = _session_id
    if proxy_session_id:
        headers["X-Transmission-Session-Id"] = proxy_session_id

    try:
        resp = requests.request(
            method=request.method,
            url=upstream_url,
            headers=headers,
            data=request.get_data(),
            stream=True,
            timeout=60,
            auth=(TRANSMISSION_USER, TRANSMISSION_PASS) if TRANSMISSION_USER else None,
        )
    except requests.RequestException as exc:
        return Response(f"Proxy error: {exc}", status=502)

    # Update session ID from 409 response
    if resp.status_code == 409:
        new_sid = resp.headers.get("X-Transmission-Session-Id", "")
        if new_sid:
            with RPC_LOCK:
                _session_id = new_sid

    # Build response with excluded headers
    response_headers = {}
    for k, v in resp.headers.items():
        if k.lower() not in {
            "transfer-encoding",
            "connection",
            "keep-alive",
            "content-encoding",
            "content-length",
            "x-frame-options",
            "content-security-policy",
        }:
            response_headers[k] = v

    def response_body():
        try:
            yield from resp.iter_content(chunk_size=8192)
        finally:
            resp.close()

    return Response(
        stream_with_context(response_body()),
        status=resp.status_code,
        headers=response_headers,
        content_type=resp.headers.get("Content-Type"),
    )


# Catch-all: proxy all unhandled paths to Transmission
@app.route(
    "/<path:target_path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
)
def catch_all(target_path: str):
    """Proxy to Transmission for all paths not handled by Flask routes."""
    return proxy_to_transmission("/" + target_path)


# ======================================================================
# HTML Console template
# ======================================================================
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>trans-commitment · Ubuntu 做种控制台</title>
<style>
:root { color-scheme: light dark; --ink:#182426; --muted:#667477; --surface:#f6f8f7;
        --deck:#0e5261; --deck-2:#143c48; --line:#d6dddc; --signal:#4ecdc4;
        --warn:#f2b84b; --danger:#ee6c66; --quiet:#91a1a4; }
@media (prefers-color-scheme: dark) { :root { --ink:#edf3f3; --muted:#9eadaf; --surface:#121819;
        --deck:#123f4a; --deck-2:#0d2d35; --line:#344244; --signal:#58d4ca; } }
* { box-sizing:border-box; }
html, body { height:100%; }
body { margin:0; display:flex; flex-direction:column; overflow:hidden; color:var(--ink); background:var(--surface);
       font-family:"Segoe UI Variable","Segoe UI",system-ui,sans-serif; }
button, input { font:inherit; }
button { min-height:32px; border:1px solid rgba(255,255,255,.24); border-radius:5px; padding:5px 11px;
         color:#fff; background:rgba(255,255,255,.08); cursor:pointer; transition:background .16s,border-color .16s; }
button:hover { background:rgba(255,255,255,.16); border-color:rgba(255,255,255,.42); }
button:disabled { opacity:.55; cursor:wait; }
button:focus-visible, input:focus-visible { outline:2px solid #fff; outline-offset:2px; }
.primary-action { background:#fff; border-color:#fff; color:#123f48; font-weight:650; }
.primary-action:hover { background:#eaf8f6; }
.primary-action.paused-action { color:#fff; background:var(--danger); border-color:var(--danger); }
.control-deck { flex:none; color:#fff; background:var(--deck); box-shadow:0 2px 12px rgba(8,34,40,.22); z-index:2; }
.command-row { min-height:48px; display:flex; align-items:center; justify-content:space-between; gap:16px;
               padding:8px 18px; background:var(--deck-2); }
.identity { display:flex; align-items:center; gap:12px; min-width:0; }
.brand { font-size:1rem; font-weight:720; white-space:nowrap; }
.service-state { display:inline-flex; align-items:center; gap:6px; color:rgba(255,255,255,.78); font-size:.76rem; white-space:nowrap; }
.state-dot { width:7px; height:7px; border-radius:50%; background:var(--quiet); box-shadow:0 0 0 3px rgba(255,255,255,.08); }
.service-state.ok .state-dot { background:var(--signal); }
.service-state.warn .state-dot { background:var(--warn); }
.service-state.error .state-dot { background:var(--danger); }
.command-actions { display:flex; align-items:center; gap:8px; }
.metrics { display:grid; grid-template-columns:minmax(260px,2fr) repeat(4,minmax(112px,1fr)); padding:0 18px; }
.metric { min-height:76px; padding:12px 14px; border-left:1px solid rgba(255,255,255,.14); min-width:0; }
.metric:first-child { border-left:0; padding-left:0; }
.metric-label { display:block; margin-bottom:5px; color:rgba(255,255,255,.66); font-size:.7rem; }
.metric-value { display:block; overflow:hidden; text-overflow:ellipsis; font:650 1rem/1.25 "Cascadia Mono","SFMono-Regular",monospace;
                font-variant-numeric:tabular-nums; white-space:nowrap; }
.usage-head { display:flex; align-items:flex-end; justify-content:space-between; gap:14px; }
.usage-number { font-size:1.18rem; }
.sparkline { width:106px; height:26px; flex:none; }
.quota-rail { position:relative; height:7px; margin-top:8px; overflow:hidden; border-radius:4px; background:rgba(255,255,255,.15); }
.quota-fill { width:0; height:100%; border-radius:4px; background:var(--signal); transition:width .35s,background .2s; }
.quota-fill.warn { background:var(--warn); }
.quota-fill.critical { background:var(--danger); }
.quota-fill.paused { background:var(--quiet); }
.quota-marker { position:absolute; top:-2px; bottom:-2px; width:2px; background:rgba(255,255,255,.72); }
.usage-foot { display:flex; justify-content:space-between; gap:12px; margin-top:6px; color:rgba(255,255,255,.62); font-size:.66rem; }
.quota-editor { display:flex; align-items:center; gap:5px; }
.quota-editor input { width:76px; min-height:30px; border:1px solid rgba(255,255,255,.3); border-radius:4px; padding:4px 6px;
                      color:#fff; background:rgba(0,0,0,.12); }
.icon-button { width:30px; min-height:30px; padding:0; line-height:1; }
.hidden { display:none !important; }
.trend-panel { max-height:0; overflow:hidden; padding:0 18px; background:var(--surface); color:var(--ink);
               transition:max-height .24s ease,padding .24s ease; }
.trend-panel.open { max-height:190px; padding-top:10px; padding-bottom:10px; border-top:1px solid var(--line); }
#chartFullCanvas { display:block; width:100%; height:150px; }
.flood-shell { flex:1; min-height:0; background:var(--surface); }
.flood-shell iframe { display:block; width:100%; height:100%; border:0; }
@media (max-width:900px) {
  .metrics { grid-template-columns:2fr repeat(2,1fr); }
  .metric:nth-child(4) { border-left:0; padding-left:0; }
}
@media (max-width:620px) {
  .command-row { padding:8px 10px; }
  .brand { font-size:.9rem; }
  .command-actions button { padding-inline:8px; }
  .metrics { grid-template-columns:1.5fr 1fr; padding:0 10px; }
  .metric { min-height:66px; padding:9px 10px; }
  .metric:nth-child(even) { border-left:0; padding-left:0; }
  .metric-value { font-size:.86rem; }
  .metric-usage { grid-column:1/-1; border-left:0; padding-left:0; }
  .sparkline { width:88px; }
}
@media (prefers-reduced-motion:reduce) { *, *::before, *::after { transition:none !important; } }
</style>
</head>
<body>
<header class="control-deck">
  <div class="command-row">
    <div class="identity">
      <strong class="brand">trans-commitment</strong>
      <span class="service-state" id="serviceState"><span class="state-dot"></span><span id="serviceStateText">连接中</span></span>
    </div>
    <div class="command-actions">
      <button id="btnChart" onclick="toggleChart()" aria-expanded="false">流量趋势</button>
      <button id="btnPause" class="primary-action" onclick="togglePause()">暂停全部</button>
    </div>
  </div>
  <div class="metrics">
    <section class="metric metric-usage" aria-label="本月流量">
      <div class="usage-head">
        <div><span class="metric-label">本月已上传</span><span class="metric-value usage-number" id="vUsed">--</span></div>
        <canvas class="sparkline" id="chartCanvas" aria-label="最近流量趋势"></canvas>
      </div>
      <div class="quota-rail" aria-hidden="true">
        <div class="quota-fill" id="pBar"></div>
        <span class="quota-marker" id="quotaMarker"></span>
      </div>
      <div class="usage-foot"><span id="vToday">今日 --</span><span id="vThreshold">安全线 --</span></div>
    </section>
    <section class="metric">
      <span class="metric-label">月度配额</span>
      <div class="quota-editor">
        <span class="metric-value" id="vQuota">--</span>
        <button class="icon-button" id="btnEditQuota" onclick="editQuota()" title="修改月度配额" aria-label="修改月度配额">&#9998;</button>
        <span class="quota-editor hidden" id="editBlock">
          <input type="number" id="inpQuota" aria-label="月度配额，单位 TiB" placeholder="TiB" step="0.1" min="0.1">
          <button class="icon-button" onclick="saveQuota()" title="保存配额" aria-label="保存配额">&#10003;</button>
          <button class="icon-button" onclick="cancelQuota()" title="取消修改" aria-label="取消修改">&#10005;</button>
        </span>
      </div>
    </section>
    <section class="metric"><span class="metric-label">VPS 本月总流量</span><span class="metric-value" id="vVnstat">--</span></section>
    <section class="metric"><span class="metric-label">活跃 / 全部种子</span><span class="metric-value" id="vActive">--</span></section>
    <section class="metric"><span class="metric-label">实时上传</span><span class="metric-value" id="vRateUp">--</span></section>
  </div>
  <div class="trend-panel" id="chartFull" aria-hidden="true"><canvas id="chartFullCanvas" aria-label="最近 60 天上传流量"></canvas></div>
</header>
<main class="flood-shell">
  <iframe src="/torrents/" id="floodFrame" title="Transmission torrent manager"></iframe>
</main>

<script>
const TB = 1099511627776;
const GB = 1073741824;
const MB = 1048576;
const CSRF_TOKEN = '__CSRF_TOKEN__';
let lastStatus = null;
let chartHistory = [];

function fmtBytes(b) {
  if (b == null || b < 0) return '--';
  if (b >= TB) return (b / TB).toFixed(2) + ' TiB';
  if (b >= GB) return (b / GB).toFixed(1) + ' GiB';
  if (b >= MB) return (b / MB).toFixed(0) + ' MiB';
  return b + ' B';
}
function fmtRate(bps) {
  if (bps == null || bps <= 0) return '0';
  if (bps >= 1e9) return (bps / 1e9).toFixed(1) + ' GB/s';
  if (bps >= 1e6) return (bps / 1e6).toFixed(1) + ' MB/s';
  if (bps >= 1e3) return (bps / 1e3).toFixed(0) + ' KB/s';
  return bps.toFixed(0) + ' B/s';
}

async function refresh() {
  try {
    const r = await fetch('/api/status');
    if (r.status === 401) { location.reload(); return; }
    if (!r.ok) throw new Error('status request failed: ' + r.status);
    const d = await r.json();
    lastStatus = d;
    document.getElementById('vUsed').textContent = fmtBytes(d.monthly_uploaded_bytes);
    document.getElementById('vQuota').textContent = fmtBytes(d.quota_bytes);
    document.getElementById('vToday').textContent = '今日 ' + fmtBytes(d.today_uploaded_bytes);
    document.getElementById('vActive').textContent = d.active_torrents + ' / ' + d.total_torrents;
    document.getElementById('vRateUp').textContent = fmtRate(d.current_upload_rate_bytes);
    document.getElementById('vVnstat').textContent = d.vnstat_monthly_total_bytes != null
      ? fmtBytes(d.vnstat_monthly_total_bytes) : '--';

    const pct = d.quota_bytes > 0 ? (d.monthly_uploaded_bytes / d.quota_bytes * 100) : 0;
    const threshold = d.quota_stop_threshold_bytes || d.quota_bytes;
    const thresholdPct = d.quota_bytes > 0 ? threshold / d.quota_bytes * 100 : 100;
    const bar = document.getElementById('pBar');
    bar.style.width = Math.min(pct, 100) + '%';
    bar.className = 'quota-fill' + (d.is_paused ? ' paused' : pct >= thresholdPct ? ' critical' : pct > 75 ? ' warn' : '');
    document.getElementById('quotaMarker').style.left = Math.min(thresholdPct, 100) + '%';
    document.getElementById('vThreshold').textContent = '安全线 ' + fmtBytes(threshold);

    const btn = document.getElementById('btnPause');
    btn.textContent = d.is_paused ? '恢复全部' : '暂停全部';
    btn.classList.toggle('paused-action', d.is_paused);

    const service = document.getElementById('serviceState');
    const serviceText = document.getElementById('serviceStateText');
    service.className = 'service-state';
    if (!d.rpc_ok) {
      service.classList.add('error');
      serviceText.textContent = 'Transmission 连接异常';
      bar.className = 'quota-fill critical';
    } else if (d.quota_paused) {
      service.classList.add('warn');
      serviceText.textContent = d.manual_paused ? '配额及手动暂停' : '已到安全线';
    } else if (d.manual_paused) {
      service.classList.add('warn');
      serviceText.textContent = '手动暂停';
    } else {
      service.classList.add('ok');
      serviceText.textContent = '运行正常';
    }

    return d;
  } catch(e) {
    const service = document.getElementById('serviceState');
    service.className = 'service-state error';
    document.getElementById('serviceStateText').textContent = '状态服务异常';
    console.error(e);
  }
}

async function togglePause() {
  const btn = document.getElementById('btnPause');
  const paused = btn.textContent === '恢复全部';
  const url = paused ? '/api/resume' : '/api/pause';
  btn.disabled = true;
  try {
    const response = await fetch(url, {method:'POST', headers:{'X-CSRF-Token': CSRF_TOKEN}});
    if (response.status === 403) { location.reload(); return; }
    if (response.status === 409) {
      const d = await response.json().catch(() => ({}));
      alert(d.error || '操作被拒绝：月度配额已超限，请先提高配额再恢复');
    }
    await refresh();
  } finally {
    btn.disabled = false;
  }
}

function prepareCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(Math.floor(rect.width), 1);
  const height = Math.max(Math.floor(rect.height), 1);
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  return {ctx, width, height};
}

function drawSparkline() {
  const canvas = document.getElementById('chartCanvas');
  const {ctx, width, height} = prepareCanvas(canvas);
  const values = chartHistory.slice(-30).map(x => x.uploaded);
  if (values.length < 2) return;
  const max = Math.max(...values, 1);
  ctx.beginPath();
  values.forEach((value, index) => {
    const x = index / (values.length - 1) * width;
    const y = height - 2 - value / max * (height - 4);
    if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = 'rgba(115,235,225,.9)';
  ctx.lineWidth = 1.5;
  ctx.stroke();
}

function drawFullChart() {
  const canvas = document.getElementById('chartFullCanvas');
  if (!document.getElementById('chartFull').classList.contains('open')) return;
  const {ctx, width, height} = prepareCanvas(canvas);
  const values = chartHistory.map(x => x.uploaded);
  if (values.length === 0) return;
  const max = Math.max(...values, 1);
  const chartBottom = height - 22;
  const gap = values.length * 5 > width ? 1 : 3;
  const barWidth = Math.max((width - gap * (values.length - 1)) / values.length, 1);
  ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--line');
  ctx.beginPath(); ctx.moveTo(0, chartBottom + .5); ctx.lineTo(width, chartBottom + .5); ctx.stroke();
  values.forEach((value, index) => {
    const x = index * (barWidth + gap);
    const barHeight = value / max * (chartBottom - 8);
    ctx.fillStyle = 'rgba(52,184,176,.78)';
    ctx.fillRect(x, chartBottom - barHeight, barWidth, barHeight);
  });
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--muted');
  ctx.font = '11px "Cascadia Mono", monospace';
  ctx.fillText(chartHistory[0].date.slice(5), 0, height - 5);
  const lastLabel = chartHistory[chartHistory.length - 1].date.slice(5);
  const labelWidth = ctx.measureText(lastLabel).width;
  ctx.fillText(lastLabel, Math.max(width - labelWidth, 0), height - 5);
  ctx.fillText(fmtBytes(max), 0, 11);
}

async function loadChart() {
  try {
    const r = await fetch('/api/history');
    const d = await r.json();
    chartHistory = d.history || [];
    drawSparkline();
    drawFullChart();
  } catch(e) { console.error('chart error', e); }
}

function toggleChart() {
  const el = document.getElementById('chartFull');
  const open = el.classList.toggle('open');
  el.setAttribute('aria-hidden', String(!open));
  const btn = document.getElementById('btnChart');
  btn.setAttribute('aria-expanded', String(open));
  btn.textContent = open ? '收起趋势' : '流量趋势';
  if (open) requestAnimationFrame(drawFullChart);
}

function editQuota() {
  const currentTiB = ((lastStatus && lastStatus.quota_bytes) || 0) / TB;
  document.getElementById('inpQuota').value = currentTiB.toFixed(1);
  document.getElementById('btnEditQuota').classList.add('hidden');
  document.getElementById('editBlock').classList.remove('hidden');
  document.getElementById('inpQuota').focus();
}
async function saveQuota() {
  const input = document.getElementById('inpQuota');
  const tib = Number(input.value);
  if (!input.checkValidity() || !Number.isFinite(tib) || tib <= 0) {
    input.reportValidity();
    return;
  }
  try {
    const response = await fetch('/api/quota', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':CSRF_TOKEN}, body:JSON.stringify({bytes: Math.round(tib*TB)})});
    if (response.status === 403) { location.reload(); return; }
    if (!response.ok) { alert('配额修改失败，请重试'); return; }
    document.getElementById('editBlock').classList.add('hidden');
    document.getElementById('btnEditQuota').classList.remove('hidden');
    await refresh();
  } catch(e) {
    alert('网络错误，配额修改失败');
    console.error(e);
  }
}
function cancelQuota() {
  document.getElementById('editBlock').classList.add('hidden');
  document.getElementById('btnEditQuota').classList.remove('hidden');
}

loadChart();
window.addEventListener('resize', () => { drawSparkline(); drawFullChart(); });
async function refreshLoop() {
  await refresh();
  window.setTimeout(refreshLoop, 10000);
}
refreshLoop();
window.setInterval(loadChart, 60000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    html = INDEX_HTML.replace("__CSRF_TOKEN__", CSRF_TOKEN)
    return Response(html, content_type="text/html; charset=utf-8")


# ======================================================================
# Main
# ======================================================================
def validate_config() -> None:
    insecure_passwords = {
        "",
        "change-me",
        "change-me-to-a-strong-password-20chars",
        "change-me-another-strong-password",
    }
    errors = []
    if not TRANSMISSION_USER:
        errors.append("TRANSMISSION_USER must not be empty")
    if TRANSMISSION_PASS in insecure_passwords:
        errors.append("TRANSMISSION_PASS must be changed from the example value")
    if not QUOTA_USER:
        errors.append("QUOTA_USER must not be empty")
    if QUOTA_PASS in insecure_passwords:
        errors.append("QUOTA_PASS must be changed from the example value")
    if MONTHLY_QUOTA_BYTES <= 0:
        errors.append("MONTHLY_QUOTA_BYTES must be positive")
    if CHECK_INTERVAL_SECONDS <= 0:
        errors.append("CHECK_INTERVAL_SECONDS must be positive")
    if QUOTA_SAFETY_MARGIN_BYTES < 0:
        errors.append("QUOTA_SAFETY_MARGIN_BYTES must not be negative")
    if not VNSTAT_INTERFACE:
        errors.append("VNSTAT_INTERFACE must not be empty")
    if TZ_ERROR:
        errors.append(TZ_ERROR)
    if errors:
        raise RuntimeError("invalid configuration: " + "; ".join(errors))


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    validate_config()
    # Fail before serving traffic if the persistent bind mount is not writable.
    save_state(load_state())
    _scheduler.start()
    from waitress import serve

    serve(app, host="0.0.0.0", port=9092, threads=8, channel_timeout=120)


if __name__ == "__main__":
    main()
