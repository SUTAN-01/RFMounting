#!/usr/bin/env bash
set -euo pipefail
APP_DIR="/opt/remote-share-mount"
sudo mkdir -p "$APP_DIR"
sudo cp -r "$(cd "$(dirname "$0")" && pwd)/remote-share-mount/"* "$APP_DIR/"
sudo chmod +x "$APP_DIR/remote-share-mount"
sudo apt-get update
sudo apt-get install -y fuse3 python3-tk
sudo tee /usr/local/bin/remote-share-mount >/dev/null <<SCRIPT
#!/usr/bin/env bash
exec /opt/remote-share-mount/remote-share-mount "\$@"
SCRIPT
sudo chmod +x /usr/local/bin/remote-share-mount
echo "Installed. Run: remote-share-mount"
