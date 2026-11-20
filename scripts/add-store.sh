#!/usr/bin/env bash
# 一鍵新增分店（在分店機上執行）：寫入 stores.csv → 重新產生 compose 設定 → 啟動新分店容器
# 用法：bash scripts/add-store.sh <分店代號> <分店名稱> <城市> [店長密碼]
# 範例：bash scripts/add-store.sh kaohsiung 高雄左營店 高雄市 khhpass
set -euo pipefail
cd "$(dirname "$0")/.."

if [ $# -lt 3 ]; then
  echo "用法：bash scripts/add-store.sh <分店代號> <分店名稱> <城市> [店長密碼]"
  echo "範例：bash scripts/add-store.sh kaohsiung 高雄左營店 高雄市 khhpass"
  exit 1
fi

id=$1; name=$2; city=$3; password=${4:-${1}pass}

if ! [[ "$id" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
  echo "❌ 分店代號只能使用小寫英文、數字與 -（例如 kaohsiung、taoyuan4）"; exit 1
fi
if cut -d, -f1 stores.csv | tr -d '\r' | grep -qx "$id"; then
  echo "❌ 分店代號 $id 已存在"; exit 1
fi
if [[ "$name$city$password" == *,* ]]; then
  echo "❌ 名稱、城市、密碼不能包含逗號"; exit 1
fi

# 確保檔案結尾有換行再追加
[ -n "$(tail -c1 stores.csv)" ] && echo >> stores.csv
echo "${id},${name},${city},${password}" >> stores.csv
bash scripts/gen-stores.sh

docker compose up -d "store-${id}"

echo -n "⏳ 等待分店就緒"
for _ in $(seq 1 30); do
  status=$(docker inspect -f '{{.State.Health.Status}}' "drink-store-${id}" 2>/dev/null || echo "none")
  [ "$status" = "healthy" ] && break
  echo -n "."; sleep 2
done
echo " $status"

echo
echo "🎉 新分店「${name}」已啟動！"
echo "   門市網站：http://<分店機IP>/store/${id}/"
echo "   店長帳號：manager / ${password}"
echo "   約 30 秒內會自動出現在總部的門市清單與營收儀表板。"
