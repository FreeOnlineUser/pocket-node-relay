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
            self._serve_landing()
        elif path == "/info":
            self._serve_info()
        elif path == "/manifest":
            self._serve_manifest()
        elif path == "/peer-limits":
            self._serve_peer_limits()
        elif path.startswith("/file/"):
            self._serve_file(path[6:])  # strip "/file/"
        else:
            self._send_text(404, f"Not found: {path}")

    # -- Endpoints --

    def _serve_landing(self):
        cfg = self.server.relay_config
        bitcoin_dir = get_bitcoin_dir(cfg)
        height = get_chain_height(bitcoin_dir)
        filters = has_block_filters(bitcoin_dir)
        version = cfg.get("relay", {}).get("version", __version__)

        filters_html = '<div class="filters">&#x26A1; Lightning block filters included</div>' if filters else ""

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pocket Node Relay</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #121212; color: #e0e0e0;
    display: flex; justify-content: center; align-items: center;
    min-height: 100vh; padding: 24px;
  }}
  .card {{
    background: #1e1e1e; border-radius: 16px; padding: 32px;
    max-width: 400px; width: 100%; text-align: center;
  }}
  h1 {{ font-size: 24px; color: #fff; margin-bottom: 4px; }}
  .subtitle {{ color: #999; font-size: 14px; margin-bottom: 24px; }}
  .stats {{
    background: #2a2a2a; border-radius: 12px; padding: 16px;
    margin-bottom: 24px; text-align: left;
  }}
  .stat {{ display: flex; justify-content: space-between; padding: 6px 0; }}
  .stat-label {{ color: #999; }}
  .stat-value {{ color: #fff; font-weight: 600; font-family: monospace; }}
  .filters {{ color: #4CAF50; font-size: 13px; margin-top: 4px; }}
  .note {{ color: #666; font-size: 12px; margin-top: 16px; line-height: 1.5; }}
</style>
</head>
<body>
<div class="card">
  <h1>&#x20BF; Pocket Node Relay</h1>
  <div class="subtitle">A relay server is sharing validated Bitcoin chainstate</div>
  <div class="stats">
    <div class="stat"><span class="stat-label">Block height</span><span class="stat-value">{height:,}</span></div>
    <div class="stat"><span class="stat-label">Relay version</span><span class="stat-value">{version}</span></div>
    <div class="stat"><span class="stat-label">Type</span><span class="stat-value">Headless relay</span></div>
    {filters_html}
  </div>
  <div class="note">
    Open Pocket Node on your phone and choose "Copy from nearby phone" during setup.
    Enter this server's .onion address to begin syncing.
  </div>
</div>
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
