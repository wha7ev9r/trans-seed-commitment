#!/usr/bin/env bash
# ============================================================
# trans-commitment 初始化脚本
# 用法：bash setup.sh
# 功能：
#  1. 从 .env.example 创建 .env（若不存在）
#  2. 下载 Flood for Transmission UI（若未下载）
#  3. 创建运行时目录并对齐 PUID/PGID 权限
#  4. 赋予 fetch.sh 执行权限
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ---- 颜色 ----
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

# ---- 1. .env ----
if [ ! -f .env ]; then
    cp .env.example .env
    echo -e "${GREEN}[OK]${NC} .env created from .env.example"
    echo -e "${YELLOW}[!!]${NC} Please edit .env now (set passwords, quota, interface, etc.)"
    echo ""
    echo "    vim .env    # or nano .env"
    echo ""
    echo "    Then run this script again."
    echo ""
    exit 0
fi
echo -e "${GREEN}[OK]${NC} .env exists"

# ---- preflight ----
for cmd in wget unzip sha256sum sed tail cut; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo -e "${RED}[FAIL]${NC} Required command not found: $cmd"
        exit 1
    fi
done

# ---- 2. Flood for Transmission UI ----
FLOOD_DIR="./flood-ui"
FLOOD_VERSION="v1.0.1"
FLOOD_ZIP_URL="https://github.com/johman10/flood-for-transmission/releases/download/${FLOOD_VERSION}/flood-for-transmission.zip"
FLOOD_ZIP_SHA256="0172d9aae27ce1a3da0b05cc3428447097cf52b707b4483ca40b5f1981002d8c"

if [ ! -f "$FLOOD_DIR/index.html" ]; then
    echo "[..] Downloading Flood for Transmission UI..."
    mkdir -p "$FLOOD_DIR"
    mkdir -p ./flood-ui-tmp
    if wget --tries=3 --timeout=30 -q -O ./flood-ui-tmp/flood-ui.zip "$FLOOD_ZIP_URL"; then
        actual_sha256="$(sha256sum ./flood-ui-tmp/flood-ui.zip | cut -d' ' -f1)"
        if [ "$actual_sha256" != "$FLOOD_ZIP_SHA256" ]; then
            rm -rf ./flood-ui-tmp
            echo -e "${RED}[FAIL]${NC} Flood UI checksum mismatch"
            exit 1
        fi
        unzip -qo ./flood-ui-tmp/flood-ui.zip -d ./flood-ui-tmp
        rm -f ./flood-ui-tmp/flood-ui.zip
        # Release zip might contain a subdirectory; find and move the actual files
        if [ -f ./flood-ui-tmp/index.html ]; then
            mv ./flood-ui-tmp/* ./flood-ui/ 2>/dev/null || true
        else
            subdir=$(find ./flood-ui-tmp -mindepth 1 -maxdepth 1 -type d | head -1)
            if [ -n "$subdir" ] && [ -f "$subdir/index.html" ]; then
                cp -r "$subdir"/* ./flood-ui/ 2>/dev/null || true
            fi
        fi
        rm -rf ./flood-ui-tmp
        if [ -f "$FLOOD_DIR/index.html" ]; then
            echo -e "${GREEN}[OK]${NC} Flood UI downloaded to $FLOOD_DIR"
        else
            echo -e "${RED}[FAIL]${NC} Could not extract Flood UI. Download manually:"
            echo "    wget $FLOOD_ZIP_URL -O flood-ui.zip"
            echo "    unzip flood-ui.zip -d ./flood-ui/"
            exit 1
        fi
    else
        rm -rf ./flood-ui-tmp
        echo -e "${RED}[FAIL]${NC} Download failed. Check network / GitHub status."
        echo "    Manual download: $FLOOD_ZIP_URL"
        exit 1
    fi
else
    echo -e "${GREEN}[OK]${NC} Flood UI already present at $FLOOD_DIR"
fi

# ---- 3. Directories ----
mkdir -p ./downloads ./watch ./transmission/config ./quota-guard/state

PUID_VALUE="$(sed -n 's/^PUID=//p' .env | tail -n 1)"
PGID_VALUE="$(sed -n 's/^PGID=//p' .env | tail -n 1)"
PUID_VALUE="${PUID_VALUE:-1000}"
PGID_VALUE="${PGID_VALUE:-1000}"
case "$PUID_VALUE" in
    *[!0-9]*|'') echo -e "${RED}[FAIL]${NC} PUID must be numeric"; exit 1 ;;
esac
case "$PGID_VALUE" in
    *[!0-9]*|'') echo -e "${RED}[FAIL]${NC} PGID must be numeric"; exit 1 ;;
esac

if [ "$(id -u)" -eq 0 ]; then
    chown -R "$PUID_VALUE:$PGID_VALUE" ./downloads ./watch ./transmission/config ./quota-guard/state
elif [ "$(id -u)" -ne "$PUID_VALUE" ]; then
    echo -e "${RED}[FAIL]${NC} .env PUID=$PUID_VALUE does not match current uid=$(id -u)"
    echo "       Run setup as root or set PUID/PGID to the deployment user's id values."
    exit 1
fi
chmod 750 ./quota-guard/state
echo -e "${GREEN}[OK]${NC} Runtime directories created"

# ---- 4. fetch.sh permission ----
chmod +x ./ubuntu-torrents/fetch.sh 2>/dev/null || true

# ---- Done ----
echo ""
echo "============================================================"
echo -e "  ${GREEN}Setup complete!${NC}"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Edit .env (if not already done)"
echo "  2. docker compose up -d"
echo "  3. Import Ubuntu torrents:"
echo "     cd ubuntu-torrents && sh fetch.sh"
echo ""
echo "  4. Access panel:"
echo "     http://localhost:9092    (quota-guard unified panel)"
echo ""
echo "  5. OpenResty reverse proxy (optional):"
echo "     See README.md for config example"
echo "============================================================"
