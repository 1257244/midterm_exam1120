#!/usr/bin/env bash
# 產生示範訂單：對 stores.csv 中每一間分店隨機下幾筆單，方便展示營收儀表板
# 在「分店機」上執行，對這台的分店下單
# 用法：bash scripts/demo_orders.sh [分店機主機，預設 localhost] [每店筆數，預設 5]
cd "$(dirname "$0")/.."
HOST=${1:-localhost}
N=${2:-5}
DRINKS=("開心奶茶" "快樂紅茶" "烏龍綠茶" "茉莉花茶" "甘蔗青茶")

tail -n +2 stores.csv | tr -d '\r' | while IFS=, read -r id _rest; do
  [ -z "$id" ] && continue
  ok=0
  for _ in $(seq 1 "$N"); do
    d1=${DRINKS[$RANDOM % 5]}
    d2=${DRINKS[$RANDOM % 5]}
    [ "$d1" = "$d2" ] && d2=${DRINKS[($RANDOM + 1) % 5]}
    code=$(curl -s -o /dev/null -w "%{http_code}" \
      --data-urlencode "qty_${d1}=$((RANDOM % 3 + 1))" \
      --data-urlencode "qty_${d2}=$((RANDOM % 2 + 1))" \
      "http://${HOST}/store/${id}/order")
    [ "$code" = "302" ] && ok=$((ok + 1))
  done
  echo "  ${id}: 成功下單 ${ok}/${N} 筆"
done
echo "完成！約 30 秒內各分店會把訂單回報給總部，請到總部機的 http://<總部機IP>/admin 查看"
