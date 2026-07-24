#!/bin/sh
# ============================================================
# trans-commitment 种子重载脚本
# 用法：bash reload.sh
# 功能：下载指定种子到 watch/ 目录，Transmission 自动加载
#       容器重建、磁盘清理后都可以跑，已存在的跳过
# ============================================================
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WATCH_DIR="${SCRIPT_DIR}/../watch"
LIST_FILE="${SCRIPT_DIR}/seed-list.txt"

mkdir -p "$WATCH_DIR"

if [ ! -f "$LIST_FILE" ]; then
    echo "[ERROR] seed-list.txt not found: $LIST_FILE" >&2
    exit 1
fi

echo "==================== Transmission Seeder ===================="
echo "Watch dir: $WATCH_DIR"
echo "--------------------------------------------------------------"

ok=0
skip=0
fail=0

while IFS= read -r line; do
    line="$(echo "$line" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    [ -z "$line" ] && continue
    case "$line" in '#'*) continue ;; esac

    basefile="$(basename "$line")"
    target="$WATCH_DIR/$basefile"

    # Skip if already loaded (renamed to .added) or already present
    if [ -f "$target" ] || [ -f "${target}.added" ]; then
        echo "[SKIP ] $basefile (already loaded)"
        skip=$((skip + 1))
        continue
    fi

    echo "[GET  ] $basefile"
    if wget --tries=3 --timeout=20 -q -O "${target}.partial" "$line"; then
        mv "${target}.partial" "$target"
        echo "        -> saved"
        ok=$((ok + 1))
    else
        echo "[FAIL ] $line" >&2
        rm -f "${target}.partial"
        fail=$((fail + 1))
    fi
done < "$LIST_FILE"

echo "--------------------------------------------------------------"
echo "Summary: ok=$ok skipped=$skip failed=$fail"
[ "$fail" -gt 0 ] && exit 1
