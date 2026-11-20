# 分店映像檔（所有分店共用）—— 請在專案根目錄 build：docker build -f store/Dockerfile -t <帳號>/drink-store .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Taipei

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY store/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY store/ .
# 品牌共用資源：樣式、產品圖、各分店預設特色飲品
COPY shared/brand.css static/brand.css
COPY shared/drinks/ static/drinks/
COPY shared/seed_specials.json seed_specials.json

VOLUME ["/data"]
EXPOSE 5000
# 啟動初期每 2 秒檢查一次，健康後 Traefik 才會開始轉送流量
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --start-interval=2s CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:5000/healthz')"

# 只用 1 個 worker，避免背景同步執行緒重複啟動
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "1", "--threads", "8", "app:app"]
