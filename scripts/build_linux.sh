#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 -m pip install --upgrade pip
python3 -m pip install -r build_requirements.txt
python3 -m pip install .
python3 -m PyInstaller --noconfirm --clean remote_share.spec

PACKAGE_DIR="$ROOT/dist/linux-package/remote-share-mount"
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"
cp -r "$ROOT/dist/remote-share-mount/"* "$PACKAGE_DIR/"
cp "$ROOT/README.md" "$PACKAGE_DIR/"

INSTALLER="$ROOT/dist/linux-package/install.sh"
cat > "$INSTALLER" <<'EOF'
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
EOF
chmod +x "$INSTALLER"

TARBALL="$ROOT/dist/remote-share-mount-linux.tar.gz"
tar -czf "$TARBALL" -C "$ROOT/dist/linux-package" .
echo "Linux package prepared at: $TARBALL"
