"""Видео-кружочки Миры.

Оживляем фото в короткое видео (image-to-video) и готовим квадратный mp4 для
Telegram video note. Пока это тихий клип (без звука) - голос добавим отдельным шагом.

Как и imagegen: fal_client импортируем лениво, фича включается при FAL_KEY.
Дополнительно нужен ffmpeg в системе (для квадратной обрезки). Вызовы блокирующие
- в боте оборачиваем в asyncio.to_thread.
"""

import logging
import os
import shutil
import subprocess
import urllib.request
import uuid

from config import (
    FAL_KEY,
    MEDIA_DIR,
    VIDEO_DURATION,
    VIDEO_MODEL,
    VIDEO_NOTE_SIZE,
)


def is_enabled():
    """Включены ли видео-кружочки (есть ключ fal.ai)."""
    return bool(FAL_KEY)


def _client():
    import fal_client  # импорт внутри: без пакета бот всё равно стартует

    os.environ.setdefault("FAL_KEY", FAL_KEY or "")
    return fal_client


def _download(url, dest):
    with urllib.request.urlopen(url) as resp:  # noqa: S310 — источник доверенный (fal)
        data = resp.read()
    with open(dest, "wb") as f:
        f.write(data)
    return dest


def _to_square_note(src, dest):
    """Обрезать в квадрат (по верху, чтобы лицо осталось в кадре) и перекодировать
    под Telegram video note: h264 / yuv420p, без звука."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg не установлен (нужен для видео-кружочков)")
    size = VIDEO_NOTE_SIZE
    vf = (
        "crop=w=min(iw\\,ih):h=min(iw\\,ih):x=(iw-min(iw\\,ih))/2:y=0,"
        f"scale={size}:{size}"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", src,
            "-vf", vf,
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-an", "-movflags", "+faststart",
            dest,
        ],
        check=True,
        capture_output=True,
    )
    return dest


def generate_circle(image_path, motion_prompt, user_id):
    """Сделать видео-кружок из фото. Вернуть путь к квадратному mp4."""
    fal_client = _client()
    image_url = fal_client.upload_file(image_path)
    result = fal_client.subscribe(
        VIDEO_MODEL,
        arguments={
            "image_url": image_url,
            "prompt": motion_prompt,
            "duration": VIDEO_DURATION,
        },
    )
    video = result.get("video") or {}
    url = video.get("url")
    if not url:
        raise RuntimeError("fal вернул результат без видео")

    user_dir = os.path.join(MEDIA_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    raw = _download(url, os.path.join(user_dir, f"{uuid.uuid4().hex}_raw.mp4"))
    note = _to_square_note(
        raw, os.path.join(user_dir, f"{uuid.uuid4().hex}_note.mp4")
    )
    logging.info("Сгенерирован видео-кружок user_id=%s", user_id)
    return note
