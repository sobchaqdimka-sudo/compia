"""Генерация фото Миры через fal.ai.

Две операции:
- generate_base_portrait — «канонический» портрет по текстовому промпту (основа);
- generate_with_reference — фото по запросу, с тем же лицом (по референсу).

fal_client импортируем лениво внутри функций: если пакет не установлен или
не задан FAL_KEY, фича просто выключена (is_enabled() вернёт False), а бот
продолжает работать как обычно. Вызовы блокирующие — в боте оборачиваем в
asyncio.to_thread.
"""

import logging
import os
import urllib.request
import uuid

from config import FAL_KEY, IMAGE_MODEL_BASE, IMAGE_MODEL_REF, MEDIA_DIR, TEST_MODE


def is_enabled():
    """Включена ли генерация фото (есть ключ fal.ai). В TEST_MODE - всегда включена."""
    return TEST_MODE or bool(FAL_KEY)


def _test_stub_path(user_id, suffix):
    """Вернуть путь-заглушку для тестов: реальный файл не создаём (фейковый
    transport не открывает его), достаточно строки."""
    path = os.path.join(MEDIA_DIR, str(user_id), f"test_{suffix}.png")
    return path


def _client():
    """Лениво вернуть fal_client с проставленным ключом."""
    import fal_client  # импорт внутри: без установленного пакета бот всё равно стартует

    os.environ.setdefault("FAL_KEY", FAL_KEY or "")
    return fal_client


def _user_dir(user_id):
    path = os.path.join(MEDIA_DIR, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path


def _download(url, dest):
    """Скачать готовое изображение по URL (URL приходит от fal, доверенный)."""
    with urllib.request.urlopen(url) as resp:  # noqa: S310 — источник доверенный (fal)
        data = resp.read()
    with open(dest, "wb") as f:
        f.write(data)
    return dest


def _first_image_url(result):
    """Достать URL первого изображения из ответа fal (формат images[].url)."""
    images = result.get("images") or []
    if not images:
        raise RuntimeError("fal вернул пустой результат без изображений")
    return images[0]["url"]


def generate_base_portrait(prompt, user_id):
    """Сгенерировать канонический портрет и сохранить как base.png. Вернуть путь."""
    if TEST_MODE:
        logging.info("[TEST_MODE] stub generate_base_portrait user_id=%s", user_id)
        return _test_stub_path(user_id, "base")
    fal_client = _client()
    result = fal_client.subscribe(
        IMAGE_MODEL_BASE,
        arguments={
            "prompt": prompt,
            "image_size": "portrait_4_3",
            "num_images": 1,
            "enable_safety_checker": True,
        },
    )
    dest = os.path.join(_user_dir(user_id), "base.png")
    path = _download(_first_image_url(result), dest)
    logging.info("Сгенерирован базовый портрет user_id=%s", user_id)
    return path


def generate_with_reference(instruction, reference_path, user_id):
    """Сгенерировать фото по запросу: редактируем референс по инструкции.

    instruction — текстовая команда для модели-редактора (Nano Banana):
    «сохрани ту же девушку, покажи в полный рост, повернись боком…».
    Вернуть путь к сохранённому файлу.
    """
    if TEST_MODE:
        logging.info("[TEST_MODE] stub generate_with_reference user_id=%s", user_id)
        return _test_stub_path(user_id, f"edit_{uuid.uuid4().hex[:8]}")
    fal_client = _client()
    reference_url = fal_client.upload_file(reference_path)
    result = fal_client.subscribe(
        IMAGE_MODEL_REF,
        arguments={
            "prompt": instruction,
            "image_urls": [reference_url],
            "num_images": 1,
        },
    )
    dest = os.path.join(_user_dir(user_id), f"{uuid.uuid4().hex}.png")
    path = _download(_first_image_url(result), dest)
    logging.info("Сгенерировано фото по референсу user_id=%s", user_id)
    return path
