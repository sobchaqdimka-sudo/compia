"""Продакт-дашборд для Compia.

Читает реальную базу `companion.db` (или указанную через --db) и собирает
визуальную статистику:

  - всего юзеров
  - разбивка по выбранной персоне (friend / coach / mira / onboarding)
  - среднее сообщений на юзера, медиана, p90
  - сессии (= куски разговора без пауз > N минут, по умолчанию 30)
  - оценка стоимости одной сессии (по средним токенам на сообщение)

Запуск:
    python3 -m tester.product_dashboard
    python3 -m tester.product_dashboard --db companion.db --gap-minutes 30

Файл выходит в `/tmp/compia_tester/product_dashboard_<timestamp>.html`.

Когда понадобится автоматизация — этот скрипт можно дёргать раз в N часов
(cron / GitHub Actions / простой systemd timer) и публиковать готовый HTML
на статический хостинг. Сейчас полностью локальный.
"""

import argparse
import html as _html
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

DEFAULT_DB = "companion.db"
OUT_DIR = "/tmp/compia_tester"

# Оценка стоимости одной сессии. Берём среднюю на 1 сообщение бота, исходя
# из реального прогона дуги (см. last-run в tester.cost). Если хочешь точнее
# - запусти `python3 -m tester.relationship_arc` и посмотри cost-блок.
# Здесь дефолт безопасный: считаем что одно полное сообщение бота с учётом
# истории + кэша обходится ~$0.005 (Sonnet 4.6).
ASSUMED_COST_PER_BOT_MSG_USD = 0.005


PERSONA_LABELS = {
    "onboarding": "Хост (онбординг)",
    "friend":     "Друг",
    "coach":      "Коуч",
    "mira":       "Міра",
}
PERSONA_COLORS = {
    "onboarding": "#a9b8c4",
    "friend":     "#f0a04b",
    "coach":      "#5e9ca6",
    "mira":       "#e85a8e",
}


def _esc(s) -> str:
    return _html.escape(str(s)) if s is not None else ""


def _connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        sys.exit(
            f"Базы {db_path} не существует. Запусти бота хоть раз, чтобы появилась.\n"
            f"Или укажи путь через --db <path>."
        )
    return sqlite3.connect(db_path)


# ---------------- метрики -----------------

def _fetch_user_personas(conn) -> Counter:
    rows = conn.execute("SELECT persona, COUNT(*) FROM users GROUP BY persona").fetchall()
    return Counter({p: c for p, c in rows})


def _fetch_total_users(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def _fetch_active_last(conn, days: int) -> int:
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return conn.execute(
        "SELECT COUNT(*) FROM users WHERE last_seen IS NOT NULL AND last_seen >= ?",
        (cutoff,),
    ).fetchone()[0]


def _fetch_message_stats(conn) -> Dict[str, float]:
    rows = conn.execute(
        "SELECT user_id, COUNT(*) FROM messages WHERE role='user' GROUP BY user_id"
    ).fetchall()
    counts = [c for _, c in rows]
    if not counts:
        return {"users_with_msgs": 0, "avg": 0, "median": 0, "p90": 0,
                "total_user_msgs": 0, "total_bot_msgs": 0}
    bot_total = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE role='assistant'"
    ).fetchone()[0]
    counts_sorted = sorted(counts)
    p90_idx = max(0, int(len(counts_sorted) * 0.9) - 1)
    return {
        "users_with_msgs": len(counts),
        "avg": round(statistics.mean(counts), 1),
        "median": int(statistics.median(counts)),
        "p90": counts_sorted[p90_idx],
        "total_user_msgs": sum(counts),
        "total_bot_msgs": bot_total,
    }


