"""Запрос к Anthropic API.

Берём характер персонажа из persona.txt и историю диалога,
отправляем в модель и возвращаем текст ответа.
"""

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY, MODEL, PERSONA_PATH

# Создаём клиент один раз — он переиспользуется для всех запросов.
client = Anthropic(api_key=ANTHROPIC_API_KEY)


def _load_persona():
    """Прочитать характер персонажа из файла.

    Читаем при каждом запросе, чтобы правки в persona.txt подхватывались
    без перезапуска бота.
    """
    with open(PERSONA_PATH, "r", encoding="utf-8") as f:
        return f.read()


def get_reply(history):
    """Отправить историю диалога в модель и вернуть текст ответа.

    history — список сообщений вида
    {"role": "user"/"assistant", "content": "..."}.
    Последним элементом идёт свежее сообщение пользователя.
    """
    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=_load_persona(),  # характер персонажа (system prompt)
        messages=history,        # вся переписка для контекста
    )
    # Ответ приходит списком блоков; для текста берём текст первого блока.
    return response.content[0].text
