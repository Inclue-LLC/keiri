# Google Cloud Run 用。Cloud Run の「リポジトリから継続的にデプロイ」でこの Dockerfile からビルドされる
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Tokyo \
    KEIRI_STORE=firestore \
    KEIRI_AUTH=google \
    KEIRI_COOKIE_SECURE=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

RUN useradd --create-home appuser
USER appuser

CMD exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port "${PORT:-8080}" --proxy-headers --forwarded-allow-ips "*"
