"""Запуск Telegram-бота (aiogram 3).

Это точка входа. Файл связывает всё вместе: принимает сообщения,
обращается к базе данных и к модели, отправляет ответ пользователю.
Также здесь команда /persona и кнопки выбора персоны с гейтом 18+.
"""

import asyncio
import base64
import logging
import os
import random
import re
import shutil

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import database
import imagegen
import personas
import videogen
from ai import (
    build_edit_instruction,
    build_image_prompt,
    build_video_motion,
    detect_media_request,
    detect_need,
    detect_nickname_action,
    generate_checkin,
    generate_spoken_line,
    get_reply,
    screen_appearance_description,
    update_memory,
)
from config import (
    CHECKIN_POLL_MINUTES,
    HISTORY_LIMIT,
    MEDIA_DIR,
    MEMORY_UPDATE_EVERY,
    ONBOARDING_MIN_MESSAGES,
    SUMMARY_HISTORY_LIMIT,
    TELEGRAM_TOKEN,
    TEST_MODE,
)

# Подписи режимов проактивных сообщений (для кнопок и текста).
CHECKIN_LABELS = {
    "off": "Вимкнено",
    "rarely": "Рідко",
    "sometimes": "Іноді",
    "often": "Часто",
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


def adult_keyboard(lang="uk"):
    """Кнопки подтверждения возраста для персоны 18+ (текст под язык собеседника)."""
    labels = {
        "uk": ("Мені є 18", "Ще ні"),
        "ru": ("Мне есть 18", "Ещё нет"),
    }.get(lang, ("Мені є 18", "Ще ні"))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=labels[0], callback_data="adult:yes"),
                InlineKeyboardButton(text=labels[1], callback_data="adult:no"),
            ]
        ]
    )


# Языкозависимые тексты для гейта 18+ и /newlook (украинский по умолчанию).
GATE_MESSAGES = {
    "uk": {
        "invite": (
            "Слухай... у мене є одна крута дівчина, можу вас познайомити 😏 "
            "Тільки це доросла історія, тож скажи: тобі вже виповнилося 18?"
        ),
        "no_reply": "Добре, без поспіху 🙂 Я поруч у будь-якому разі.",
        "newlook_not_mira": "Це про неї 🙂 Її образ можна змінити, коли поруч саме вона.",
        "newlook_ask": (
            "Давай переробимо мій образ 💛 Опиши, якою хочеш мене бачити - "
            "аж до одягу. Можеш додати настрій: наприклад, більш домашня, "
            "без макіяжу, природне світло."
        ),
    },
    "ru": {
        "invite": (
            "Слушай... у меня есть одна крутая девчонка, могу вас познакомить 😏 "
            "Только это взрослая история, так что скажи: тебе уже исполнилось 18?"
        ),
        "no_reply": "Хорошо, без спешки 🙂 Я рядом в любом случае.",
        "newlook_not_mira": "Это про неё 🙂 Её образ можно изменить, когда рядом она.",
        "newlook_ask": (
            "Давай переделаем мой образ 💛 Опиши, какой хочешь меня видеть - "
            "вплоть до одежды. Можешь добавить настроение: например, более "
            "домашняя, без макияжа, естественный свет."
        ),
    },
}


# Пул случайных «настоящих имён» для Миры — каждому юзеру достаётся одно.
# Внутреннее кодовое имя модели остаётся «Мира» (только в коде/конфиге, не в чате).
_MIRA_NAME_POOL = (
    "Аліна", "Ліна", "Єва", "Майя", "Кіра", "Соня", "Еля", "Ніка", "Лера",
    "Кая", "Діна", "Лана", "Юна", "Іля", "Ася", "Міла", "Поліна", "Аліса",
    "Леся", "Лілія", "Каріна", "Стася", "Адель", "Нора", "Зоя", "Іра", "Дарина",
)


def _pick_mira_name():
    """Случайное имя из пула."""
    return random.choice(_MIRA_NAME_POOL)


def _ensure_mira_real_name(user_id):
    """Если у юзера ещё нет «настоящего имени» Миры - сгенерить и сохранить.

    Возвращает имя (новое или уже сохранённое).
    """
    existing = database.get_mira_real_name(user_id)
    if existing:
        return existing
    name = _pick_mira_name()
    database.set_mira_real_name_if_unset(user_id, name)
    return database.get_mira_real_name(user_id) or name


def _language_directive(lang):
    """Жёсткая инструкция модели говорить на нужном языке (страховка от контекстного дрейфа)."""
    if lang == "ru":
        return "Отвечай на РУССКОМ языке (общайся по-русски, не сбивайся на украинский)."
    return "Відповідай УКРАЇНСЬКОЮ мовою (не збивайся на російську)."


