"""Прогон дуги отношений Мира↔юзер: фазы 1→2→3.

Один пользователь с фиксированной биографией (см. arc_bio.py). Между
фазами симулируем «промотку времени» прямой записью в БД:
  - Phase 1 «знакомство»: только что активировали, msgs < 25.
  - Phase 2 «сближение»: mira_activated_at = -2 дня (или msgs >= 25).
  - Phase 3 «своя»:       mira_activated_at = -5 дней + msgs >= 80.

После каждой фазы сохраняем сегмент транскрипта. В конце прогоняем
arc_analyzer на каждом сегменте, сравниваем фазы и рендерим HTML-
дашборд через arc_dashboard.

Запуск:
    COMPIA_TEST_MODE=1 python3 -m tester.relationship_arc
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from typing import List

import anthropic

from .arc_bio import ANDRIY_SYSTEM, USER_BIO, BIO_HOOKS
from .harness import Event, FakeMessage, install, restore
from .simulator import (
    TEST_UID, _bot_visible_events, _events_to_user_lines,
    _format_transcript_for_user_llm, _setup_isolated_db, _teardown_isolated_db,
    _simulate_gate_acceptance, _simulate_start_choice,
)

LOG = logging.getLogger("tester.arc")

USER_MODEL = "claude-haiku-4-5"

# Сколько ходов юзера на каждую фазу. Phase 1 включает онбординг до Миры
# плюс ~6 разговоров с Мирой.
TURNS_PER_PHASE = {
    1: 12,   # из них ~5 уходит на онбординг+гейт, ~7 с Мирой
    2: 9,
    3: 9,
}


def _ask_user(turns_for_llm: List[dict]) -> str:
    """Получить следующую реплику Андрея."""
    client = anthropic.Anthropic()
    if not turns_for_llm or turns_for_llm[0]["role"] != "user":
        turns_for_llm = [{"role": "user", "content": "(початок розмови)"}] + turns_for_llm
    resp = client.messages.create(
        model=USER_MODEL,
        max_tokens=220,
        system=ANDRIY_SYSTEM,
        messages=turns_for_llm,
    )
    text = "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()
    return text or "..."


def _fast_forward(days_ago: int, min_user_msgs: int):
    """Перемотать `mira_activated_at` назад и долить фейковых user-сообщений."""
    import database
    conn = sqlite3.connect(database.DB_PATH)
    conn.execute(
        "UPDATE users SET mira_activated_at = datetime('now', ?) "
        "WHERE user_id = ?",
        (f"-{days_ago} days", TEST_UID),
    )
    cur = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE user_id = ? AND role = 'user'",
        (TEST_UID,),
    )
    have = cur.fetchone()[0]
    to_add = max(0, min_user_msgs - have)
    for i in range(to_add):
        conn.execute(
            "INSERT INTO messages (user_id, role, content) "
            "VALUES (?, 'user', ?)",
            (TEST_UID, f"(пропущена розмова #{i+1})"),
        )
    conn.commit()
    conn.close()
    LOG.info(
        "  ⏩ FAST-FORWARD: mira_activated -%dd, дозалив %d фейкових user-msg "
        "(зараз %d)", days_ago, to_add, max(have, min_user_msgs),
    )


async def _drive_turn(
    turn_n: int, turns_all: List[dict], events_log: List[Event],
) -> List[dict]:
    """Один ход: спросить юзера, прогнать handle_message, собрать ответы."""
    import bot as bot_module

    user_text = _ask_user(_format_transcript_for_user_llm(turns_all))
    turns_all.append({"turn": turn_n, "role": "user", "text": user_text})
    LOG.info("[U] %s", user_text)

    msg = FakeMessage(TEST_UID, text=user_text, events=events_log)
    try:
        await bot_module.handle_message(msg)
    except Exception:
        LOG.exception("handle_message впав")
        return []

    new_events = _bot_visible_events(events_log)
    new_bot_lines = []
    for line in _events_to_user_lines(new_events):
        rec = {"turn": turn_n, "role": "bot", "text": line}
        turns_all.append(rec)
        new_bot_lines.append(rec)
        LOG.info("[B] %s", line)

    # Гейт?
    events_log.clear()
    if await _simulate_gate_acceptance(new_events, events_log):
        gate_events = _bot_visible_events(events_log)
        for line in _events_to_user_lines(gate_events):
            rec = {"turn": turn_n, "role": "bot", "text": line}
            turns_all.append(rec)
            new_bot_lines.append(rec)
            LOG.info("[B] %s", line)
    events_log.clear()
    return new_bot_lines


async def run_arc(keep_db: bool = False) -> dict:
    """Прогнать всю дугу. Вернуть структуру {bio, phase_segments, all_turns}."""
    LOG.info("=" * 60)
    LOG.info("RELATIONSHIP ARC: Андрій × Мира")
    LOG.info("=" * 60)

    _setup_isolated_db()
    import bot as bot_module
    import database

    events_log: List[Event] = []
    original_bot = install(events_log)

    turns_all: List[dict] = []
    phase_segments: dict = {1: [], 2: [], 3: []}
    started_at = time.time()

    try:
        # /start + клик «просто поговорити»
        start_msg = FakeMessage(TEST_UID, text="/start", events=events_log)
        await bot_module.handle_start(start_msg)
        new_events = _bot_visible_events(events_log)
        events_log.clear()
        for line in _events_to_user_lines(new_events):
            turns_all.append({"turn": 0, "role": "bot", "text": line})
            LOG.info("[B] %s", line)
        if await _simulate_start_choice(new_events, events_log):
            extra = _bot_visible_events(events_log)
            events_log.clear()
            for line in _events_to_user_lines(extra):
                turns_all.append({"turn": 0, "role": "bot", "text": line})
                LOG.info("[B] %s", line)

        # PHASE 1: знакомство (включая онбординг + переход в Миру).
        LOG.info("--- PHASE 1: знайомство ---")
        phase1_start_idx = len(turns_all)
        for n in range(1, TURNS_PER_PHASE[1] + 1):
            await _drive_turn(n, turns_all, events_log)
        phase_segments[1] = turns_all[phase1_start_idx:]

        # Промотка к Phase 2.
        _fast_forward(days_ago=2, min_user_msgs=26)

        LOG.info("--- PHASE 2: зближення ---")
        phase2_start_idx = len(turns_all)
        base = TURNS_PER_PHASE[1]
        for n in range(base + 1, base + TURNS_PER_PHASE[2] + 1):
            await _drive_turn(n, turns_all, events_log)
        phase_segments[2] = turns_all[phase2_start_idx:]

        # Промотка к Phase 3.
        _fast_forward(days_ago=5, min_user_msgs=82)

        LOG.info("--- PHASE 3: своя ---")
        phase3_start_idx = len(turns_all)
        base = TURNS_PER_PHASE[1] + TURNS_PER_PHASE[2]
        for n in range(base + 1, base + TURNS_PER_PHASE[3] + 1):
            await _drive_turn(n, turns_all, events_log)
        phase_segments[3] = turns_all[phase3_start_idx:]

    finally:
        restore(original_bot)

    elapsed = time.time() - started_at
    LOG.info("Дуга прогнана за %.1fs", elapsed)

    result = {
        "bio": USER_BIO,
        "phase_segments": {str(k): v for k, v in phase_segments.items()},
        "all_turns": turns_all,
        "elapsed_sec": round(elapsed, 1),
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }

    _teardown_isolated_db(keep_db)
    return result


def _dump_raw(result: dict) -> str:
    out_dir = "/tmp/compia_tester"
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"arc_{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return path


def main():
    os.environ["COMPIA_TEST_MODE"] = "1"
    os.environ.setdefault(
        "TELEGRAM_TOKEN", "123456789:AAEtestplaceholderAAAAAAAAAAAAAAAAAAA",
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    result = asyncio.run(run_arc(keep_db=False))
    raw_path = _dump_raw(result)
    print(f"\nСырой транскрипт: {raw_path}")

    from .arc_analyzer import analyze_arc
    print("Аналізую тон по фазах через Claude Sonnet...")
    analysis = analyze_arc(result)
    analysis_path = raw_path.replace(".json", "_analysis.json")
    with open(analysis_path, "w", encoding="utf-8") as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2)
    print(f"Аналіз: {analysis_path}")

    from .arc_dashboard import render_dashboard
    html_path = raw_path.replace(".json", "_dashboard.html")
    render_dashboard(result, analysis, html_path)
    print(f"Дашборд: {html_path}")


if __name__ == "__main__":
    main()
