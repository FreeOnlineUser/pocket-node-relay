#!/usr/bin/env python3
"""
Pocket Node Relay — headless chainstate relay server.

Implements the same HTTP API as the phone's ShareServer.kt so that
Pocket Node's ShareClient can download chainstate over Tor without
any client-side changes.
"""

import argparse
import json
import logging
import mimetypes
import os
import re
import signal
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import yaml

__version__ = "0.1.0"

logger = logging.getLogger("relay")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str = "config.yaml") -> dict:
    if not os.path.exists(path):
        logger.error(f"Config file not found: {path}")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)

# ---------------------------------------------------------------------------
# Bitcoind helpers
# ---------------------------------------------------------------------------

def get_bitcoin_dir(cfg: dict) -> Path:
    return Path(cfg["bitcoin_datadir"]).expanduser()


def get_chain_height(bitcoin_dir: Path) -> int:
    """Read chain height from saved fetch data or debug.log."""
    # First try chain_height.json (saved by fetch.py)
    height_file = Path("chain_height.json")
    if height_file.exists():
        try:
            data = json.loads(height_file.read_text())
            h = data.get("chainHeight", 0)
            if h > 0:
                return h
        except Exception:
            pass

    # Fallback: read from debug.log if bitcoind is running
    debug_log = bitcoin_dir / "debug.log"
    if not debug_log.exists():
        return 0
    try:
        size = debug_log.stat().st_size
        with open(debug_log, "r") as f:
            if size > 65536:
                f.seek(size - 65536)
                f.readline()
            lines = f.readlines()
        for line in reversed(lines):
            if "UpdateTip:" in line:
                m = re.search(r"height=(\d+)", line)
                if m:
                    return int(m.group(1))
    except Exception as e:
        logger.warning(f"Could not read chain height: {e}")
    return 0


def has_block_filters(bitcoin_dir: Path) -> bool:
    filter_dir = bitcoin_dir / "indexes" / "blockfilter" / "basic"
    if not filter_dir.exists():
        return False
    return len(list(filter_dir.iterdir())) > 1

# ---------------------------------------------------------------------------
# File manifest (matches ShareServer.kt exactly)
# ---------------------------------------------------------------------------

ALLOWED_PREFIXES = ("chainstate/", "blocks/", "indexes/")


def build_manifest(bitcoin_dir: Path, include_filters: bool = True) -> dict:
    """Build file manifest matching ShareServer.kt output."""
    files = []
    total_size = 0

    # Chainstate
    chainstate_dir = bitcoin_dir / "chainstate"
    if chainstate_dir.exists():
        for f in chainstate_dir.rglob("*"):
            if f.is_file():
                rel = f"chainstate/{f.relative_to(chainstate_dir)}"
                size = f.stat().st_size
                files.append({"path": rel, "size": size})
                total_size += size

    # Block index
    index_dir = bitcoin_dir / "blocks" / "index"
    if index_dir.exists():
        for f in index_dir.rglob("*"):
            if f.is_file():
                rel = f"blocks/index/{f.relative_to(index_dir)}"
                size = f.stat().st_size
                files.append({"path": rel, "size": size})
                total_size += size

    # XOR key
    xor_file = bitcoin_dir / "blocks" / "xor.dat"
    if xor_file.exists():
        size = xor_file.stat().st_size
        files.append({"path": "blocks/xor.dat", "size": size})
        total_size += size

    # Block files (non-empty blk/rev .dat files)
    blocks_dir = bitcoin_dir / "blocks"
    if blocks_dir.exists():
        for pattern in ("blk*.dat", "rev*.dat"):
            for f in sorted(blocks_dir.glob(pattern)):
                if f.is_file() and f.stat().st_size > 0:
                    rel = f"blocks/{f.name}"
                    size = f.stat().st_size
                    files.append({"path": rel, "size": size})
                    total_size += size

    # Fee estimates
    fee_file = bitcoin_dir / "fee_estimates.dat"
    if fee_file.exists():
        size = fee_file.stat().st_size
        files.append({"path": "fee_estimates.dat", "size": size})
        total_size += size

    # Block filters (optional)
    if include_filters:
        filter_dir = bitcoin_dir / "indexes" / "blockfilter" / "basic"
        if filter_dir.exists() and len(list(filter_dir.iterdir())) > 1:
            for f in filter_dir.rglob("*"):
                if f.is_file():
                    rel = f"indexes/blockfilter/basic/{f.relative_to(filter_dir)}"
                    size = f.stat().st_size
                    files.append({"path": rel, "size": size})
                    total_size += size

    return {
        "files": files,
        "totalSize": total_size,
        "fileCount": len(files),
    }

