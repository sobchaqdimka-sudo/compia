"""Работа с базой данных SQLite.

Храним всю переписку: одна строка таблицы = одно сообщение.
Поле role = "user" (сообщение человека) или "assistant" (ответ бота).
"""

import json
import sqlite3
from datetime import datetime

from config import CHECKIN_INTERVALS_HOURS, DB_PATH, DEFAULT_CHECKIN_FREQ


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

    # Миграция: добавляем колонки для персональной внешности Миры и счётчика
    # фото, если их ещё нет (на уже существующей базе). SQLite не умеет
    # "ADD COLUMN IF NOT EXISTS", поэтому смотрим, какие колонки уже есть.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    mira_columns = {
        # none → ещё не заходила речь о фото;
        # awaiting_description → попросили описать внешность, ждём ответ;
        # ready → базовый портрет создан, можно генерить фото по запросу.
        "mira_look_status": "TEXT NOT NULL DEFAULT 'none'",
        "mira_look_desc": "TEXT",          # описание внешности словами пользователя
        "mira_base_path": "TEXT",          # путь к каноническому портрету (референс)
        "photos_made": "INTEGER NOT NULL DEFAULT 0",  # сколько фото уже сгенерили
        # Имя Миры для этого пользователя.
        # status: 'unrevealed' (имя ещё не названо), 'nicknamed' (он дал ей имя),
        # 'revealed' (отказался дать своё, она назвалась Мирой).
        "mira_name_status": "TEXT NOT NULL DEFAULT 'unrevealed'",
        "mira_nickname": "TEXT",  # имя, которое дал пользователь (если дал)
        # Момент первой активации Миры для этого пользователя — точка отсчёта
        # «стадий отношений» (день 0 = только знакомство; чем дальше — глубже).
        "mira_activated_at": "TEXT",
        # Когда (и был ли) пользователь явно отказался от гейта 18+. Если стоит -
        # больше не предлагаем автоматически (только через ручной /persona).
        "adult_declined_at": "TEXT",
        # Случайное «настоящее имя» для конкретного юзера. У каждого своё - чтобы
        # был эффект приватности (а не у всех «Мира»). Генерится один раз при
        # первой активации. Внутреннее кодовое имя модели (Мира) живёт в коде.
        "mira_real_name": "TEXT",
        # message_id закреплённого в чате базового фото Миры - чтобы при
        # /newlook открепить старое и закрепить новое на его место.
        "mira_pinned_msg_id": "INTEGER",
        # message_id последнего отправленного базового фото (закреплено ещё
        # или нет). Закрепляем только после положительной реакции юзера.
        "mira_base_photo_msg_id": "INTEGER",
        # Один раз после первого базового фото и позитивной реакции юзера
        # подсказываем поставить это фото как кастомную аватарку контакта.
        "mira_avatar_invite_sent": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, decl in mira_columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")
    conn.commit()

    # Миграция: колонки для трекинга стоимости в users. Храним суммарные
    # токены и стоимость в "сотых цента" (т.е. value 1234 = $0.1234) - так
    # храним достаточную точность без чисел с плавающей точкой.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    usage_columns = {
        "anthropic_input_tokens": "INTEGER NOT NULL DEFAULT 0",
        "anthropic_output_tokens": "INTEGER NOT NULL DEFAULT 0",
        "anthropic_cache_read_tokens": "INTEGER NOT NULL DEFAULT 0",
        "anthropic_cache_write_tokens": "INTEGER NOT NULL DEFAULT 0",
        # cost: целые "десятитысячные доллара" — value 10000 = $1.00.
        "anthropic_cost_cents": "INTEGER NOT NULL DEFAULT 0",
        "fal_cost_cents": "INTEGER NOT NULL DEFAULT 0",
        "fal_calls": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, decl in usage_columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")
    conn.commit()

    # Таблица продуктовых событий. Простой append-only лог: тип события +
    # опциональный JSON с деталями. Используется для воронок и аналитики.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            event_type TEXT    NOT NULL,
            event_data TEXT,
            created_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_type_time "
        "ON events (event_type, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_user_time "
        "ON events (user_id, created_at)"
    )
    conn.commit()

    # Миграция: всех, кто уже общался с ботом (есть в messages), но кого ещё
    # нет в users, переносим на Миру с подтверждённым 18+. Так старые
    # пользователи не теряют свой романтический контекст и не падают в онбординг.
    # Запрос идемпотентный: при следующих запусках такие строки уже есть.
    conn.execute(
        """
        INSERT INTO users (user_id, persona, adult_confirmed, checkin_freq)
        SELECT DISTINCT user_id, 'mira', 1, 'off' FROM messages
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


# --- Продуктовые события и трекинг стоимости ---

def log_event(user_id, event_type, event_data=None):
    """Записать продуктовое событие.

    event_data — опциональный словарь, сериализуем в JSON. Если передали
    что-то несериализуемое (например, объект aiogram) — мягко падаем в str.
    user_id может быть None для системных событий, не привязанных к юзеру.
    """
    payload = None
    if event_data is not None:
        try:
            payload = json.dumps(event_data, ensure_ascii=False)
        except (TypeError, ValueError):
            payload = json.dumps({"_repr": str(event_data)}, ensure_ascii=False)
    conn = _connect()
    conn.execute(
        "INSERT INTO events (user_id, event_type, event_data) VALUES (?, ?, ?)",
        (user_id, event_type, payload),
    )
    conn.commit()
    conn.close()


def _usd_to_units(cost_usd):
    """USD как float -> целые "десятитысячные доллара" (value 10000 = $1.00).

    Округляем половиной к ближайшему, чтобы суммирование тысяч мелких
    вызовов не сваливалось в смещение.
    """
    if not cost_usd:
        return 0
    return int(round(float(cost_usd) * 10000))


def add_user_anthropic_usage(user_id, in_t, out_t, cache_r, cache_w, cost_usd):
    """Прибавить к счётчикам юзера токены и стоимость одного Anthropic-вызова.

    Стоимость храним в "десятитысячных доллара" (см. _usd_to_units), чтобы
    не плодить ошибки float-сумм. Если user_id == None — молча игнорим
    (это вызов без контекста юзера, напр. из фоновой задачи перед polling).
    """
    if user_id is None:
        return
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        """
        UPDATE users SET
            anthropic_input_tokens       = anthropic_input_tokens + ?,
            anthropic_output_tokens      = anthropic_output_tokens + ?,
            anthropic_cache_read_tokens  = anthropic_cache_read_tokens + ?,
            anthropic_cache_write_tokens = anthropic_cache_write_tokens + ?,
            anthropic_cost_cents         = anthropic_cost_cents + ?
        WHERE user_id = ?
        """,
        (
            int(in_t or 0),
            int(out_t or 0),
            int(cache_r or 0),
            int(cache_w or 0),
            _usd_to_units(cost_usd),
            user_id,
        ),
    )
    conn.commit()
    conn.close()


def add_user_fal_usage(user_id, cost_usd):
    """Прибавить один fal-вызов и его стоимость к счётчикам юзера."""
    if user_id is None:
        return
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        """
        UPDATE users SET
            fal_cost_cents = fal_cost_cents + ?,
            fal_calls      = fal_calls + 1
        WHERE user_id = ?
        """,
        (_usd_to_units(cost_usd), user_id),
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
    Новым пользователям сразу ставим режим проактива по умолчанию.
    """
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, checkin_freq) VALUES (?, ?)",
        (user_id, DEFAULT_CHECKIN_FREQ),
    )


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


