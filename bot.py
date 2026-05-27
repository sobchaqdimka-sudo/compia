"""Запуск Telegram-бота (aiogram 3).

Это точка входа. Файл связывает всё вместе: принимает сообщения,
обращается к базе данных и к модели, отправляет ответ пользователю.
Также здесь команда /persona и кнопки выбора персоны с гейтом 18+.
"""

import asyncio
import logging
import random
import re

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import database
import imagegen
import personas
from ai import (
    build_image_prompt,
    detect_need,
    generate_checkin,
    get_reply,
    screen_appearance_description,
    update_memory,
)
from config import (
    CHECKIN_POLL_MINUTES,
    HISTORY_LIMIT,
    MEMORY_UPDATE_EVERY,
    ONBOARDING_MIN_MESSAGES,
    SUMMARY_HISTORY_LIMIT,
    TELEGRAM_TOKEN,
)

# Подписи режимов проактивных сообщений (для кнопок и текста).
CHECKIN_LABELS = {
    "off": "Вимкнено",
    "rarely": "Рідко",
    "sometimes": "Іноді",
    "often": "Часто",
}

# Вступительные сообщения при выборе персоны. Изначально на украинском
# (дальше персона сама подстроится под язык собеседника).
PERSONA_INTROS = {
    "friend": "Ну що, давай знайомитись 🙂 Я Алекс. Розкажи, як ти взагалі, що нового?",
    "coach": "Радий знайомству, я Ніка. З чим хочеш розібратися? Що зараз для тебе важливо?",
    "mira": "Привіт 💛 Я Міра. Рада, що ти поруч. Давай знайомитись - як тебе звати, розкажи трохи про себе?",
}

# Простое логирование, чтобы видеть в консоли, что бот работает.
logging.basicConfig(level=logging.INFO)

# bot — подключение к Telegram; dp — диспетчер, который раздаёт сообщения хэндлерам.
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()


# --- Клавиатуры (кнопки под сообщением) ---

def persona_keyboard():
    """Кнопки выбора персоны. Берём только те, что помечены selectable."""
    rows = []
    for key, info in personas.PERSONAS.items():
        if info["selectable"]:
            rows.append(
                [InlineKeyboardButton(text=info["name"], callback_data=f"persona:{key}")]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def adult_keyboard():
    """Кнопки подтверждения возраста для персоны 18+."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Мені є 18", callback_data="adult:yes"),
                InlineKeyboardButton(text="Ще ні", callback_data="adult:no"),
            ]
        ]
    )


def start_keyboard():
    """Две опции на старте: выбрать персону сразу или просто пообщаться."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Обрати, хто поруч", callback_data="start:choose")],
            [InlineKeyboardButton(text="Просто поговорити", callback_data="start:chat")],
        ]
    )


