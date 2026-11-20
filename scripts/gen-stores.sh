#!/usr/bin/env bash
# 讀取 stores.csv，產生 docker-compose.stores.yml（每一列 = 一間分店）
# 用法：bash scripts/gen-stores.sh
set -euo pipefail
cd "$(dirname "$0")/.."

CSV=stores.csv
OUT=docker-compose.stores.yml

{
  echo "# ⚠️ 此檔由 scripts/gen-stores.sh 依 stores.csv 自動產生，請勿手動修改"
  echo "# 所有分店都使用同一個 drink-store 映像檔，只有環境變數不同"
  echo "services:"
} > "$OUT"

volumes=()
count=0
while IFS=, read -r id name city password || [ -n "${id:-}" ]; do
  id=$(echo "$id" | tr -d '\r ')
  name=$(echo "$name" | tr -d '\r')
  city=$(echo "$city" | tr -d '\r')
  password=$(echo "${password:-}" | tr -d '\r')
  [ -z "$id" ] && continue
  [ "$id" = "store_id" ] && continue          # 標題列
  [[ "$id" == \#* ]] && continue              # 註解列
  if ! [[ "$id" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
    echo "❌ 分店代號「$id」不合法：只能使用小寫英文、數字與 -" >&2
    exit 1
  fi
  [ -z "$password" ] && password="${id}pass"
  envkey=$(echo "$id" | tr 'a-z-' 'A-Z_')

  cat >> "$OUT" <<EOF

  store-${id}:
    image: \${DOCKERHUB_USER:?請在 .env 設定 DOCKERHUB_USER}/drink-store:\${TAG:-latest}
    container_name: drink-store-${id}
    restart: unless-stopped
    depends_on:
      - hq
    environment:
      STORE_ID: "${id}"
      STORE_NAME: "${name}"
      STORE_CITY: "${city}"
      MANAGER_USER: manager
      MANAGER_PASSWORD: "\${STORE_PASSWORD_${envkey}:-${password}}"
      HQ_URL: http://hq:5000
      HQ_API_KEY: \${HQ_API_KEY:-change-me-api-key}
      SECRET_KEY: \${SECRET_KEY:-dev-secret-change-me}
      BRAND_NAME: \${BRAND_NAME:-開心飲品}
      SYNC_INTERVAL: "30"
      TZ: Asia/Taipei
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.store-${id}.rule=Path(\`/store/${id}\`) || PathPrefix(\`/store/${id}/\`)"
      - "traefik.http.routers.store-${id}.entrypoints=web"
      - "traefik.http.routers.store-${id}.middlewares=store-${id}-strip"
      - "traefik.http.middlewares.store-${id}-strip.stripprefix.prefixes=/store/${id}"
      - "traefik.http.services.store-${id}.loadbalancer.server.port=5000"
    volumes:
      - store-${id}-data:/data
    networks:
      - drinknet
EOF
  volumes+=("store-${id}-data")
  count=$((count + 1))
done < "$CSV"

{
  echo
  echo "networks:"
  echo "  drinknet:"
  echo "    name: drinknet"
  echo
  echo "volumes:"
  for v in "${volumes[@]}"; do echo "  ${v}:"; done
} >> "$OUT"

echo "✅ 已產生 $OUT（共 $count 間分店）"
