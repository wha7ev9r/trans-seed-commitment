#!/usr/bin/env python3
"""quota-guard: Transmission 月度流量配额守护 + 统一控制面板."""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from functools import wraps
from http import HTTPStatus
from threading import Lock
from urllib.parse import urljoin

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, g, jsonify, request

# ======================================================================
# Config from environment
# ======================================================================
TRANSMISSION_HOST = os.getenv("TRANSMISSION_HOST", "transmission")
TRANSMISSION_PORT = int(os.getenv("TRANSMISSION_PORT", "9091"))
TRANSMISSION_USER = os.getenv("TRANSMISSION_USER", "")
TRANSMISSION_PASS = os.getenv("TRANSMISSION_PASS", "")

QUOTA_USER = os.getenv("QUOTA_USER", "seed")
QUOTA_PASS = os.getenv("QUOTA_PASS", "change-me")
MONTHLY_QUOTA_BYTES = int(os.getenv("MONTHLY_QUOTA_BYTES", "1099511627776"))

VNSTAT_HOST = os.getenv("VNSTAT_HOST", "host.docker.internal")
VNSTAT_PORT = int(os.getenv("VNSTAT_PORT", "8685"))

STATE_FILE = "/data/state.json"
CHECK_INTERVAL_SECONDS = 60  # 每分钟轮询 Transmission

RPC_URL = f"http://{TRANSMISSION_HOST}:{TRANSMISSION_PORT}/transmission/rpc"
VNSTAT_URL = f"http://{VNSTAT_HOST}:{VNSTAT_PORT}"

# ======================================================================
# Persistent state
# ======================================================================
STATE_LOCK = Lock()


def _now() -> datetime:
    return datetime.now(UTC)


def _this_month_key() -> str:
    n = _now()
    return f"{n.year}-{n.month:02d}"


def load_state() -> dict:
    default = {
        "month_key": _this_month_key(),
        "day_key": _now().strftime("%Y-%m-%d"),
        "monthly_uploaded_bytes": 0,
        "today_uploaded_bytes": 0,
        "cumulative_uploaded_bytes": 0,  # Transmission session start
        "is_paused": False,
        "quota_bytes": MONTHLY_QUOTA_BYTES,
        "history": OrderedDict(),  # { "2025-07-01": {"uploaded": 123, "sessions": 45} }
    }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for key, fallback in default.items():
            default[key] = saved.get(key, fallback)
        history = default.get("history")
        default["history"] = OrderedDict(history if isinstance(history, dict) else {})
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return default


def save_state(state: dict) -> None:
    with STATE_LOCK:
        state["_updated_at"] = _now().isoformat()
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
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
    if _session_id:
        headers["X-Transmission-Session-Id"] = _session_id

    sess = _rpc_session()
    try:
        resp = sess.post(RPC_URL, json=payload, headers=headers, timeout=15)
    except requests.RequestException as exc:
        _last_rpc_error = str(exc)
        return None

    if resp.status_code == 409:
        _session_id = resp.headers.get("X-Transmission-Session-Id", "")
        headers["X-Transmission-Session-Id"] = _session_id
        try:
            resp = sess.post(RPC_URL, json=payload, headers=headers, timeout=15)
        except requests.RequestException as exc:
            _last_rpc_error = str(exc)
            return None

    if not resp.ok:
        _last_rpc_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        return None

    data = resp.json()
    if data.get("result") != "success":
        _last_rpc_error = data.get("result", "unknown rpc error")
        return None

    _last_rpc_error = ""
    return data.get("arguments", {})


def get_session_stats() -> dict | None:
    return rpc_call("session-stats")


def get_torrents(ids: list[int] | None = None) -> list[dict]:
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
        return []
    return result.get("torrents", [])


def torrent_stop_all() -> bool:
    result = rpc_call("torrent-stop", {})
    return result is not None


def torrent_start_all() -> bool:
    result = rpc_call("torrent-start", {})
    return result is not None


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


