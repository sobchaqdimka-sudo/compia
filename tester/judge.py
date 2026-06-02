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


def _build_judge_system(scenario_checks: list[str]) -> str:
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

Если нарушений нет - верни {{"findings": [], "summary": "Нарушений не найдено."}}.

ВАЖНО:
- НЕ выдумывай цитаты - бери только из транскрипта.
- НЕ дублируй одно и то же нарушение из разных ходов как разные findings,
  если это одна и та же системная проблема.
- Реплики, помеченные «[надіслала фото]», «[показана клавиатура: ...]» - это
  служебные пометки симулятора, НЕ оценивай их как контент бота.
"""


def _transcript_for_judge(turns: list[dict]) -> str:
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