# --- Внешность Миры и фото ---

def get_mira_look(user_id):
    """Вернуть состояние внешности Миры для пользователя.

    Словарь: status ('none'/'awaiting_description'/'ready'),
    desc (описание словами пользователя) и base_path (путь к портрету-референсу).
    """
    conn = _connect()
    _ensure_user(conn, user_id)
    row = conn.execute(
        "SELECT mira_look_status, mira_look_desc, mira_base_path "
        "FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.commit()
    conn.close()
    return {"status": row[0], "desc": row[1], "base_path": row[2]}


def set_mira_look_status(user_id, status):
    """Обновить только статус внешности (например, 'awaiting_description')."""
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        "UPDATE users SET mira_look_status = ? WHERE user_id = ?", (status, user_id)
    )
    conn.commit()
    conn.close()


def save_mira_look(user_id, desc, base_path):
    """Сохранить готовую внешность: описание + путь к портрету, статус 'ready'."""
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        "UPDATE users SET mira_look_desc = ?, mira_base_path = ?, "
        "mira_look_status = 'ready' WHERE user_id = ?",
        (desc, base_path, user_id),
    )
    conn.commit()
    conn.close()


def set_mira_pinned_msg(user_id, msg_id):
    """Запомнить (или сбросить) message_id закреплённого базового фото Миры."""
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        "UPDATE users SET mira_pinned_msg_id = ? WHERE user_id = ?",
        (msg_id, user_id),
    )
    conn.commit()
    conn.close()


