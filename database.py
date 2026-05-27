"""Работа с базой данных SQLite.

Храним всю переписку: одна строка таблицы = одно сообщение.
Поле role = "user" (сообщение человека) или "assistant" (ответ бота).
"""

import sqlite3

from config import DB_PATH


def _connect():
    """Открыть соединение с базой. Каждая функция открывает и закрывает своё."""
    return sqlite3.connect(DB_PATH)


def init_db():
    """Создать таблицу сообщений, если её ещё нет. Вызывается один раз при старте."""
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            role       TEXT    NOT NULL,
            content    TEXT    NOT NULL,
            created_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    conn.close()


def add_message(user_id, role, content):
    """Сохранить одно сообщение в базу."""
    conn = _connect()
    # Значения подставляем через "?", а не через форматирование строки —
    # так безопаснее (защита от SQL-инъекций).
    conn.execute(
        "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
        (user_id, role, content),
    )
    conn.commit()
    conn.close()


def get_history(user_id, limit):
    """Вернуть последние `limit` сообщений пользователя в хронологическом порядке.

    Результат сразу в формате для Anthropic API:
    список словарей вида {"role": ..., "content": ...}.
    """
    conn = _connect()
    rows = conn.execute(
        """
        SELECT role, content FROM messages
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, limit),
    ).fetchall()
    conn.close()

    # Из базы достаём «свежие сверху» (ORDER BY id DESC), а модели нужно
    # «старые сверху» — поэтому разворачиваем список.
    rows.reverse()
    return [{"role": role, "content": content} for role, content in rows]
