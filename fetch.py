#!/usr/bin/env python3
"""
Fetch chainstate from a phone's ShareServer.

Acts as the receiving side of phone-to-phone sharing.
Downloads manifest, then all files into the local data directory
so the relay can serve them to other phones.

Usage:
    python3 fetch.py <phone-ip> [--port 8432] [--no-filters]
    python3 fetch.py 10.0.1.42
    python3 fetch.py 10.0.1.42 --no-filters
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

import yaml


def load_config(path: str = "config.yaml") -> dict:
    if not os.path.exists(path):
        print(f"Config file not found: {path}")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


def format_bytes(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    elif b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    elif b < 1024 * 1024 * 1024:
        return f"{b / (1024 * 1024):.1f} MB"
    else:
        return f"{b / (1024 * 1024 * 1024):.2f} GB"


def format_speed(bps: float) -> str:
    if bps < 1024 * 1024:
        return f"{bps / 1024:.0f} KB/s"
    else:
        return f"{bps / (1024 * 1024):.1f} MB/s"


def get_info(host: str, port: int) -> dict:
    """Fetch /info from the phone's ShareServer."""
    url = f"http://{host}:{port}/info"
    try:
        req = urllib.request.urlopen(url, timeout=5)
        return json.loads(req.read())
    except Exception as e:
        print(f"Failed to connect to {host}:{port}: {e}")
        sys.exit(1)


def get_manifest(host: str, port: int) -> dict:
    """Fetch /manifest from the phone's ShareServer."""
    url = f"http://{host}:{port}/manifest"
    try:
        req = urllib.request.urlopen(url, timeout=30)
        return json.loads(req.read())
    except urllib.error.HTTPError as e:
        if e.code == 503:
            print(f"Server busy: {e.read().decode()}")
        else:
            print(f"Failed to get manifest: HTTP {e.code}")
        sys.exit(1)
    except Exception as e:
        print(f"Failed to get manifest: {e}")
        sys.exit(1)


def fetch_peer_limits(host: str, port: int, data_dir: Path):
    """Fetch and save peer channel limits."""
    url = f"http://{host}:{port}/peer-limits"
    try:
        req = urllib.request.urlopen(url, timeout=5)
        limits = json.loads(req.read())
        if limits:
            limits_file = Path("peer_limits.json")
            # Merge with existing
            existing = {}
            if limits_file.exists():
                try:
                    existing = json.loads(limits_file.read_text())
                except Exception:
                    pass
            merged = 0
            for key, value in limits.items():
                if value > existing.get(key, -1):
                    existing[key] = value
                    merged += 1
            limits_file.write_text(json.dumps(existing, indent=2))
            if merged:
                print(f"  Merged {merged} peer channel limits")
    except Exception:
        pass  # Non-critical


def download_file(host: str, port: int, file_path: str, dest: Path, file_size: int) -> bool:
    """Download a single file from the ShareServer."""
    url = f"http://{host}:{port}/file/{file_path}"
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        req = urllib.request.urlopen(url, timeout=60)
        buf_size = 65536
        downloaded = 0
        start_time = time.time()
        last_report = start_time

        with open(dest, "wb") as f:
            while True:
                chunk = req.read(buf_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)

                # Progress update every 0.5s
                now = time.time()
                if now - last_report >= 0.5 or downloaded == file_size:
                    elapsed = now - start_time
                    speed = downloaded / elapsed if elapsed > 0 else 0
                    pct = (downloaded * 100 // file_size) if file_size > 0 else 100
                    print(f"\r    {pct:3d}%  {format_bytes(downloaded)} / {format_bytes(file_size)}  {format_speed(speed)}   ", end="", flush=True)
                    last_report = now

        print()  # newline after progress
        return True
    except Exception as e:
        print(f"\n    Error: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Fetch chainstate from a phone's ShareServer")
    parser.add_argument("host", help="Phone IP address or hostname")
    parser.add_argument("--port", type=int, default=8432, help="ShareServer port (default: 8432)")
    parser.add_argument("--no-filters", action="store_true", help="Skip block filter download")
    parser.add_argument("-c", "--config", default="config.yaml", help="Config file path")
    parser.add_argument("--clean", action="store_true", help="Delete existing data before downloading")
    parser.add_argument("--resume", action="store_true", help="Skip files that already exist with correct size")
    args = parser.parse_args()

    cfg = load_config(args.config)
    data_dir = Path(cfg["bitcoin_datadir"]).expanduser()

    # Connect and show info
    print(f"Connecting to {args.host}:{args.port}...")
    info = get_info(args.host, args.port)

    print(f"\n  Node found:")
    print(f"    Version:     {info['version']}")
    print(f"    Block height: {info['chainHeight']:,}")
    print(f"    Filters:     {'yes' if info.get('hasFilters') else 'no'}")
    print(f"    Transfers:   {info['activeTransfers']}/{info['maxConcurrent']}")
    print()

    # Get manifest
    print("Fetching file list...")
    manifest = get_manifest(args.host, args.port)
    files = manifest["files"]
    total_size = manifest["totalSize"]

    # Filter out block filters if requested
    if args.no_filters:
        files = [f for f in files if not f["path"].startswith("indexes/")]
        total_size = sum(f["size"] for f in files)

    print(f"  {len(files)} files, {format_bytes(total_size)} total")
    print()

    # Clean existing data if requested
    if args.clean and data_dir.exists():
        import shutil
        for subdir in ("chainstate", "blocks"):
            p = data_dir / subdir
            if p.exists():
                shutil.rmtree(p)
                print(f"  Cleaned {p}")
        idx_dir = data_dir / "indexes"
        if idx_dir.exists():
            shutil.rmtree(idx_dir)
            print(f"  Cleaned {idx_dir}")
        print()

    # Ensure data directory exists
    data_dir.mkdir(parents=True, exist_ok=True)

    # Fetch peer limits (non-blocking, non-critical)
    fetch_peer_limits(args.host, args.port, data_dir)

    # Download all files
    print(f"Downloading to {data_dir}...")
    print()

    overall_start = time.time()
    bytes_done = 0
    skipped = 0
    failed = []

    for i, file_info in enumerate(files):
        file_path = file_info["path"]
        file_size = file_info["size"]
        dest = data_dir / file_path

        short_name = file_path.split("/")[-1]

        # Skip existing files in resume mode
        if args.resume and dest.exists() and dest.stat().st_size == file_size:
            bytes_done += file_size
            skipped += 1
            continue

        print(f"  [{i + 1}/{len(files)}] {file_path} ({format_bytes(file_size)})")

        if download_file(args.host, args.port, file_path, dest, file_size):
            bytes_done += file_size
        else:
            failed.append(file_path)

    # Summary
    elapsed = time.time() - overall_start
    avg_speed = bytes_done / elapsed if elapsed > 0 else 0

    print()
    print("=" * 50)
    if skipped:
        print(f"  Skipped:  {skipped} existing files")
    if not failed:
        print(f"✅ Download complete!")
    else:
        print(f"⚠️  Download finished with {len(failed)} errors:")
        for f in failed:
            print(f"    - {f}")
    print(f"  Files:    {len(files) - len(failed)}/{len(files)}")
    print(f"  Size:     {format_bytes(bytes_done)}")
    print(f"  Time:     {elapsed:.1f}s")
    print(f"  Speed:    {format_speed(avg_speed)}")
    print(f"  Stored:   {data_dir}")
    print()
    print("The relay is now ready to serve this chainstate to other phones.")
    print(f"  .onion address: check /var/lib/tor/pocket-relay/hostname")


if __name__ == "__main__":
    main()
