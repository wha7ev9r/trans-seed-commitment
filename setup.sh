#!/usr/bin/env bash
# ============================================================
# trans-commitment 初始化脚本
# 用法：bash setup.sh
# 功能：
#   1. 从 .env.example 创建 .env（若不存在）
#   2. 下载 Flood for Transmission UI（若未下载）
#   3. 创建运行时目录（downloads/watch/transmission config）
#   4. 复制 Transmission settings.json 模板到运行时目录
#   5. 赋予 fetch.sh 执行权限
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

# ---- 2. Flood for Transmission UI ----
FLOOD_DIR="./flood-ui"
FLOOD_ZIP_URL="https://github.com/johman10/flood-for-transmission/releases/latest/download/flood-for-transmission.zip"

if [ ! -f "$FLOOD_DIR/index.html" ]; then
    echo "[..] Downloading Flood for Transmission UI..."
    mkdir -p ./flood-ui-tmp
    if wget --tries=3 --timeout=30 -q -O ./flood-ui-tmp/flood-ui.zip "$FLOOD_ZIP_URL"; then
        unzip -qo ./flood-ui-tmp/flood-ui.zip -d ./flood-ui-tmp
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
mkdir -p ./downloads ./watch ./transmission/config ./quota-guard/state ./vnstat
echo -e "${GREEN}[OK]${NC} Runtime directories created"

# ---- 4. Transmission settings ----
TEMPLATE_SETTINGS="./transmission/settings.json"
RUNTIME_SETTINGS="./transmission/config/settings.json"

if [ -f "$TEMPLATE_SETTINGS" ] && [ ! -f "$RUNTIME_SETTINGS" ]; then
    cp "$TEMPLATE_SETTINGS" "$RUNTIME_SETTINGS"
    echo -e "${GREEN}[OK]${NC} Transmission settings.json copied (encryption=2, blocklist, etc.)"
elif [ -f "$RUNTIME_SETTINGS" ]; then
    echo -e "${YELLOW}[!]${NC} runtime settings.json already exists (will NOT overwrite)"
fi

# ---- 5. fetch.sh permission ----
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
