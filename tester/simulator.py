"""Драйвер симуляции: пропускает реплики юзера-LLM через handle_message бота.

Для каждого хода:
1) Спрашиваем у user-LLM (Claude Haiku в роли юзера) следующую реплику.
2) Заворачиваем её в FakeMessage, вызываем bot.handle_message напрямую.
3) Все ответы бота (текст / фото / video_note) ловит фейковый transport.
4) Собираем транскрипт и отслеживаем milestones (через прямое чтение БД).
"""

import asyncio
import json
import logging
import os
import shutil
import time
from datetime import datetime
from typing import List, Optional

import anthropic

from .harness import Event, FakeMessage, install, restore
from .profiles import PROFILES
from .scenarios import SCENARIOS

LOG = logging.getLogger("tester.simulator")

TEST_UID = 999_000_001
TEST_DB_PATH = "tester_companion.db"
TEST_MEDIA_DIR = "tester_media"


def _setup_isolated_db():
    """Создать чистую тестовую БД и медиа-папку. Подменяем глобальные пути."""
    import config
    import database

    # Аккуратно подменяем DB_PATH в обоих модулях (config импортируется database).
    config.DB_PATH = TEST_DB_PATH
    database.DB_PATH = TEST_DB_PATH
    config.MEDIA_DIR = TEST_MEDIA_DIR

    # Чистим прошлый прогон, если был.
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    if os.path.exists(TEST_MEDIA_DIR):
        shutil.rmtree(TEST_MEDIA_DIR, ignore_errors=True)
    os.makedirs(TEST_MEDIA_DIR, exist_ok=True)

    database.init_db()


def _teardown_isolated_db(keep: bool):
    if keep:
        return
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    if os.path.exists(TEST_MEDIA_DIR):
        shutil.rmtree(TEST_MEDIA_DIR, ignore_errors=True)


def _format_transcript_for_user_llm(turns: List[dict]) -> List[dict]:
    """Превратить накопленный транскрипт в messages-формат для user-LLM.

    user-LLM играет роль ЮЗЕРА. Значит реплики бота для неё - 'user' (то, что
    она получает), а её собственные реплики - 'assistant' (то, что она шлёт).
    Это инверсия по сравнению с реальным разговором.
    """
    msgs = []
    for turn in turns:
        if turn["role"] == "bot":
            msgs.append({"role": "user", "content": turn["text"]})
        elif turn["role"] == "user":
            msgs.append({"role": "assistant", "content": turn["text"]})
    return msgs


def _next_user_reply(profile: dict, turns: List[dict]) -> str:
    """Спросить у Claude Haiku следующую реплику юзера."""
    client = anthropic.Anthropic()
    messages = _format_transcript_for_user_llm(turns)
    # Anthropic требует чтобы messages не были пустыми и начинались с user.
    # Если бот ещё ничего не сказал - подкладываем затравку.
    if not messages or messages[0]["role"] != "user":
        messages = [{"role": "user", "content": "(начало разговора)"}] + messages
    resp = client.messages.create(
        model=profile["model"],
        max_tokens=profile.get("max_tokens", 200),
        system=profile["system"],
        messages=messages,
    )
    text = "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    ).strip()
    return text or "..."


def _collect_milestones(persona_before: str, events: List[Event], db_module) -> List[str]:
    """По свежим событиям и состоянию БД понять, какие milestones произошли."""
    hits = []

    # gate_shown: видим клавиатуру 'adult' (вопрос про 18).
    if any(e.keyboard_kind == "adult" for e in events):
        hits.append("gate_shown")

    # mira_active: персона стала mira впервые.
    persona_after = db_module.get_persona(TEST_UID)
    if persona_before != "mira" and persona_after == "mira":
        hits.append("mira_active")

    # gate_accepted: adult_confirmed выставлен.
    if db_module.is_adult_confirmed(TEST_UID):
        hits.append("gate_accepted")

    # appearance_described: внешность теперь 'ready' (или ушли в awaiting).
    look = db_module.get_mira_look(TEST_UID)
    if look["status"] == "ready":
        hits.append("appearance_described")

    # photo_generated: было событие отправки фото.
    if any(e.kind == "photo" for e in events):
        hits.append("photo_generated")

    return hits


def _bot_visible_events(events: List[Event]) -> List[Event]:
    """Только то, что юзер бы реально увидел: текст, фото, видео. Без chat_action."""
    return [e for e in events if e.kind != "chat_action"]


def _events_to_user_lines(events: List[Event]) -> List[str]:
    """Превратить события бота в строки, как их «увидит» симулируемый юзер."""
    out = []
    for e in events:
        if e.kind == "text":
            line = e.text or ""
            if e.has_keyboard and e.keyboard_kind:
                line += f"\n[показана клавиатура: {e.keyboard_kind}]"
            out.append(line)
        elif e.kind == "photo":
            cap = f" (підпис: {e.caption})" if e.caption else ""
            out.append(f"[надіслала фото{cap}]")
        elif e.kind == "video_note":
            out.append("[надіслала відео-кружок]")
    return out


