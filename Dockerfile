FROM python:3.11-slim

# Установка ffmpeg-зависимости делается через pip-пакет imageio-ffmpeg
# (бинарник внутри venv). Системный пакет не нужен.

WORKDIR /app

# Сначала зависимости — слой кэшируется, пока requirements.txt не меняется.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir aiohttp

# Затем сам код.
COPY . .

# /data — место, куда Fly монтирует volume (см. fly.toml).
# При первом старте подкаталог media создаст сам бот (в _user_dir).
RUN mkdir -p /data

ENV COMPIA_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

# Дашборд (aiohttp).
EXPOSE 8080

CMD ["python", "bot.py"]
