#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$ROOT/scripts/build_linux.sh"

STAGE="/tmp/remote-share-mount-deb-stage"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$STAGE/opt/remote-share-mount" "$STAGE/usr/local/bin"

cp -r "$ROOT/dist/remote-share-mount/"* "$STAGE/opt/remote-share-mount/"
cat > "$STAGE/usr/local/bin/remote-share-mount" <<'EOF'
#!/usr/bin/env bash
exec /opt/remote-share-mount/remote-share-mount "$@"
EOF
chmod +x "$STAGE/usr/local/bin/remote-share-mount"
chmod 0755 "$STAGE" "$STAGE/DEBIAN" "$STAGE/opt" "$STAGE/opt/remote-share-mount" "$STAGE/usr" "$STAGE/usr/local" "$STAGE/usr/local/bin"
chmod 0755 "$STAGE/usr/local/bin/remote-share-mount"

cat > "$STAGE/DEBIAN/control" <<'EOF'
Package: remote-share-mount
Version: 0.2.0
Section: utils
Priority: optional
Architecture: amd64
Maintainer: Remote Share Mount
Depends: fuse3, python3-tk
Description: Cross-platform remote directory sharing and mounting GUI
EOF

dpkg-deb --build "$STAGE" "/tmp/remote-share-mount_0.2.0_amd64.deb"
cp "/tmp/remote-share-mount_0.2.0_amd64.deb" "$ROOT/dist/remote-share-mount_0.2.0_amd64.deb"
echo "Deb package prepared at: $ROOT/dist/remote-share-mount_0.2.0_amd64.deb"