# ======================================================================
# Quota tracking logic
# ======================================================================
def check_quota() -> dict:
    """Poll Transmission stats, accumulate quota, auto-pause if exceeded."""
    state = load_state()
    today_str = _now().strftime("%Y-%m-%d")
    month_key = _this_month_key()

    # Month rollover
    if state["month_key"] != month_key:
        state["month_key"] = month_key
        state["monthly_uploaded_bytes"] = 0
        state["today_uploaded_bytes"] = 0
        state["is_paused"] = False
        state["cumulative_uploaded_bytes"] = 0

    # Day rollover keeps the daily chart accurate across a long-running daemon.
    if state.get("day_key") != today_str:
        state["day_key"] = today_str
        state["today_uploaded_bytes"] = 0

    stats = get_session_stats()
    if stats is None:
        return {"status": "rpc_error", "error": _last_rpc_error}

    current_total = stats.get("uploadedBytes", 0)
    previous_total = state.get("cumulative_uploaded_bytes", 0)

    # Detect session restart (current_total < previous_total means daemon restarted)
    if previous_total > current_total:
        delta = current_total  # fresh start
    else:
        delta = current_total - previous_total

    state["monthly_uploaded_bytes"] += delta
    state["today_uploaded_bytes"] += delta
    state["cumulative_uploaded_bytes"] = current_total

    # Update daily history
    if today_str not in state["history"]:
        state["history"][today_str] = {"uploaded": 0, "sessions": 0}
    state["history"][today_str]["uploaded"] = state["today_uploaded_bytes"]
    state["history"][today_str]["sessions"] = len(get_torrents())

    # Keep only last 90 days of history
    if len(state["history"]) > 90:
        while len(state["history"]) > 90:
            state["history"].popitem(last=False)

    # Check quota exceeded
    if (
        state["monthly_uploaded_bytes"] >= state["quota_bytes"]
        and not state["is_paused"]
    ):
        if torrent_stop_all():
            state["is_paused"] = True

    # Auto-resume at month start
    elif state["monthly_uploaded_bytes"] < state["quota_bytes"] and state["is_paused"]:
        if torrent_start_all():
            state["is_paused"] = False

    save_state(state)

    return {
        "status": "ok",
        "month_key": state["month_key"],
        "uploaded_bytes": state["monthly_uploaded_bytes"],
        "quota_bytes": state["quota_bytes"],
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
        traffic = interfaces[0].get("traffic", {})
        months = traffic.get("month", [])
        if months:
            latest = months[-1]
            return latest.get("rx", 0) + latest.get("tx", 0)
    except (IndexError, KeyError, TypeError):
        pass
    return None


# ======================================================================
# Scheduler
# ======================================================================
_scheduler = BackgroundScheduler(timezone=UTC, daemon=True)


def _scheduled_check() -> None:
    print(f"[{_now().isoformat()}] quota check running...")
    result = check_quota()
    if result["status"] == "rpc_error":
        print(f"  RPC error: {result.get('error', 'unknown')}")
        return
    pct = result["uploaded_bytes"] / max(result["quota_bytes"], 1) * 100
    print(
        f"  uploaded: {result['uploaded_bytes'] / 1e9:.2f} GB / {result['quota_bytes'] / 1e9:.2f} GB ({pct:.1f}%)"
        f"  paused: {result['is_paused']}"
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
app.config["JSON_AS_ASCII"] = False
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
        return user == QUOTA_USER and pwd == QUOTA_PASS
    except Exception:
        return False


@app.before_request
def _auth_middleware():
    # Docker healthcheck only confirms the web server is alive; all data/control
    # routes still require Basic Auth.
    if request.path == "/healthz":
        return None
    if not check_basic_auth():
        return Response(
            "Authentication required",
            401,
            {"WWW-Authenticate": 'Basic realm="QuotaGuard"'},
        )


# ======================================================================
# API routes
# ======================================================================
@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    state = load_state()
    stats = get_session_stats()
    vnstat_data = get_vnstat_data()
    vnstat_total = get_vnstat_monthly_total(vnstat_data)

    session_up = stats.get("uploadedBytes", 0) if stats else 0
    session_down = stats.get("downloadedBytes", 0) if stats else 0
    torrents = get_torrents()
    active = sum(1 for t in torrents if t.get("rateUpload", 0) > 0)
    total_seeds = len(torrents)
    current_up = sum(t.get("rateUpload", 0) for t in torrents)
    current_down = sum(t.get("rateDownload", 0) for t in torrents)

    return jsonify(
        {
            "month_key": state["month_key"],
            "monthly_uploaded_bytes": state["monthly_uploaded_bytes"],
            "quota_bytes": state["quota_bytes"],
            "is_paused": state["is_paused"],
            "today_uploaded_bytes": state["today_uploaded_bytes"],
            "session_uploaded_bytes_total": session_up,
            "session_downloaded_bytes_total": session_down,
            "total_torrents": total_seeds,
            "active_torrents": active,
            "current_upload_rate_bytes": current_up,
            "current_download_rate_bytes": current_down,
            "vnstat_monthly_total_bytes": vnstat_total,
            "rpc_ok": stats is not None,
            "rpc_error": _last_rpc_error if stats is None else "",
        }
    )


@app.route("/api/quota", methods=["GET", "POST"])
def api_quota():
    if request.method == "GET":
        state = load_state()
        return jsonify(
            {
                "quota_bytes": state["quota_bytes"],
                "monthly_uploaded_bytes": state["monthly_uploaded_bytes"],
            }
        )

    data = request.get_json(silent=True) or {}
    new_quota = data.get("bytes")
    if not isinstance(new_quota, (int, float)) or new_quota <= 0:
        return jsonify({"error": "bytes must be a positive integer"}), 400

    state = load_state()
    state["quota_bytes"] = int(new_quota)
    save_state(state)
    return jsonify({"quota_bytes": state["quota_bytes"], "ok": True})


@app.route("/api/pause", methods=["POST"])
def api_pause():
    ok = torrent_stop_all()
    state = load_state()
    state["is_paused"] = ok
    save_state(state)
    return jsonify({"paused": state["is_paused"], "ok": ok})


@app.route("/api/resume", methods=["POST"])
def api_resume():
    ok = torrent_start_all()
    state = load_state()
    state["is_paused"] = not ok
    save_state(state)
    return jsonify({"paused": state["is_paused"], "ok": ok})


@app.route("/api/history")
def api_history():
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
#   /torrents/xxx  ->  http://transmission:9091/xxx      (strip /torrents prefix)
#   /transmission/* ->  http://transmission:9091/transmission/*  (no strip)
#   other paths     ->  http://transmission:9091/other          (no strip)

PROXY_EXCLUDED_HEADERS = frozenset(
    {
        "host",
        "connection",
        "transfer-encoding",
        "keep-alive",
        "proxy-connection",
        "te",
        "trailer",
    }
)


def proxy_to_transmission(target_path: str) -> Response:
    """Forward the current request to Transmission and return the response."""
    global _session_id

    # Strip /torrents/ prefix for Flood UI static resources
    actual_path = target_path
    if target_path.startswith("/torrents/"):
        actual_path = target_path[
            len("/torrents/") - 1 :
        ]  # keep leading /, remove /torrents
    elif target_path == "/torrents":
        actual_path = "/"
    upstream_url = f"http://{TRANSMISSION_HOST}:{TRANSMISSION_PORT}{actual_path}"

    # Build headers
    headers = {}
    for k, v in request.headers:
        if k.lower() not in PROXY_EXCLUDED_HEADERS:
            headers[k] = v

    # Forward session ID for Transmission RPC
    if _session_id:
        headers["X-Transmission-Session-Id"] = _session_id

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
            _session_id = new_sid

    # Build response with excluded headers
    response_headers = {}
    for k, v in resp.headers.items():
        if k.lower() not in {
            "transfer-encoding",
            "connection",
            "keep-alive",
            "content-encoding",
        }:
            response_headers[k] = v

    return Response(
        resp.iter_content(chunk_size=8192),
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
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
:root { --pico-font-size: 90%; }
body { margin: 0; font-family: system-ui, -apple-system, sans-serif; }
.topbar { background: var(--pico-primary-background); color: var(--pico-primary-inverse);
          padding: 10px 20px; display: flex; flex-wrap: wrap; align-items: center; gap: 12px 20px;
          position: sticky; top: 0; z-index: 100; min-height: 50px; }
.topbar .brand { font-weight: 700; font-size: 1.1rem; white-space: nowrap; }
.topbar .block { display: flex; align-items: baseline; gap: 6px; white-space: nowrap; }
.topbar .label { opacity: .75; font-size: .75rem; }
.topbar .value { font-variant-numeric: tabular-nums; font-weight: 600; }
.topbar .warn { color: #ffb347; }
.topbar .critical { color: #ff6b6b; }
.topbar button { font-size: .8rem; padding: 4px 12px; cursor: pointer; border-radius: 4px; border: none;
                 background: rgba(255,255,255,.15); color: inherit; transition: background .2s; }
.topbar button:hover { background: rgba(255,255,255,.25); }
.topbar input[type="number"] { width: 100px; padding: 4px 8px; border-radius: 4px; border: 1px solid rgba(255,255,255,.3);
                              background: rgba(255,255,255,.1); color: inherit; }
.topbar .hidden { display: none; }
.progress-wrap { height: 4px; background: rgba(255,255,255,.2); border-radius: 2px; flex: 1; min-width: 80px; }
.progress-bar { height: 100%; border-radius: 2px; background: #4ecdc4; transition: width .5s; }
.progress-bar.warn { background: #ffb347; }
.progress-bar.critical { background: #ff6b6b; }
.progress-bar.paused { background: #888; }
.iframe-wrap { position: fixed; top: 52px; left: 0; right: 0; bottom: 0; }
.iframe-wrap iframe { width: 100%; height: 100%; border: none; }
.chart-wrap { width: 160px; height: 36px; display: flex; align-items: center; gap: 4px; }
.chart-wrap canvas { width: 120px !important; height: 30px !important; }
#btnChart { font-size: .7rem; padding: 2px 8px; }
@media (max-width: 768px) { .topbar { padding: 8px 12px; gap: 6px 10px; }
                            .progress-wrap { display: none; } .chart-wrap { display: none; }
                            .iframe-wrap { top: 80px; } }
</style>
</head>
<body>
<div class="topbar" id="topbar">
  <span class="brand">trans-commitment</span>

  <div class="block">
    <span class="label">本月已用</span>
    <span class="value" id="vUsed">--</span>
  </div>
  <div class="progress-wrap" id="pWrap"><div class="progress-bar" id="pBar"></div></div>
  <div class="block">
    <span class="label">配额</span>
    <span class="value" id="vQuota">--</span>
    <button onclick="editQuota()" title="修改月度配额" style="font-size:.7rem;padding:2px 6px;">&#9998;</button>
    <span class="hidden" id="editBlock">
      <input type="number" id="inpQuota" placeholder="TB" step="0.1" min="0.1" style="width:70px;">
      <button onclick="saveQuota()">OK</button>
      <button onclick="cancelQuota()">&#10005;</button>
    </span>
  </div>
  <div class="block">
    <span class="label">VPS 总</span>
    <span class="value" id="vVnstat">--</span>
  </div>
  <div class="block">
    <span class="label">活跃/种子</span>
    <span class="value" id="vActive">--</span>
  </div>
  <div class="block chart-wrap">
    <span class="label">30d</span>
    <canvas id="chartCanvas"></canvas>
    <button id="btnChart" onclick="toggleChart()">&#9660;</button>
  </div>
  <span id="actions">
    <button id="btnPause" onclick="togglePause()">暂停全部</button>
  </span>
  <div style="flex:1"></div>
  <div class="block"><span class="label">实时</span><span class="value" id="vRateUp">--</span><span class="label">↑</span></div>
</div>
<div id="chartFull" style="display:none; position:fixed; top:52px; left:0; right:0; height:180px;
            background:var(--pico-card-background-color); z-index:99; padding:10px; border-bottom:1px solid var(--pico-muted-border-color);">
  <canvas id="chartFullCanvas"></canvas>
</div>
<div class="iframe-wrap">
  <iframe src="/torrents/" id="floodFrame" allow="camera;microphone"></iframe>
</div>

<script>
const TB = 1099511627776;
const GB = 1073741824;
const MB = 1048576;
let lastStatus = null;

function fmtBytes(b) {
  if (b == null || b < 0) return '--';
  if (b >= TB) return (b / TB).toFixed(2) + ' TB';
  if (b >= GB) return (b / GB).toFixed(1) + ' GB';
  if (b >= MB) return (b / MB).toFixed(0) + ' MB';
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
    const d = await r.json();
    lastStatus = d;
    document.getElementById('vUsed').textContent = fmtBytes(d.monthly_uploaded_bytes);
    document.getElementById('vQuota').textContent = fmtBytes(d.quota_bytes);
    document.getElementById('vActive').textContent = d.active_torrents + '/' + d.total_torrents;
    document.getElementById('vRateUp').textContent = fmtRate(d.current_upload_rate_bytes);
    document.getElementById('vVnstat').textContent = d.vnstat_monthly_total_bytes
      ? fmtBytes(d.vnstat_monthly_total_bytes) : '--';

    const pct = d.quota_bytes > 0 ? (d.monthly_uploaded_bytes / d.quota_bytes * 100) : 0;
    const bar = document.getElementById('pBar');
    bar.style.width = Math.min(pct, 100) + '%';
    bar.className = 'progress-bar' + (d.is_paused ? ' paused' : pct > 90 ? ' critical' : pct > 75 ? ' warn' : '');

    const btn = document.getElementById('btnPause');
    btn.textContent = d.is_paused ? '恢复全部' : '暂停全部';
    btn.style.background = d.is_paused ? 'rgba(255,107,107,.5)' : '';

    if (!d.rpc_ok) { bar.className = 'progress-bar critical'; }
    return d;
  } catch(e) { console.error(e); }
}

async function togglePause() {
  const btn = document.getElementById('btnPause');
  const paused = btn.textContent === '恢复全部';
  const url = paused ? '/api/resume' : '/api/pause';
  await fetch(url, {method:'POST'});
  await refresh();
}

async function loadChart() {
  try {
    const r = await fetch('/api/history');
    const d = await r.json();
    const hist = d.history || [];
    const labels = hist.map(x => x.date.slice(5));
    const data = hist.map(x => x.uploaded);
    if (data.length === 0) return;
    new Chart(document.getElementById('chartCanvas'), {
      type: 'line',
      data: { labels, datasets: [{ data, borderColor: 'rgba(255,255,255,.6)', borderWidth: 1, pointRadius: 0, tension: 0.3 }] },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { x: { display: false, ticks: { maxTicksLimit: 6 } }, y: { display: false } },
      }
    });
    new Chart(document.getElementById('chartFullCanvas'), {
      type: 'bar',
      data: { labels, datasets: [{ data, backgroundColor: 'rgba(78,205,196,.5)', borderRadius: 4 }] },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => fmtBytes(ctx.raw) } } },
        scales: { x: { ticks: { maxTicksLimit: 14, autoSkip: true } }, y: { ticks: { callback: v => fmtBytes(v) } } },
      }
    });
  } catch(e) { console.error('chart error', e); }
}

function toggleChart() {
  const el = document.getElementById('chartFull');
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

function editQuota() {
  const currentTB = ((lastStatus && lastStatus.quota_bytes) || 0) / TB;
  document.getElementById('inpQuota').value = currentTB.toFixed(1);
  document.getElementById('editBlock').classList.remove('hidden');
}
async function saveQuota() {
  const tb = parseFloat(document.getElementById('inpQuota').value) || 0;
  if (tb <= 0) return;
  await fetch('/api/quota', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({bytes: Math.round(tb*TB)})});
  document.getElementById('editBlock').classList.add('hidden');
  await refresh();
}
function cancelQuota() { document.getElementById('editBlock').classList.add('hidden'); }

refresh();
loadChart();
setInterval(refresh, 15000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return Response(INDEX_HTML, content_type="text/html; charset=utf-8")


# ======================================================================
# Main
# ======================================================================
def main() -> None:
    _scheduler.start()
    from waitress import serve

    serve(app, host="0.0.0.0", port=9092, threads=8, channel_timeout=120)


if __name__ == "__main__":
    main()
