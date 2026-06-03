"""Одноразовый вызов run_checkins() с диагностикой кому пора и почему.

Скрипт делает ОДИН проход по БД, как это делает фоновый checkin_loop, и:
  1) показывает кому пора писать (по правилам get_due_checkin_users);
  2) для каждого юзера, который попал в список - показывает кто он;
  3) если попал не тот / никто не попал - объясняет, какое условие
     отфильтровало интересующего тебя user_id (через --diag);
  4) с твоего y/N подтверждения - РЕАЛЬНО отправляет в Telegram через
     рабочего aiogram-бота. После отправки - закрывает сессию.

Это НЕ запускает полноценного бота: long-polling, обработчики, ничего.
Только один проход checkins и выход.

Запуск (РЕАЛЬНАЯ отправка, реальный TELEGRAM_TOKEN из .env):
    python3 -m tester.checkin_send_once
    python3 -m tester.checkin_send_once --diag 8219878514
    python3 -m tester.checkin_send_once --only 8219878514 --yes
"""

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
from datetime import datetime


def _diag_user(db_path: str, user_id: int) -> None:
    """Сказать что get_due_checkin_users думает про конкретного user_id."""
    from config import CHECKIN_INTERVALS_HOURS

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT persona, checkin_freq, last_seen, last_checkin_at "
        "FROM users WHERE user_id = ?", (user_id,),
    ).fetchone()
    has_facts = conn.execute(
        "SELECT 1 FROM user_facts WHERE user_id = ?", (user_id,),
    ).fetchone() is not None
    conn.close()

    print(f"\n=== ДІАГНОСТИКА user_id={user_id} ===")
    if not row:
        print(f"  ❌ Юзера {user_id} немає в таблиці users.")
        return
    persona, freq, last_seen, last_checkin = row
    print(f"  persona:         {persona}")
    print(f"  checkin_freq:    {freq}")
    print(f"  last_seen:       {last_seen}")
    print(f"  last_checkin_at: {last_checkin}")
    print(f"  user_facts:      {'є' if has_facts else 'НЕМАЄ'}")

    fail = []
    if freq == "off":
        fail.append("checkin_freq='off' (виключено)")
    if last_seen is None:
        fail.append("last_seen IS NULL (юзер ще не писав)")
    if not has_facts:
        fail.append("немає user_facts (бот не зберіг досьє - пише тільки тим, кого пам'ятає)")

    hours = CHECKIN_INTERVALS_HOURS.get(freq)
    if hours is not None and last_seen:
        try:
            ls = datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S")
            hours_since = (datetime.utcnow() - ls).total_seconds() / 3600.0
            if hours_since < hours:
                fail.append(
                    f"мовчить лише {hours_since:.1f}г, поріг для '{freq}' = {hours}г"
                )
        except Exception:
            pass

    if last_checkin and last_seen and last_checkin >= last_seen:
        fail.append(
            f"last_checkin_at ({last_checkin}) >= last_seen ({last_seen}) - "
            f"бот вже писав першим, а юзер не повертався → пропуск"
        )

    if not fail:
        print("  ✅ Усі умови виконані - буде в списку.")
    else:
        print("  ❌ ЩО ВІДФІЛЬТРОВУЄ:")
        for f in fail:
            print(f"     - {f}")
    print()
    if last_checkin and last_seen and last_checkin >= last_seen:
        print("  💡 Швидкий fix: обнули last_checkin_at:")
        print(f"     sqlite3 {db_path} \"UPDATE users SET last_checkin_at=NULL "
              f"WHERE user_id={user_id}\"")


async def _main(args):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not os.getenv("TELEGRAM_TOKEN"):
        print("❌ TELEGRAM_TOKEN не задано в .env. Скрипт реально шле в Telegram, "
              "тож токен обов'язковий.")
        return 1

    import bot as bot_module
    import database

    db_path = database.DB_PATH

    if args.diag:
        _diag_user(db_path, args.diag)

    due = database.get_due_checkin_users()
    print(f"\n=== get_due_checkin_users(): {len(due)} юзерів ===")
    for uid, persona, _facts in due:
        marker = " ← наш" if args.only and uid == args.only else ""
        print(f"  user_id={uid:>15}  persona={persona}{marker}")

    if args.only:
        due = [t for t in due if t[0] == args.only]
        print(f"\nФільтр --only={args.only}: {len(due)} залишилось.")
        if not due:
            print(
                "Юзер не в списку. Запусти з --diag <user_id> щоб побачити "
                "яке саме правило його відфільтрувало."
            )
            await bot_module.bot.session.close()
            return 2

    if not due:
        await bot_module.bot.session.close()
        print("Нікому нічого не шлю - порожній список.")
        return 0

    if not args.yes:
        ans = input(f"\nВідправити {len(due)} проактивне(их) повідомлення? (y/N): ")
        if ans.strip().lower() != "y":
            print("Скасовано.")
            await bot_module.bot.session.close()
            return 0

    # Реальная отправка. Мы НЕ зовём bot.run_checkins() напрямую (если хочешь
    # отправить только --only). Воспроизводим её логику с явным списком.
    from ai import generate_checkin
    for uid, persona, facts in due:
        try:
            text = await asyncio.to_thread(generate_checkin, persona, facts)
            await bot_module.send_bubbles(uid, text)
            database.add_message(uid, "assistant", text)
            database.set_last_checkin(uid)
            print(f"✅ Відправлено user_id={uid}")
        except Exception as e:
            print(f"❌ user_id={uid}: {e}")
            logging.exception("send failed")

    await bot_module.bot.session.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description="One-shot checkin sender")
    parser.add_argument("--diag", type=int, default=None,
                        help="показати чому конкретний user_id потрапляє / не потрапляє в список")
    parser.add_argument("--only", type=int, default=None,
                        help="відправити ТІЛЬКИ цьому user_id (інших з due пропустити)")
    parser.add_argument("--yes", action="store_true",
                        help="не питати підтвердження - одразу шле")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
