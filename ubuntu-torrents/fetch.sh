#!/bin/sh
# ============================================================
# 一键拉取 Ubuntu 官方 .torrent 到 ../watch/ 目录
# Transmission 监控 watch 目录，自动加载做种
#
# 用法：
#   cd ubuntu-torrents && sh fetch.sh          # 拉取所有
#   sh fetch.sh                                 # 在本目录直接执行
#   sh fetch.sh -n                              # 仅测试 URL 可达，不下载
# ============================================================
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WATCH_DIR="${SCRIPT_DIR}/../watch"
LIST_FILE="${SCRIPT_DIR}/list.txt"

DRY_RUN=0
[ "${1:-}" = "-n" ] && DRY_RUN=1

mkdir -p "$WATCH_DIR"

if [ ! -f "$LIST_FILE" ]; then
    echo "[ERROR] list.txt not found: $LIST_FILE" >&2
    exit 1
fi

echo "==================== Ubuntu Torrent Fetcher ===================="
echo "Watch dir: $WATCH_DIR"
echo "List file: $LIST_FILE"
[ "$DRY_RUN" = "1" ] && echo "(dry-run mode, only HEAD check)"
echo "--------------------------------------------------------------"

ok=0
skip=0
fail=0

while IFS= read -r line; do
    # 去首尾空白
    line="$(echo "$line" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    # 跳过空行和注释
    [ -z "$line" ] && continue
    case "$line" in '#'*) continue ;; esac

    filename="$(basename "$line")"
    target="$WATCH_DIR/$filename"

    if [ -f "$target" ]; then
        echo "[SKIP ] $filename (already exists)"
        skip=$((skip + 1))
        continue
    fi

    if [ "$DRY_RUN" = "1" ]; then
        # 仅测试可达性
        if wget --spider --tries=1 --timeout=10 -q "$line"; then
            echo "[ OK  ] $filename (reachable)"
            ok=$((ok + 1))
        else
            echo "[FAIL ] $filename (unreachable: $line)" >&2
            fail=$((fail + 1))
        fi
        continue
    fi

    echo "[GET  ] $filename"
    if wget --tries=3 --timeout=20 -q -O "$target.partial" "$line"; then
        mv "$target.partial" "$target"
        echo "        -> saved"
        ok=$((ok + 1))
    else
        echo "[FAIL ] $line" >&2
        rm -f "$target.partial"
        fail=$((fail + 1))
    fi
done < "$LIST_FILE"

echo "--------------------------------------------------------------"
echo "Summary:  ok=$ok  skipped=$skip  failed=$fail"
[ "$fail" -gt 0 ] && exit 1
exit 0
