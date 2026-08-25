FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py db.py s3util.py ./
COPY static ./static

ENV PORT=8100 \
    DB_PATH=/data/r2hub.db \
    UVICORN_HOST=0.0.0.0

VOLUME ["/data"]
EXPOSE 8100

LABEL org.opencontainers.image.title="R2 Hub" \
      org.opencontainers.image.description="多账号 Cloudflare R2 聚合图床" \
      org.opencontainers.image.source="https://github.com/YOUR_GITHUB_USERNAME/r2hub"

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8100/health',timeout=3)"]

CMD ["python", "main.py"]
