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
    TALKING_MODEL,
    TALKING_RESOLUTION,
    TTS_MODEL,
    TTS_VOICE,
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


def _ffmpeg_exe():
    """Путь к ffmpeg: системный, иначе бинарник из пакета imageio-ffmpeg."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError(
            "Не найден ffmpeg. Установи: pip install imageio-ffmpeg "
            "(или brew install ffmpeg)"
        ) from exc


def _to_square_note(src, dest, keep_audio=False):
    """Обрезать в квадрат (по верху, чтобы лицо осталось в кадре) и перекодировать
    под Telegram video note: h264 / yuv420p. keep_audio=True сохраняет звук (для
    говорящих кружочков), иначе звук убираем (тихий клип)."""
    size = VIDEO_NOTE_SIZE
    vf = (
        "crop=w=min(iw\\,ih):h=min(iw\\,ih):x=(iw-min(iw\\,ih))/2:y=0,"
        f"scale={size}:{size}"
    )
    cmd = [_ffmpeg_exe(), "-y", "-i", src, "-vf", vf,
           "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if keep_audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", dest]
    subprocess.run(cmd, check=True, capture_output=True)
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


def _first_audio_url(result):
    """Достать URL аудио из ответа TTS (audio.url или audio_url)."""
    audio = result.get("audio")
    if isinstance(audio, dict) and audio.get("url"):
        return audio["url"]
    if result.get("audio_url"):
        return result["audio_url"]
    raise RuntimeError("fal вернул результат без аудио")


def generate_talking_circle(image_path, text, user_id):
    """Озвучить текст голосом Миры и сделать говорящий кружок (липсинк).

    Шаги: TTS (ElevenLabs) -> аудио; затем фото + аудио -> видео (VEED Fabric);
    затем квадрат со звуком. Вернуть путь к mp4.
    """
    fal_client = _client()

    tts = fal_client.subscribe(
        TTS_MODEL,
        arguments={"text": text, "voice": TTS_VOICE},
    )
    audio_url = _first_audio_url(tts)

    image_url = fal_client.upload_file(image_path)
    result = fal_client.subscribe(
        TALKING_MODEL,
        arguments={
            "image_url": image_url,
            "audio_url": audio_url,
            "resolution": TALKING_RESOLUTION,
        },
    )
    video = result.get("video") or {}
    url = video.get("url")
    if not url:
        raise RuntimeError("fal вернул результат без видео")

    user_dir = os.path.join(MEDIA_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    raw = _download(url, os.path.join(user_dir, f"{uuid.uuid4().hex}_traw.mp4"))
    note = _to_square_note(
        raw, os.path.join(user_dir, f"{uuid.uuid4().hex}_tnote.mp4"), keep_audio=True
    )
    logging.info("Сгенерирован говорящий кружок user_id=%s", user_id)
    return note