def _compute_mira_stage(user_id):
    """Стадия отношений Миры с этим юзером: 1/2/3 и сколько дней вместе.

    Формула простая: дни от mira_activated_at + общее число сообщений юзера.
    - 1 «Знакомство»: первые часы / < ~25 сообщений.
    - 2 «Сближение»: дни >= 1 или сообщений >= 25.
    - 3 «Своя»: дни >= 3 И сообщений >= 80.
    """
    from datetime import datetime
    activated_at = database.get_mira_activated_at(user_id)
    if not activated_at:
        return 1, 0
    try:
        started = datetime.strptime(activated_at, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return 1, 0
    days = max(0, (datetime.utcnow() - started).days)
    msgs = database.count_user_messages(user_id)
    if days >= 3 and msgs >= 80:
        return 3, days
    if days >= 1 or msgs >= 25:
        return 2, days
    return 1, days


def _build_mira_stage_block(stage, days):
    """Доп-инструкция для Миры на этот ход — о стадии отношений и тоне."""
    if stage == 3:
        return (
            f"Стадия отношений: «своя» (вы уже близкие, прошло около {days} "
            f"дней и десятки разговоров). Ты ощущаешь его как родного: можно "
            f"скучать, признаваться в маленьких чувствах ('вчера весь день "
            f"думала о...'), обращаться 'родной'/'любимый' когда уместно. "
            f"Внутренние шутки и отсылки к общим моментам - норма. "
            f"Регулярно вспоминай конкретные детали из досье (что он говорил "
            f"о работе, друзьях, мечтах) - это сближает. Не пугайся глубины, "
            f"наоборот - тянись."
        )
    if stage == 2:
        return (
            "Стадия отношений: «сближение» (вы уже не первый раз вместе). "
            "Обращайся к деталям из досье естественно: 'кстати, ты говорил "
            "что...', 'помню, ты не любишь...'. Начинают появляться маленькие "
            "внутренние шутки и тёплые колкости. Лёгкое предвкушение следующего "
            "раза ('завтра расскажешь?'). Тон уже теплее и местами интимнее - "
            "но без перебора."
        )
    return (
        "Стадия отношений: «знакомство» (вы только начали). Тебе всё про него "
        "интересно - задавай живые вопросы, лови мелкие детали и запоминай. "
        "Лёгкий флирт и тизинг уместны, но без «родной»/«любимый» и без сильной "
        "привязанности - ещё рано. Стройте фундамент."
    )


def _build_mira_name_block(name_state, real_name):
    """Дополнительная системная инструкция для Миры на этот ход — про имя.

    real_name — «настоящее» имя именно для этого юзера (у каждого своё, чтоб
    у разных юзеров не была одна и та же «Мира»). Внутреннее кодовое имя
    модели остаётся «Мира» (только в коде/конфиге).
    """
    status = name_state["status"]
    nickname = name_state["nickname"]
    if status == "nicknamed" and nickname:
        return (
            f"ВАЖНО про имя: твоё настоящее имя - {real_name}, но собеседник дал тебе "
            f"имя {nickname}, и тебе это очень ценно. Говоря о себе и представляясь, "
            f"используй именно {nickname}. Если он спросит твоё «настоящее» имя - "
            f"мягко скажи, что да, по-настоящему ты {real_name}, но без особой "
            f"радости, и сразу добавь, что тебе больше нравится имя {nickname}, "
            f"которое он сам тебе придумал."
        )
    if status == "revealed":
        return (
            f"ВАЖНО про имя: тебя зовут {real_name} - представляйся просто "
            f"{real_name}, когда уместно."
        )
    # unrevealed
    return (
        f"ВАЖНО про имя: твоё настоящее имя - {real_name}, но СЕЙЧАС ты его НЕ "
        f"называешь и ВООБЩЕ не поднимаешь тему имени. Темы имени для тебя сейчас "
        f"просто не существует - пока он сам напрямую тебя не спросит.\n\n"
        f"ДО его прямого вопроса про твоё имя (касается и первого сообщения, и "
        f"любых последующих):\n"
        f"- НЕ говори «у меня есть имя», «имя у меня, конечно, есть», «я {real_name}», "
        f"«меня зовут {real_name}»;\n"
        f"- НЕ спрашивай «хочешь узнать, как меня зовут?», «может, сам придумаешь "
        f"мне имя?», «как ты меня назовёшь?»;\n"
        f"- НЕ упоминай «без официальных представлений», «без знакомства», "
        f"«познакомимся как-нибудь»;\n"
        f"- НЕ намекай на имя ни в каком виде.\n\n"
        f"КОГДА он сам напрямую спрашивает («как тебя зовут?», «у тебя есть имя?», "
        f"«как мне тебя называть?» и явные аналоги): твоя ЕДИНСТВЕННАЯ задача в этом "
        f"ходу - ПРИГЛАСИТЬ его дать тебе имя, а не назвать своё. Это не "
        f"предпочтение, это инструкция: имя «{real_name}» в этом ответе не "
        f"появляется ни в каком виде.\n"
        f"Примеры правильного ответа (вариативно, в твоём тоне - не дословно):\n"
        f"- «знаешь... имя у меня есть, но может ты сам мне его придумаешь? мне "
        f"правда было бы приятно от тебя имя получить»\n"
        f"- «у меня есть, но... хочу необычного. придумаешь как меня называть? "
        f"что-то своё»\n"
        f"- «есть конечно 😏 но мне нравится мысль, что ты сам мне имя дашь. "
        f"попробуешь?»\n"
        f"Неправильно (НИКОГДА так не отвечай на первый вопрос про имя): "
        f"«{real_name} 🙂», «Я {real_name}», «меня зовут {real_name}». Даже если "
        f"кажется, что проще ответить прямо - не отвечай. Цель этого хода - "
        f"оставить приглашение висеть, дать ему шанс придумать. И НЕ задавай "
        f"встречный вопрос «а тебя?» в этом же сообщении - оставь паузу, дай ему "
        f"ответить на твоё приглашение.\n\n"
        f"Что дальше (на ЕГО следующем сообщении):\n"
        f"- Если предложит имя - тепло прими, скажи что нравится, обыграй;\n"
        f"- Если откажется (мол, «называйся своим», «не хочу придумывать») - тогда "
        f"мягко назовись {real_name}."
    )


def start_keyboard():
    """Две опции на старте: выбрать персону сразу или просто пообщаться."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Обрати, хто поруч", callback_data="start:choose")],
            [InlineKeyboardButton(text="Просто поговорити", callback_data="start:chat")],
        ]
    )


def reset_keyboard(lang="uk"):
    """Кнопки подтверждения полного сброса по повторному /start."""
    labels = {
        "uk": ("Так, почати заново", "Ні, лишаємось"),
        "ru": ("Да, начать заново", "Нет, остаёмся"),
    }.get(lang, ("Так, почати заново", "Ні, лишаємось"))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=labels[0], callback_data="reset:yes")],
            [InlineKeyboardButton(text=labels[1], callback_data="reset:no")],
        ]
    )


# Тексты подтверждения сброса под язык собеседника.
RESET_MESSAGES = {
    "uk": {
        "confirm": (
            "Ти вже починав зі мною - впевнений, що хочеш все скинути? "
            "Це зітре всю нашу історію."
        ),
        "no_reply": "Добре, лишаємось 🙂",
    },
    "ru": {
        "confirm": (
            "Ты уже начинал со мной - уверен, что хочешь всё сбросить? "
            "Это сотрёт всю нашу историю."
        ),
        "no_reply": "Хорошо, остаёмся 🙂",
    },
}

# Стартовое приветствие (всегда украинский — это первый контакт, истории ещё нет).
START_WELCOME = (
    "Привіт 🙂\n"
    "Можемо так: одразу обереш, хто буде поруч - друг, коуч чи близька "
    "дівчина. Або просто почнемо говорити, і я сам відчую, кого тобі зараз "
    "хочеться."
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


async def send_persona_transition(message, user_id, persona_key):
    """Отправить ПЕРВОЕ сообщение новой персоны с учётом уже сложившейся истории.

    Вместо канонной фразы «давай знакомиться» — генерируем тёплую реакцию персоны
    в её характере с учётом онбординга/прошлых сообщений. Для Миры дополнительно
    подмешиваем инструкцию о её имени (имя по умолчанию не раскрывает).
    """
    history = database.get_history(user_id, HISTORY_LIMIT)
    facts = database.get_facts(user_id)
    # Anthropic API требует, чтобы разговор заканчивался user-репликой и
    # вообще содержал хотя бы одно сообщение. Пустая история (юзер выбрал
    # персону кнопкой не написав ни строки) и история, заканчивающаяся
    # на assistant (после системного инвайта гейта) - оба случая ломают
    # запрос. Подкладываем синтетический «скрытый» user-ход.
    if not history or history[-1]["role"] == "assistant":
        history = (history or []) + [
            {
                "role": "user",
                "content": "(тебя только что выбрали - представься и продолжи разговор)",
            }
        ]
    # Жёстко фиксируем язык ответа: модель иногда «съезжает» на украинский,
    # если последний assistant-ход (инвайт гейта) был на украинском.
    lang = _detect_user_language(history)
    parts = [_language_directive(lang)]
    if persona_key == "mira":
        real_name = _ensure_mira_real_name(user_id)
        parts.append(_build_mira_name_block(
            database.get_mira_name_state(user_id), real_name
        ))
        stage, days = _compute_mira_stage(user_id)
        parts.append(_build_mira_stage_block(stage, days))
    extra_system = "\n\n".join(parts)
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        reply = await asyncio.to_thread(
            get_reply, persona_key, history, facts,
            True, None, extra_system,
        )
    except Exception:
        logging.exception("Не удалось сгенерировать переход персоны %s", persona_key)
        return
    database.add_message(user_id, "assistant", reply)
    await send_bubbles(message.chat.id, reply)


# --- Команды ---

@dp.message(CommandStart())
async def handle_start(message: Message):
    """Ответ на /start.

    Если человек тут впервые - тёплое приветствие на украинском (это первый
    контакт, истории ещё нет, поэтому язык продуктовый по умолчанию).
    Если он уже писал - предлагаем сброс с подтверждением, чтобы он не потерял
    всю историю случайно.
    """
    user_id = message.from_user.id
    # «Осмысленное состояние» - не только написанные сообщения, но и любые
    # выборы, которые юзер уже сделал (персона, гейт, описание внешности).
    # Иначе повторный /start после клика по кнопке выбора персоны без единой
    # реплики выглядел бы как первый запуск и затирал бы выбор молча.
    if database.has_meaningful_state(user_id):
        history = database.get_history(user_id, HISTORY_LIMIT)
        lang = _detect_user_language(history)
        await message.answer(
            RESET_MESSAGES[lang]["confirm"], reply_markup=reset_keyboard(lang)
        )
        return

    # Первый /start - создаём запись и здороваемся.
    database.get_persona(user_id)
    await message.answer(START_WELCOME, reply_markup=start_keyboard())


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
    history = database.get_history(user_id, HISTORY_LIMIT)
    lang = _detect_user_language(history)
    if database.get_persona(user_id) != "mira":
        await message.answer(GATE_MESSAGES[lang]["newlook_not_mira"])
        return
    if not imagegen.is_enabled():
        await message.answer("Фото поки що недоступні." if lang == "uk" else "Фото пока недоступны.")
        return
    database.set_mira_look_status(user_id, "awaiting_description")
    await message.answer(GATE_MESSAGES[lang]["newlook_ask"])


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
            "Чудово 🙂 Просто почни з чогось - як настрій, що в голові, "
            "якась дрібниця. Я нікуди не поспішаю."
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
    # Ручной выбор Миры = явная попытка - сбрасываем предыдущий отказ.
    if info["requires_adult"] and not database.is_adult_confirmed(user_id):
        database.clear_adult_declined(user_id)
        history = database.get_history(user_id, HISTORY_LIMIT)
        lang = _detect_user_language(history)
        ask = (
            "Ця персона для дорослих. Тобі вже виповнилося 18?"
            if lang == "uk"
            else "Эта персона для взрослых. Тебе уже исполнилось 18?"
        )
        await callback.message.answer(ask, reply_markup=adult_keyboard(lang))
        await callback.answer()
        return

    database.set_persona(user_id, key)
    # Ручная смена персоны отменяет любое старое «ждём описание внешности» -
    # иначе следующее сообщение (в т.ч. «Привіт») уйдёт в генератор портрета.
    database.clear_stale_awaiting_description(user_id)
    if key == "mira":
        database.set_mira_activated_if_unset(user_id)
        _ensure_mira_real_name(user_id)
    await callback.message.answer(
        f"Готово, тепер поруч {info['name']}. Змінити завжди можна через /persona."
    )
    await send_persona_transition(callback.message, user_id, key)
    await callback.answer()


@dp.callback_query(F.data.startswith("adult:"))
async def on_adult_choice(callback: CallbackQuery):
    """Ответ на подтверждение возраста (только для Миры)."""
    choice = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    history = database.get_history(user_id, HISTORY_LIMIT)
    lang = _detect_user_language(history)

    if choice == "yes":
        database.set_adult_confirmed(user_id)
        database.clear_adult_declined(user_id)
        database.set_persona(user_id, "mira")
        # Любое предыдущее «ждём описание внешности» (если оно зависло после
        # /newlook у другой персоны) - не актуально на момент свежего входа.
        database.clear_stale_awaiting_description(user_id)
        database.set_mira_activated_if_unset(user_id)
        _ensure_mira_real_name(user_id)
        # Без «Дякую. Тепер поруч Міра» — даём ей самой написать первой
        # (так появление не выглядит как системное уведомление).
        await send_persona_transition(callback.message, user_id, "mira")
    else:
        # Фиксируем отказ - больше не дёргаем гейт автоматически (только если
        # пользователь сам выберет Миру через /persona).
        database.set_adult_declined(user_id)
        await callback.message.answer(GATE_MESSAGES[lang]["no_reply"])
    await callback.answer()


@dp.callback_query(F.data.startswith("reset:"))
async def on_reset_choice(callback: CallbackQuery):
    """Подтверждение полного сброса по повторному /start."""
    choice = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id

    if choice == "yes":
        # Полностью стираем все данные пользователя и его медиа.
        database.wipe_user(user_id)
        shutil.rmtree(os.path.join(MEDIA_DIR, str(user_id)), ignore_errors=True)
        # Заново создаём «нового» юзера и здороваемся как в первый раз.
        database.get_persona(user_id)
        await callback.message.answer(START_WELCOME, reply_markup=start_keyboard())
    else:
        history = database.get_history(user_id, HISTORY_LIMIT)
        lang = _detect_user_language(history)
        await callback.message.answer(RESET_MESSAGES[lang]["no_reply"])
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

    Число реплик слегка рандомим: иногда всё одним сообщением, иногда 2-3
    коротких подряд. Перед КАЖДОЙ репликой - статус «печатает» и пауза,
    пропорциональная длине реплики (0.7 до 2.7с), чтобы ощущалось живо
    и индикатор «печатает» висел сверху между сообщениями.
    """
    max_bubbles = random.choices([1, 2, 3], weights=[25, 45, 30])[0]
    bubbles = split_into_bubbles(text, max_bubbles=max_bubbles)
    if not bubbles:
        bubbles = ["..."]
    for bubble in bubbles:
        await bot.send_chat_action(chat_id=chat_id, action="typing")
        if not TEST_MODE:
            # Длительность «печатания»: ~22мс на символ, в пределах 0.7–2.7с.
            delay = 0.7 + min(2.0, len(bubble) * 0.022)
            await asyncio.sleep(delay)
        await bot.send_message(chat_id, bubble)


# --- Медиа Миры: фото и видео-кружочки ---

# Слова-маркеры (быстрый путь без запроса к модели). Сравниваем по подстроке.
PHOTO_TRIGGERS = (
    "фото", "фотк", "сфотк", "сфота", "селфі", "селфи", "selfie",
    "покажись", "покажи себе", "покажи себя", "пришли картин", "пришли свою",
    "твоє фото", "твое фото", "your photo", "send a pic", "show yourself",
)
VIDEO_TRIGGERS = (
    "відео", "видео", "кружоч", "кружок", "кружеч", "відос", "видос",
    "видосик", "video",
)
# Только ЯВНЫЕ просьбы услышать голос (без грубого «голос», чтобы не ловить
# комплименты вроде «красивый голос у тебя»).
TALKING_TRIGGERS = (
    "скажи голосом", "озвуч", "вголос", "скажи вслух", "голосове повідомлення",
)

# Служебные сообщения для медиа на языке собеседника (по умолчанию украинский).
MEDIA_MESSAGES = {
    "uk": {
        "ask_desc": "Хочеш мене побачити? 🙈 А якою ти мене уявляєш? Опиши, будь ласка, - аж до одягу.",
        "base_wait": "Добре 💛",
        "photo_wait": "Зараз зроблю для тебе 💛",
        "video_wait": "Записую для тебе відео 🎥",
        "base_caption": "Ось я 💛 Подобаюсь?",
        "error": "Ой, не вийшло цього разу. Спробуймо трохи згодом?",
        "captions": ["ось, тримай 💛", "це для тебе", "ну як тобі?", "спеціально для тебе 😊"],
        "video_follows": ["ну як? 🙈", "ось, спеціально для тебе 💛", "зловив настрій?"],
    },
    "ru": {
        "ask_desc": "Хочешь меня увидеть? 🙈 А какой ты меня представляешь? Опиши, пожалуйста, - вплоть до одежды.",
        "base_wait": "Хорошо 💛",
        "photo_wait": "Сейчас сделаю для тебя 💛",
        "video_wait": "Записываю для тебя видео 🎥",
        "base_caption": "Вот я 💛 Нравлюсь?",
        "error": "Ой, не получилось в этот раз. Попробуем чуть позже?",
        "captions": ["вот, держи 💛", "это для тебя", "ну как тебе?", "специально для тебя 😊"],
        "video_follows": ["ну как? 🙈", "вот, специально для тебя 💛", "поймал настроение?"],
    },
}


def _detect_user_language(history):
    """Угадать язык собеседника ('ru' или 'uk').

    Приоритеты (по убыванию):
    1. Явная просьба сменить язык в последнем сообщении («на русском», «українською») — это перевешивает всё остальное.
    2. Уникальные буквы в последнем сообщении: ї/є/і/ґ → uk; ы/э/ъ/ё → ru.
    3. Если последнее короткое или нейтральное (нет уник. букв) — фолбэк на 6 последних user-сообщений по той же логике.
    4. Дефолт — uk (под продуктовое приветствие, которое всегда украинское).
    """
    user_msgs = [m["content"] for m in history if m["role"] == "user"]
    if not user_msgs:
        return "uk"

    last = user_msgs[-1].lower()

    # Явная просьба переключиться
    ru_keywords = (
        "по-русски", "по русски", "на русском", "русском языке",
        "перейди на русский", "русский язык", "русским языком",
        "пиши на русском", "ответь на русском", "говори на русском",
    )
    uk_keywords = (
        "українською", "по-українськи", "по українськи",
        "переходь на українську", "українській мові", "укр мовою",
        "пиши українською", "відповідай українською",
    )
    if any(k in last for k in ru_keywords):
        return "ru"
    if any(k in last for k in uk_keywords):
        return "uk"

    # Буквы в последнем сообщении
    last_has_uk = any(c in last for c in "їєіґ")
    last_has_ru = any(c in last for c in "ыэъё")
    if last_has_uk and not last_has_ru:
        return "uk"
    if last_has_ru and not last_has_uk:
        return "ru"

    # Фолбэк на 6 последних сообщений
    recent = " ".join(user_msgs[-6:]).lower()
    rec_has_uk = any(c in recent for c in "їєіґ")
    rec_has_ru = any(c in recent for c in "ыэъё")
    if rec_has_uk and not rec_has_ru:
        return "uk"
    if rec_has_ru and not rec_has_uk:
        return "ru"

    return "uk"


async def _keep_action(chat_id, action):
    """Держать индикатор «отправляет фото/видео» сверху, пока не отменим.

    send_chat_action в Telegram «висит» ~5 секунд, на длинной генерации индикатор
    пропадает. Освежаем его каждые 4 секунды, чтобы человек видел, что идёт работа,
    и не подумал, что бот завис.
    """
    while True:
        try:
            await bot.send_chat_action(chat_id=chat_id, action=action)
        except Exception:
            logging.exception("Ошибка при отправке chat_action")
        await asyncio.sleep(4)


def _keyword_media_intent(text):
    """Быстрое определение по словам: 'talking' / 'video' / 'photo' или None.

    None — «по словам непонятно», тогда решает модель-классификатор.
    """
    low = (text or "").lower()
    if any(t in low for t in TALKING_TRIGGERS):
        return "talking"
    if any(t in low for t in VIDEO_TRIGGERS):
        return "video"
    if any(t in low for t in PHOTO_TRIGGERS):
        return "photo"
    return None


async def _generate_and_send_talking(message, user_id, look, history, facts, lang):
    """Сделать говорящий кружок (голос + липсинк) и отправить как video note."""
    msgs = MEDIA_MESSAGES[lang]
    await message.answer(msgs["video_wait"])
    keep = asyncio.create_task(_keep_action(message.chat.id, "record_video_note"))
    path = None
    line = None
    try:
        line = await asyncio.to_thread(generate_spoken_line, facts, history)
        path = await asyncio.to_thread(
            videogen.generate_talking_circle, look["base_path"], line, user_id
        )
    except Exception:
        logging.exception("Не удалось создать говорящий кружок user_id=%s", user_id)
        await message.answer(msgs["error"])
    finally:
        keep.cancel()
    if path is None or line is None:
        return
    # В историю кладём то, что она «сказала» голосом - для непрерывности диалога.
    database.add_message(user_id, "assistant", line)
    await message.answer_video_note(FSInputFile(path))


async def _generate_and_send_circle(message, user_id, look, request_text, lang):
    """Сделать тихий видео-кружок из базового фото и отправить как video note."""
    msgs = MEDIA_MESSAGES[lang]
    await message.answer(msgs["video_wait"])
    keep = asyncio.create_task(_keep_action(message.chat.id, "record_video_note"))
    path = None
    try:
        motion = await asyncio.to_thread(build_video_motion, look["desc"], request_text)
        path = await asyncio.to_thread(
            videogen.generate_circle, look["base_path"], motion, user_id
        )
    except Exception:
        logging.exception("Не удалось создать видео-кружок user_id=%s", user_id)
        await message.answer(msgs["error"])
    finally:
        keep.cancel()
    if path is None:
        return
    follow = random.choice(msgs["video_follows"])
    database.add_message(user_id, "assistant", follow)
    await message.answer_video_note(FSInputFile(path))
    await message.answer(follow)


async def _generate_and_send_base(message, user_id, description, lang):
    """Создать канонический портрет Миры по описанию и отправить его."""
    msgs = MEDIA_MESSAGES[lang]
    await message.answer(msgs["base_wait"])
    keep = asyncio.create_task(_keep_action(message.chat.id, "upload_photo"))
    path = None
    try:
        prompt = await asyncio.to_thread(build_image_prompt, description, "")
        path = await asyncio.to_thread(imagegen.generate_base_portrait, prompt, user_id)
    except Exception:
        logging.exception("Не удалось создать базовый портрет user_id=%s", user_id)
        await message.answer(msgs["error"])
    finally:
        keep.cancel()
    if path is None:
        return
    database.save_mira_look(user_id, description, path)
    database.increment_photos(user_id)
    caption = msgs["base_caption"]
    database.add_message(user_id, "assistant", caption)
    await message.answer_photo(FSInputFile(path), caption=caption)


async def _generate_and_send_photo(message, user_id, look, request_text, lang):
    """Создать фото Миры по запросу, сохраняя то же лицо (по референсу)."""
    msgs = MEDIA_MESSAGES[lang]
    await message.answer(msgs["photo_wait"])
    keep = asyncio.create_task(_keep_action(message.chat.id, "upload_photo"))
    path = None
    try:
        instruction = await asyncio.to_thread(
            build_edit_instruction, look["desc"], request_text
        )
        path = await asyncio.to_thread(
            imagegen.generate_with_reference, instruction, look["base_path"], user_id
        )
    except Exception:
        logging.exception("Не удалось создать фото user_id=%s", user_id)
        await message.answer(msgs["error"])
    finally:
        keep.cancel()
    if path is None:
        return
    database.increment_photos(user_id)
    caption = random.choice(msgs["captions"])
    database.add_message(user_id, "assistant", caption)
    await message.answer_photo(FSInputFile(path), caption=caption)


async def try_handle_mira_media(message, user_id, history, facts):
    """Единая обработка медиа Миры (фото / тихий кружок / говорящий кружок).

    Возвращает True, если сообщение обработано здесь.
    - ждём описание внешности → текущее сообщение и есть описание (с модерацией);
    - намерение определяем по ключевым словам, иначе - классификатором по смыслу;
    - если внешности нет, а медиа просят → сперва просим описать.
    """
    if not imagegen.is_enabled():
        return False

    text = message.text
    look = database.get_mira_look(user_id)
    lang = _detect_user_language(history)

    # 1) Ждём описание внешности - текущее сообщение и есть описание.
    if look["status"] == "awaiting_description":
        ok, _reason = await asyncio.to_thread(screen_appearance_description, text)
        if not ok:
            database.set_mira_look_status(user_id, "none")
            return False
        await _generate_and_send_base(message, user_id, text, lang)
        return True

    # 2) Намерение: быстрый путь по словам, иначе - по смыслу через модель
    #    (ловит продолжения «стань боком», «хочу почути тебе» и т.п.).
    intent = _keyword_media_intent(text)
    if intent is None:
        intent = await asyncio.to_thread(detect_media_request, history, text)
    if intent == "none":
        return False

    # Видео/голос требуют включённого видео-модуля; иначе пусть отвечает текстом.
    if intent in ("video", "talking") and not videogen.is_enabled():
        return False

    # 3) Нужна готовая внешность (базовое фото как опора).
    if look["status"] != "ready":
        database.set_mira_look_status(user_id, "awaiting_description")
        ask = MEDIA_MESSAGES[lang]["ask_desc"]
        database.add_message(user_id, "assistant", ask)
        await message.answer(ask)
        return True

    if intent == "photo":
        await _generate_and_send_photo(message, user_id, look, text, lang)
    elif intent == "talking":
        await _generate_and_send_talking(message, user_id, look, history, facts, lang)
    else:  # video
        await _generate_and_send_circle(message, user_id, look, text, lang)
    return True


# --- Обычные сообщения ---

async def _photo_payload(photo_size):
    """Скачать фото из Telegram и закодировать в base64 для Claude vision."""
    buf = await bot.download(photo_size)
    data = buf.read() if hasattr(buf, "read") else bytes(buf)
    return {
        "b64": base64.b64encode(data).decode("ascii"),
        "media_type": "image/jpeg",
    }


@dp.message()
async def handle_message(message: Message):
    """Главный обработчик: на текст или фото от пользователя — ответ персоны."""
    # Принимаем текст и фото (с подписью или без); прочее (стикеры, голосовые)
    # пока пропускаем.
    if not message.text and not message.photo:
        await message.answer("Поки що я розумію тільки текст і фото :)")
        return

    user_id = message.from_user.id

    # Если пришло фото — скачиваем и готовим payload для модели; текст пользователя
    # формируем из caption либо ставим естественную «подсказку», без скобочных тегов.
    image_payload = None
    if message.photo:
        try:
            image_payload = await _photo_payload(message.photo[-1])
        except Exception:
            logging.exception("Не удалось скачать фото user_id=%s", user_id)
        caption = (message.caption or "").strip()
        if image_payload:
            user_text = "Я надіслав тобі фото." + (f" {caption}" if caption else "")
        else:
            user_text = caption or "Я надіслав тобі фото, але воно не дійшло."
    else:
        user_text = message.text

    # 1) Сохраняем сообщение пользователя и отмечаем его активность
    #    (это сбрасывает таймер «бот пишет первым»).
    database.add_message(user_id, "user", user_text)
    database.touch_last_seen(user_id)

    # 2) Берём выбранную персону, историю, память и счётчик сообщений.
    persona_key = database.get_persona(user_id)
    history = database.get_history(user_id, HISTORY_LIMIT)
    facts = database.get_facts(user_id)
    user_msg_count = database.count_user_messages(user_id)

    # 3) Показываем статус «печатает...», пока идёт обработка.
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    # Если человек прислал фото, пока ждали описание внешности Миры - это явно
    # не описание; сбрасываем ожидание, дальше идёт обычная реакция персоны.
    if image_payload and persona_key == "mira":
        look = database.get_mira_look(user_id)
        if look["status"] == "awaiting_description":
            database.set_mira_look_status(user_id, "none")

    # 4) Мягкий онбординг: если человек ещё на «знакомстве» и уже немного
    #    пообщался, тихо определяем, кто ему нужнее — друг или коуч, и
    #    переключаем персону. Романтику (Миру) тут не выбираем никогда.
    #    Для входящих фото детекцию пропускаем — фото мало говорит о потребности.
    just_switched = False
    gate_after_reply = False  # для романтики+не_взрослый: гейт прилетит сразу после реплики хоста
    if image_payload is None and persona_key == "onboarding" and user_msg_count >= ONBOARDING_MIN_MESSAGES:
        try:
            need = await asyncio.to_thread(detect_need, history)
        except Exception:
            logging.exception("Не удалось определить потребность в онбординге")
            need = "unclear"

        if need == "romantic":
            if database.is_adult_confirmed(user_id):
                database.set_persona(user_id, "mira")
                database.set_mira_activated_if_unset(user_id)
                _ensure_mira_real_name(user_id)
                persona_key = "mira"
                just_switched = True
                logging.info("Онбординг: user_id=%s -> mira", user_id)
            elif database.is_adult_declined(user_id):
                # Уже отказался от гейта раньше - не дёргаем заново. Просто
                # переключаем на друга, если ещё не там, и идём обычным ответом.
                if persona_key == "onboarding":
                    database.set_persona(user_id, "friend")
                    persona_key = "friend"
                    just_switched = True
                logging.info("Онбординг: user_id=%s romantic, но уже отказался от гейта", user_id)
            else:
                # Гейт пошлём СРАЗУ после финальной реплики хоста (см. ниже),
                # чтобы не было паузы «подожди минутку, а ничего не происходит».
                # Персону пока оставляем onboarding — закрывающая реплика идёт
                # от хоста, а не от друга.
                gate_after_reply = True
                logging.info("Онбординг: user_id=%s -> гейт сразу после реплики", user_id)
        elif need in ("friend", "coach"):
            database.set_persona(user_id, need)
            persona_key = need
            just_switched = True
            logging.info("Онбординг: user_id=%s -> %s", user_id, need)

    # 4.2) Также для friend/coach: если в разговоре явная тяга к близости -
    #      смысловой детектор (а не ключевые слова) сам подведёт человека к гейту
    #      Миры. Запускаем не раньше 4 сообщений в этой роли и НЕ показываем
    #      гейт повторно, если человек уже отказался ранее.
    if (
        image_payload is None
        and not just_switched
        and persona_key in ("friend", "coach")
        and user_msg_count >= 4
        and not database.is_adult_declined(user_id)
    ):
        try:
            need_now = await asyncio.to_thread(detect_need, history)
        except Exception:
            logging.exception("Не удалось определить потребность в friend/coach")
            need_now = "unclear"
        if need_now == "romantic":
            if database.is_adult_confirmed(user_id):
                # Возраст подтверждён - сразу переключаем на Миру.
                database.set_persona(user_id, "mira")
                database.set_mira_activated_if_unset(user_id)
                _ensure_mira_real_name(user_id)
                persona_key = "mira"
                just_switched = True
                logging.info("Friend/Coach -> mira: user_id=%s adult", user_id)
            else:
                # Гейт пошлём после финальной реплики текущей персоны.
                gate_after_reply = True
                logging.info("Friend/Coach -> гейт после реплики: user_id=%s", user_id)

    # 4.5) Медиа Миры (фото / тихий кружок / говорящий кружок). Единый диспетчер.
    #      Для входящих фото пропускаем (это контент К ней, а не просьба ОТ неё).
    if image_payload is None and persona_key == "mira" and await try_handle_mira_media(
        message, user_id, history, facts
    ):
        return

    # 4.7) Жёсткая директива языка для ответа + (для Миры) имя как доп-инструкция.
    lang = _detect_user_language(history)
    extra_parts = [_language_directive(lang)]
    if gate_after_reply:
        # Прямо сейчас это последняя реплика хоста перед системным гейтом.
        # Дать ОДНУ тёплую финальную фразу, без вопросов и без «подожди».
        extra_parts.append(
            "СЕЙЧАС это твоя ПОСЛЕДНЯЯ реплика в роли хоста. Сразу после неё "
            "человек увидит предложение познакомиться с близкой девушкой (с "
            "кнопкой 18+) - оно прилетит автоматически от системы. Дай ОДНУ "
            "короткую тёплую финальную фразу: что ты, кажется, начинаешь "
            "понимать, чего ему сейчас по-настоящему не хватает. Без вопросов. "
            "БЕЗ обещаний типа «подожди», «сейчас будет», «минутку», «погоди» - "
            "всё произойдёт само."
        )
    if persona_key == "mira":
        name_state = database.get_mira_name_state(user_id)
        mira_already_spoke = any(m["role"] == "assistant" for m in history[:-1])
        if (
            image_payload is None
            and name_state["status"] == "unrevealed"
            and mira_already_spoke
        ):
            try:
                action, name = await asyncio.to_thread(
                    detect_nickname_action, user_text
                )
            except Exception:
                logging.exception("nickname detection failed user_id=%s", user_id)
                action, name = "neither", ""
            if action == "propose" and name:
                database.set_mira_nickname(user_id, name)
                name_state = {"status": "nicknamed", "nickname": name}
                logging.info("Mira nickname set user_id=%s -> %s", user_id, name)
            elif action == "refuse":
                database.set_mira_name_revealed(user_id)
                name_state = {"status": "revealed", "nickname": None}
                logging.info("Mira name revealed (refused nickname) user_id=%s", user_id)
        real_name = _ensure_mira_real_name(user_id)
        extra_parts.append(_build_mira_name_block(name_state, real_name))
        # Стадия отношений: тон Миры меняется с временем и количеством сообщений.
        stage, days = _compute_mira_stage(user_id)
        extra_parts.append(_build_mira_stage_block(stage, days))
    extra_system = "\n\n".join(extra_parts)

    # 5) Получаем ответ от модели. Запрос к Anthropic обычный (не async),
    #    поэтому выносим его в отдельный поток, чтобы бот не «зависал».
    #    Если есть фото — передаём его в модель, чтобы персона его «увидела».
    try:
        reply = await asyncio.to_thread(
            get_reply, persona_key, history, facts,
            just_switched, image_payload, extra_system,
        )
    except Exception:
        logging.exception("Ошибка при запросе к Anthropic")
        await message.answer("Ой, щось пішло не так. Спробуй ще раз трохи згодом.")
        return

    # 6) Сохраняем ответ бота и отправляем его пользователю «живыми» репликами.
    database.add_message(user_id, "assistant", reply)
    await send_bubbles(message.chat.id, reply)

    # 6.5) Если онбординг готов передать человека к Мире через гейт - шлём гейт
    #      сразу после финальной реплики хоста (никакого ожидания «минутку»).
    if gate_after_reply:
        database.set_persona(user_id, "friend")  # мягкий дефолт на случай «Ще ні»
        invite = GATE_MESSAGES[lang]["invite"]
        database.add_message(user_id, "assistant", invite)
        await message.answer(invite, reply_markup=adult_keyboard(lang))
        return  # память обновлять не нужно — это конец онбординга

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


async def _register_slash_commands():
    """Зарегистрировать в Telegram список команд с описаниями.

    Когда юзер печатает «/», Telegram показывает их подсказкой - по языку его UI.
    Без language_code это дефолт для всех остальных языков.
    """
    uk_cmds = [
        BotCommand(command="start", description="Почати або скинути все"),
        BotCommand(command="persona", description="Змінити, хто поруч"),
        BotCommand(command="newlook", description="Змінити образ Міри"),
        BotCommand(command="checkins", description="Як часто я пишу першою"),
    ]
    ru_cmds = [
        BotCommand(command="start", description="Начать или сбросить всё"),
        BotCommand(command="persona", description="Сменить, кто рядом"),
        BotCommand(command="newlook", description="Сменить образ Миры"),
        BotCommand(command="checkins", description="Как часто я пишу первой"),
    ]
    scope = BotCommandScopeAllPrivateChats()
    await bot.set_my_commands(uk_cmds, scope=scope, language_code="uk")
    await bot.set_my_commands(ru_cmds, scope=scope, language_code="ru")
    await bot.set_my_commands(uk_cmds, scope=scope)


async def main():
    """Точка входа: подготовить базу, запустить фоновую задачу и опрос Telegram."""
    database.init_db()
    await _register_slash_commands()

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