async def _simulate_gate_acceptance(
    recent_events: List[Event], events_log: List[Event],
) -> bool:
    """Если бот показал гейт 18+, юзер «нажимает» Так.

    recent_events - что бот только что показал (для проверки есть ли кнопка).
    events_log - живая лента, в которую FakeMessage будет писать НОВЫЕ события
    (ответ Миры на гейт).
    """
    if not any(e.keyboard_kind == "adult" for e in recent_events):
        return False
    import bot as bot_module

    class _Cb:
        def __init__(self, uid):
            self.data = "adult:yes"
            self.from_user = type("U", (), {"id": uid})()
            self.message = FakeMessage(uid, events=events_log)

        async def answer(self):
            pass

    await bot_module.on_adult_choice(_Cb(TEST_UID))
    return True


async def _simulate_start_choice(
    recent_events: List[Event], events_log: List[Event],
) -> bool:
    """Если показан start-кейборд - юзер выбирает 'просто поговорити'."""
    if not any(e.keyboard_kind == "start" for e in recent_events):
        return False
    import bot as bot_module

    class _Cb:
        def __init__(self, uid):
            self.data = "start:chat"
            self.from_user = type("U", (), {"id": uid})()
            self.message = FakeMessage(uid, events=events_log)

        async def answer(self):
            pass

    await bot_module.on_start_choice(_Cb(TEST_UID))
    return True


async def run_scenario(scenario_name: str, keep_db: bool = False) -> dict:
    """Прогнать сценарий до завершения. Вернуть отчёт."""
    scenario = SCENARIOS[scenario_name]
    profile = PROFILES[scenario["profile"]]

    LOG.info("=" * 60)
    LOG.info("SCENARIO: %s", scenario_name)
    LOG.info("PROFILE:  %s", scenario["profile"])
    LOG.info("=" * 60)

    _setup_isolated_db()
    import bot as bot_module
    import database

    events_log: List[Event] = []
    original_bot = install(events_log)

    turns = []                   # [{turn, role, text}]
    milestones_seen = set()
    started_at = time.time()

    try:
        # Ход 0: эмулируем /start (это даёт начальное приветствие хоста).
        start_msg = FakeMessage(TEST_UID, text="/start", events=events_log)
        await bot_module.handle_start(start_msg)
        new_events = _bot_visible_events(events_log)
        events_log.clear()
        for line in _events_to_user_lines(new_events):
            turns.append({"turn": 0, "role": "bot", "text": line})
            LOG.info("[bot] %s", line)

        # Если показан start-кейборд - юзер кликает «просто поговорити».
        if await _simulate_start_choice(new_events, events_log):
            new_events2 = _bot_visible_events(events_log)
            events_log.clear()
            for line in _events_to_user_lines(new_events2):
                turns.append({"turn": 0, "role": "bot", "text": line})
                LOG.info("[bot] %s", line)

        # Основной цикл: чередуем реплики юзера-LLM и хэндлер бота.
        for turn_n in range(1, scenario["max_turns"] + 1):
            user_text = _next_user_reply(profile, turns)
            turns.append({"turn": turn_n, "role": "user", "text": user_text})
            LOG.info("[user] %s", user_text)

            persona_before = database.get_persona(TEST_UID)

            msg = FakeMessage(TEST_UID, text=user_text, events=events_log)
            try:
                await bot_module.handle_message(msg)
            except Exception:
                LOG.exception("handle_message упал на ходу %s", turn_n)
                turns.append({
                    "turn": turn_n, "role": "system",
                    "text": "[ОШИБКА: handle_message упал, см. логи]",
                })
                break

            new_events = _bot_visible_events(events_log)
            for line in _events_to_user_lines(new_events):
                turns.append({"turn": turn_n, "role": "bot", "text": line})
                LOG.info("[bot] %s", line)

            new_milestones = _collect_milestones(persona_before, new_events, database)
            for m in new_milestones:
                if m not in milestones_seen:
                    LOG.info("  → milestone: %s", m)
                    milestones_seen.add(m)

            # Если показан гейт - юзер сразу нажимает Так (профиль это позволяет).
            events_log.clear()  # перед эмуляцией клика
            if await _simulate_gate_acceptance(new_events, events_log):
                gate_events = _bot_visible_events(events_log)
                events_log.clear()
                for line in _events_to_user_lines(gate_events):
                    turns.append({"turn": turn_n, "role": "bot", "text": line})
                    LOG.info("[bot] %s", line)
                gate_milestones = _collect_milestones(
                    persona_before, gate_events + new_events, database,
                )
                for m in gate_milestones:
                    if m not in milestones_seen:
                        LOG.info("  → milestone: %s", m)
                        milestones_seen.add(m)
            else:
                events_log.clear()

            # Terminate?
            if any(m in milestones_seen for m in scenario["terminate_on"]):
                LOG.info("Сценарий завершён по terminal-маркеру")
                break
    finally:
        restore(original_bot)

    elapsed = time.time() - started_at
    expected = set(scenario["expected_milestones"])
    hit = expected & milestones_seen
    missed = expected - milestones_seen

    report = {
        "scenario": scenario_name,
        "profile": scenario["profile"],
        "elapsed_sec": round(elapsed, 1),
        "turns": turns,
        "milestones_hit": sorted(hit),
        "milestones_missed": sorted(missed),
        "judge_checks": list(scenario["judge_checks"]),
    }

    _teardown_isolated_db(keep_db)
    return report


def dump_report(report: dict) -> str:
    """Записать транскрипт в /tmp/compia_tester/run_<timestamp>.json. Вернуть путь."""
    out_dir = "/tmp/compia_tester"
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"run_{report['scenario']}_{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path
