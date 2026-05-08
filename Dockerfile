# Build stage
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y \
    unrar-free \
    && rm -rf /var/lib/apt/lists/*

# Download mangal CLI
ADD https://github.com/metafates/mangal/releases/download/v4.0.6/mangal_4.0.6_Linux_x86_64.tar.gz /tmp/mangal.tar.gz
RUN tar -xzf /tmp/mangal.tar.gz -C /usr/local/bin/ mangal && rm /tmp/mangal.tar.gz

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

RUN mkdir -p /app/frontend/static/css \
             /app/frontend/static/js \
             /app/frontend/static/img

# Runtime stage
FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    unrar-free \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -r mangashelf && useradd -r -g mangashelf -d /app -s /sbin/nologin mangashelf

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --from=builder /app .
COPY --from=builder /usr/local/bin/mangal /usr/local/bin/mangal

# Pre-install mangal Lua sources (as root, then chown)
RUN mkdir -p /app/.config/mangal /app/.local/share/mangal && \
    HOME=/app XDG_CONFIG_HOME=/app/.config XDG_DATA_HOME=/app/.local/share \
    mangal sources install MangaDex Manganato Mangasee 2>/dev/null || true && \
    chown -R mangashelf:mangashelf /app/.config /app/.local

COPY entrypoint.py /entrypoint.py
RUN chmod +x /entrypoint.py

EXPOSE 8080

ENV HOME=/app
ENV XDG_CONFIG_HOME=/app/.config
ENV XDG_DATA_HOME=/app/.local/share

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/docs')" || exit 1

ENTRYPOINT ["/entrypoint.py"]
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080"]
