"""Запуск Telegram-бота (aiogram 3).

Это точка входа. Файл связывает всё вместе: принимает сообщения,
обращается к базе данных и к модели, отправляет ответ пользователю.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message

import database
from ai import get_reply, update_memory
from config import (
    HISTORY_LIMIT,
    MEMORY_UPDATE_EVERY,
    SUMMARY_HISTORY_LIMIT,
    TELEGRAM_TOKEN,
)

# Простое логирование, чтобы видеть в консоли, что бот работает.
logging.basicConfig(level=logging.INFO)

# bot — подключение к Telegram; dp — диспетчер, который раздаёт сообщения хэндлерам.
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def handle_start(message: Message):
    """Ответ на команду /start — приветствие."""
    await message.answer("Привет! Я рядом. О чём поговорим?")


@dp.message()
async def handle_message(message: Message):
    """Главный обработчик: на любое текстовое сообщение генерируем ответ персонажа."""
    # Бот работает только с текстом. Картинки, стикеры и прочее пока пропускаем.
    if not message.text:
        await message.answer("Я пока понимаю только текст :)")
        return

    user_id = message.from_user.id

    # 1) Сохраняем сообщение пользователя в базу.
    database.add_message(user_id, "user", message.text)

    # 2) Достаём последние сообщения для контекста (включая только что сохранённое)
    #    и долговременный «конспект» о пользователе.
    history = database.get_history(user_id, HISTORY_LIMIT)
    facts = database.get_facts(user_id)

    # 3) Показываем статус «печатает...», пока ждём ответ модели.
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    # 4) Получаем ответ от модели. Запрос к Anthropic обычный (не async),
    #    поэтому выносим его в отдельный поток, чтобы бот не «зависал».
    try:
        reply = await asyncio.to_thread(get_reply, history, facts)
    except Exception:
        logging.exception("Ошибка при запросе к Anthropic")
        await message.answer("Ой, что-то пошло не так. Попробуй ещё раз чуть позже.")
        return

    # 5) Сохраняем ответ бота и отправляем его пользователю.
    database.add_message(user_id, "assistant", reply)
    await message.answer(reply)

    # 6) Долговременная память: раз в MEMORY_UPDATE_EVERY сообщений пользователя
    #    обновляем «конспект». Делаем это ПОСЛЕ ответа, чтобы человек не ждал лишнего.
    try:
        user_msg_count = database.count_user_messages(user_id)
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
