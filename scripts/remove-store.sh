#!/usr/bin/env bash
# 停用分店：從 stores.csv 移除 → 重新產生 compose 設定 → 停止並移除該分店容器
# 分店資料（volume）預設會保留，加上 --purge 才會一起刪除
# 用法：bash scripts/remove-store.sh <分店代號> [--purge]
set -euo pipefail
cd "$(dirname "$0")/.."

id=${1:?用法：bash scripts/remove-store.sh <分店代號> [--purge]}
if ! cut -d, -f1 stores.csv | tr -d '\r' | grep -qx "$id"; then
  echo "❌ 找不到分店代號 $id"; exit 1
fi

docker compose stop "store-${id}" || true
docker compose rm -f "store-${id}" || true

grep -v "^${id}," stores.csv > stores.csv.tmp && mv stores.csv.tmp stores.csv
bash scripts/gen-stores.sh

if [ "${2:-}" = "--purge" ]; then
  docker volume rm "drinkshop_store-${id}-data" || true
  echo "🗑️  已刪除分店 ${id} 的資料"
fi
echo "✅ 分店 ${id} 已停用。可到總部後台的分店明細頁，把它從門市清單移除（歷史營收會保留）。"
