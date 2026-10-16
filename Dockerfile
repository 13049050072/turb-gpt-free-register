FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TURB_DB_PATH=/data/turb.sqlite3

WORKDIR /app

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub -o /tmp/google-linux-signing-key.pub; \
    gpg --dearmor --batch --yes --output /usr/share/keyrings/google-linux-signing-key.gpg /tmp/google-linux-signing-key.pub; \
    echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux-signing-key.gpg] https://dl.google.com/linux/chrome/deb/ stable main' > /etc/apt/sources.list.d/google-chrome.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends google-chrome-stable; \
    rm -f /tmp/google-linux-signing-key.pub; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

CMD ["sh", "-c", "exec python web.py --host 0.0.0.0 --port ${PORT:-5000}"]