def checkin_keyboard():
    """Кнопки выбора частоты проактивных сообщений."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Вимкнути", callback_data="checkin:off"),
                InlineKeyboardButton(text="Рідко", callback_data="checkin:rarely"),
            ],
            [
                InlineKeyboardButton(text="Іноді", callback_data="checkin:sometimes"),
                InlineKeyboardButton(text="Часто", callback_data="checkin:often"),
            ],
        ]
    )


async def send_persona_intro(message, user_id, persona_key):
    """Отправить вступительное сообщение от выбранной персоны и сохранить его."""
    intro = PERSONA_INTROS.get(persona_key)
    if intro:
        await message.answer(intro)
        # Сохраняем как сообщение бота, чтобы персона помнила, что уже представилась.
        database.add_message(user_id, "assistant", intro)


# --- Команды ---

@dp.message(CommandStart())
async def handle_start(message: Message):
    """Ответ на /start. Приветствие ВСЕГДА на украинском (требование продукта).

    Даём две опции: выбрать персону сразу или просто пообщаться (мягкий онбординг).
    """
    # Создаём запись о пользователе (для нового это персона onboarding).
    database.get_persona(message.from_user.id)
    await message.answer(
        "Привіт! Я поряд 💛\n"
        "Можемо почати по-різному: або одразу обереш, хто буде поруч "
        "(друг, коуч чи Міра), або просто поговоримо, і я сам відчую, що тобі ближче.",
        reply_markup=start_keyboard(),
    )


@dp.message(Command("persona"))
async def handle_persona(message: Message):
    """Показать кнопки выбора персоны. Сменить можно в любой момент."""
    await message.answer(
        "Кого тобі хочеться поруч зараз? Обрати можна будь-коли.",
        reply_markup=persona_keyboard(),
    )


@dp.message(Command("checkins"))
async def handle_checkins(message: Message):
    """Настройка частоты проактивных сообщений («бот пишет первым»)."""
    current = database.get_checkin_freq(message.from_user.id)
    await message.answer(
        "Я можу інколи писати тобі першим, по-доброму, коли тебе давно не було.\n"
        f"Зараз: {CHECKIN_LABELS.get(current, current)}. "
        "Обери, як часто. Вимкнути можна будь-коли.",
        reply_markup=checkin_keyboard(),
    )


@dp.message(Command("newlook"))
async def handle_newlook(message: Message):
    """Пересоздать внешность Миры: сбросить и попросить описать заново."""
    user_id = message.from_user.id
    if database.get_persona(user_id) != "mira":
        await message.answer(
            "Це про Міру 🙂 Її образ можна змінити, коли поруч саме вона."
        )
        return
    if not imagegen.is_enabled():
        await message.answer("Фото поки що недоступні.")
        return
    database.set_mira_look_status(user_id, "awaiting_description")
    await message.answer(
        "Давай переробимо мій образ 💛 Опиши, якою хочеш мене бачити - аж до одягу. "
        "Можеш додати настрій: наприклад, більш домашня, без макіяжу, природне світло."
    )


# --- Нажатия на кнопки ---

@dp.callback_query(F.data.startswith("start:"))
async def on_start_choice(callback: CallbackQuery):
    """Выбор на старте: сразу выбрать персону или просто пообщаться."""
    choice = callback.data.split(":", 1)[1]
    if choice == "choose":
        await callback.message.answer(
            "Добре. Кого тобі хочеться поруч?",
            reply_markup=persona_keyboard(),
        )
    else:  # chat — остаёмся в мягком онбординге
        await callback.message.answer(
            "Тоді просто розкажи, як ти? Що зараз на душі?"
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("persona:"))
async def on_persona_chosen(callback: CallbackQuery):
    """Пользователь выбрал персону кнопкой."""
    key = callback.data.split(":", 1)[1]
    info = personas.PERSONAS.get(key)
    user_id = callback.from_user.id

    if info is None:
        await callback.answer()
        return

    # Персона 18+ (Мира): сначала спрашиваем возраст, если ещё не подтверждён.
    if info["requires_adult"] and not database.is_adult_confirmed(user_id):
        await callback.message.answer(
            "Ця персона для дорослих. Тобі вже виповнилося 18?",
            reply_markup=adult_keyboard(),
        )
        await callback.answer()
        return

    database.set_persona(user_id, key)
    await callback.message.answer(
        f"Готово, тепер поруч {info['name']}. Змінити завжди можна через /persona."
    )
    await send_persona_intro(callback.message, user_id, key)
    await callback.answer()


@dp.callback_query(F.data.startswith("adult:"))
async def on_adult_choice(callback: CallbackQuery):
    """Ответ на подтверждение возраста (только для Миры)."""
    choice = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id

    if choice == "yes":
        database.set_adult_confirmed(user_id)
        database.set_persona(user_id, "mira")
        await callback.message.answer("Дякую. Тепер поруч Міра 💛")
        await send_persona_intro(callback.message, user_id, "mira")
    else:
        await callback.message.answer("Добре, без поспіху 🙂 Я поруч у будь-якому разі.")
    await callback.answer()


@dp.callback_query(F.data.startswith("checkin:"))
async def on_checkin_choice(callback: CallbackQuery):
    """Пользователь выбрал частоту проактивных сообщений."""
    freq = callback.data.split(":", 1)[1]
    if freq not in CHECKIN_LABELS:
        await callback.answer()
        return
    database.set_checkin_freq(callback.from_user.id, freq)
    await callback.message.answer(
        f"Готово. Як часто я пишу першим: {CHECKIN_LABELS[freq]}."
    )
    await callback.answer()


# --- Отправка ответа «живыми» сообщениями ---

def split_into_bubbles(text, max_bubbles=4):
    """Разбить ответ модели на отдельные короткие реплики.

    Модель иногда отдаёт текст в несколько строк/абзацев. Каждую непустую
    строку делаем отдельным сообщением — так уходят пустые строки и переписка
    выглядит как живой texting. Чтобы не спамить, хвост сверх лимита склеиваем.
    """
    parts = [p.strip() for p in re.split(r"\n+", text or "") if p.strip()]
    if len(parts) <= max_bubbles:
        return parts
    return parts[: max_bubbles - 1] + ["\n".join(parts[max_bubbles - 1 :])]


async def send_bubbles(chat_id, text):
    """Отправить ответ несколькими сообщениями, как живой человек в мессенджере.

    Число реплик слегка рандомим: иногда всё одним сообщением (когда мысль
    цельная или это история подлиннее), иногда 2-3 коротких подряд. Так
    переписка не выглядит каждый раз одинаково «ровно по два смс».
    Между репликами короткая пауза и статус «печатает», чтобы ощущалось живо.
    """
    max_bubbles = random.choices([1, 2, 3], weights=[25, 45, 30])[0]
    bubbles = split_into_bubbles(text, max_bubbles=max_bubbles)
    if not bubbles:
        bubbles = ["..."]
    for i, bubble in enumerate(bubbles):
        if i > 0:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
            await asyncio.sleep(min(1.5, 0.4 + len(bubble) / 70))
        await bot.send_message(chat_id, bubble)


# Слова-маркеры просьбы «пришли фото» (укр/рус/англ). Сравниваем по подстроке
# в нижнем регистре — отсюда стемы вроде "фотк" ловят и "фотку", и "фоткой".
PHOTO_TRIGGERS = (
    "фото", "фотк", "сфотк", "сфота", "селфі", "селфи", "selfie",
    "покажись", "покажи себе", "покажи себя", "пришли картин", "пришли свою",
    "як ти виглядаєш", "как ты выглядишь", "хочу тебе побачити",
    "хочу тебя увидеть", "хочу побачити тебе", "твоє фото", "твое фото",
    "your photo", "send a pic", "show yourself",
)


def looks_like_photo_request(text):
    """Похоже ли сообщение на просьбу прислать фото."""
    low = (text or "").lower()
    return any(trigger in low for trigger in PHOTO_TRIGGERS)


async def _generate_and_send_base(message, user_id, description):
    """Создать канонический портрет Миры по описанию и отправить его."""
    await message.answer("Добре... дай мені хвилинку 💛")
    await bot.send_chat_action(chat_id=message.chat.id, action="upload_photo")
    try:
        prompt = await asyncio.to_thread(build_image_prompt, description, "")
        path = await asyncio.to_thread(imagegen.generate_base_portrait, prompt, user_id)
    except Exception:
        logging.exception("Не удалось создать базовый портрет user_id=%s", user_id)
        await message.answer("Ой, не вийшло цього разу. Спробуймо трохи згодом?")
        return
    database.save_mira_look(user_id, description, path)
    database.increment_photos(user_id)
    database.add_message(user_id, "assistant", "[надіслала тобі своє фото] Ось я 💛")
    await message.answer_photo(FSInputFile(path), caption="Ось я 💛 Подобаюсь?")


async def _generate_and_send_photo(message, user_id, look, request_text):
    """Создать фото Миры по запросу, сохраняя то же лицо (по референсу)."""
    await message.answer("Зараз зроблю для тебе 💛")
    await bot.send_chat_action(chat_id=message.chat.id, action="upload_photo")
    try:
        prompt = await asyncio.to_thread(build_image_prompt, look["desc"], request_text)
        path = await asyncio.to_thread(
            imagegen.generate_with_reference, prompt, look["base_path"], user_id
        )
    except Exception:
        logging.exception("Не удалось создать фото user_id=%s", user_id)
        await message.answer("Ой, не вийшло цього разу. Спробуймо трохи згодом?")
        return
    database.increment_photos(user_id)
    database.add_message(user_id, "assistant", "[надіслала тобі своє фото]")
    await message.answer_photo(FSInputFile(path))


async def try_handle_mira_photo(message, user_id):
    """Логика фото Миры. Возвращает True, если сообщение обработано здесь.

    Сценарии:
    - ждём описание внешности → текущее сообщение и есть описание (с модерацией);
    - явная просьба фото, а внешности ещё нет → просим описать;
    - просьба фото, внешность готова → генерируем фото по референсу.
    """
    if not imagegen.is_enabled():
        return False

    look = database.get_mira_look(user_id)
    status = look["status"]
    text = message.text

    if status == "awaiting_description":
        ok, _reason = await asyncio.to_thread(screen_appearance_description, text)
        if not ok:
            # Это не описание внешности (или недопустимо) - выходим из ожидания,
            # пусть Мира ответит обычным сообщением, без нотаций.
            database.set_mira_look_status(user_id, "none")
            return False
        await _generate_and_send_base(message, user_id, text)
        return True

    if looks_like_photo_request(text):
        if status != "ready":
            database.set_mira_look_status(user_id, "awaiting_description")
            ask = (
                "Хочеш мене побачити? 🙈 А якою ти мене уявляєш? "
                "Опиши, будь ласка, - аж до одягу."
            )
            database.add_message(user_id, "assistant", ask)
            await message.answer(ask)
            return True
        await _generate_and_send_photo(message, user_id, look, text)
        return True

    return False


# --- Обычные сообщения ---

@dp.message()
async def handle_message(message: Message):
    """Главный обработчик: на любое текстовое сообщение генерируем ответ персоны."""
    # Бот работает только с текстом. Картинки, стикеры и прочее пока пропускаем.
    if not message.text:
        await message.answer("Поки що я розумію тільки текст :)")
        return

    user_id = message.from_user.id

    # 1) Сохраняем сообщение пользователя и отмечаем его активность
    #    (это сбрасывает таймер «бот пишет первым»).
    database.add_message(user_id, "user", message.text)
    database.touch_last_seen(user_id)

    # 2) Берём выбранную персону, историю, память и счётчик сообщений.
    persona_key = database.get_persona(user_id)
    history = database.get_history(user_id, HISTORY_LIMIT)
    facts = database.get_facts(user_id)
    user_msg_count = database.count_user_messages(user_id)

    # 3) Показываем статус «печатает...», пока идёт обработка.
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    # 4) Мягкий онбординг: если человек ещё на «знакомстве» и уже немного
    #    пообщался, тихо определяем, кто ему нужнее — друг или коуч, и
    #    переключаем персону. Романтику (Миру) тут не выбираем никогда.
    just_switched = False
    if persona_key == "onboarding" and user_msg_count >= ONBOARDING_MIN_MESSAGES:
        try:
            need = await asyncio.to_thread(detect_need, history)
        except Exception:
            logging.exception("Не удалось определить потребность в онбординге")
            need = "unclear"

        if need == "romantic":
            if database.is_adult_confirmed(user_id):
                database.set_persona(user_id, "mira")
                persona_key = "mira"
                just_switched = True
                logging.info("Онбординг: user_id=%s -> mira", user_id)
            else:
                # Человек тянется к близости. Предлагаем Миру через гейт 18+.
                # Другом ставим как мягкий дефолт, чтобы он не застрял в онбординге.
                database.set_persona(user_id, "friend")
                invite = (
                    "Здається, тобі хочеться когось по-справжньому близького, свого 💛 "
                    "Для цього в мене є Міра - тепла й ніжна супутниця. Тільки це "
                    "доросла історія, тож скажи: тобі вже виповнилося 18?"
                )
                database.add_message(user_id, "assistant", invite)
                await message.answer(invite, reply_markup=adult_keyboard())
                logging.info("Онбординг: user_id=%s -> предложена Мира (гейт 18+)", user_id)
                return
        elif need in ("friend", "coach"):
            database.set_persona(user_id, need)
            persona_key = need
            just_switched = True
            logging.info("Онбординг: user_id=%s -> %s", user_id, need)

    # 4.5) Фото Миры: либо просим описать внешность, либо генерируем по запросу.
    #      Если сообщение обработано здесь (фото/вопрос об описании) — выходим.
    if persona_key == "mira" and await try_handle_mira_photo(message, user_id):
        return

    # 5) Получаем ответ от модели. Запрос к Anthropic обычный (не async),
    #    поэтому выносим его в отдельный поток, чтобы бот не «зависал».
    try:
        reply = await asyncio.to_thread(
            get_reply, persona_key, history, facts, just_switched
        )
    except Exception:
        logging.exception("Ошибка при запросе к Anthropic")
        await message.answer("Ой, щось пішло не так. Спробуй ще раз трохи згодом.")
        return

    # 6) Сохраняем ответ бота и отправляем его пользователю «живыми» репликами.
    database.add_message(user_id, "assistant", reply)
    await send_bubbles(message.chat.id, reply)

    # 7) Долговременная память: раз в MEMORY_UPDATE_EVERY сообщений пользователя
    #    обновляем «конспект». Делаем это ПОСЛЕ ответа, чтобы человек не ждал лишнего.
    try:
        if user_msg_count % MEMORY_UPDATE_EVERY == 0:
            recent = database.get_history(user_id, SUMMARY_HISTORY_LIMIT)
            new_facts = await asyncio.to_thread(update_memory, facts, recent)
            database.save_facts(user_id, new_facts)
            logging.info("Долговременная память обновлена для user_id=%s", user_id)
    except Exception:
        # Если обновить память не вышло — не страшно, просто логируем.
        logging.exception("Не удалось обновить долговременную память")


# --- Проактивные сообщения (фоновая задача) ---

async def run_checkins():
    """Один проход: найти, кому пора написать первым, и отправить сообщение."""
    due = await asyncio.to_thread(database.get_due_checkin_users)
    for user_id, persona_key, facts in due:
        try:
            # Текст генерируем в отдельном потоке (запрос к Anthropic блокирующий).
            text = await asyncio.to_thread(generate_checkin, persona_key, facts)
            await send_bubbles(user_id, text)
            # Сохраняем как сообщение бота, чтобы сохранить непрерывность диалога.
            database.add_message(user_id, "assistant", text)
            database.set_last_checkin(user_id)
            logging.info("Проактивное сообщение отправлено user_id=%s", user_id)
        except Exception:
            logging.exception(
                "Не удалось отправить проактивное сообщение user_id=%s", user_id
            )


async def checkin_loop():
    """Фоновый цикл: периодически проверяет, кому пора написать первым."""
    while True:
        await asyncio.sleep(CHECKIN_POLL_MINUTES * 60)
        try:
            await run_checkins()
        except Exception:
            logging.exception("Ошибка в фоновой задаче проактивных сообщений")


async def main():
    """Точка входа: подготовить базу, запустить фоновую задачу и опрос Telegram."""
    database.init_db()

    # Диагностика фото: какой Python запустил бота и виден ли ему fal_client.
    # Если тут WARNING — пакет стоит в ДРУГОМ интерпретаторе (типичная беда на Mac).
    import sys
    logging.info("Python бота: %s", sys.executable)
    if not imagegen.is_enabled():
        logging.info("Фото выключены: не задан FAL_KEY в .env")
    else:
        try:
            import fal_client
            logging.info("fal_client доступен: %s", fal_client.__file__)
        except Exception as exc:
            logging.warning(
                "fal_client НЕ виден этому Python (%s). Установи в него: "
                "%s -m pip install --user fal-client | детали: %r",
                sys.executable, sys.executable, exc,
            )

    # Фоновая задача «бот пишет первым» крутится параллельно с приёмом сообщений.
    asyncio.create_task(checkin_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