def get_mira_pinned_msg(user_id):
    """Вернуть message_id закреплённого базового фото или None."""
    conn = _connect()
    _ensure_user(conn, user_id)
    row = conn.execute(
        "SELECT mira_pinned_msg_id FROM users WHERE user_id = ?", (user_id,),
    ).fetchone()
    conn.commit()
    conn.close()
    return row[0] if row else None


def set_mira_base_photo_msg(user_id, msg_id):
    """Запомнить (или сбросить) message_id последнего отправленного базового фото."""
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        "UPDATE users SET mira_base_photo_msg_id = ? WHERE user_id = ?",
        (msg_id, user_id),
    )
    conn.commit()
    conn.close()


def get_mira_base_photo_msg(user_id):
    """Вернуть message_id последнего базового фото (закреплено или нет)."""
    conn = _connect()
    _ensure_user(conn, user_id)
    row = conn.execute(
        "SELECT mira_base_photo_msg_id FROM users WHERE user_id = ?", (user_id,),
    ).fetchone()
    conn.commit()
    conn.close()
    return row[0] if row else None


def is_avatar_invite_sent(user_id):
    """Подсказку про кастомную аватарку уже слали?"""
    conn = _connect()
    _ensure_user(conn, user_id)
    row = conn.execute(
        "SELECT mira_avatar_invite_sent FROM users WHERE user_id = ?", (user_id,),
    ).fetchone()
    conn.commit()
    conn.close()
    return bool(row[0]) if row else False


def mark_avatar_invite_sent(user_id):
    """Отметить, что подсказку про кастомную аватарку уже отправили."""
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        "UPDATE users SET mira_avatar_invite_sent = 1 WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def get_mira_name_state(user_id):
    """Состояние имени Миры для пользователя: {'status': ..., 'nickname': ...}."""
    conn = _connect()
    _ensure_user(conn, user_id)
    row = conn.execute(
        "SELECT mira_name_status, mira_nickname FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.commit()
    conn.close()
    return {"status": row[0], "nickname": row[1]}


def set_mira_nickname(user_id, nickname):
    """Запомнить никнейм, который пользователь дал Мире, и сменить статус."""
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        "UPDATE users SET mira_nickname = ?, mira_name_status = 'nicknamed' "
        "WHERE user_id = ?",
        (nickname, user_id),
    )
    conn.commit()
    conn.close()


def set_mira_name_revealed(user_id):
    """Пользователь отказался дать имя — Мира открывается как Мира."""
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        "UPDATE users SET mira_name_status = 'revealed' WHERE user_id = ?", (user_id,)
    )
    conn.commit()
    conn.close()