def _fetch_sessions(conn, gap_minutes: int) -> Dict[str, float]:
    """Сессия = непрерывная цепочка сообщений ОДНОГО юзера без пауз > gap_minutes.

    Считаем по таблице messages, читаем созданные timestamps и группируем
    каждый user_id отдельно. Возвращаем агрегаты + распределение по длине.
    """
    rows = conn.execute(
        "SELECT user_id, role, created_at FROM messages "
        "WHERE role IN ('user','assistant') ORDER BY user_id, created_at"
    ).fetchall()
    by_user: Dict[int, List[Tuple[str, datetime]]] = defaultdict(list)
    for uid, role, ts in rows:
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        by_user[uid].append((role, dt))

    sessions: List[Dict] = []
    for uid, msgs in by_user.items():
        current = []
        prev_dt = None
        for role, dt in msgs:
            if prev_dt and (dt - prev_dt) > timedelta(minutes=gap_minutes):
                if current:
                    sessions.append(_summ_session(uid, current))
                current = []
            current.append((role, dt))
            prev_dt = dt
        if current:
            sessions.append(_summ_session(uid, current))

    if not sessions:
        return {"count": 0, "avg_msgs": 0, "avg_minutes": 0, "avg_bot_msgs": 0,
                "sessions_per_user": 0, "total_sessions": 0}

    return {
        "count": len(sessions),
        "total_sessions": len(sessions),
        "avg_msgs": round(statistics.mean([s["msg_count"] for s in sessions]), 1),
        "avg_bot_msgs": round(statistics.mean([s["bot_msgs"] for s in sessions]), 1),
        "avg_minutes": round(statistics.mean([s["minutes"] for s in sessions]), 1),
        "sessions_per_user": round(len(sessions) / max(1, len(by_user)), 2),
    }


def _summ_session(uid: int, msgs: List[Tuple[str, datetime]]) -> Dict:
    minutes = (msgs[-1][1] - msgs[0][1]).total_seconds() / 60.0
    return {
        "user_id": uid,
        "msg_count": len(msgs),
        "bot_msgs": sum(1 for r, _ in msgs if r == "assistant"),
        "minutes": round(minutes, 1),
    }


def _fetch_persona_messages(conn) -> Counter:
    """Сколько сообщений ассистента по персонам (приближённо: по persona юзера).

    В таблице messages нет поля persona, так что берём текущую персону юзера
    и приписываем все его assistant-сообщения к ней. Это неидеально (юзер
    мог проходить через несколько персон) но даёт хорошую первую картину.
    """
    rows = conn.execute(
        "SELECT u.persona, COUNT(*) "
        "FROM messages m JOIN users u ON u.user_id = m.user_id "
        "WHERE m.role = 'assistant' GROUP BY u.persona"
    ).fetchall()
    return Counter({p or "(?)": c for p, c in rows})


def _fetch_funnel(conn) -> Dict[str, int]:
    """Воронка: онбординг → друг/коуч → Мира → фото."""
    return {
        "in_onboarding": conn.execute(
            "SELECT COUNT(*) FROM users WHERE persona = 'onboarding'"
        ).fetchone()[0],
        "in_friend": conn.execute(
            "SELECT COUNT(*) FROM users WHERE persona = 'friend'"
        ).fetchone()[0],
        "in_coach": conn.execute(
            "SELECT COUNT(*) FROM users WHERE persona = 'coach'"
        ).fetchone()[0],
        "in_mira": conn.execute(
            "SELECT COUNT(*) FROM users WHERE persona = 'mira'"
        ).fetchone()[0],
        "adult_confirmed": conn.execute(
            "SELECT COUNT(*) FROM users WHERE adult_confirmed = 1"
        ).fetchone()[0],
        "adult_declined": conn.execute(
            "SELECT COUNT(*) FROM users WHERE adult_declined_at IS NOT NULL"
        ).fetchone()[0],
        "with_mira_photo": conn.execute(
            "SELECT COUNT(*) FROM users WHERE photos_made > 0"
        ).fetchone()[0],
        "with_mira_nickname": conn.execute(
            "SELECT COUNT(*) FROM users WHERE mira_name_status = 'nicknamed'"
        ).fetchone()[0],
    }


# ---------------- HTML -----------------

CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       margin: 0; padding: 0; background: #fafafa; color: #2c2c2c; font-size: 14px;
       line-height: 1.5; }
