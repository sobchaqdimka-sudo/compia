"""Dry-run проактивного сообщения («бот пишет первым»).

Вызывает `ai.generate_checkin` для конкретного user_id и печатает что бы
Мира/друг/коуч написал(а), НЕ отправляя ни в Telegram, ни в БД. Это
безопасный способ посмотреть тон check-in'а до реального запуска.

Источники досье и персоны:
1. Если есть реальная companion.db с этим user_id - читаем оттуда.
2. Если нет - используем дефолтную romantic-Мира + пустое досье
   (можно расширить через --facts-file).

Запуск:
    COMPIA_TEST_MODE=1 python3 -m tester.checkin_dry_run --user-id 8219878514
    COMPIA_TEST_MODE=1 python3 -m tester.checkin_dry_run --user-id 8219878514 --variants 3
    COMPIA_TEST_MODE=1 python3 -m tester.checkin_dry_run --user-id 8219878514 --persona mira
"""

import argparse
import logging
import os
import sqlite3
import sys
from typing import Optional, Tuple


def _read_user(db_path: str, user_id: int) -> Optional[Tuple[str, str]]:
    """Прочитать (persona, facts) из companion.db. None если юзера нет / БД нет."""
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT persona FROM users WHERE user_id = ?", (user_id,),
        ).fetchone()
        if not row:
            return None
        persona = row[0] or "mira"
        f_row = conn.execute(
            "SELECT facts FROM user_facts WHERE user_id = ?", (user_id,),
        ).fetchone()
        facts = f_row[0] if f_row else ""
        return persona, facts
    finally:
        conn.close()


def _print_facts_summary(facts: str) -> None:
    if not facts.strip():
        print("  (досье порожнє)")
        return
    for line in facts.strip().splitlines()[:25]:
        print(f"  {line}")
    rest = max(0, len(facts.splitlines()) - 25)
    if rest:
        print(f"  ... +{rest} рядків")


def main():
    parser = argparse.ArgumentParser(description="Compia checkin dry-run")
    parser.add_argument("--user-id", type=int, required=True,
                        help="реальный telegram user_id для якого згенерувати")
    parser.add_argument("--db", default="companion.db",
                        help="шлях до companion.db (default: companion.db)")
    parser.add_argument("--persona", default=None,
                        help="перевизначити персону: mira / friend / coach / onboarding")
    parser.add_argument("--variants", type=int, default=2,
                        help="скільки варіантів згенерувати (default 2)")
    parser.add_argument("--facts-file", default=None,
                        help="взяти досьє з файлу (інакше з БД або порожнє)")
    args = parser.parse_args()

    os.environ["COMPIA_TEST_MODE"] = "1"
    os.environ.setdefault(
        "TELEGRAM_TOKEN", "123456789:AAEtestplaceholderAAAAAAAAAAAAAAAAAAA",
    )
    logging.basicConfig(level=logging.WARNING)

    from ai import generate_checkin
    from .cost import install_tracker, reset, summary

    user = _read_user(args.db, args.user_id)
    persona = args.persona
    facts = ""
    if user is not None:
        db_persona, db_facts = user
        persona = persona or db_persona
        facts = db_facts
    else:
        persona = persona or "mira"
        print(
            f"⚠ user_id={args.user_id} не знайдено в {args.db}. "
            f"Беру дефолти: persona='{persona}', порожнє досьє.\n"
        )
    if args.facts_file:
        with open(args.facts_file, encoding="utf-8") as f:
            facts = f.read()

    print("=" * 70)
    print(f"DRY-RUN check-in")
    print(f"user_id: {args.user_id}")
    print(f"persona: {persona}")
    print(f"досьє:")
    _print_facts_summary(facts or "")
    print("=" * 70)
    print()

    install_tracker()
    reset()

    for i in range(1, args.variants + 1):
        try:
            text = generate_checkin(persona, facts)
        except Exception as e:
            print(f"❌ Варіант {i}: помилка генерації — {e}")
            continue
        print(f"--- Варіант {i} ---")
        print(text.strip())
        print()

    s = summary()
    t = s["totals"]
    print("=" * 70)
    print(f"Вартість dry-run: ${t['cost_usd']:.5f} "
          f"({t['calls']} викликів, in={t['input']}, out={t['output']})")
    print(f"≈ ${t['cost_usd']/max(1,args.variants):.5f} за один згенерований варіант")
    print()
    print("⚠ Це БУЛО dry-run. У Telegram нічого не відправлено, в БД нічого не записано.")
    print("Щоб реально відправити - використай команду run_checkins() в боті")
    print("(вона сама обере кому пора, базуючись на checkin_freq і last_seen).")


if __name__ == "__main__":
    main()
