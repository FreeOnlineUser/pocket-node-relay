#!/usr/bin/env python3
"""
Fetch chainstate from a phone's ShareServer.

Acts as the receiving side of phone-to-phone sharing.
Downloads manifest, then all files into the local data directory
so the relay can serve them to other phones.

Usage:
    python3 fetch.py <phone-ip> [--port 8432] [--no-filters] [--resume] [--parallel 4]
    python3 fetch.py 10.0.1.42
    python3 fetch.py 10.0.1.42 --resume --parallel 8
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Shared progress state for parallel downloads
# ---------------------------------------------------------------------------

class ProgressTracker:
    def __init__(self, total_files: int, total_bytes: int):
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.completed_files = 0
        self.completed_bytes = 0
        self.skipped_files = 0
        self.skipped_bytes = 0
        self.failed = []
        self.active = {}  # thread_id -> (path, downloaded, total)
        self.lock = threading.Lock()
        self.start_time = time.time()
        self._last_print = 0

    def skip(self, path: str, size: int):
        with self.lock:
            self.skipped_files += 1
            self.skipped_bytes += size

    def start_file(self, thread_id: int, path: str, size: int):
        with self.lock:
            self.active[thread_id] = (path, 0, size)

    def update_file(self, thread_id: int, downloaded: int):
        with self.lock:
            if thread_id in self.active:
                path, _, total = self.active[thread_id]
                self.active[thread_id] = (path, downloaded, total)
        self._maybe_print()

    def finish_file(self, thread_id: int, size: int):
        with self.lock:
            self.completed_files += 1
            self.completed_bytes += size
            self.active.pop(thread_id, None)
        self._maybe_print()

    def fail_file(self, thread_id: int, path: str):
        with self.lock:
            self.failed.append(path)
            self.active.pop(thread_id, None)

    def _maybe_print(self):
        now = time.time()
        if now - self._last_print < 0.5:
            return
        self._last_print = now

        with self.lock:
            done = self.completed_bytes + self.skipped_bytes
            # Add partial progress from active downloads
            for _, (_, downloaded, _) in self.active.items():
                done += downloaded

            elapsed = now - self.start_time
            speed = done / elapsed if elapsed > 0 else 0
            pct = (done * 100 // self.total_bytes) if self.total_bytes > 0 else 0
            n_done = self.completed_files + self.skipped_files
            n_active = len(self.active)

            # Show active file names (shortened)
            active_names = []
            for _, (path, dl, total) in self.active.items():
                name = path.split("/")[-1]
                if total > 0:
                    fpct = dl * 100 // total
                    active_names.append(f"{name} {fpct}%")
                else:
                    active_names.append(name)

        active_str = " | ".join(active_names[:4])
        if len(active_names) > 4:
            active_str += f" +{len(active_names) - 4}"

        print(f"\r  {pct:3d}%  {format_bytes(done)}/{format_bytes(self.total_bytes)}  "
              f"{format_speed(speed)}  "
              f"[{n_done}/{self.total_files}]  "
              f"{active_str}        ", end="", flush=True)


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
    url = f"http://{host}:{port}/info"
    try:
        req = urllib.request.urlopen(url, timeout=5)
        return json.loads(req.read())
    except Exception as e:
        print(f"Failed to connect to {host}:{port}: {e}")
        sys.exit(1)


def get_manifest(host: str, port: int) -> dict:
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
    url = f"http://{host}:{port}/peer-limits"
    try:
        req = urllib.request.urlopen(url, timeout=5)
        limits = json.loads(req.read())
        if limits:
            limits_file = Path("peer_limits.json")
            existing = {}
            if limits_file.exists():
                try:
                    existing = json.loads(limits_file.read_text())
                except Exception:
                    pass
            merged = 0
            for key, value in limits.items():
                old = existing.get(key)
                if isinstance(value, bool):
                    # Booleans: always take new value
                    if old != value:
                        existing[key] = value
                        merged += 1
                elif isinstance(value, (int, float)):
                    # Numbers: keep the larger value
                    if old is None or (isinstance(old, (int, float)) and value > old):
                        existing[key] = value
                        merged += 1
            limits_file.write_text(json.dumps(existing, indent=2))
            if merged:
                print(f"  Merged {merged} peer channel limits")
    except Exception:
        pass


def start_session(host: str, port: int, total_files: int):
    """Tell the sender how many files we'll download (for progress display)."""
    url = f"http://{host}:{port}/start-session"
    try:
        data = json.dumps({"totalFiles": total_files}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # Non-critical, sender just won't show total progress


def complete_session(host: str, port: int):
    """Tell the sender we're done downloading (triggers '✅ Freedom shared!')."""
    url = f"http://{host}:{port}/complete"
    try:
        req = urllib.request.Request(url, data=b'{}', headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def download_one(host: str, port: int, file_path: str, dest: Path, 
                 file_size: int, tracker: ProgressTracker) -> bool:
    """Download a single file. Called from thread pool."""
    thread_id = threading.get_ident()
    url = f"http://{host}:{port}/file/{file_path}"
    dest.parent.mkdir(parents=True, exist_ok=True)

    tracker.start_file(thread_id, file_path, file_size)
    try:
        req = urllib.request.urlopen(url, timeout=60)
        buf_size = 65536
        downloaded = 0

        with open(dest, "wb") as f:
            while True:
                chunk = req.read(buf_size)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if downloaded % (256 * 1024) < buf_size:
                    tracker.update_file(thread_id, downloaded)

        tracker.finish_file(thread_id, file_size)
        return True
    except Exception:
        tracker.fail_file(thread_id, file_path)
        # Remove partial file
        try:
            if dest.exists():
                dest.unlink()
        except Exception:
            pass
        return False


def main():
    parser = argparse.ArgumentParser(description="Fetch chainstate from a phone's ShareServer")
    parser.add_argument("host", help="Phone IP address or hostname")
    parser.add_argument("--port", type=int, default=8432, help="ShareServer port (default: 8432)")
    parser.add_argument("--no-filters", action="store_true", help="Skip block filter download")
    parser.add_argument("-c", "--config", default="config.yaml", help="Config file path")
    parser.add_argument("--clean", action="store_true", help="Delete existing data before downloading")
    parser.add_argument("--full", action="store_true", help="Re-download all files (default: incremental, skip unchanged)")
    parser.add_argument("--parallel", "-j", type=int, default=4, help="Parallel downloads (default: 4)")
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

    # Clean existing data if requested
    if args.clean and data_dir.exists():
        import shutil
        for subdir in ("chainstate", "blocks", "indexes"):
            p = data_dir / subdir
            if p.exists():
                shutil.rmtree(p)
                print(f"  Cleaned {p}")

    # Ensure data directory exists
    data_dir.mkdir(parents=True, exist_ok=True)

    # Fetch peer limits
    fetch_peer_limits(args.host, args.port, data_dir)

    # Build download list — incremental by default (skip unchanged files)
    manifest_paths = set()
    download_list = []
    skip_bytes = 0
    skip_count = 0
    for file_info in files:
        file_path = file_info["path"]
        file_size = file_info["size"]
        dest = data_dir / file_path
        manifest_paths.add(file_path)

        if not args.full and dest.exists() and dest.stat().st_size == file_size:
            skip_bytes += file_size
            skip_count += 1
            continue
        download_list.append((file_path, file_size, dest))

    if skip_count:
        print(f"  Skipping {skip_count} unchanged files ({format_bytes(skip_bytes)})")

    # Note: stale file cleanup runs AFTER successful download (see below)

    remaining = sum(s for _, s, _ in download_list)
    print(f"  Downloading {len(download_list)} files ({format_bytes(remaining)})")
    print(f"  Parallel: {args.parallel} connections")
    print()

    if not download_list:
        print("✅ Nothing to download — all files present.")
        return

    # Announce session to sender for progress tracking
    start_session(args.host, args.port, len(download_list))

    # Parallel download
    tracker = ProgressTracker(len(files), total_size)
    tracker.skipped_files = skip_count
    tracker.skipped_bytes = skip_bytes

    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {}
        for file_path, file_size, dest in download_list:
            fut = pool.submit(download_one, args.host, args.port, file_path, dest, file_size, tracker)
            futures[fut] = file_path

        for fut in as_completed(futures):
            pass  # Results tracked by ProgressTracker

    print()  # newline after progress line

    # Summary
    elapsed = time.time() - tracker.start_time
    total_done = tracker.completed_bytes + tracker.skipped_bytes
    avg_speed = tracker.completed_bytes / elapsed if elapsed > 0 else 0

    # Tell sender we're done
    if not tracker.failed:
        complete_session(args.host, args.port)

        # Clean up stale files only after successful download
        # Only clean dirs that have files in the manifest (don't touch others)
        manifest_dirs = set()
        for p in manifest_paths:
            parts = p.split("/")
            if len(parts) > 1:
                manifest_dirs.add(parts[0])  # e.g. "chainstate", "blocks", "indexes"
        
        stale_count = 0
        for top_dir in manifest_dirs:
            local_dir = data_dir / top_dir
            if not local_dir.exists():
                continue
            for f in local_dir.rglob("*"):
                if f.is_file():
                    rel = str(f.relative_to(data_dir))
                    if rel not in manifest_paths:
                        f.unlink()
                        stale_count += 1
        if stale_count:
            print(f"  Cleaned {stale_count} stale files")

    print()
    print("=" * 50)
    if tracker.skipped_files:
        print(f"  Skipped:  {tracker.skipped_files} existing files ({format_bytes(tracker.skipped_bytes)})")
    if not tracker.failed:
        print(f"✅ Download complete!")
    else:
        print(f"⚠️  {len(tracker.failed)} files failed (re-run with --resume to retry)")
        for f in tracker.failed[:20]:
            print(f"    - {f}")
        if len(tracker.failed) > 20:
            print(f"    ... and {len(tracker.failed) - 20} more")
    print(f"  Downloaded: {format_bytes(tracker.completed_bytes)} in {elapsed:.1f}s ({format_speed(avg_speed)})")
    print(f"  Total:      {format_bytes(total_done)} / {format_bytes(total_size)}")
    print(f"  Stored:     {data_dir}")
    print()
    # Save chain height for the relay to serve
    height_file = Path("chain_height.json")
    height_file.write_text(json.dumps({
        "chainHeight": info.get("chainHeight", 0),
        "version": info.get("version", "unknown"),
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, indent=2))
    print(f"  Height:   {info.get('chainHeight', 0):,} (saved to chain_height.json)")

    print()
    print("The relay is now ready to serve this chainstate to other phones.")
    print(f"  .onion address: check /var/lib/tor/pocket-relay/hostname")


if __name__ == "__main__":
    main()