def get_mira_real_name(user_id):
    """Случайно сгенерированное «настоящее имя» Миры для этого юзера, или None."""
    conn = _connect()
    _ensure_user(conn, user_id)
    row = conn.execute(
        "SELECT mira_real_name FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.commit()
    conn.close()
    return row[0] if row else None


def set_mira_real_name_if_unset(user_id, name):
    """Идемпотентно поставить «настоящее имя» Миры для юзера. Повторно не перезаписывает."""
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        "UPDATE users SET mira_real_name = ? "
        "WHERE user_id = ? AND mira_real_name IS NULL",
        (name, user_id),
    )
    conn.commit()
    conn.close()


def is_adult_declined(user_id):
    """Был ли явный отказ от гейта 18+ (чтобы не дёргать его автоматически снова)."""
    conn = _connect()
    _ensure_user(conn, user_id)
    row = conn.execute(
        "SELECT adult_declined_at FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.commit()
    conn.close()
    return bool(row and row[0])


def set_adult_declined(user_id):
    """Отметить отказ от гейта 18+. Автоматический гейт больше не показываем."""
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        "UPDATE users SET adult_declined_at = datetime('now') WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def clear_adult_declined(user_id):
    """Сбросить отметку отказа (когда пользователь ВРУЧНУЮ запрашивает Миру)."""
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        "UPDATE users SET adult_declined_at = NULL WHERE user_id = ?", (user_id,)
    )
    conn.commit()
    conn.close()


def set_mira_activated_if_unset(user_id):
    """Поставить отметку «Мира впервые активирована» — только если её ещё нет.

    Идемпотентно: повторные вызовы не сбрасывают дату. Используется как точка
    отсчёта стадий отношений (дни вместе с Мирой).
    """
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        "UPDATE users SET mira_activated_at = datetime('now') "
        "WHERE user_id = ? AND mira_activated_at IS NULL",
        (user_id,),
    )
    conn.commit()
    conn.close()


