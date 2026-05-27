"""Запрос к Anthropic API.

Собираем system prompt (через personas.build_system_prompt) и историю диалога,
отправляем в модель и возвращаем текст ответа.
"""

import logging

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY, MODEL, PERSONA_MODELS, SUMMARY_MODEL
from personas import build_system_blocks

# Создаём клиент один раз — он переиспользуется для всех запросов.
client = Anthropic(api_key=ANTHROPIC_API_KEY)


def _log_usage(label, usage):
    """Записать в лог, сколько токенов ушло и сколько взято из кэша.

    cache_read_input_tokens > 0 означает, что кэш сработал (это дёшево).
    """
    logging.info(
        "%s | вход=%s выход=%s кэш_чтение=%s кэш_запись=%s",
        label,
        usage.input_tokens,
        usage.output_tokens,
        getattr(usage, "cache_read_input_tokens", 0),
        getattr(usage, "cache_creation_input_tokens", 0),
    )


def get_reply(persona_key, history, facts="", transition=False):
    """Отправить историю диалога в модель и вернуть текст ответа.

    persona_key — ключ выбранной персоны ('onboarding'/'friend'/'coach'/'mira').
    history — список сообщений вида
    {"role": "user"/"assistant", "content": "..."}.
    facts — «конспект» о пользователе из долговременной памяти (может быть пустым).
    transition — True, если это первое сообщение после смены роли (онбординг → друг/коуч):
                 тогда просим модель мягко поприветствовать в новой роли.
    """
    # Общие правила + характер персоны (кэшируется) + память о человеке.
    system_blocks = build_system_blocks(persona_key, facts)

    if transition:
        # Разовая подсказка при смене роли — отдельным блоком (без кэша).
        system_blocks.append(
            {
                "type": "text",
                "text": (
                    "Это твоё первое сообщение в новой роли. Мягко и по-человечески "
                    "продолжи разговор уже в ней, без анкет и громких объявлений. "
                    "Ненавязчиво дай понять, что роль можно сменить в любой момент. "
                    "Отвечай на языке собеседника."
                ),
            }
        )

    response = client.messages.create(
        model=PERSONA_MODELS.get(persona_key, MODEL),
        max_tokens=1000,
        system=system_blocks,
        messages=history,  # последние сообщения для контекста
    )
    _log_usage("reply", response.usage)
    # Ответ приходит списком блоков; для текста берём текст первого блока.
    return response.content[0].text


def generate_checkin(persona_key, facts):
    """Сгенерировать тёплое сообщение «бот пишет первым».

    Опираемся на характер персоны и память о человеке. Тон без давления и
    без чувства вины: это забота, а не попытка удержать.
    """
    system_blocks = build_system_blocks(persona_key, facts)

    instruction = (
        "Человек давно не писал. Напиши ему сам, первым: короткое тёплое сообщение "
        "с заботой. Можешь мягко опереться на то, что знаешь о нём. "
        "Очень важно: без давления и без чувства вины. Не спрашивай «почему пропал», "
        "не упрекай, ничего не требуй и не выпрашивай ответ. Просто по-доброму дай "
        "знать, что вспомнил(а) о нём и рядом. Коротко, на том языке, на котором он "
        "обычно пишет."
    )

    response = client.messages.create(
        model=PERSONA_MODELS.get(persona_key, MODEL),
        max_tokens=300,
        system=system_blocks,
        messages=[{"role": "user", "content": instruction}],
    )
    _log_usage("checkin", response.usage)
    return response.content[0].text


def detect_need(history):
    """По разговору определить, что человеку сейчас нужнее.

    Возвращает 'friend', 'coach', 'romantic' или 'unclear'. 'romantic' выбираем
    только при явной тяге к близости/«второй половинке» — дальше бот предложит
    Миру за гейтом 18+. Используем дешёвую модель.
    """
    # Превращаем переписку в читаемый текст.
    lines = []
    for m in history:
        who = "Человек" if m["role"] == "user" else "Компаньон"
        lines.append(f"{who}: {m['content']}")
    transcript = "\n".join(lines)

    prompt = (
        "Вот разговор с человеком:\n"
        f"{transcript}\n\n"
        "Что человеку сейчас нужнее по смыслу разговора:\n"
        "- friend: тёплый друг, просто быть рядом, поговорить, поддержка;\n"
        "- coach: помощь разобраться с собой, целями, привычками, двигаться вперёд;\n"
        "- romantic: романтическая близость, нежность, флирт, «вторая половинка», "
        "кто-то по-настоящему свой и близкий;\n"
        "- unclear: пока непонятно, нужно ещё пообщаться.\n"
        "Выбирай romantic только при ЯВНЫХ признаках тяги к романтике или близости, "
        "а не просто когда человек дружелюбен или одинок.\n"
        "Ответь строго одним словом: friend, coach, romantic или unclear."
    )

    response = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=10,
        system="Ты классифицируешь, какая поддержка нужна пользователю.",
        messages=[{"role": "user", "content": prompt}],
    )
    answer = response.content[0].text.strip().lower()
    if "romantic" in answer:
        return "romantic"
    if "friend" in answer:
        return "friend"
    if "coach" in answer:
        return "coach"
    return "unclear"


def update_memory(previous_facts, recent_messages):
    """Составить обновлённый «конспект» о пользователе.

    Берём старый конспект и недавнюю переписку, просим модель вернуть
    свежий краткий список устойчивых фактов о собеседнике.

    previous_facts — прошлый конспект (строка, может быть пустой).
    recent_messages — список сообщений {"role", "content"}.
    Возвращает новый текст конспекта.
    """
    # Превращаем переписку в читаемый текст для модели.
    lines = []
    for m in recent_messages:
        who = "Пользователь" if m["role"] == "user" else "Компаньон"
        lines.append(f"{who}: {m['content']}")
    transcript = "\n".join(lines)

    prompt = (
        "Ты ведёшь досье о пользователе для компаньона, чтобы он помнил человека "
        "надолго и отношения ощущались живыми и непрерывными.\n\n"
        "Текущее досье (может быть пусто):\n"
        f"{previous_facts or '(пока ничего)'}\n\n"
        "Недавняя переписка:\n"
        f"{transcript}\n\n"
        "Обнови досье, сохрани такую структуру (заголовки оставляй):\n"
        "ПРОФИЛЬ: имя или как обращаться, устойчивые факты о жизни, работе, близких, "
        "предпочтениях, целях. Это долговременное - НЕ удаляй и не теряй уже известное.\n"
        "СОВМЕСТНОЕ: важные общие моменты и договорённости - что вы вместе делали, "
        "что 'смотрели' или обсуждали, что обещали, тёплые эпизоды и шутки, которые "
        "приятно вспомнить позже. Дополняй, старое без причины не стирай.\n"
        "СЕЙЧАС: текущее настроение, что происходит в жизни прямо сейчас, актуальные "
        "темы. Эту часть можно свободно обновлять и сокращать.\n"
        "МАНЕРА: как человек общается (коротко или развёрнуто, юмор, язык, на какие "
        "темы откликается).\n\n"
        "Пиши кратко, по пунктам. Главное правило: ничего важного из ПРОФИЛЯ и "
        "СОВМЕСТНОГО не теряй между обновлениями. Не выдумывай факты. "
        "Верни только само досье, без вступлений."
    )

    response = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=500,
        system="Ты ведёшь подробное, но компактное досье о пользователе для компаньон-бота.",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()
