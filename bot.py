"""Запуск Telegram-бота (aiogram 3).

Это точка входа. Файл связывает всё вместе: принимает сообщения,
обращается к базе данных и к модели, отправляет ответ пользователю.
Также здесь команда /persona и кнопки выбора персоны с гейтом 18+.
"""

import asyncio
import logging

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
from ai import detect_need, get_reply, update_memory
from config import (
    HISTORY_LIMIT,
    MEMORY_UPDATE_EVERY,
    ONBOARDING_MIN_MESSAGES,
    SUMMARY_HISTORY_LIMIT,
    TELEGRAM_TOKEN,
)

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


# --- Команды ---

@dp.message(CommandStart())
async def handle_start(message: Message):
    """Ответ на /start. Приветствие ВСЕГДА на украинском (требование продукта)."""
    # Создаём запись о пользователе (для нового это персона onboarding).
    database.get_persona(message.from_user.id)
    await message.answer(
        "Привіт! Я поряд. Можемо просто поговорити - як ти, що на душі?\n"
        "Якщо захочеш обрати, хто буде поруч (друг, коуч чи Міра), напиши /persona."
    )


@dp.message(Command("persona"))
async def handle_persona(message: Message):
    """Показать кнопки выбора персоны. Сменить можно в любой момент."""
    await message.answer(
        "Кого тобі хочеться поруч зараз? Обрати можна будь-коли.",
        reply_markup=persona_keyboard(),
    )


# --- Нажатия на кнопки ---

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
    else:
        await callback.message.answer("Без проблем, лишаємо як є. Нічого не змінюю.")
    await callback.answer()


# --- Обычные сообщения ---

@dp.message()
async def handle_message(message: Message):
    """Главный обработчик: на любое текстовое сообщение генерируем ответ персоны."""
    # Бот работает только с текстом. Картинки, стикеры и прочее пока пропускаем.
    if not message.text:
        await message.answer("Поки що я розумію тільки текст :)")
        return

    user_id = message.from_user.id

    # 1) Сохраняем сообщение пользователя в базу.
    database.add_message(user_id, "user", message.text)

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

    # 6) Сохраняем ответ бота и отправляем его пользователю.
    database.add_message(user_id, "assistant", reply)
    await message.answer(reply)

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


async def main():
    """Точка входа: подготовить базу и запустить опрос Telegram."""
    database.init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
