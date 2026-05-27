"""Запрос к Anthropic API.

Собираем system prompt (через personas.build_system_prompt) и историю диалога,
отправляем в модель и возвращаем текст ответа.
"""

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY, MODEL, SUMMARY_MODEL
from personas import build_system_prompt

# Создаём клиент один раз — он переиспользуется для всех запросов.
client = Anthropic(api_key=ANTHROPIC_API_KEY)


def get_reply(persona_key, history, facts=""):
    """Отправить историю диалога в модель и вернуть текст ответа.

    persona_key — ключ выбранной персоны ('onboarding'/'friend'/'coach'/'mira').
    history — список сообщений вида
    {"role": "user"/"assistant", "content": "..."}.
    facts — «конспект» о пользователе из долговременной памяти (может быть пустым).
    """
    # Общие правила + характер персоны + память о человеке.
    system_prompt = build_system_prompt(persona_key, facts)

    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=system_prompt,
        messages=history,  # вся переписка для контекста
    )
    # Ответ приходит списком блоков; для текста берём текст первого блока.
    return response.content[0].text


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
        "обращаться, важные детали жизни, предпочтения, цели, текущее настроение и "
        "контекст отношений с Мирой. Пиши по пунктам, кратко, только то, что важно "
        "помнить надолго. Не выдумывай факты. Верни только сам список, без вступлений."
    )

    response = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=500,
        system="Ты ведёшь краткие заметки о пользователе для компаньон-бота.",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()
