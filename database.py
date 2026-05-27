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
    """Создать таблицы, если их ещё нет. Вызывается один раз при старте."""
    conn = _connect()
    # Таблица всех сообщений переписки.
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
    # Долговременная память: по одной строке на пользователя с его «конспектом».
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_facts (
            user_id    INTEGER PRIMARY KEY,
            facts      TEXT    NOT NULL,
            updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    # Состояние пользователя: выбранная персона, подтверждение 18+,
    # настройка проактивных сообщений и отметки активности.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id         INTEGER PRIMARY KEY,
            persona         TEXT    NOT NULL DEFAULT 'onboarding',
            adult_confirmed INTEGER NOT NULL DEFAULT 0,
            checkin_freq    TEXT    NOT NULL DEFAULT 'off',
            last_seen       TEXT,
            last_checkin_at TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()

    # Миграция: всех, кто уже общался с ботом (есть в messages), но кого ещё
    # нет в users, переносим на Миру с подтверждённым 18+. Так старые
    # пользователи не теряют свой романтический контекст и не падают в онбординг.
    # Запрос идемпотентный: при следующих запусках такие строки уже есть.
    conn.execute(
        """
        INSERT INTO users (user_id, persona, adult_confirmed)
        SELECT DISTINCT user_id, 'mira', 1 FROM messages
        WHERE user_id NOT IN (SELECT user_id FROM users)
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


def count_user_messages(user_id):
    """Сколько всего сообщений написал сам пользователь (роль 'user')."""
    conn = _connect()
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE user_id = ? AND role = 'user'",
        (user_id,),
    ).fetchone()
    conn.close()
    return row[0]


def get_facts(user_id):
    """Вернуть «конспект» о пользователе (или пустую строку, если его ещё нет)."""
    conn = _connect()
    row = conn.execute(
        "SELECT facts FROM user_facts WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else ""


def save_facts(user_id, facts):
    """Сохранить (или перезаписать) конспект о пользователе.

    ON CONFLICT — если строка с таким user_id уже есть, обновляем её,
    а не создаём вторую (это называется «upsert»).
    """
    conn = _connect()
    conn.execute(
        """
        INSERT INTO user_facts (user_id, facts, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(user_id) DO UPDATE SET
            facts = excluded.facts,
            updated_at = excluded.updated_at
        """,
        (user_id, facts),
    )
    conn.commit()
    conn.close()


# --- Состояние пользователя (персона, возраст) ---

def _ensure_user(conn, user_id):
    """Создать строку пользователя, если её ещё нет (внутренний помощник).

    INSERT OR IGNORE ничего не делает, если строка с таким user_id уже есть.
    """
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))


def get_persona(user_id):
    """Вернуть ключ выбранной персоны. Для нового пользователя - 'onboarding'."""
    conn = _connect()
    _ensure_user(conn, user_id)
    row = conn.execute(
        "SELECT persona FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.commit()
    conn.close()
    return row[0]


def set_persona(user_id, persona):
    """Запомнить выбранную персону пользователя."""
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        "UPDATE users SET persona = ? WHERE user_id = ?", (persona, user_id)
    )
    conn.commit()
    conn.close()


def is_adult_confirmed(user_id):
    """Подтвердил ли пользователь, что ему есть 18 лет."""
    conn = _connect()
    _ensure_user(conn, user_id)
    row = conn.execute(
        "SELECT adult_confirmed FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.commit()
    conn.close()
    return bool(row[0])


def set_adult_confirmed(user_id):
    """Отметить, что возраст подтверждён (18+)."""
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        "UPDATE users SET adult_confirmed = 1 WHERE user_id = ?", (user_id,)
    )
    conn.commit()
    conn.close()
