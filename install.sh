#!/bin/bash
# Pocket Node Relay — Debian/Ubuntu installer
# Run as root: sudo ./install.sh

set -e

echo "=== Pocket Node Relay Installer ==="

# Check root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root: sudo ./install.sh"
    exit 1
fi

# Detect OS
if ! command -v apt-get &>/dev/null; then
    echo "This installer is for Debian/Ubuntu systems."
    exit 1
fi

echo ""
echo "1. Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv tor curl

echo ""
echo "2. Installing Python dependencies..."
cd "$(dirname "$0")"
python3 -m venv venv
source venv/bin/activate
pip install -q -r requirements.txt

echo ""
echo "3. Setting up Bitcoin Core..."
BITCOIN_VERSION="29.0"
ARCH=$(uname -m)
case "$ARCH" in
    x86_64)  BITCOIN_ARCH="x86_64-linux-gnu" ;;
    aarch64) BITCOIN_ARCH="aarch64-linux-gnu" ;;
    *)       echo "Unsupported architecture: $ARCH"; exit 1 ;;
esac

if ! command -v bitcoind &>/dev/null; then
    echo "   Downloading Bitcoin Core ${BITCOIN_VERSION}..."
    cd /tmp
    curl -sLO "https://bitcoincore.org/bin/bitcoin-core-${BITCOIN_VERSION}/bitcoin-${BITCOIN_VERSION}-${BITCOIN_ARCH}.tar.gz"
    tar xzf "bitcoin-${BITCOIN_VERSION}-${BITCOIN_ARCH}.tar.gz"
    install -m 0755 "bitcoin-${BITCOIN_VERSION}/bin/bitcoind" /usr/local/bin/
    install -m 0755 "bitcoin-${BITCOIN_VERSION}/bin/bitcoin-cli" /usr/local/bin/
    rm -rf "bitcoin-${BITCOIN_VERSION}" "bitcoin-${BITCOIN_VERSION}-${BITCOIN_ARCH}.tar.gz"
    echo "   Bitcoin Core ${BITCOIN_VERSION} installed."
    cd -
else
    echo "   bitcoind already installed: $(bitcoind --version | head -1)"
fi

echo ""
echo "4. Creating relay user..."
if ! id -u relay &>/dev/null; then
    useradd -r -m -s /bin/bash relay
    echo "   User 'relay' created."
else
    echo "   User 'relay' already exists."
fi

echo ""
echo "5. Setting up Bitcoin data directory..."
BITCOIN_DIR="/home/relay/.bitcoin"
mkdir -p "$BITCOIN_DIR"

if [ ! -f "$BITCOIN_DIR/bitcoin.conf" ]; then
    cat > "$BITCOIN_DIR/bitcoin.conf" << 'EOF'
# Pocket Node Relay — bitcoind config
# Pruned node, enough to serve chainstate snapshots

server=1
daemon=1
listen=0
txindex=0

# Prune to ~2GB (keeps tip blocks for sharing)
prune=2000

# RPC (localhost only, for monitoring)
rpcuser=relay
rpcpassword=CHANGE_ME_GENERATE_RANDOM
rpcallowip=127.0.0.1
rpcbind=127.0.0.1

# Block filters (optional, for Lightning support)
# Uncomment these if you want to serve filters to phones:
# blockfilterindex=1
# peerblockfilters=1
# listen=1
# bind=127.0.0.1

# Performance
dbcache=512
maxmempool=100
EOF
    echo "   bitcoin.conf created. EDIT THE RPC PASSWORD!"
else
    echo "   bitcoin.conf already exists."
fi

chown -R relay:relay "$BITCOIN_DIR"

echo ""
echo "6. Configuring Tor hidden service..."
TOR_CONF="/etc/tor/torrc"
if ! grep -q "pocket-relay" "$TOR_CONF" 2>/dev/null; then
    cat >> "$TOR_CONF" << 'EOF'

# Pocket Node Relay
HiddenServiceDir /var/lib/tor/pocket-relay/
HiddenServicePort 8432 127.0.0.1:8432
EOF
    echo "   Tor hidden service configured."
    systemctl restart tor
    sleep 2
    
    ONION_FILE="/var/lib/tor/pocket-relay/hostname"
    if [ -f "$ONION_FILE" ]; then
        echo ""
        echo "   Your .onion address: $(cat $ONION_FILE)"
    fi
else
    echo "   Tor hidden service already configured."
fi

echo ""
echo "7. Installing systemd services..."

# bitcoind service
cat > /etc/systemd/system/bitcoind-relay.service << 'EOF'
[Unit]
Description=Bitcoin Core (Pocket Node Relay)
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
User=relay
ExecStart=/usr/local/bin/bitcoind -datadir=/home/relay/.bitcoin -daemon
ExecStop=/usr/local/bin/bitcoin-cli -datadir=/home/relay/.bitcoin stop
Restart=on-failure
RestartSec=30
TimeoutStartSec=60
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
EOF

# Relay server service
RELAY_DIR="$(pwd)"
cat > /etc/systemd/system/pocket-relay.service << EOF
[Unit]
Description=Pocket Node Relay Server
After=network-online.target bitcoind-relay.service tor@default.service
Wants=bitcoind-relay.service

[Service]
Type=simple
User=relay
WorkingDirectory=${RELAY_DIR}
ExecStart=${RELAY_DIR}/venv/bin/python3 ${RELAY_DIR}/relay.py -c ${RELAY_DIR}/config.yaml
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
echo "   Services installed."

echo ""
echo "8. Setting up config..."
RELAY_DIR="$(pwd)"
if [ ! -f "$RELAY_DIR/config.yaml" ]; then
    cp "$RELAY_DIR/config.example.yaml" "$RELAY_DIR/config.yaml"
    echo "   config.yaml created from example."
fi

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit /home/relay/.bitcoin/bitcoin.conf (set rpcpassword)"
echo "  2. Edit config.yaml if needed"
echo "  3. Start services:"
echo "     sudo systemctl enable --now bitcoind-relay"
echo "     sudo systemctl enable --now pocket-relay"
echo "  4. Wait for bitcoind to sync (check: bitcoin-cli -datadir=/home/relay/.bitcoin getblockchaininfo)"
echo "  5. Your .onion address: cat /var/lib/tor/pocket-relay/hostname"
echo ""
