"""Реестр персон и сборка system prompt.

Каждая персона - это текстовый файл в папке personas/.
Здесь хранится список персон с их данными, а build_system_prompt
собирает итоговую инструкцию для модели:
общие правила (common.txt) + характер персоны + память о человеке.
"""

import os

from config import PERSONAS_DIR

# Описание всех персон. Ключ - короткое имя, по которому храним выбор в базе.
# name        - как показываем на кнопке;
# file        - файл с характером в папке personas/;
# requires_adult - нужна ли отметка 18+ (только Мира);
# selectable  - можно ли выбрать кнопкой в /persona (онбординг - нельзя).
PERSONAS = {
    "onboarding": {
        "name": "Знайомство",
        "file": "onboarding.txt",
        "requires_adult": False,
        "selectable": False,
    },
    "friend": {
        "name": "Друг",
        "file": "friend.txt",
        "requires_adult": False,
        "selectable": True,
    },
    "coach": {
        "name": "Коуч",
        "file": "coach.txt",
        "requires_adult": False,
        "selectable": True,
    },
    "mira": {
        "name": "Міра 💛",
        "file": "mira.txt",
        "requires_adult": True,
        "selectable": True,
    },
}

# Персона по умолчанию для новых пользователей.
DEFAULT_PERSONA = "onboarding"


def _read(filename):
    """Прочитать текстовый файл из папки personas/."""
    path = os.path.join(PERSONAS_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def build_system_prompt(persona_key, facts=""):
    """Собрать инструкцию для модели одной строкой.

    Склеиваем: общие правила + характер выбранной персоны + память о человеке.
    Если персона неизвестна - откатываемся на персону по умолчанию.
    """
    if persona_key not in PERSONAS:
        persona_key = DEFAULT_PERSONA

    common = _read("common.txt")
    character = _read(PERSONAS[persona_key]["file"])
    prompt = common + "\n\n" + character

    if facts:
        prompt += "\n\nЩо ти пам'ятаєш про співрозмовника (враховуй це):\n" + facts

    return prompt


def build_system_blocks(persona_key, facts=""):
    """Собрать system prompt блоками — для prompt caching.

    Оба блока (статическая часть и память) помечаем cache_control. Память
    меняется редко (раз в несколько сообщений), поэтому между обновлениями она
    читается из кэша дёшево, а не пересылается заново. Когда память обновится,
    перезапишется только её блок, а статическая часть останется в кэше.
    """
    if persona_key not in PERSONAS:
        persona_key = DEFAULT_PERSONA

    static = _read("common.txt") + "\n\n" + _read(PERSONAS[persona_key]["file"])

    blocks = [
        {
            "type": "text",
            "text": static,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    if facts:
        blocks.append(
            {
                "type": "text",
                "text": "Що ти пам'ятаєш про співрозмовника (враховуй це):\n" + facts,
                "cache_control": {"type": "ephemeral"},
            }
        )
    return blocks
