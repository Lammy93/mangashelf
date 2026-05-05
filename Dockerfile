FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    unrar-free \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

RUN mkdir -p /app/frontend/static/css \
             /app/frontend/static/js \
             /app/frontend/static/img

EXPOSE 8080

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080"]
