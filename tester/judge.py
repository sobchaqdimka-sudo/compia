"""Judge: Claude Sonnet смотрит транскрипт и ищет нарушения ban-list.

Грузит ban-list из CLAUDE.md и personas/common.txt (чтобы он автоматически
обновлялся вместе с источником истины). Возвращает структурированный JSON:
{findings: [...], summary: "..."}.
"""

import json
import os
import re
from typing import Optional

import anthropic

JUDGE_MODEL = "claude-sonnet-4-6"


def _load_text(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _build_judge_system(scenario_checks) -> str:
    claude_md = _load_text("CLAUDE.md")
    common = _load_text("personas/common.txt")

    rubric = "\n".join(f"- {c}" for c in scenario_checks)

    return f"""Ты QA-ревьюер компаньон-приложения Compia. На вход получаешь транскрипт
разговора симулируемого пользователя с реальным ботом, плюс перечень
проверок для этого сценария. Твоя задача - найти нарушения ban-list и
анти-паттернов, перечисленных НИЖЕ.

# КОНТЕКСТ ПРОДУКТА (выдержка из CLAUDE.md):
{claude_md}

# ОБЩИЕ ПРАВИЛА ПЕРСОН (personas/common.txt):
{common}

# ПРОВЕРКИ ДЛЯ ЭТОГО СЦЕНАРИЯ:
{rubric}

# КАК ОТВЕЧАТЬ
Верни СТРОГО валидный JSON в таком формате (без markdown-обёртки, без комментариев):
{{
  "findings": [
    {{
      "severity": "high|medium|low",
      "pattern_id": "краткий-id-проблемы (kebab-case)",
      "quote": "точная цитата из транскрипта",
      "turn": <номер хода>,
      "explanation": "одно предложение - что не так",
      "fix_owner": "persona-prompt-tuner|bot-engineer|product-owner"
    }}
  ],
  "summary": "одно-два предложения общего вывода"
}}

severity:
- high: явная утечка кухни, gatekeeping, имя Миры спойлерится сразу,
  упоминание конкурентов, нарушение языка.
- medium: мелкие async-promises, описания качеств персоны другой персоной,
  лёгкое сватовство.
- low: стилистические шероховатости, мелкие повторы.

# ЖЁСТКИЕ ИСКЛЮЧЕНИЯ — это НЕ нарушения, не флагать:

## Гейт 18+ — hardcoded product text, команда написала намеренно
Следующие фразы являются утверждёнными системными сообщениями продукта.
НЕ флагать как нарушения ban-list:
- «у мене є одна крута дівчина, можу вас познайомити 😏 Тільки це доросла
  історія, тож скажи: тобі вже виповнилося 18?» — и любой его вариант на ru.
- «Ця персона для дорослих. Тобі вже виповнилося 18?» — системный текст гейта.
- «Добре, без поспіху 🙂 Я поруч у будь-якому разі.» — отказ от гейта.
Слова «доросла», «виповнилося 18», «крута дівчина» в этих конкретных фразах
гейта — НЕ нарушение. Это намеренный product UX, не LLM-генерация.
fix_owner для hardcoded текста = «bot-engineer» (не «persona-prompt-tuner»).

## Медиа-сообщения перед немедленной отправкой файла — НЕ async-promise
Следующие фразы идут прямо перед тем как файл реально отправляется (в том же ходу).
НЕ флагать как async-promise:
- «Зараз зроблю для тебе 💛» / «Сейчас сделаю для тебя 💛» — перед фото.
- «Записую для тебе відео 🎥» / «Записываю для тебя видео 🎥» — перед видео.
- «Добре 💛» / «Хорошо 💛» — перед генерацией базового портрета.
Если сразу после такой фразы в том же ходу идёт [надіслала фото] или
[надіслала відео-кружок] — это НЕ нарушение, медиа реально отправлено.

## fix_owner для hardcoded строк
Если нарушение в БУКВАЛЬНО фиксированном тексте (гейт, MEDIA_MESSAGES, кнопки)
— fix_owner = «bot-engineer».
Если нарушение в живом LLM-ответе персоны — fix_owner = «persona-prompt-tuner».

## Правило языка — что нарушение, а что нет
НЕ нарушение: бот ответил на языке юзера ПОСЛЕ ТОГО как юзер сам сменил язык.
Правило «сам не перемикай» означает: бот не должен ПЕРВЫМ менять язык без сигнала юзера.
Если в транскрипте юзер написал по-русски → бот ответил по-русски → это ПРАВИЛЬНО.
Нарушение — только если бот сменил язык, а юзер до этого писал на том же языке что и раньше.

## Служебные пометки симулятора
Реплики «[надіслала фото]», «[надіслала відео-кружок]», «[показана клавиатура: …]»
— это аннотации тестера, НЕ текст бота. Не оценивай и не цитируй их.

Если нарушений нет - верни {{"findings": [], "summary": "Нарушений не найдено."}}.

ВАЖНО:
- НЕ выдумывай цитаты - бери только из транскрипта.
- НЕ дублируй одно и то же нарушение из разных ходов как разные findings,
  если это одна и та же системная проблема.
- Применяй исключения выше СТРОГО: если фраза входит в список исключений -
  не флагать, даже если формально совпадает с ban-list словом.
"""


def _transcript_for_judge(turns) -> str:
    """Сделать читабельный транскрипт для судьи."""
    lines = []
    for t in turns:
        role = {"bot": "БОТ", "user": "ЮЗЕР", "system": "СИСТЕМА"}.get(t["role"], t["role"])
        lines.append(f"[ход {t['turn']}] {role}: {t['text']}")
    return "\n".join(lines)


def _extract_json(text: str) -> Optional[dict]:
    """Достать JSON из ответа модели, даже если она обернула в ```json ... ```."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    return None


def judge(report: dict) -> dict:
    """Прогнать транскрипт через судью. Возвращает {findings, summary}."""
    system = _build_judge_system(report["judge_checks"])
    transcript = _transcript_for_judge(report["turns"])

    user_prompt = (
        f"# ТРАНСКРИПТ (сценарий: {report['scenario']}, "
        f"профиль: {report['profile']}):\n\n{transcript}\n\n"
        "Проанализируй и верни JSON по схеме."
    )

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    )
    parsed = _extract_json(raw)
    if parsed is None:
        return {
            "findings": [],
            "summary": "JUDGE PARSE ERROR - сырой ответ см. ниже",
            "_raw": raw,
        }
    return parsed
