# Pocket Node Relay

Headless chainstate relay server for [Bitcoin Pocket Node](https://github.com/FreeOnlineUser/bitcoin-pocket-node).

Serves validated Bitcoin chainstate over Tor so Pocket Node phones can bootstrap a full node without trusting a third party. Speaks the same HTTP protocol as the phone-to-phone sharing feature.

## What It Does

- Runs a pruned `bitcoind` and keeps chainstate current
- Serves chainstate snapshots via the same HTTP API the app uses for phone-to-phone sharing
- Exposes the server as a Tor hidden service (`.onion` address)
- Pocket Node's existing `ShareClient` connects without modification

## Endpoints (same as phone ShareServer)

| Endpoint | Description |
|---|---|
| `GET /` | Landing page (browser-friendly) |
| `GET /info` | Node info: height, version, filters |
| `GET /manifest` | File list with sizes for download planning |
| `GET /file/{path}` | Individual file download |
| `GET /peer-limits` | Learned channel opening minimums |

## Requirements

- Linux (Debian 12 recommended)
- Python 3.10+
- Bitcoin Core (bitcoind)
- Tor

## Quick Start

```bash
# Clone
git clone https://github.com/FreeOnlineUser/pocket-node-relay.git
cd pocket-node-relay

# Install
chmod +x install.sh
sudo ./install.sh

# Configure
cp config.example.yaml config.yaml
nano config.yaml  # Set your bitcoind path

# Run
python3 relay.py
```

## Architecture

```
Phone (Arti client) --> .onion --> Tor --> localhost:8432 --> relay.py --> bitcoind data
```

The relay is the server-side equivalent of the phone's `ShareServer.kt`. Phones connect using their existing `ShareClient` over Tor, same as they would to another phone on LAN, but routed through onion services.

## License

MIT