.container { max-width: 1400px; margin: 0 auto; padding: 24px; }
h1 { font-size: 28px; margin: 0 0 6px; }
h2 { font-size: 18px; margin: 18px 0 12px; }
.subtitle { color: #888; margin: 0 0 24px; font-size: 13px; }
.grid { display: grid; gap: 16px; }
.grid-3 { grid-template-columns: repeat(3, 1fr); }
.grid-4 { grid-template-columns: repeat(4, 1fr); }
.card { background: #fff; padding: 18px 22px; border-radius: 12px;
        box-shadow: 0 2px 12px rgba(0,0,0,0.05); }
.big-num { font-size: 36px; font-weight: 700; line-height: 1; margin: 4px 0; }
.big-num span { font-size: 14px; color: #aaa; font-weight: 400; }
.card-label { font-size: 11px; text-transform: uppercase; color: #888;
              letter-spacing: 0.5px; }
.persona-row { display: flex; align-items: center; gap: 12px; margin: 10px 0; }
.persona-name { width: 160px; font-weight: 600; }
.persona-bar { flex: 1; height: 24px; background: #f0f0f0; border-radius: 4px;
               position: relative; overflow: hidden; }
.persona-fill { position: absolute; left:0; top:0; bottom:0; }
.persona-count { width: 80px; text-align: right; font-variant-numeric: tabular-nums; }
.funnel-step { display: flex; justify-content: space-between; padding: 8px 14px;
               background: #f7f7f9; border-radius: 6px; margin: 4px 0;
               font-variant-numeric: tabular-nums; }
.funnel-step strong { font-size: 18px; }
.cost-note { font-size: 12px; color: #888; margin-top: 12px;
             padding: 12px; background: #fff8e1; border-radius: 6px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid #eee; }
th { background: #f7f7f9; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
@media (max-width: 900px) {
  .grid-3, .grid-4 { grid-template-columns: 1fr 1fr; }
}
"""


def _stat_card(label: str, num, sub: str = "") -> str:
    return f"""
<div class="card">
  <div class="card-label">{_esc(label)}</div>
  <div class="big-num">{_esc(num)}</div>
  <div class="card-label" style="margin-top:6px; text-transform:none; color:#666">{_esc(sub)}</div>
</div>
"""


def _persona_breakdown(personas: Counter, total: int) -> str:
    rows = []
    for key in ("onboarding", "friend", "coach", "mira"):
        n = personas.get(key, 0)
        pct = (n * 100 // total) if total else 0
        color = PERSONA_COLORS.get(key, "#999")
        rows.append(f"""
<div class="persona-row">
  <div class="persona-name">{_esc(PERSONA_LABELS.get(key, key))}</div>
  <div class="persona-bar">
    <div class="persona-fill" style="width:{pct}%; background:{color}"></div>
  </div>
  <div class="persona-count">{n} <span style="color:#888">({pct}%)</span></div>
</div>""")
    # Других персон в норме нет, но если появятся - покажем.
    for k, n in personas.items():
        if k in PERSONA_LABELS:
            continue
        pct = (n * 100 // total) if total else 0
        rows.append(f"""
<div class="persona-row">
  <div class="persona-name">{_esc(k)} (?)</div>
  <div class="persona-bar"><div class="persona-fill" style="width:{pct}%; background:#999"></div></div>
  <div class="persona-count">{n}</div>
</div>""")
    return f'<div class="card"><h2>Розподіл по компаньйонах</h2>{"".join(rows)}</div>'


def _funnel_card(funnel: Dict[str, int], total: int) -> str:
    steps = [
        ("В онбордингу зараз", funnel["in_onboarding"]),
        ("Підтвердили 18+", funnel["adult_confirmed"]),
        ("Відмовились від 18+", funnel["adult_declined"]),
        ("Дійшли до Міри (поточна персона)", funnel["in_mira"]),
        ("Дали Мірі ім'я (nicknamed)", funnel["with_mira_nickname"]),
        ("Отримали хоча б 1 фото Міри", funnel["with_mira_photo"]),
    ]
    rows = [
        f'<div class="funnel-step"><span>{_esc(name)}</span>'
        f'<strong>{val}</strong></div>'
        for name, val in steps
    ]
    return f'<div class="card"><h2>Воронка ключових кроків</h2>{"".join(rows)}</div>'


def _sessions_card(sess: Dict[str, float], cost_per_session: float) -> str:
    if sess["count"] == 0:
        return '<div class="card"><h2>Сесії</h2><p>Поки немає даних.</p></div>'
    return f"""
<div class="card">
  <h2>Сесії (gap > {sess.get('gap_minutes', 30)} хв = нова сесія)</h2>
  <div class="grid grid-4">
    {_stat_card("Всього сесій", sess["count"])}
    {_stat_card("Сесій на юзера", sess["sessions_per_user"])}
    {_stat_card("Середня тривалість", f"{sess['avg_minutes']} хв")}
    {_stat_card("Повідомлень за сесію", f"{sess['avg_msgs']}")}
  </div>
  <div class="cost-note">
    <strong>Оцінка вартості одної сесії:</strong>
    {sess['avg_bot_msgs']} відповідей бота × ~${ASSUMED_COST_PER_BOT_MSG_USD:.5f} =
    <strong>≈ ${cost_per_session:.4f}</strong> за сесію.<br>
    <span style="font-size:11px">
    Дефолтна ставка $/повідомлення взята з прогону <code>tester.relationship_arc</code>
    (Sonnet 4.6, з кешем). Точне число — в дашборді дуги.
    </span>
  </div>
</div>
"""


def render(db_path: str, gap_minutes: int) -> str:
    """Собрать данные из БД и записать HTML. Вернуть путь."""
    conn = _connect(db_path)
    try:
        total_users = _fetch_total_users(conn)
        personas = _fetch_user_personas(conn)
        active_7d = _fetch_active_last(conn, 7)
        active_30d = _fetch_active_last(conn, 30)
        msg_stats = _fetch_message_stats(conn)
        sessions = _fetch_sessions(conn, gap_minutes)
        sessions["gap_minutes"] = gap_minutes
        funnel = _fetch_funnel(conn)
        bot_per_persona = _fetch_persona_messages(conn)
    finally:
        conn.close()

    cost_per_session = sessions.get("avg_bot_msgs", 0) * ASSUMED_COST_PER_BOT_MSG_USD

    top_cards = (
        _stat_card("Всього юзерів", total_users)
        + _stat_card("Активні за 7 днів", active_7d,
                     f"{active_7d * 100 // max(1, total_users)}% від бази")
        + _stat_card("Активні за 30 днів", active_30d,
                     f"{active_30d * 100 // max(1, total_users)}% від бази")
        + _stat_card("Юзерів з повідомленнями", msg_stats["users_with_msgs"])
    )

    msg_cards = (
        _stat_card("Сер. повідомлень / юзер", msg_stats["avg"])
        + _stat_card("Медіана", msg_stats["median"])
        + _stat_card("P90", msg_stats["p90"])
        + _stat_card("Всього user-msg", f"{msg_stats['total_user_msgs']:,}")
    )

    bot_per_rows = []
    for key in ("onboarding", "friend", "coach", "mira"):
        n = bot_per_persona.get(key, 0)
        bot_per_rows.append(
            f'<tr><td>{_esc(PERSONA_LABELS.get(key, key))}</td>'
            f'<td class="num">{n:,}</td></tr>'
        )

    html = f"""<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<title>Compia · продакт-метрики</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">
  <h1>Compia · продакт-дашборд</h1>
  <p class="subtitle">
    Згенеровано {_esc(datetime.utcnow().isoformat(timespec='seconds') + 'Z')} ·
    база: <code>{_esc(db_path)}</code> · поріг сесії: {gap_minutes} хв.
  </p>

  <div class="grid grid-4">{top_cards}</div>

  <div style="margin-top:24px" class="grid grid-3">
    {_persona_breakdown(personas, total_users)}
    {_funnel_card(funnel, total_users)}
    <div class="card">
      <h2>Відповіді бота по персоні</h2>
      <table>
        <thead><tr><th>персона</th><th>повідомлень бота</th></tr></thead>
        <tbody>{''.join(bot_per_rows)}</tbody>
      </table>
      <p class="cost-note" style="background:#f7f7f9">
        Приписуємо за поточною персоною юзера. Якщо хтось міняв персону —
        старі повідомлення підуть до поточної. Для точного звіту потрібно
        додати поле persona в таблицю messages.
      </p>
    </div>
  </div>

  <h2 style="margin-top:28px">Активність повідомлень</h2>
  <div class="grid grid-4">{msg_cards}</div>

  <div style="margin-top:24px">
    {_sessions_card(sessions, cost_per_session)}
  </div>
</div>
</body>
</html>
"""

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(OUT_DIR, f"product_dashboard_{stamp}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Compia product dashboard")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help=f"шлях до SQLite (default: {DEFAULT_DB})")
    parser.add_argument("--gap-minutes", type=int, default=30,
                        help="пауза в хвилинах для розділення сесій (default: 30)")
    args = parser.parse_args()

    out_path = render(args.db, args.gap_minutes)
    print(f"Готово: {out_path}")
    print(f"Відкрити: open {out_path}")


if __name__ == "__main__":
    main()
