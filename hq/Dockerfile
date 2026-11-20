# 總部映像檔 —— 請在專案根目錄 build：docker build -f hq/Dockerfile -t <帳號>/drink-hq .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Taipei

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY hq/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY hq/ .
# 品牌共用資源：樣式與產品圖（與分店完全相同）
COPY shared/brand.css static/brand.css
COPY shared/drinks/ static/drinks/

VOLUME ["/data"]
EXPOSE 5000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --start-interval=2s CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:5000/healthz')"

CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "1", "--threads", "8", "app:app"]
