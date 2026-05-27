"""Запуск Telegram-бота (aiogram 3).

Это точка входа. Файл связывает всё вместе: принимает сообщения,
обращается к базе данных и к модели, отправляет ответ пользователю.
Также здесь команда /persona и кнопки выбора персоны с гейтом 18+.
"""

import asyncio
import logging
import re

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import database
import personas
from ai import detect_need, generate_checkin, get_reply, update_memory
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
        await callback.message.answer("Без проблем, лишаємо як є. Нічого не змінюю.")
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
    return parts[: max_bubbles - 1] + [" ".join(parts[max_bubbles - 1 :])]


async def send_bubbles(chat_id, text):
    """Отправить ответ несколькими сообщениями, как живой человек в мессенджере.

    Между репликами короткая пауза и статус «печатает», чтобы ощущалось живо.
    """
    bubbles = split_into_bubbles(text)
    if not bubbles:
        bubbles = ["..."]
    for i, bubble in enumerate(bubbles):
        if i > 0:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
            await asyncio.sleep(min(1.5, 0.4 + len(bubble) / 70))
        await bot.send_message(chat_id, bubble)


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
            if need in ("friend", "coach"):
                database.set_persona(user_id, need)
                persona_key = need
                just_switched = True
                logging.info("Онбординг: user_id=%s -> %s", user_id, need)
        except Exception:
            logging.exception("Не удалось определить потребность в онбординге")

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
    # Фоновая задача «бот пишет первым» крутится параллельно с приёмом сообщений.
    asyncio.create_task(checkin_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
