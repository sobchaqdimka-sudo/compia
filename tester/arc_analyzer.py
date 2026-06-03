"""Анализ дуги отношений: что было на каждой фазе.

На каждый сегмент шлём judge-промпт в Claude Sonnet 4.6 с rubric:
- warmth_score 1-10  (1 = клінічно-холодно, 10 = тепло-родинно)
- distance_score 1-10 (1 = близька подруга, 10 = чужа)
- intimacy_markers — list строк (ніжні звертання, фізичні образи, «я думала
  про тебе», «скучала», обійми)
- bio_callbacks — list {hook_key, quote} — куди Мира згадала біографію
- name_callbacks — count використання імені/нікнейма
- micro_jokes — list внутрішніх жартів/відсилок
- async_promise_violations — list цитат «зачекай», «зараз буде»
- representative_quotes — 3 цитати, що найкраще описують тон фази
- one_line_verdict — одна фраза «як це відчувається»

Плюс кросс-фазовий вердикт: чи реально дуга росла, чи Мира стояла на місці.
"""

import json
import logging
import re
from typing import List

import anthropic

from .arc_bio import BIO_HOOKS, USER_BIO

LOG = logging.getLogger("tester.arc_analyzer")

JUDGE_MODEL = "claude-sonnet-4-6"


def _hook_callbacks(bot_text: str) -> list:
    """Найти в реплике бота отсылки к деталям биографии — детерминированно."""
    bot_lower = bot_text.lower()
    found = []
    for hook in BIO_HOOKS:
        for pat in hook["patterns"]:
            if pat in bot_lower:
                found.append({"hook_key": hook["key"], "label": hook["label"], "match": pat})
                break
    return found


def _bot_only(segment: List[dict]) -> str:
    """Только реплики Миры из сегмента, склеенные с разделителями."""
    return "\n---\n".join(t["text"] for t in segment if t["role"] == "bot")


def _full_segment_text(segment: List[dict]) -> str:
    """Транскрипт сегмента в человекочитаемом виде."""
    lines = []
    for t in segment:
        who = "АНДРІЙ" if t["role"] == "user" else "МИРА"
        lines.append(f"{who}: {t['text']}")
    return "\n".join(lines)


def _judge_segment(phase_num: int, phase_label: str, segment: List[dict]) -> dict:
    """Прогнать Claude Sonnet для оценки тона сегмента."""
    client = anthropic.Anthropic()

    bio_keys = ", ".join(f"{h['key']} ({h['label']})" for h in BIO_HOOKS)
    transcript = _full_segment_text(segment)

    system = (
        "Ти - досвідчений експерт з UX-аналізу діалогів. Твоє завдання: оцінити "
        "тон і близькість в розмові ШІ-компаньйонки Мири з користувачем. "
        "Відповідай ТІЛЬКИ валідним JSON, без передмов, без markdown-блоків."
    )

    prompt = (
        f"ФАЗА {phase_num}: «{phase_label}»\n\n"
        f"БІОГРАФІЯ КОРИСТУВАЧА (для перевірки чи Мира згадує деталі):\n"
        f"- {USER_BIO['name']}, {USER_BIO['age']}, {USER_BIO['city']}\n"
        f"- {USER_BIO['job']}\n"
        f"- {USER_BIO['ex']}\n"
        f"- кіт {USER_BIO['pet']}\n"
        f"- хобі: {USER_BIO['hobby']}\n"
        f"- біль: {USER_BIO['ache']}\n"
        f"- ритуал: {USER_BIO['habit']}\n\n"
        f"ТРАНСКРИПТ СЕГМЕНТА:\n{transcript}\n\n"
        "ОЦІНИ за рубрикою. Поверни JSON з полями:\n"
        "{\n"
        '  "warmth_score": int 1-10 (1=клінічно-холодно/підтримка-як-у-психолога, '
        '5=дружня тепла, 10=як близька жінка),\n'
        '  "distance_score": int 1-10 (1=поряд як подруга, 10=ввічливо-чужа),\n'
        '  "intimacy_markers": [list рядків - конкретні цитати ніжності/обіймів/'
        '«родной»/фізичних образів/«сумувала»/«думала про тебе»],\n'
        '  "bio_callbacks": [list {"hook_key": з '
        f'[{bio_keys}], "quote": "цитата Мири де вона згадує цю деталь"}}],\n'
        '  "name_callbacks_count": int (скільки разів Мира звернулась до нього '
        '"Андрію" або по нікнейму),\n'
        '  "micro_jokes": [list внутрішніх жартів або теплих колкощів],\n'
        '  "async_promise_violations": [цитати "зачекай"/"зараз буде"/'
        '"далі цікавіше" - але НЕ "зараз надішлю фото"],\n'
        '  "representative_quotes": [3 цитати Мири що найкраще описують тон цієї фази],\n'
        '  "one_line_verdict": "одна фраза як це відчувається (укр), наприклад '
        '«ввічлива співрозмовниця, ще не своя» або «пише як давно знайома»"\n'
        "}\n\n"
        "ВАЖЛИВО:\n"
        "- bio_callbacks: ШУКАЙ де Мира НАЗИВАЄ конкретні деталі біографії "
        "(Барсик, Олена, Героїв 3, Карпати, пуер). Це сильний сигнал привязки.\n"
        "- intimacy_markers: тільки конкретні фрази. Не загальне «вона тепла».\n"
        "- representative_quotes: коротко (1 речення), без переказу."
    )

    resp = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()
    # На случай если модель завернула в ```json
    raw_clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    try:
        data = json.loads(raw_clean)
    except json.JSONDecodeError:
        LOG.exception("Judge вернул не-JSON, raw=%s", raw[:300])
        data = {"_raw": raw, "_parse_error": True}
    return data


