"""Настройки проекта.

Здесь мы один раз читаем секреты и параметры из файла .env,
чтобы остальной код просто импортировал готовые переменные
и не лез в .env напрямую.
"""

import os

from dotenv import load_dotenv

# Загружаем переменные из файла .env в окружение процесса.
load_dotenv()

# Тестовый режим: симулятор юзера + judge для E2E-проверок ban-list.
# Включается переменной окружения COMPIA_TEST_MODE=1. В этом режиме fal.ai
# не дёргается (вместо реальной генерации возвращаются заглушки), а длинные
# паузы в send_bubbles сокращаются - чтобы прогон тестера был быстрым.
TEST_MODE = os.getenv("COMPIA_TEST_MODE") == "1"

# Токены обязательны. Если их нет — лучше сразу упасть с понятной ошибкой,
# чем получить непонятный сбой где-то позже.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

if not TELEGRAM_TOKEN:
    raise RuntimeError(
        "Не задан TELEGRAM_TOKEN. Создай файл .env по образцу .env.example"
    )
if not ANTHROPIC_API_KEY:
    raise RuntimeError(
        "Не задан ANTHROPIC_API_KEY. Создай файл .env по образцу .env.example"
    )

# Модель Anthropic для основных ответов (все персоны).
MODEL = "claude-sonnet-4-6"

# Переопределение модели для отдельных персон (если захочешь сделать какую-то
# дешевле/быстрее, напр. {"onboarding": "claude-haiku-4-5"}). Сейчас все на MODEL.
PERSONA_MODELS = {}

# Сколько последних сообщений класть в контекст ответа.
# 16 пар = 8 ходов диалога. На 10 Мира забывала факты, сказанные 6 ходов
# назад («Барсик - це кіт?» через 6 ходов после того как услышала «руда
# котяра»). Давнее покрывает «конспект» памяти (user_facts). Вся переписка
# при этом хранится в базе целиком.
HISTORY_LIMIT = 16

# --- Долговременная память ---
# Обновлять «конспект» о пользователе раз в столько ЕГО сообщений.
# Снижено с 15: на 15 первое обновление случается уже к концу первой фазы
# знакомства - досье собирается слишком медленно, Мира не успевает закрепить
# базовые факты (имя, питомец, город) до того, как они уйдут из HISTORY_LIMIT.
MEMORY_UPDATE_EVERY = 6

# Сколько последних сообщений отдавать модели при обновлении конспекта.
SUMMARY_HISTORY_LIMIT = 30

# Модель для составления конспекта. Берём дешёвую и быструю — для заметок хватает.
SUMMARY_MODEL = "claude-haiku-4-5"

# --- Онбординг ---
# Сколько сообщений человек должен написать, прежде чем бот попробует понять,
# нужен ему друг, коуч или близкая девушка. Держим небольшим, чтобы LLM-хост
# не успевал «уйти в фантазии» и сам начать знакомить - переключает только система.
ONBOARDING_MIN_MESSAGES = 6

# --- Проактивные сообщения («бот пишет первым») ---
# Режим по умолчанию для НОВЫХ пользователей.
# Существующих (при миграции) ставим в 'off', чтобы не писать им без спроса.
DEFAULT_CHECKIN_FREQ = "sometimes"

# После скольких часов молчания человека бот может написать первым (по режимам).
CHECKIN_INTERVALS_HOURS = {
    "rarely": 72,      # редко: ~раз в 3 дня
    "sometimes": 36,   # иногда: примерно сутки-полтора
    "often": 12,       # часто: примерно раз в полдня
}

# Как часто фоновая задача проверяет, кому пора написать (в минутах).
CHECKIN_POLL_MINUTES = 30

# Корень, куда складываются persistent-данные (БД и медиа). На хостинге это
# смонтированный volume (например, /data на Fly.io). Локально - корень репо.
DATA_DIR = os.environ.get("COMPIA_DATA_DIR", ".")

# Имя файла базы данных SQLite.
DB_PATH = os.path.join(DATA_DIR, "companion.db")

# Папка с текстами персон: по файлу на персону + common.txt с общими правилами.
PERSONAS_DIR = "personas"

# --- Генерация фото Миры (fal.ai) ---
# Ключ fal.ai. Без него фича фото просто выключена (бот работает как обычно).
FAL_KEY = os.getenv("FAL_KEY")

# Модель для базового («канонического») портрета по текстовому описанию.
IMAGE_MODEL_BASE = "fal-ai/flux/dev"
# Модель для фото по запросу: редактор по инструкции (Nano Banana / Gemini 2.5
# Flash Image). Держит того же человека и слушает позу/ракурс/одежду.
IMAGE_MODEL_REF = "fal-ai/nano-banana/edit"

# Куда сохраняем сгенерированные изображения (по подпапке на пользователя).
MEDIA_DIR = os.path.join(DATA_DIR, "media")

# Дефолтная «сцена» для базового портрета: домашний, естественный кадр (не студия).
BASE_PORTRAIT_SCENE = (
    "casual candid head-and-shoulders photo taken at home, soft natural window light, "
    "relaxed everyday look, gentle natural expression, looking at the camera"
)

# --- Видео-кружочки (image-to-video) ---
# Модель оживления фото в короткое видео.
VIDEO_MODEL = "fal-ai/kling-video/v2.1/standard/image-to-video"
# Длительность клипа в секундах (fal ждёт строку).
VIDEO_DURATION = "5"
# Сторона квадрата для Telegram video note (px).
VIDEO_NOTE_SIZE = 512
# Базовое «движение» по умолчанию (тихий живой клип, без звука).
VIDEO_MOTION_DEFAULT = (
    "she looks at the camera, soft natural smile, subtle head movement, slow blink, "
    "hair moves slightly, realistic and gentle, minimal motion"
)

# --- Говорящие кружочки (TTS + липсинк), всё на fal ---
# Озвучка: ElevenLabs multilingual (поддерживает рус/укр), на выходе mp3.
TTS_MODEL = "fal-ai/elevenlabs/tts/multilingual-v2"
# Голос (тёплый женский, мультиязычный). Можно поменять на другой пресет ElevenLabs.
TTS_VOICE = "Sarah"
# Говорящая голова: фото + аудио -> видео с синхроном губ.
TALKING_MODEL = "veed/fabric-1.0"
TALKING_RESOLUTION = "480p"
