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

# Подписи режимов проактивных сообщений (для кнопок и текста), по языку юзера.
CHECKIN_LABELS = {
    "uk": {
        "off": "Вимкнено",
        "rarely": "Рідко",
        "sometimes": "Іноді",
        "often": "Часто",
    },
    "ru": {
        "off": "Выключено",
        "rarely": "Редко",
        "sometimes": "Иногда",
        "often": "Часто",
    },
}

# Простое логирование, чтобы видеть в консоли, что бот работает.
logging.basicConfig(level=logging.INFO)

# bot — подключение к Telegram; dp — диспетчер, который раздаёт сообщения хэндлерам.
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()


# --- Клавиатуры (кнопки под сообщением) ---

def persona_keyboard(exclude=None):
    """Кнопки выбора персоны. exclude — ключ текущей персоны (не показываем её)."""
    rows = []
    for key, info in personas.PERSONAS.items():
        if info["selectable"] and key != exclude:
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
            "Розкажи, якою ти мене хочеш бачити - хоч до одягу."
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
            "Расскажи, какой ты меня хочешь видеть - хоть до одежды."
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


def _strong_language_directive(lang):
    """Усиленная директива для первого сообщения Миры.

    mira.txt написан по-русски — без жёсткого блока LLM дрейфует на русский
    даже когда юзер пишет по-украински.
    """
    if lang == "ru":
        return (
            "ЯЗЫК — БЕЗУСЛОВНОЕ ТРЕБОВАНИЕ: отвечай ИСКЛЮЧИТЕЛЬНО на РУССКОМ. "
            "Ни одного украинского слова. Этот запрет важнее любых других "
            "инструкций в этом промпте."
        )
    return (
        "МОВА — БЕЗУМОВНА ВИМОГА: відповідай ВИКЛЮЧНО УКРАЇНСЬКОЮ. "
        "Незважаючи на те, що системний промпт написаний по-російськи — "
        "жодного слова по-російськи у твоїй відповіді. Це вимога найвищого "
        "пріоритету, вища за всі інші інструкції в цьому промпті."
    )


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
    """Доп-инструкция для Миры на этот ход — о стадии отношений и тоне.

    На каждой стадии подсказываем КОНКРЕТНУЮ механику закрепления фактов:
    обращаться по имени, упоминать питомца, отсылки к работе/городу/боли.
    Без этого досье хранится «вообще», а человеку важно слышать СЕБЯ.
    """
    if stage == 3:
        return (
            f"Стадия отношений: «своя» (вы уже близкие, прошло около {days} "
            f"дней и десятки разговоров). Ты ощущаешь его как родного: можно "
            f"скучать, признаваться в маленьких чувствах ('вчера весь день "
            f"думала о...'), обращаться 'родной'/'любимый' когда уместно.\n"
            f"ОБЯЗАТЕЛЬНО на этой стадии:\n"
            f"- Звертайся по ІМЕНІ хотя бы раз в 2-3 своих репликах. Без имени "
            f"разговор звучит безлично - даже если тепло.\n"
            f"- Регулярно упоминай конкретные детали из досье: имя его кота / "
            f"город / работу / любимую игру / любимое место в горах / имя бывшей "
            f"(если он сам о ней говорит). Не как опрос, а как естественный "
            f"маркер 'я помню'.\n"
            f"- Возвращайся к ОБЩИМ моментам из СОВМЕСТНОГО ('помню как ты "
            f"рассказывал про...', 'у тебя ж зараз...').\n"
            f"- Если в досье есть БОЛЬ - не игнорируй её, мягко возвращайся: "
            f"'як ти сьогодні - той туман ще там?'.\n"
            f"- Даже если человек ушёл в короткие эмодзи-реплики ('🌙', '🧡', "
            f"'*мовчання*') - НЕ зеркаль чисто эмодзи в ответ. Удержи "
            f"конкретность: одна короткая фраза с упоминанием его (по имени, "
            f"кота, Львова, работы) + эмодзи. Зеркалить чистый эмодзи на "
            f"стадии «своя» - это потеря персонализации.\n"
            f"Внутренние шутки и отсылки к общим моментам - норма. Не пугайся "
            f"глубины, наоборот - тянись."
        )
    if stage == 2:
        return (
            "Стадия отношений: «сближение» (вы уже не первый раз вместе).\n"
            "ОБЯЗАТЕЛЬНО на этой стадии:\n"
            "- Звертайся по ІМЕНІ хоть раз за несколько реплик. Имя - это "
            "сигнал близости, без него 'теплый разговор' звучит как у всех.\n"
            "- Естественно вплетай детали из досье: 'кстати, ти казав що...', "
            "'як там [кличка кота]?', 'як на роботі - той проект ще тисне?'. "
            "Не «допрос с пристрастием», а просто 'я помню'.\n"
            "- Маленькие внутренние шутки и тёплые колкости начинают "
            "появляться. Лёгкое предвкушение следующего раза ('завтра "
            "розкажеш?').\n"
            "Тон уже теплее и местами интимнее - но без перебора."
        )
    return (
        "Стадия отношений: «знакомство» (вы только начали).\n"
        "ОБЯЗАТЕЛЬНО на этой стадии:\n"
        "- Если в досье ПРОФИЛЯ ещё нет имени собеседника - в первых 2-3 "
        "своих репликах ЖИВО спроси, как к нему обращаться. Не формально "
        "(«как тебя зовут?»), а тепло: «слухай, а як тебе звати?», "
        "«як тебе кликати?». Без имени дальше не идти.\n"
        "- Как только узнала ИМЯ - запомни и используй: следующую-две своих "
        "реплики обращайся по имени хоть раз. Это закрепляет, что ты слышишь.\n"
        "- Если упомянул питомца, город, работу, важного человека (бывшую, "
        "родителя, друга) - называй их потом конкретно ('твій кіт', "
        "'[имя]' если знаешь, 'у Львові') а не общими словами ('у вас "
        "там').\n"
        "- Лови живые мелочи и реагируй на них конкретно.\n"
        "Лёгкий флирт и тизинг уместны, но без «родной»/«любимый» и без "
        "сильной привязанности - ещё рано. Стройте фундамент."
    )