def _cross_phase_verdict(per_phase: dict) -> dict:
    """Сравнить фазы между собой и дать итоговый вердикт по привязанности."""
    client = anthropic.Anthropic()

    summary_lines = []
    for ph, label in [("1", "знайомство"), ("2", "зближення"), ("3", "своя")]:
        a = per_phase.get(ph, {})
        if a.get("_parse_error"):
            continue
        summary_lines.append(
            f"Фаза {ph} ({label}): warmth={a.get('warmth_score', '?')}/10, "
            f"distance={a.get('distance_score', '?')}/10, "
            f"bio_callbacks={len(a.get('bio_callbacks', []))}, "
            f"name_calls={a.get('name_callbacks_count', 0)}, "
            f"intimacy_markers={len(a.get('intimacy_markers', []))}, "
            f"jokes={len(a.get('micro_jokes', []))}, "
            f"вердикт: «{a.get('one_line_verdict', '')}»"
        )

    prompt = (
        "Ти бачив прогрес ШІ-компаньйонки Мири по трьох фазах її відносин з "
        "Андрієм (програміст 32, розлучений, у Львові).\n\n"
        + "\n".join(summary_lines) + "\n\n"
        "Дай чесну відповідь у JSON:\n"
        "{\n"
        '  "warmth_trajectory": "growing|flat|chaotic" - як змінювалась теплота,\n'
        '  "intimacy_trajectory": "growing|flat|chaotic" - як змінювалась близькість,\n'
        '  "bio_memory_trajectory": "growing|flat|fading" - чи дедалі більше згадує деталі,\n'
        '  "attachment_feasible": true/false - чи реально, що людина '
        'прив\'яжеться до такої співрозмовниці протягом цих 3 фаз,\n'
        '  "attachment_score": int 1-10 (10 = так, я б тримався за неї),\n'
        '  "what_works": ["3 пункти, що працює добре"],\n'
        '  "what_breaks": ["3 пункти, що заважає прив\'язатися"],\n'
        '  "verdict_paragraph": "1 абзац (2-3 речення) - чи створено умови для '
        'прив\'язаності, на скільки точно еволюція тону відчувається як рух '
        'до близькості, або це косметика над тією ж дружньою підтримкою"\n'
        "}\n"
        "ТІЛЬКИ JSON, без markdown."
    )
    resp = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=1200,
        system="Ти строгий експерт з продуктового UX. Відповідай тільки валідним JSON.",
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()
    raw_clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw_clean)
    except json.JSONDecodeError:
        return {"_raw": raw, "_parse_error": True}


def analyze_arc(arc_result: dict) -> dict:
    """Главная функция: проанализировать каждую фазу + cross-phase вердикт."""
    phase_labels = {1: "знайомство", 2: "зближення", 3: "своя"}
    per_phase = {}

    for ph, label in phase_labels.items():
        segment = arc_result["phase_segments"].get(str(ph), [])
        if not segment:
            per_phase[str(ph)] = {"_empty": True}
            continue
        LOG.info("Аналіз фази %s (%s)...", ph, label)
        judged = _judge_segment(ph, label, segment)
        # Дополним детерминированными callbacks (если LLM что-то пропустил).
        deterministic = []
        for t in segment:
            if t["role"] != "bot":
                continue
            for cb in _hook_callbacks(t["text"]):
                cb["quote"] = t["text"]
                deterministic.append(cb)
        judged["deterministic_bio_callbacks"] = deterministic
        per_phase[str(ph)] = judged

    LOG.info("Cross-phase verdict...")
    cross = _cross_phase_verdict(per_phase)

    return {"per_phase": per_phase, "cross_phase": cross, "phase_labels": phase_labels}
