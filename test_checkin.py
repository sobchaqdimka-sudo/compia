"""Тестовая отправка проактивного сообщения («бот пишет первым»).

Запуск:
    python test_checkin.py            # выберет последнего активного пользователя
    python test_checkin.py 123456789  # явный Telegram user_id

Скрипт ничего не меняет в базе и не трогает таймеры. Он просто генерирует
проактивное сообщение для выбранного пользователя (с его персоной и памятью)
и отправляет его тем же стилем, что и боевой бот - короткими репликами.
Можно запускать, даже когда основной бот работает: отправка не мешает опросу.
"""

import asyncio
import sys

import database
from ai import generate_checkin
from bot import bot, send_bubbles


def pick_user_id():
    """Взять user_id из аргумента или последнего активного пользователя."""
    if len(sys.argv) > 1:
        return int(sys.argv[1])
    conn = database._connect()
    row = conn.execute(
        "SELECT user_id FROM users WHERE last_seen IS NOT NULL "
        "ORDER BY last_seen DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        raise SystemExit(
            "В базе нет активных пользователей. Передай user_id аргументом: "
            "python test_checkin.py <твой_telegram_id>"
        )
    return row[0]


async def main():
    database.init_db()
    user_id = pick_user_id()
    persona = database.get_persona(user_id)
    facts = database.get_facts(user_id)

    print(f"Генерирую проактивное сообщение: user_id={user_id}, персона={persona}")
    text = await asyncio.to_thread(generate_checkin, persona, facts)
    print("\n--- Текст от модели ---\n" + text + "\n-----------------------")

    await send_bubbles(user_id, text)
    await bot.session.close()
    print("Отправлено в Telegram. Посмотри, как пришло в чат.")


if __name__ == "__main__":
    asyncio.run(main())