# ---------------------------------------------------------------------------
# HTTP Handler (mirrors ShareServer.kt API)
# ---------------------------------------------------------------------------

class RelayHandler(BaseHTTPRequestHandler):
    """HTTP handler implementing the same API as ShareServer.kt."""

    server_version = f"PocketNodeRelay/{__version__}"

    def log_message(self, format, *args):
        logger.info(format % args)

    def do_GET(self):
        path = self.path.split("?")[0]  # strip query string

        if path == "/":
            self._serve_dashboard()
        elif path == "/info":
            self._serve_info()
        elif path == "/manifest":
            self._serve_manifest()
        elif path == "/peer-limits":
            self._serve_peer_limits()
        elif path == "/status":
            self._serve_status()
        elif path.startswith("/file/"):
            self._serve_file(path[6:])  # strip "/file/"
        else:
            self._send_text(404, f"Not found: {path}")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/fetch":
            self._handle_fetch()
        else:
            self._send_text(404, f"Not found: {path}")

    # -- Endpoints --

    def _serve_dashboard(self):
        html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pocket Node Relay</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#121212;color:#e0e0e0;min-height:100vh;padding:24px;display:flex;justify-content:center}
  .container{max-width:600px;width:100%}
  h1{font-size:28px;color:#fff;text-align:center;margin-bottom:4px}
  .subtitle{color:#999;font-size:14px;text-align:center;margin-bottom:24px}
  .card{background:#1e1e1e;border-radius:16px;padding:24px;margin-bottom:16px}
  .card h2{font-size:16px;color:#FF9800;margin-bottom:16px}
  .stat{display:flex;justify-content:space-between;padding:6px 0}
  .stat-label{color:#999}
  .stat-value{color:#fff;font-weight:600;font-family:monospace}
  .filters{color:#4CAF50;font-size:13px;margin-top:4px}
  .qr-row{display:flex;gap:16px;justify-content:center;flex-wrap:wrap}
  .qr-box{background:#fff;border-radius:12px;padding:16px;text-align:center}
  .qr-box canvas{display:block;margin:0 auto 8px}
  .qr-label{font-size:12px;color:#333;font-weight:600}
  .qr-addr{font-size:10px;color:#666;word-break:break-all;max-width:200px;margin-top:4px}
  .btn{display:block;width:100%;padding:14px;border-radius:12px;font-size:16px;font-weight:600;border:none;cursor:pointer;margin-top:12px}
  .btn-primary{background:#FF9800;color:#000}
  .btn-primary:hover{background:#FFA726}
  .btn-primary:disabled{background:#555;color:#888;cursor:not-allowed}
  input[type=text]{width:100%;padding:12px;border-radius:8px;border:1px solid #444;background:#2a2a2a;color:#fff;font-size:14px;margin-top:8px}
  input[type=text]:focus{outline:none;border-color:#FF9800}
  .progress-bar{width:100%;height:8px;background:#2a2a2a;border-radius:4px;margin-top:12px;overflow:hidden}
  .progress-fill{height:100%;background:#FF9800;border-radius:4px;transition:width 0.3s}
  .status-text{font-size:13px;color:#999;margin-top:8px;text-align:center}
  .error{color:#f44336}
  .success{color:#4CAF50}
  .hidden{display:none}
</style>
</head>
<body>
<div class="container">
  <h1>&#x20BF; Pocket Node Relay</h1>
  <div class="subtitle">Chainstate relay server for Bitcoin Pocket Node</div>

  <!-- Status Card -->
  <div class="card">
    <h2>&#x1F4E1; Relay Status</h2>
    <div id="stats">
      <div class="stat"><span class="stat-label">Chain height</span><span class="stat-value" id="height">loading...</span></div>
      <div class="stat"><span class="stat-label">Block filters</span><span class="stat-value" id="filters">...</span></div>
      <div class="stat"><span class="stat-label">Files</span><span class="stat-value" id="filecount">...</span></div>
      <div class="stat"><span class="stat-label">Total size</span><span class="stat-value" id="totalsize">...</span></div>
      <div class="stat"><span class="stat-label">Active transfers</span><span class="stat-value" id="transfers">...</span></div>
      <div class="stat"><span class="stat-label">Last updated</span><span class="stat-value" id="updated">...</span></div>
    </div>
  </div>

  <!-- QR Codes -->
  <div class="card">
    <h2>&#x1F4F1; Connect from Phone</h2>
    <div class="qr-row">
      <div class="qr-box">
        <canvas id="qr-lan"></canvas>
        <div class="qr-label">LAN</div>
        <div class="qr-addr" id="lan-addr"></div>
      </div>
      <div class="qr-box" id="tor-qr-box">
        <canvas id="qr-tor"></canvas>
        <div class="qr-label">Tor</div>
        <div class="qr-addr" id="tor-addr"></div>
      </div>
    </div>
  </div>

  <!-- Receive from Phone -->
  <div class="card">
    <h2>&#x1F4E5; Receive from Phone</h2>
    <p style="color:#999;font-size:13px">Enter the IP of a phone running "Share The Freedom" to pull chainstate.</p>
    <input type="text" id="phone-ip" placeholder="e.g. 192.168.1.42">
    <button class="btn btn-primary" id="fetch-btn" onclick="startFetch()">Fetch Chainstate</button>
    <div id="fetch-progress" class="hidden">
      <div class="progress-bar"><div class="progress-fill" id="progress-fill" style="width:0%"></div></div>
      <div class="status-text" id="fetch-status">Starting...</div>
    </div>
  </div>
</div>

<!-- QR Code library (minimal, no deps) -->
<script>
// QR Code generator (minimal implementation)
// Based on qrcode-generator by Kazuhiko Arase (MIT license)
var qrcode=function(){function r(r,t){var e=r,n=a[t],o=null,i=0,u=null,f=[],v={},c=function(r,t){o=function(r){for(var t=new Array(r),e=0;e<r;e++){t[e]=new Array(r);for(var n=0;n<r;n++)t[e][n]=null}return t}(i=4*e+17);w(0,0);w(i-7,0);w(0,i-7);A(r,t)};v.addData=function(r){var t=new l(r);f.push(t);u=null};v.make=function(){if(f.length==0)return;var r=0;var t=0;var e=[];for(var n=0;n<f.length;n++){var o=f[n];e.push({mode:4,data:o.data})}var a=1e9;for(var n=0;n<e.length;n++){var i=h(e[n].mode,e[n].data.length);if(i<0)throw"data too long";if(a>i)a=i}u=0;for(var n=0;n<e.length;n++){u+=4;u+=s(e[n].mode,a);u+=e[n].data.length*8}var l=g(a);if(u>l*8)throw"data too long";if(u+4<=l*8)u+=4;while(u%8!=0)u++;while(true){if(u>=l*8)break;u+=8;if(u>=l*8)break;u+=8}var v=0;var c=0;var p=[];for(var n=0;n<e.length;n++){p.push(e[n].data)}var m=d(a,t,p);c(a,m)};v.getModuleCount=function(){return i};v.isDark=function(r,t){if(r<0||i<=r||t<0||i<=t)throw r+","+t;return o[r][t]};var w=function(r,t){for(var e=-1;e<=7;e++)if(!(r+e<=-1||i<=r+e))for(var n=-1;n<=7;n++)if(!(t+n<=-1||i<=t+n))if(0<=e&&e<=6&&(n==0||n==6)||0<=n&&n<=6&&(e==0||e==6)||2<=e&&e<=4&&2<=n&&n<=4)o[r+e][t+n]=true;else o[r+e][t+n]=false};var A=function(r,t){for(var a=i-1;a>=0;a-=2){if(a==6)a--;for(var f=-1;f<=i;f++){var s=a%2==0;var l=null;if(o[s?f:i-1-f][a]!=null){continue}if(t<8*e.length){l=(t>>>3<e.length)&&((e.charCodeAt(t>>>3)>>>(7-t%8))&1)==1;t++}else if(t<8*e.length+4){l=((n>>>(3-t%4))&1)==1;t++}o[s?f:i-1-f][a]=l}}var e=function(){var t=g(r);var a=[];for(var o=0;o<f.length;o++){var i=f[o];a.push(i.data)}return d(r,0,a)}()};return v}function a(r){switch(r){case 0:return 7;case 1:return 10;case 2:return 13;case 3:return 17;default:return 0}}function s(r,t){return 8}function g(r){return[19,34,55,80,108,136,156,194,232,274,324,370,428,461,523,589,659,720,790,858,929,1003,1091,1171,1273,1367,1465,1528,1628,1732,1840,1952,2068,2188,2303,2431,2563,2699,2809,2953][r-1]}function h(r,t){for(var e=1;e<=40;e++){var n=g(e);if(t*8+4+s(r,e)<=n*8)return e}return-1}function d(r,t,e){for(var n="",a=0;a<e.length;a++){n+=e[a]}var o=g(r);var i=n.length;var u=[];u.push(64|i>>4);u.push((i&15)<<4);for(var f=0;f<i;f++){u[u.length-1]|=n.charCodeAt(f)>>>(f%2==0?4:0)&15;if(f%2==0)u.push((n.charCodeAt(f)&15)<<4)}if(u.length<o)u.push(0);while(u.length<o){u.push(236);if(u.length<o)u.push(17)}return u}function l(r){this.data=r}return r}();

function makeQR(canvasId, text, size) {
  // Use a simple QR approach via Google Charts API image
  var canvas = document.getElementById(canvasId);
  var img = new Image();
  img.crossOrigin = "anonymous";
  img.onload = function() {
    canvas.width = size;
    canvas.height = size;
    var ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0, size, size);
  };
  img.src = "https://chart.googleapis.com/chart?cht=qr&chs=" + size + "x" + size + "&chl=" + encodeURIComponent(text) + "&choe=UTF-8";
}

function formatBytes(b) {
  if (b < 1024) return b + " B";
  if (b < 1048576) return (b/1024).toFixed(1) + " KB";
  if (b < 1073741824) return (b/1048576).toFixed(1) + " MB";
  return (b/1073741824).toFixed(2) + " GB";
}

function refreshStatus() {
  fetch("/info").then(r => r.json()).then(d => {
    document.getElementById("height").textContent = d.chainHeight.toLocaleString();
    document.getElementById("filters").textContent = d.hasFilters ? "yes ⚡" : "no";
    document.getElementById("transfers").textContent = d.activeTransfers + "/" + d.maxConcurrent;
  });
  fetch("/status").then(r => r.json()).then(d => {
    document.getElementById("filecount").textContent = d.fileCount.toLocaleString();
    document.getElementById("totalsize").textContent = formatBytes(d.totalSize);
    document.getElementById("updated").textContent = d.lastFetched || "never";
    // LAN QR
    var lanUrl = "http://" + d.lanAddress + ":8432";
    document.getElementById("lan-addr").textContent = d.lanAddress + ":8432";
    makeQR("qr-lan", lanUrl, 160);
    // Tor QR
    if (d.onionAddress) {
      var torUrl = "http://" + d.onionAddress + ":8432";
      document.getElementById("tor-addr").textContent = d.onionAddress.substring(0,16) + "...:8432";
      makeQR("qr-tor", torUrl, 160);
    } else {
      document.getElementById("tor-qr-box").classList.add("hidden");
    }
  });
}

function startFetch() {
  var ip = document.getElementById("phone-ip").value.trim();
  if (!ip) return;
  document.getElementById("fetch-btn").disabled = true;
  document.getElementById("fetch-progress").classList.remove("hidden");
  document.getElementById("fetch-status").textContent = "Connecting to " + ip + "...";
  document.getElementById("progress-fill").style.width = "0%";

  fetch("/fetch", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({host:ip})})
    .then(r => r.json())
    .then(d => {
      if (d.error) {
        document.getElementById("fetch-status").innerHTML = '<span class="error">' + d.error + '</span>';
        document.getElementById("fetch-btn").disabled = false;
      } else {
        pollFetch();
      }
    });
}

function pollFetch() {
  fetch("/status").then(r => r.json()).then(d => {
    if (d.fetchState === "running") {
      document.getElementById("fetch-status").textContent = d.fetchProgress || "Downloading...";
      var pct = d.fetchPercent || 0;
      document.getElementById("progress-fill").style.width = pct + "%";
      setTimeout(pollFetch, 1000);
    } else if (d.fetchState === "done") {
      document.getElementById("fetch-status").innerHTML = '<span class="success">✅ Download complete!</span>';
      document.getElementById("progress-fill").style.width = "100%";
      document.getElementById("fetch-btn").disabled = false;
      refreshStatus();
    } else if (d.fetchState === "error") {
      document.getElementById("fetch-status").innerHTML = '<span class="error">❌ ' + (d.fetchError||"Failed") + '</span>';
      document.getElementById("fetch-btn").disabled = false;
    } else {
      document.getElementById("fetch-btn").disabled = false;
    }
  });
}

refreshStatus();
setInterval(refreshStatus, 10000);
</script>
</body>
</html>"""
        self._send_text(200, html, content_type="text/html")

    def _serve_info(self):
        cfg = self.server.relay_config
        bitcoin_dir = get_bitcoin_dir(cfg)
        max_concurrent = cfg.get("relay", {}).get("max_concurrent", 4)

        info = {
            "version": cfg.get("relay", {}).get("version", __version__),
            "chainHeight": get_chain_height(bitcoin_dir),
            "hasFilters": has_block_filters(bitcoin_dir),
            "maxConcurrent": max_concurrent,
            "activeTransfers": self.server.active_transfers,
        }
        self._send_json(200, info)

    def _serve_manifest(self):
        cfg = self.server.relay_config
        bitcoin_dir = get_bitcoin_dir(cfg)
        max_concurrent = cfg.get("relay", {}).get("max_concurrent", 4)

        if self.server.active_transfers >= max_concurrent:
            self._send_text(503, f"Max concurrent transfers reached ({max_concurrent}). Try again later.")
            return

        manifest = build_manifest(bitcoin_dir)
        self._send_json(200, manifest)

    def _serve_status(self):
        """Extended status for the dashboard."""
        cfg = self.server.relay_config
        bitcoin_dir = get_bitcoin_dir(cfg)
        manifest = build_manifest(bitcoin_dir)

        # Get LAN IP
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
            s.close()
        except Exception:
            lan_ip = "unknown"

        # Last fetched time
        height_file = Path("chain_height.json")
        last_fetched = None
        if height_file.exists():
            try:
                data = json.loads(height_file.read_text())
                last_fetched = data.get("fetchedAt")
            except Exception:
                pass

        status = {
            "fileCount": manifest["fileCount"],
            "totalSize": manifest["totalSize"],
            "lanAddress": lan_ip,
            "onionAddress": self.server.onion_address,
            "lastFetched": last_fetched,
            "fetchState": self.server.fetch_status.get("state", "idle") if self.server.fetch_status else "idle",
            "fetchProgress": self.server.fetch_status.get("progress", "") if self.server.fetch_status else "",
            "fetchPercent": self.server.fetch_status.get("percent", 0) if self.server.fetch_status else 0,
            "fetchError": self.server.fetch_status.get("error", "") if self.server.fetch_status else "",
        }
        self._send_json(200, status)

    def _handle_fetch(self):
        """Trigger chainstate fetch from a phone."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode() if content_length > 0 else "{}"
        try:
            data = json.loads(body)
        except Exception:
            self._send_json(400, {"error": "Invalid JSON"})
            return

        host = data.get("host", "").strip()
        if not host:
            self._send_json(400, {"error": "Missing 'host' field"})
            return

        if self.server.fetch_status and self.server.fetch_status.get("state") == "running":
            self._send_json(409, {"error": "Fetch already in progress"})
            return

        port = data.get("port", 8432)
        no_filters = data.get("noFilters", False)

        # Start fetch in background thread
        self.server.fetch_status = {"state": "running", "progress": "Connecting...", "percent": 0}

        def run_fetch():
            try:
                import subprocess
                cfg = self.server.relay_config
                cmd = [
                    sys.executable, "fetch.py", host,
                    "--port", str(port),
                    "-c", "config.yaml",
                    "--clean", "-j", "4",
                ]
                if no_filters:
                    cmd.append("--no-filters")

                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    cwd=str(Path(__file__).parent), text=True
                )

                for line in proc.stdout:
                    line = line.strip()
                    if "%" in line and "GB" in line:
                        # Parse progress line
                        try:
                            pct = int(line.split("%")[0].strip())
                            self.server.fetch_status = {
                                "state": "running",
                                "progress": line[:80],
                                "percent": pct,
                            }
                        except Exception:
                            self.server.fetch_status["progress"] = line[:80]

                proc.wait()
                if proc.returncode == 0:
                    self.server.fetch_status = {"state": "done", "progress": "Complete", "percent": 100}
                else:
                    self.server.fetch_status = {"state": "error", "error": "Fetch failed", "percent": 0}
            except Exception as e:
                self.server.fetch_status = {"state": "error", "error": str(e), "percent": 0}

        t = threading.Thread(target=run_fetch, daemon=True)
        t.start()
        self._send_json(200, {"status": "started"})

    def _serve_peer_limits(self):
        """Serve cached peer channel minimums (relay learns from connected phones)."""
        limits_file = Path("peer_limits.json")
        if limits_file.exists():
            try:
                data = json.loads(limits_file.read_text())
            except Exception:
                data = {}
        else:
            data = {}
        self._send_json(200, data)

    def _serve_file(self, relative_path: str):
        # Security: prevent path traversal
        normalized = relative_path.replace("\\", "/")
        if ".." in normalized or normalized.startswith("/"):
            self._send_text(403, "Invalid path")
            return

        # Only allow files under known directories, plus fee_estimates.dat
        if not any(normalized.startswith(p) for p in ALLOWED_PREFIXES):
            if normalized not in ("fee_estimates.dat",):
                self._send_text(403, f"Path not in allowed directories: {normalized}")
                return

        cfg = self.server.relay_config
        bitcoin_dir = get_bitcoin_dir(cfg)
        file_path = bitcoin_dir / normalized

        if not file_path.exists() or not file_path.is_file():
            self._send_text(404, f"File not found: {normalized}")
            return

        # Resolve to ensure we're still under bitcoin_dir
        try:
            file_path.resolve().relative_to(bitcoin_dir.resolve())
        except ValueError:
            self._send_text(403, "Path traversal detected")
            return

        file_size = file_path.stat().st_size
        self.server.active_transfers += 1
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(file_size))
            self.send_header("Connection", "close")
            self.end_headers()

            with open(file_path, "rb") as f:
                buf_size = 65536
                while True:
                    chunk = f.read(buf_size)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            logger.warning(f"Client disconnected during transfer: {normalized}")
        finally:
            self.server.active_transfers -= 1

    # -- Helpers --

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, text: str, content_type: str = "text/plain"):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class RelayHTTPServer(HTTPServer):
    """Extended HTTPServer with relay state."""
    def __init__(self, *args, relay_config=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.relay_config = relay_config or {}
        self.active_transfers = 0
        self.onion_address = None
        self.fetch_status = None  # {"state": "idle|running|done|error", "progress": "", "error": ""}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Pocket Node Relay Server")
    parser.add_argument("-c", "--config", default="config.yaml", help="Config file path")
    parser.add_argument("--host", default=None, help="Override bind host")
    parser.add_argument("--port", type=int, default=None, help="Override bind port")
    args = parser.parse_args()

    cfg = load_config(args.config)

    # Logging
    log_level = cfg.get("log_level", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Validate bitcoin datadir
    bitcoin_dir = get_bitcoin_dir(cfg)
    if not bitcoin_dir.exists():
        logger.error(f"Bitcoin data directory not found: {bitcoin_dir}")
        sys.exit(1)

    chainstate_dir = bitcoin_dir / "chainstate"
    if not chainstate_dir.exists():
        logger.warning(f"Chainstate directory not found: {chainstate_dir}")
        logger.warning("The relay will start but has nothing to serve until bitcoind syncs.")

    # Server config
    host = args.host or cfg.get("server", {}).get("host", "127.0.0.1")
    port = args.port or cfg.get("server", {}).get("port", 8432)

    # Show startup info
    height = get_chain_height(bitcoin_dir)
    filters = has_block_filters(bitcoin_dir)
    logger.info(f"Bitcoin datadir: {bitcoin_dir}")
    logger.info(f"Chain height: {height:,}")
    logger.info(f"Block filters: {'yes' if filters else 'no'}")

    # Check for .onion address
    tor_cfg = cfg.get("tor", {})
    if tor_cfg.get("enabled"):
        hs_dir = Path(tor_cfg.get("hidden_service_dir", "/var/lib/tor/pocket-relay"))
        hostname_file = hs_dir / "hostname"
        try:
            if hostname_file.exists():
                onion = hostname_file.read_text().strip()
                logger.info(f"Tor hidden service: {onion}:{tor_cfg.get('hidden_service_port', 8432)}")
            else:
                logger.info("Tor hidden service configured but hostname not yet generated.")
                logger.info("Start Tor and the hostname file will be created.")
        except PermissionError:
            logger.info("Tor hidden service configured (cannot read hostname file — run as root or add user to debian-tor group)")

    # Start server
    server = RelayHTTPServer((host, port), RelayHandler, relay_config=cfg)

    # Store onion address if available
    if tor_cfg.get("enabled"):
        hs_dir2 = Path(tor_cfg.get("hidden_service_dir", "/var/lib/tor/pocket-relay"))
        try:
            hf = hs_dir2 / "hostname"
            if hf.exists():
                server.onion_address = hf.read_text().strip()
        except Exception:
            pass

    def shutdown_handler(sig, frame):
        logger.info("Shutting down...")
        server.shutdown()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    logger.info(f"Relay server listening on {host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        logger.info("Server stopped.")


if __name__ == "__main__":
    main()