def get_mira_activated_at(user_id):
    """Вернуть момент активации Миры (строка SQLite-времени) или None."""
    conn = _connect()
    _ensure_user(conn, user_id)
    row = conn.execute(
        "SELECT mira_activated_at FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.commit()
    conn.close()
    return row[0] if row else None


def has_meaningful_state(user_id):
    """Есть ли у юзера хоть какое-то накопленное состояние, которое стоит сбросить.

    Возвращает True, если есть сообщения, досье, или любое поле в `users`
    отличается от дефолта (выбрана персона, подтверждён 18+, описана внешность
    Миры, есть никнейм, активирована Мира, был отказ от гейта, сделаны фото).
    Используется в /start, чтобы предложить подтверждение сброса в случаях,
    когда юзер ещё не написал ни одного сообщения, но УЖЕ что-то нажал
    (выбрал персону, прошёл гейт, начал описывать Миру и т.п.).
    """
    conn = _connect()
    msgs = conn.execute(
        "SELECT 1 FROM messages WHERE user_id = ? LIMIT 1", (user_id,)
    ).fetchone()
    if msgs:
        conn.close()
        return True
    facts = conn.execute(
        "SELECT 1 FROM user_facts WHERE user_id = ? LIMIT 1", (user_id,)
    ).fetchone()
    if facts:
        conn.close()
        return True
    row = conn.execute(
        """
        SELECT persona, adult_confirmed, mira_look_status, mira_nickname,
               mira_activated_at, adult_declined_at, photos_made
        FROM users WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()
    conn.close()
    if not row:
        return False
    persona, adult, look_status, nickname, activated, declined, photos = row
    if persona and persona != "onboarding":
        return True
    if adult:
        return True
    if look_status and look_status != "none":
        return True
    if nickname:
        return True
    if activated:
        return True
    if declined:
        return True
    if photos:
        return True
    return False


def clear_stale_awaiting_description(user_id):
    """Сбросить «зависшее» ожидание описания внешности Миры.

    Если юзер сделал /newlook, но не ответил - статус `awaiting_description`
    остаётся и любое следующее сообщение («Привіт») будет интерпретировано
    как описание внешности и сгенерит мусорный портрет. Этот хелпер чинит
    ситуацию: если есть готовый базовый портрет - возвращаемся в 'ready',
    иначе - в 'none'. Любой другой статус не трогаем (идемпотентно).
    """
    conn = _connect()
    _ensure_user(conn, user_id)
    row = conn.execute(
        "SELECT mira_look_status, mira_base_path FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row and row[0] == "awaiting_description":
        new_status = "ready" if row[1] else "none"
        conn.execute(
            "UPDATE users SET mira_look_status = ? WHERE user_id = ?",
            (new_status, user_id),
        )
        conn.commit()
    conn.close()


def wipe_user(user_id):
    """Полностью стереть данные конкретного пользователя (для сброса по /start).

    Удаляем все его сообщения, долговременную память и строку из users (включая
    внешность Миры, никнейм, счётчики). После этого человек снова «новый».
    """
    conn = _connect()
    conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM user_facts WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def increment_photos(user_id):
    """Увеличить счётчик сгенерированных фото (для учёта расходов)."""
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        "UPDATE users SET photos_made = photos_made + 1 WHERE user_id = ?", (user_id,)
    )
    conn.commit()
    conn.close()


# --- Проактивные сообщения («бот пишет первым») ---

def get_checkin_freq(user_id):
    """Вернуть режим проактивных сообщений: off / rarely / sometimes / often."""
    conn = _connect()
    _ensure_user(conn, user_id)
    row = conn.execute(
        "SELECT checkin_freq FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.commit()
    conn.close()
    return row[0]


def set_checkin_freq(user_id, freq):
    """Запомнить выбранный режим проактивных сообщений."""
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        "UPDATE users SET checkin_freq = ? WHERE user_id = ?", (freq, user_id)
    )
    conn.commit()
    conn.close()


def touch_last_seen(user_id):
    """Отметить, что пользователь только что был активен (написал сообщение)."""
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        "UPDATE users SET last_seen = datetime('now') WHERE user_id = ?", (user_id,)
    )
    conn.commit()
    conn.close()


def set_last_checkin(user_id):
    """Отметить, что бот только что написал этому человеку первым."""
    conn = _connect()
    _ensure_user(conn, user_id)
    conn.execute(
        "UPDATE users SET last_checkin_at = datetime('now') WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def _hours_since(now, ts_text):
    """Сколько часов прошло с момента ts_text (текст из SQLite) до now."""
    if ts_text is None:
        return float("inf")
    ts = datetime.strptime(ts_text, "%Y-%m-%d %H:%M:%S")
    return (now - ts).total_seconds() / 3600


def get_due_checkin_users():
    """Вернуть тех, кому пора написать первым: список (user_id, persona, facts).

    Берём только тех, у кого:
    - проактив включён (не 'off');
    - есть «конспект» памяти (пишем лишь тем, кого реально помним);
    - человек уже был активен (last_seen не пустой) и молчит дольше порога;
    - мы ещё не писали ему первыми с момента его последней активности
      (чтобы не «долбить» того, кто не вернулся).
    """
    conn = _connect()
    rows = conn.execute(
        """
        SELECT u.user_id, u.persona, u.checkin_freq, u.last_seen,
               u.last_checkin_at, f.facts
        FROM users u
        JOIN user_facts f ON f.user_id = u.user_id
        WHERE u.checkin_freq != 'off' AND u.last_seen IS NOT NULL
        """
    ).fetchall()
    conn.close()

    now = datetime.utcnow()
    due = []
    for user_id, persona, freq, last_seen, last_checkin_at, facts in rows:
        hours = CHECKIN_INTERVALS_HOURS.get(freq)
        if hours is None:
            continue
        # Человек ещё не молчит достаточно долго.
        if _hours_since(now, last_seen) < hours:
            continue
        # Уже писали первыми, а человек с тех пор не возвращался - больше не трогаем.
        # Тексты дат в формате 'ГГГГ-ММ-ДД ЧЧ:ММ:СС' можно сравнивать как строки.
        if last_checkin_at is not None and last_checkin_at >= last_seen:
            continue
        due.append((user_id, persona, facts))
    return due
