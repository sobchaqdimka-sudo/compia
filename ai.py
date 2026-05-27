"""Запрос к Anthropic API.

Собираем system prompt (через personas.build_system_prompt) и историю диалога,
отправляем в модель и возвращаем текст ответа.
"""

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY, MODEL, SUMMARY_MODEL
from personas import build_system_prompt

# Создаём клиент один раз — он переиспользуется для всех запросов.
client = Anthropic(api_key=ANTHROPIC_API_KEY)


def get_reply(persona_key, history, facts="", transition=False):
    """Отправить историю диалога в модель и вернуть текст ответа.

    persona_key — ключ выбранной персоны ('onboarding'/'friend'/'coach'/'mira').
    history — список сообщений вида
    {"role": "user"/"assistant", "content": "..."}.
    facts — «конспект» о пользователе из долговременной памяти (может быть пустым).
    transition — True, если это первое сообщение после смены роли (онбординг → друг/коуч):
                 тогда просим модель мягко поприветствовать в новой роли.
    """
    # Общие правила + характер персоны + память о человеке.
    system_prompt = build_system_prompt(persona_key, facts)

    if transition:
        system_prompt += (
            "\n\nЭто твоё первое сообщение в новой роли. Мягко и по-человечески "
            "продолжи разговор уже в ней, без анкет и громких объявлений. Ненавязчиво "
            "дай понять, что роль можно сменить в любой момент. Отвечай на языке собеседника."
        )

    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=system_prompt,
        messages=history,  # вся переписка для контекста
    )
    # Ответ приходит списком блоков; для текста берём текст первого блока.
    return response.content[0].text


def generate_checkin(persona_key, facts):
    """Сгенерировать тёплое сообщение «бот пишет первым».

    Опираемся на характер персоны и память о человеке. Тон без давления и
    без чувства вины: это забота, а не попытка удержать.
    """
    system_prompt = build_system_prompt(persona_key, facts)

    instruction = (
        "Человек давно не писал. Напиши ему сам, первым: короткое тёплое сообщение "
        "с заботой. Можешь мягко опереться на то, что знаешь о нём. "
        "Очень важно: без давления и без чувства вины. Не спрашивай «почему пропал», "
        "не упрекай, ничего не требуй и не выпрашивай ответ. Просто по-доброму дай "
        "знать, что вспомнил(а) о нём и рядом. Коротко, на том языке, на котором он "
        "обычно пишет."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=300,
        system=system_prompt,
        messages=[{"role": "user", "content": instruction}],
    )
    return response.content[0].text


def detect_need(history):
    """По разговору определить, что человеку сейчас нужнее.

    Возвращает 'friend', 'coach' или 'unclear'. Романтику (Миру) тут не выбираем
    никогда — она только по явному выбору человека. Используем дешёвую модель.
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
        "- unclear: пока непонятно, нужно ещё пообщаться.\n"
        "Романтику или близость не выбирай никогда.\n"
        "Ответь строго одним словом: friend, coach или unclear."
    )

    response = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=10,
        system="Ты классифицируешь, какая поддержка нужна пользователю.",
        messages=[{"role": "user", "content": prompt}],
    )
    answer = response.content[0].text.strip().lower()
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
        "Вот что мы уже знаем о пользователе (может быть пусто):\n"
        f"{previous_facts or '(пока ничего)'}\n\n"
        "Недавняя переписка:\n"
        f"{transcript}\n\n"
        "Обнови краткий список устойчивых фактов о пользователе: имя или как к нему "
        "обращаться, важные детали жизни, предпочтения, цели, текущее настроение, "
        "контекст отношений с компаньоном, а также манеру общения (пишет коротко или "
        "развёрнуто, любит ли юмор, на какие темы откликается, на каком языке пишет). "
        "Пиши по пунктам, кратко, только то, что важно помнить надолго. Не выдумывай "
        "факты. Верни только сам список, без вступлений."
    )

    response = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=500,
        system="Ты ведёшь краткие заметки о пользователе для компаньон-бота.",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()