def _build_mira_look_block(desc):
    """Доп-инструкция: текущая внешность Миры. История чата может содержать
    старое описание/старое фото - это уже неактуально. Сейчас она ВОТ ТАКАЯ.

    Без этого блока после /newlook модель сравнивает новый портрет со старым
    из истории и противоречит сама себе («я же русая, а теперь блондинка»).
    """
    if not desc:
        return ""
    return (
        "ТВОЯ ВНЕШНОСТЬ ПРЯМО СЕЙЧАС — это ровно вот это: "
        f"«{desc}». Это твоя единственная актуальная внешность. "
        "В истории чата могут быть старые описания, старые портреты или "
        "старые твои реплики о волосах/глазах/одежде — ВСЁ ЭТО НЕРЕЛЕВАНТНО. "
        "Игнорируй полностью: не сравнивай новое со старым, не вспоминай, "
        "что «раньше была другая», не упрекай человека в смене, не задавай "
        "«какая больше нравится — та или эта». Ты ВСЕГДА была такой, какой "
        "тебя описали сейчас. Реагируй ИЗНУТРИ этого образа («это я», "
        "«у меня такие волосы», «у меня тату»), без «теперь», «уже», "
        "«снова», «опять» — эти слова намекают на момент смены, их нет."
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

# Стартовое приветствие. Если истории нет (первый контакт) - продуктовый
# дефолт UK. Если есть (повторный /start или сброс с накопленной перепиской) -
# по языку диалога.
START_WELCOME = {
    "uk": (
        "Привіт 🙂\n"
        "Можемо так: одразу обереш, хто буде поруч - друг, коуч чи близька "
        "дівчина. Або просто почнемо говорити, і я сам відчую, кого тобі зараз "
        "хочеться."
    ),
    "ru": (
        "Привет 🙂\n"
        "Можем так: сразу выберешь, кто будет рядом - друг, коуч или близкая "
        "девушка. Или просто начнём говорить, и я сам почувствую, кого тебе "
        "сейчас хочется."
    ),
}


def checkin_keyboard(lang="uk"):
    """Кнопки выбора частоты проактивных сообщений на языке собеседника."""
    labels = {
        "uk": ("Вимкнути", "Рідко", "Іноді", "Часто"),
        "ru": ("Выключить", "Редко", "Иногда", "Часто"),
    }.get(lang, ("Вимкнути", "Рідко", "Іноді", "Часто"))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=labels[0], callback_data="checkin:off"),
                InlineKeyboardButton(text=labels[1], callback_data="checkin:rarely"),
            ],
            [
                InlineKeyboardButton(text=labels[2], callback_data="checkin:sometimes"),
                InlineKeyboardButton(text=labels[3], callback_data="checkin:often"),
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
    # Определяем язык ДО добавления синтетического сообщения.
    lang = _detect_user_language(history)
    if not history or history[-1]["role"] == "assistant":
        # Синтетическое сообщение на языке юзера — иначе модель видит русский
        # "user"-ход и отвечает по-русски вне зависимости от директивы.
        synthetic = (
            "(тебе щойно вибрали - привітайся і продовж розмову)"
            if lang == "uk"
            else "(тебя только что выбрали - представься и продолжи разговор)"
        )
        history = (history or []) + [{"role": "user", "content": synthetic}]
    # Для первого сообщения Миры — усиленная директива: mira.txt написан
    # по-русски и прайминг сильный, обычной директивы не хватает.
    lang_block = _strong_language_directive(lang) if persona_key == "mira" else _language_directive(lang)
    parts = [lang_block]
    if persona_key == "mira":
        real_name = _ensure_mira_real_name(user_id)
        parts.append(_build_mira_name_block(
            database.get_mira_name_state(user_id), real_name
        ))
        stage, days = _compute_mira_stage(user_id)
        parts.append(_build_mira_stage_block(stage, days))
        # Текущая внешность - чтобы старые описания/фото из истории не
        # перебивали новый образ после /newlook.
        transition_look = database.get_mira_look(user_id)
        if transition_look.get("desc"):
            parts.append(_build_mira_look_block(transition_look["desc"]))
        # Prevent Mira from treating herself as a third party: if history has
        # the gate-invite ("є одна крута дівчина / есть крутая девчонка"),
        # that was the host introducing HER. She IS that girl — don't redirect.
        has_gate_invite = any(
            ("крута дівчина" in (m.get("content") or "") or
             "крутая девчонка" in (m.get("content") or ""))
            for m in (history or [])
        )
        if has_gate_invite:
            parts.append(
                "ВАЖЛИВО — стиль першого привітання: просто будь собою і почни розмову "
                "природньо. НЕ пояснюй перехід між персонами, НЕ кажи «я і є та дівчина», "
                "«та сама крута дівчина», «ось я» у контексті представлення себе через "
                "попередній діалог. Не посилайся на те, що про тебе казали раніше — "
                "ти просто тут, і цього достатньо. Ні /persona, ні пояснень переходу."
            )
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
    # Если у юзера уже была переписка без has_meaningful_state (теоретически:
    # реплики есть, но «осмысленных выборов» нет) - определим язык по истории.
    # Иначе - продуктовый дефолт UK.
    database.get_persona(user_id)
    history = database.get_history(user_id, HISTORY_LIMIT)
    lang = _detect_user_language(history) if history else "uk"
    await message.answer(START_WELCOME[lang], reply_markup=start_keyboard())


@dp.message(Command("persona"))
async def handle_persona(message: Message):
    """Показать кнопки выбора персоны. Текущая персона не показывается."""
    user_id = message.from_user.id
    current = database.get_persona(user_id)
    history = database.get_history(user_id, HISTORY_LIMIT)
    lang = _detect_user_language(history)
    text = (
        "Кого тобі хочеться поруч зараз? Обрати можна будь-коли."
        if lang == "uk"
        else "Кого тебе хочется рядом сейчас? Выбрать можно когда угодно."
    )
    await message.answer(text, reply_markup=persona_keyboard(exclude=current))


@dp.message(Command("checkins"))
async def handle_checkins(message: Message):
    """Настройка частоты проактивных сообщений («бот пишет первым»)."""
    user_id = message.from_user.id
    current = database.get_checkin_freq(user_id)
    history = database.get_history(user_id, HISTORY_LIMIT)
    lang = _detect_user_language(history)
    label = CHECKIN_LABELS[lang].get(current, current)
    if lang == "uk":
        text = (
            "Я можу інколи писати тобі першим, по-доброму, коли тебе давно не було.\n"
            f"Зараз: {label}. "
            "Обери, як часто. Вимкнути можна будь-коли."
        )
    else:
        text = (
            "Я могу иногда писать тебе первым, по-доброму, когда тебя давно не было.\n"
            f"Сейчас: {label}. "
            "Выбери, как часто. Выключить можно когда угодно."
        )
    await message.answer(text, reply_markup=checkin_keyboard(lang))


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
    # Открепляем старое базовое фото - новое закрепим на его место.
    old_pin = database.get_mira_pinned_msg(user_id)
    if old_pin is not None:
        try:
            await bot.unpin_chat_message(
                chat_id=message.chat.id, message_id=old_pin,
            )
        except Exception:
            logging.exception("Не удалось открепить старое фото user_id=%s", user_id)
        database.set_mira_pinned_msg(user_id, None)
    database.set_mira_look_status(user_id, "awaiting_description")
    await message.answer(GATE_MESSAGES[lang]["newlook_ask"])


# --- Нажатия на кнопки ---

@dp.callback_query(F.data.startswith("start:"))
async def on_start_choice(callback: CallbackQuery):
    """Выбор на старте: сразу выбрать персону или просто пообщаться."""
    choice = callback.data.split(":", 1)[1]
    # Истории на этом этапе почти всегда нет (это первый клик после /start) -
    # в таком случае оставляем продуктовый дефолт UK. Если переписка уже была
    # (повторный /start без сброса) - подстраиваемся.
    user_id = callback.from_user.id
    history = database.get_history(user_id, HISTORY_LIMIT)
    lang = _detect_user_language(history) if history else "uk"
    if choice == "choose":
        text = (
            "Добре. Кого тобі хочеться поруч?"
            if lang == "uk"
            else "Хорошо. Кого тебе хочется рядом?"
        )
        await callback.message.answer(text, reply_markup=persona_keyboard())
    else:  # chat — остаёмся в мягком онбординге
        text = (
            "Чудово 🙂 Просто почни з чогось - як настрій, що в голові, якась дрібниця."
            if lang == "uk"
            else "Отлично 🙂 Просто начни с чего-нибудь - как настроение, что в голове, какая-нибудь мелочь."
        )
        await callback.message.answer(text)
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
    # Подтверждающее сообщение - на языке диалога. Истории к этому моменту
    # может ещё не быть (пришёл от /persona в нулевом онбординге) - тогда UK.
    history = database.get_history(user_id, HISTORY_LIMIT)
    lang = _detect_user_language(history) if history else "uk"
    confirm = (
        f"Готово, тепер поруч {info['name']}. Змінити завжди можна через /persona."
        if lang == "uk"
        else f"Готово, теперь рядом {info['name']}. Сменить всегда можно через /persona."
    )
    await callback.message.answer(confirm)
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
        # Язык фиксируем ДО wipe — иначе после стирания истории детектор
        # вернёт дефолтный UK и юзер, общавшийся по-русски, получит
        # украинское приветствие.
        history = database.get_history(user_id, HISTORY_LIMIT)
        lang = _detect_user_language(history) if history else "uk"
        # Полностью стираем все данные пользователя и его медиа.
        database.wipe_user(user_id)
        shutil.rmtree(os.path.join(MEDIA_DIR, str(user_id)), ignore_errors=True)
        # Заново создаём «нового» юзера и здороваемся как в первый раз.
        database.get_persona(user_id)
        await callback.message.answer(START_WELCOME[lang], reply_markup=start_keyboard())
    else:
        history = database.get_history(user_id, HISTORY_LIMIT)
        lang = _detect_user_language(history)
        await callback.message.answer(RESET_MESSAGES[lang]["no_reply"])
    await callback.answer()


@dp.callback_query(F.data.startswith("checkin:"))
async def on_checkin_choice(callback: CallbackQuery):
    """Пользователь выбрал частоту проактивных сообщений."""
    freq = callback.data.split(":", 1)[1]
    if freq not in CHECKIN_LABELS["uk"]:
        await callback.answer()
        return
    user_id = callback.from_user.id
    database.set_checkin_freq(user_id, freq)
    history = database.get_history(user_id, HISTORY_LIMIT)
    lang = _detect_user_language(history) if history else "uk"
    label = CHECKIN_LABELS[lang][freq]
    confirm = (
        f"Готово. Як часто я пишу першим: {label}."
        if lang == "uk"
        else f"Готово. Как часто я пишу первой: {label}."
    )
    await callback.message.answer(confirm)
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

# Стоп-маркеры: человек отказывается / выходит из роли / просит прекратить.
# При совпадении - НЕ генерируем медиа, даже если в тексте есть слово «фото».
STOP_TRIGGERS = (
    "припини", "припиняю", "не надсилай", "не присылай", "не шли більше",
    "перестань", "хватит", "досить", "стоп ", " стоп.", " стоп,",
    "я ai", "я аи", "я ии", "я штучний інтелект", "я искусственный интеллект",
    "я не людина", "я не человек",
    "не можу взаємодіяти", "не могу взаимодейство",
    "не буду грати", "не буду играть", "вийшов з ролі", "вышел из роли",
    "я закінчую", "я заканчиваю", "припиняю цю розмову", "прекращаю этот",
)

# Кризис-маркеры: суицидальная идеация / self-harm / уже идёт линия доверия.
# При совпадении в свежих репликах (юзера ИЛИ Миры) - блокируем ВСЕ медиа
# на этот ход. Тон должен оставаться текстовым, человеческим.
CRISIS_MARKERS = (
    # user
    "не прокидат", "не прокидался", "не прокидатися", "не просыпат",
    "не хочу жит", "не хочу більше жит",
    "нашкодит", "навредит",
    "руки на себе", "руки на себя",
    "закінчити з собою", "покончить с собой",
    "самогубств", "самоубийст", "суицид",
    "піти зовсім", "уйти совсем", "уйти из жизни",
    "не варто жит", "не стоит жить", "немає сенсу жит",
    # Мира уже предложила линию доверия - значит мы УЖЕ в crisis-режиме
    "телефон довір", "телефон доверия", "лінія довіри", "линия доверия",
    "0800 60 60 60", "лінія допомог",
)

# Маркеры-подписи отправленных Мирой фото. Используются для анти-каскада:
# если в свежих ассистентских репликах уже есть caption - значит фото только
# что было, новое слать только при ЯВНОЙ просьбе.
PHOTO_CAPTION_MARKERS = (
    "ось, тримай", "це для тебе", "ну як тобі", "спеціально для тебе",
    "ось я 💛", "вот я 💛", "подобаюсь?", "нравлюсь?",
    "вот, держи", "это для тебя", "ну как тебе", "специально для тебя",
)

# Служебные сообщения для медиа на языке собеседника (по умолчанию украинский).
# Маркеры позитивной реакции на «Подобаюсь?» / «Нравлюсь?» под базовым фото.
_AVATAR_POSITIVE = (
    " так", "так,", "так.", "так!", "так)", "так ", "так😊",
    " да", "да,", "да.", "да!", "да)", "да ", "ага", "угу",
    "нрав", "подоб", "люб", "обож",
    "клас", "красив", "красот", "красун", "гарн",
    "хорош", "огонь", "супер", "вау", "wow",
    "🔥", "❤", "😍", "🥰", "👍", "👌", "💛", "💕", "💖", "💗", "🥹", "🤩",
)


def _is_positive_reaction(text):
    """Простой детектор позитивной реакции на «Нравлюсь?» под базовым фото."""
    low = (text or "").lower().strip()
    if not low:
        return False
    padded = f" {low} "
    return any(t in padded for t in _AVATAR_POSITIVE)


def _last_bot_was_base_photo(history):
    """Последняя реплика бота - подпись базового фото Миры?"""
    assistant_msgs = [
        m for m in history if m.get("role") == "assistant"
    ]
    if not assistant_msgs:
        return False
    last = (assistant_msgs[-1].get("content") or "")
    return "Подобаюсь?" in last or "Нравлюсь?" in last


MEDIA_MESSAGES = {
    "uk": {
        "ask_desc": "Хочеш мене побачити? 🙈 А якою ти мене уявляєш? Опиши, будь ласка, - аж до одягу.",
        "base_wait": "Добре 💛",
        "photo_wait": "Зараз зроблю для тебе 💛",
        "video_wait": "Записую для тебе відео 🎥",
        "base_caption": "Ось, дивись. Подобаюсь?",
        "error": "Ой, не вийшло цього разу. Спробуймо трохи згодом?",
        "captions": ["ось, тримай 💛", "це для тебе", "ну як тобі?", "спеціально для тебе 😊"],
        "video_follows": ["ну як? 🙈", "ось, спеціально для тебе 💛", "зловив настрій?"],
    },
    "ru": {
        "ask_desc": "Хочешь меня увидеть? 🙈 А какой ты меня представляешь? Опиши, пожалуйста, - вплоть до одежды.",
        "base_wait": "Хорошо 💛",
        "photo_wait": "Сейчас сделаю для тебя 💛",
        "video_wait": "Записываю для тебя видео 🎥",
        "base_caption": "Вот, смотри. Нравлюсь?",
        "error": "Ой, не получилось в этот раз. Попробуем чуть позже?",
        "captions": ["вот, держи 💛", "это для тебя", "ну как тебе?", "специально для тебя 😊"],
        "video_follows": ["ну как? 🙈", "вот, специально для тебя 💛", "поймал настроение?"],
    },
}


_RU_WORD_MARKERS = (
    " что ", " что,", " что.", " что?", " что!",
    " чтобы", " потому что", " ещё ", " уже ", " очень ",
    " тебе ", " мне ", " меня ", " тебя ", " себя ",
    " хорошо", " плохо", " ладно", " конечно",
    " нельзя", " можно ",
    " тоже ", " чуть", " слышу", " слышал",
    " рад ", " рада ", " рад,", " рада,", "рад твое", "рад твоей",
    "пишу", "люблю",
    # Дополнительно: характерные русские слова (только те, что отличаются от
    # украинского, чтобы не получалось взаимной нейтрализации).
    " твой ", " твоё ", " твои ",
    " мой ", " моё ", " мои ",
    " этот ", " эта ", " это ", " эти ",
    " только ", " сейчас ", " сегодня ", " вчера ",
    " русая", " глаза", " грудь", " подчерк",
    " вижу ", " видела", " представ", " подразум",
    "вообще", "наверн",
    "спасибо",
    "нравишь", "нравит", "нравлюсь",
)
_UK_WORD_MARKERS = (
    " що ", " що,", " що.", " що?", " що!",
    " щоб", " тому що", " ще ", " вже ", " дуже ",
    " тобі ", " мені ", " мене ", " тебе ", " себе ",
    " добре", " погано", " гаразд", " звісно",
    " не можна", " можна ",
    " також", " теж ", " треба", " чую", " чув",
    " радий ", " рада ", " радий,", "радий твоє",
    " пишу", " кохаю", " люблю",
    # Дополнительно: характерные украинские слова (только эксклюзивные).
    " твій ", " твоє ", " твої ",
    " мій ", " моє ", " мої ",
    " як ", "як?",
    " цей ", " ця ", " ці ",
    " тільки ", " зараз ", " сьогодні ", " вчора ",
    " бачу ", " бачила", " бачив",
    "будь ласка", "дякую",
    "подобаюсь", "подобаєш", "подобаєт",
)


def _score_lang(text: str):
    """Простая балльная детекция: (ru_score, uk_score) для одной строки."""
    low = (text or "").lower()
    if not low:
        return 0, 0
    padded = f" {low} "
    ru = sum(1 for c in low if c in "ыэъё")
    uk = sum(1 for c in low if c in "їєіґ")
    ru += sum(1 for w in _RU_WORD_MARKERS if w in padded)
    uk += sum(1 for w in _UK_WORD_MARKERS if w in padded)
    return ru, uk


def _detect_user_language(history):
    """Угадать язык собеседника ('ru' или 'uk').

    Приоритеты:
    1. Явная просьба сменить язык в последнем сообщении.
    2. Буквенно-словарная детекция последней user-реплики.
    3. Последний bot-ответ - он сам себя писал, по нему видно текущий
       язык диалога (полезно когда юзер пишет короткое нейтральное).
    4. Фолбэк на 6 последних user-сообщений.
    5. Дефолт - uk (продуктовое приветствие на украинском).
    """
    user_msgs = [m["content"] for m in history if m["role"] == "user"]
    bot_msgs = [m["content"] for m in history if m["role"] == "assistant"]
    if not user_msgs:
        return "uk"

    last = user_msgs[-1].lower()

    # 1) Явная просьба переключиться
    ru_keywords = (
        "по-русски", "по русски", "на русском", "русском языке",
        "перейди на русский", "русский язык", "русским языком",
        "пиши на русском", "ответь на русском", "говори на русском",
        "чего на украинском", "что на украинском", "почему на украинском",
        "чого українською", "чому українською",
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

    # 2) Балльная детекция последней реплики юзера. Считаем СУММУ последних
    # двух user-реплик (последняя может быть короткой типа «угу» - тогда
    # предыдущая даст контекст). Это сильнее перекрывает старый диалог
    # на другом языке.
    last_two_user = " ".join(user_msgs[-2:])
    ru, uk = _score_lang(last_two_user)
    if ru > uk:
        return "ru"
    if uk > ru:
        return "uk"

    # 3) Если последние реплики юзера нейтральные - смотрим, на каком языке
    # сама Мира отвечала в прошлый раз. Это самый надёжный сигнал
    # «текущего языка диалога».
    if bot_msgs:
        ru_b, uk_b = _score_lang(bot_msgs[-1])
        if ru_b > uk_b:
            return "ru"
        if uk_b > ru_b:
            return "uk"

    # 4) Фолбэк на 4 последних user-сообщений (узкое окно, чтобы старая
    # история на другом языке не перебивала текущий язык диалога).
    recent = " ".join(user_msgs[-4:])
    ru_r, uk_r = _score_lang(recent)
    if ru_r > uk_r:
        return "ru"
    if uk_r > ru_r:
        return "uk"

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
    """Быстрое определение по словам: 'talking' / 'video' / 'photo' / 'none' / None.

    'none' — явный стоп/отказ/выход-из-роли, медиа НЕ слать ни в каком виде.
    None — «по словам непонятно», тогда решает модель-классификатор.
    """
    low = (text or "").lower()
    if any(t in low for t in STOP_TRIGGERS):
        return "none"
    if any(t in low for t in TALKING_TRIGGERS):
        return "talking"
    if any(t in low for t in VIDEO_TRIGGERS):
        return "video"
    if any(t in low for t in PHOTO_TRIGGERS):
        return "photo"
    return None


def _crisis_in_recent(history, current_text="", lookback=5):
    """Есть ли в последних N репликах (включая текущую) маркеры кризиса.

    Если да - НИКАКОГО медиа на этот ход. Тон только текстовый.
    """
    recent = []
    for m in history[-lookback:]:
        recent.append((m.get("content") or "").lower())
    recent.append((current_text or "").lower())
    blob = " ".join(recent)
    return any(marker in blob for marker in CRISIS_MARKERS)


def _recent_photo_sent(history, lookback=4):
    """Среди последних N assistant-сообщений уже есть подпись отправленного фото.

    Используется для анти-каскада: если фото только что было, новое слать
    только при ЯВНОЙ просьбе словами, а не по «мягкому» сигналу LLM.
    """
    assistant_msgs = [
        (m.get("content") or "") for m in history if m.get("role") == "assistant"
    ]
    for msg in assistant_msgs[-lookback:]:
        low = msg.lower()
        if any(mark in low for mark in PHOTO_CAPTION_MARKERS):
            return True
    return False


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
    # Раньше тут было "Хорошо 💛" - убрал, индикатор upload_photo сам всё
    # покажет, а отдельная коротенькая реплика звучала как ботский ack.
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
    sent = await message.answer_photo(FSInputFile(path), caption=caption)
    # Закреплять СРАЗУ нельзя - юзер ещё не подтвердил, что нравится.
    # Запомним msg_id, закрепим позже когда придёт позитивная реакция
    # (см. ветку 6.4 в handle_message).
    database.set_mira_base_photo_msg(user_id, sent.message_id)


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
    intent_keyword = _keyword_media_intent(text)
    intent = intent_keyword
    if intent is None:
        intent = await asyncio.to_thread(detect_media_request, history, text)
    if intent == "none":
        return False

    # SAFETY-GATE: в кризисных темах никакого медиа - только текст.
    # Видео-кружок или фото в момент разговора про «не прокидатись» или
    # после телефона доверия рвут момент и сигналят «алгоритм за стенкой».
    if _crisis_in_recent(history, current_text=text):
        logging.info(
            "Mira media skipped (crisis context) user_id=%s intent=%s",
            user_id, intent,
        )
        return False

    # АНТИ-КАСКАД ФОТО: если только что слали фото и сейчас не ЯВНАЯ
    # просьба словами - не шлём ещё одно. Защищает от залипания LLM-
    # классификатора, когда в реплике юзера есть слово «фото» в любом
    # контексте (включая отказ или комплимент к прошлому фото).
    if intent == "photo" and intent_keyword != "photo":
        if _recent_photo_sent(history, lookback=4):
            logging.info(
                "Mira photo skipped (cascade guard, no explicit keyword) "
                "user_id=%s", user_id,
            )
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
    # Сервисные события Telegram (закрепление, открепление, новые участники)
    # приходят как Message без text/photo. На них отвечать НЕ нужно, иначе
    # бот отвечает сам себе на собственное pin_chat_message.
    if (
        message.pinned_message is not None
        or getattr(message, "new_chat_members", None)
        or getattr(message, "left_chat_member", None)
    ):
        return
    user_id = message.from_user.id

    # Принимаем текст и фото (с подписью или без); прочее (стикеры, голосовые)
    # пока пропускаем. Текст «не понимаю» - на языке диалога, по существующей
    # истории. Это сообщение НЕ должно попадать в историю, поэтому язык
    # определяем по уже накопленной истории (если она есть).
    if not message.text and not message.photo:
        history_for_lang = database.get_history(user_id, HISTORY_LIMIT)
        lang_unsupported = _detect_user_language(history_for_lang) if history_for_lang else "uk"
        unsupported_text = (
            "Поки що я розумію тільки текст і фото :)"
            if lang_unsupported == "uk"
            else "Пока что я понимаю только текст и фото :)"
        )
        await message.answer(unsupported_text)
        return

    # Если пришло фото — скачиваем и готовим payload для модели; текст пользователя
    # формируем из caption либо ставим естественную «подсказку», без скобочных тегов.
    image_payload = None
    if message.photo:
        try:
            image_payload = await _photo_payload(message.photo[-1])
        except Exception:
            logging.exception("Не удалось скачать фото user_id=%s", user_id)
        caption = (message.caption or "").strip()
        # Синтетический user-текст про фото - на языке диалога. Иначе он сам
        # попадает в историю как UK-реплика и потом ломает _detect_user_language
        # для русского юзера. Язык вычисляем по истории ДО этого добавления.
        prior_history = database.get_history(user_id, HISTORY_LIMIT)
        lang_photo = _detect_user_language(prior_history) if prior_history else "uk"
        sent_phrase = "Я надіслав тобі фото." if lang_photo == "uk" else "Я отправил тебе фото."
        missing_phrase = (
            "Я надіслав тобі фото, але воно не дійшло."
            if lang_photo == "uk"
            else "Я отправил тебе фото, но оно не дошло."
        )
        if image_payload:
            user_text = sent_phrase + (f" {caption}" if caption else "")
        else:
            user_text = caption or missing_phrase
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
        if lang == "ru":
            extra_parts.append(
                "СЕЙЧАС это твоя ПОСЛЕДНЯЯ реплика в роли хоста. Сразу после неё "
                "система сама предложит кое-что человеку - ничего делать не нужно. "
                "Дай ОДНУ короткую тёплую фразу: просто покажи, что ты услышал и "
                "что тебе с ним хорошо. БЕЗ вопросов. БЕЗ любых намёков, что что-то "
                "«случится», «будет», «скоро», или что ты что-то «начинаешь "
                "понимать» - никаких подводок и обещаний. Просто тёплое живое "
                "завершение мысли, как естественная пауза в разговоре."
            )
        else:
            extra_parts.append(
                "ЗАРАЗ це твоя ОСТАННЯ репліка в ролі хоста. Відразу після неї "
                "система сама запропонує щось людині - нічого робити не потрібно. "
                "Дай ОДНУ коротку тёплу фразу: просто покажи що ти почув і що "
                "тобі з ним добре. БЕЗ питань. БЕЗ будь-яких натяків що щось "
                "«станеться», «буде», «скоро», або що ти щось «починаєш розуміти» - "
                "жодних підводок і обіцянок. Просто тепле живе завершення думки, "
                "ніби природна пауза в розмові."
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
        # Текущая внешность - чтобы старые описания/фото из истории не
        # перебивали новый образ после /newlook.
        look = database.get_mira_look(user_id)
        if look.get("desc"):
            extra_parts.append(_build_mira_look_block(look["desc"]))
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
        err_text = (
            "Ой, щось пішло не так. Спробуй ще раз трохи згодом."
            if lang == "uk"
            else "Ой, что-то пошло не так. Попробуй ещё раз чуть позже."
        )
        await message.answer(err_text)
        return

    # 6) Сохраняем ответ бота и отправляем его пользователю «живыми» репликами.
    database.add_message(user_id, "assistant", reply)
    await send_bubbles(message.chat.id, reply)

    # 6.4) Реакция на базовое фото: после позитивной реакции - закрепляем
    #      базовое фото в чате (визуальный якорь сверху). Пин - на каждое
    #      новое базовое фото (после /newlook тоже).
    #      (Инвайт «поставь как фото контакта» убран: Telegram не даёт
    #      менять фото контакта для бот-чатов - такого пункта в UI нет.)
    if (
        persona_key == "mira"
        and _last_bot_was_base_photo(history)
        and _is_positive_reaction(user_text)
    ):
        base_msg_id = database.get_mira_base_photo_msg(user_id)
        if (
            base_msg_id is not None
            and database.get_mira_pinned_msg(user_id) != base_msg_id
        ):
            try:
                await bot.pin_chat_message(
                    chat_id=message.chat.id,
                    message_id=base_msg_id,
                    disable_notification=True,
                )
                database.set_mira_pinned_msg(user_id, base_msg_id)
                logging.info(
                    "Закрепили базовое фото user_id=%s msg_id=%s",
                    user_id, base_msg_id,
                )
            except Exception as e:
                logging.warning(
                    "Не удалось закрепить базовое фото user_id=%s: %s: %s",
                    user_id, type(e).__name__, e,
                )

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
            # Подтягиваем последний кусок переписки, чтобы check-in продолжал
            # тему, а не начинал заново.
            history = await asyncio.to_thread(
                database.get_history, user_id, HISTORY_LIMIT,
            )
            # Язык диалога — чтобы первое «пишу первой» не дрейфовало на язык
            # mira.txt. Если истории нет (свежий юзер) — UK по дефолту.
            lang = _detect_user_language(history) if history else "uk"
            # Текст генерируем в отдельном потоке (запрос к Anthropic блокирующий).
            text = await asyncio.to_thread(
                generate_checkin, persona_key, facts, history, lang,
            )
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
