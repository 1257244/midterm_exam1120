#!/usr/bin/env bash
# 打包總部與分店的映像檔；加上 --push 會上傳到 Docker Hub
# 用法：bash scripts/build-push.sh          （只 build，本機測試用）
#       bash scripts/build-push.sh --push   （build 並上傳，需要先 docker login）
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "❌ 找不到 .env，請先執行：cp .env.example .env"; exit 1; }
set -a; . ./.env; set +a
: "${DOCKERHUB_USER:?請在 .env 設定 DOCKERHUB_USER}"
TAG=${TAG:-latest}

echo "🔨 build ${DOCKERHUB_USER}/drink-hq:${TAG}"
docker build -f hq/Dockerfile -t "${DOCKERHUB_USER}/drink-hq:${TAG}" .
echo "🔨 build ${DOCKERHUB_USER}/drink-store:${TAG}"
docker build -f store/Dockerfile -t "${DOCKERHUB_USER}/drink-store:${TAG}" .

if [ "${1:-}" = "--push" ]; then
  echo "📤 push 到 Docker Hub"
  docker push "${DOCKERHUB_USER}/drink-hq:${TAG}"
  docker push "${DOCKERHUB_USER}/drink-store:${TAG}"
  echo "✅ 上傳完成：${DOCKERHUB_USER}/drink-hq:${TAG}、${DOCKERHUB_USER}/drink-store:${TAG}"
else
  echo "✅ build 完成（加上 --push 可上傳到 Docker Hub）"
fi
