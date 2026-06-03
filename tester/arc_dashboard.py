"""Сборка HTML-дашборда дуги отношений.

Один self-contained .html файл: можно открыть в браузере / послать как файл.
Никаких внешних CSS/JS — всё inline. Без emoji.

Структура:
  - Шапка с био юзера
  - Полоса прогресса warmth/intimacy по 3 фазам
  - 3 колонки фаз: метрики + цитаты + bio-callbacks + Мирины «характерные» фразы
  - Полный транскрипт (collapsible)
  - Итоговый вердикт «возможна ли привязанность»
"""

import html as _html
from typing import Optional


def _esc(s: Optional[str]) -> str:
    return _html.escape(str(s)) if s is not None else ""


def _bar(score: Optional[int], max_val: int = 10, color: str = "#e85a8e") -> str:
    if not isinstance(score, (int, float)):
        return '<div class="bar"><div class="bar-fill empty"></div></div>'
    pct = max(0, min(100, int(score) * 100 // max_val))
    return (
        f'<div class="bar"><div class="bar-fill" style="width:{pct}%; '
        f'background:{color}"></div></div>'
    )


def _warmth_color(score) -> str:
    """От 1 (синий-холодный) к 10 (тёплый-розовый)."""
    if not isinstance(score, (int, float)):
        return "#999"
    if score >= 8:
        return "#e85a8e"     # pink-warm
    if score >= 6:
        return "#f0a04b"     # warm-orange
    if score >= 4:
        return "#a9b8c4"     # neutral
    return "#5e7ca6"         # cold


def _phase_card(num: int, label: str, segment: list, judged: dict) -> str:
    if judged.get("_empty"):
        return f'<div class="phase-card"><h2>Фаза {num}: {_esc(label)}</h2><p>порожньо</p></div>'
    if judged.get("_parse_error"):
        return (
            f'<div class="phase-card"><h2>Фаза {num}: {_esc(label)}</h2>'
            f'<p>parse error</p><pre>{_esc(judged.get("_raw", ""))[:500]}</pre></div>'
        )

    warmth = judged.get("warmth_score")
    distance = judged.get("distance_score")
    color = _warmth_color(warmth)

    bio_callbacks = judged.get("bio_callbacks", []) or []
    intimacy = judged.get("intimacy_markers", []) or []
    jokes = judged.get("micro_jokes", []) or []
    quotes = judged.get("representative_quotes", []) or []
    name_calls = judged.get("name_callbacks_count", 0)
    violations = judged.get("async_promise_violations", []) or []
    verdict = judged.get("one_line_verdict", "")

    # Сегмент в человекочитаемом виде (collapsible).
    seg_html_rows = []
    for t in segment:
        cls = "u" if t["role"] == "user" else "b"
        who = "Андрій" if t["role"] == "user" else "Мира"
        text = _esc(t["text"]).replace("\n", "<br>")
        seg_html_rows.append(f'<div class="msg {cls}"><b>{who}:</b> {text}</div>')
    seg_html = "".join(seg_html_rows)

    def _quote_list(items, css_class="quote"):
        if not items:
            return '<p class="empty">—</p>'
        out = ['<ul>']
        for q in items:
            if isinstance(q, dict):
                text = q.get("quote") or q.get("match") or str(q)
            else:
                text = str(q)
            out.append(f'<li class="{css_class}">{_esc(text)}</li>')
        out.append('</ul>')
        return "".join(out)

    bio_block = ""
    if bio_callbacks:
        rows = ["<ul>"]
        for cb in bio_callbacks:
            key = cb.get("hook_key", "")
            quote = cb.get("quote", "")
            rows.append(
                f'<li><span class="tag">{_esc(key)}</span> '
                f'<span class="quote">{_esc(quote)}</span></li>'
            )
        rows.append("</ul>")
        bio_block = "".join(rows)
    else:
        bio_block = '<p class="empty">жодного звертання до біографії</p>'

    return f"""
<div class="phase-card" style="border-top:6px solid {color}">
  <div class="phase-header">
    <h2>Фаза {num}: {_esc(label)}</h2>
    <p class="verdict-line" style="color:{color}">«{_esc(verdict)}»</p>
  </div>

  <div class="metric-row">
    <div class="metric">
      <div class="metric-label">Теплота</div>
      <div class="metric-value" style="color:{color}">{_esc(warmth) if warmth is not None else "?"}<span>/10</span></div>
      {_bar(warmth, color=color)}
    </div>
    <div class="metric">
      <div class="metric-label">Дистанція</div>
      <div class="metric-value">{_esc(distance) if distance is not None else "?"}<span>/10</span></div>
      {_bar(distance, color="#5e7ca6")}
    </div>
    <div class="metric">
      <div class="metric-label">Звернень на ім'я</div>
      <div class="metric-value">{name_calls}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Деталі біо</div>
      <div class="metric-value">{len(bio_callbacks)}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Близькі маркери</div>
      <div class="metric-value">{len(intimacy)}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Внутрішні жарти</div>
      <div class="metric-value">{len(jokes)}</div>
    </div>
  </div>

  <h3>Характерні цитати Мири</h3>
  {_quote_list(quotes)}

  <h3>Згадки біографії</h3>
  {bio_block}

  <h3>Маркери близькості</h3>
  {_quote_list(intimacy)}

  <h3>Внутрішні жарти</h3>
  {_quote_list(jokes)}

  {('<h3 style="color:#c0392b">Порушення</h3>' + _quote_list(violations)) if violations else ""}

  <details class="transcript-details">
    <summary>повний сегмент ({len([t for t in segment if t['role']=='user'])} ходів)</summary>
    <div class="transcript">{seg_html}</div>
  </details>
</div>
"""


def _bio_card(bio: dict) -> str:
    rows = []
    for k, label in [
        ("name", "Імя"), ("age", "Вік"), ("city", "Місто"),
        ("job", "Робота"), ("ex", "Особисте"), ("pet", "Кіт"),
        ("hobby", "Захоплення"), ("music", "Музика"),
        ("habit", "Звичка"), ("ache", "Біль"),
    ]:
        if k in bio:
            rows.append(
                f'<div class="bio-row"><span class="bio-key">{_esc(label)}</span>'
                f'<span class="bio-val">{_esc(bio[k])}</span></div>'
            )
    return f'<div class="bio-card"><h2>Користувач</h2>{"".join(rows)}</div>'


def _trajectory_arrow(traj: str) -> str:
    return {
        "growing": "▲",
        "flat": "→",
        "fading": "▼",
        "chaotic": "⟿",
    }.get(traj, "?")


def _cross_block(cross: dict) -> str:
    if cross.get("_parse_error"):
        return (
            f'<div class="cross"><h2>Кросс-фазовий вердикт</h2>'
            f'<pre>{_esc(cross.get("_raw", ""))[:600]}</pre></div>'
        )
    warmth_t = cross.get("warmth_trajectory", "?")
    intim_t = cross.get("intimacy_trajectory", "?")
    bio_t = cross.get("bio_memory_trajectory", "?")
    feasible = cross.get("attachment_feasible")
    score = cross.get("attachment_score", "?")
    works = cross.get("what_works", []) or []
    breaks = cross.get("what_breaks", []) or []
    verdict = cross.get("verdict_paragraph", "")

    feasible_label = "так" if feasible is True else ("ні" if feasible is False else "?")
    feasible_color = "#1abc9c" if feasible is True else ("#c0392b" if feasible is False else "#999")

    def _bullets(items):
        if not items:
            return '<p class="empty">—</p>'
        return "<ul>" + "".join(f"<li>{_esc(s)}</li>" for s in items) + "</ul>"

    return f"""
<div class="cross">
  <h2>Чи виникає прив'язаність?</h2>
  <div class="cross-headline">
    <div class="big-stat">
      <div class="big-num" style="color:{feasible_color}">{_esc(feasible_label)}</div>
      <div class="big-label">прив'язаність реальна</div>
    </div>
    <div class="big-stat">
      <div class="big-num">{_esc(score)}<span>/10</span></div>
      <div class="big-label">оцінка</div>
      {_bar(score if isinstance(score,int) else None)}
    </div>
    <div class="trajectories">
      <div>Теплота: <b>{_trajectory_arrow(warmth_t)} {_esc(warmth_t)}</b></div>
      <div>Близькість: <b>{_trajectory_arrow(intim_t)} {_esc(intim_t)}</b></div>
      <div>Памʼять біо: <b>{_trajectory_arrow(bio_t)} {_esc(bio_t)}</b></div>
    </div>
  </div>
  <div class="cross-grid">
    <div class="cross-col">
      <h3 style="color:#1abc9c">Що працює</h3>
      {_bullets(works)}
    </div>
    <div class="cross-col">
      <h3 style="color:#c0392b">Що ламає</h3>
      {_bullets(breaks)}
    </div>
  </div>
  <p class="verdict-paragraph">{_esc(verdict)}</p>
</div>
"""


CSS = """
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  margin: 0; padding: 0; background: #fafafa; color: #2c2c2c;
  font-size: 14px; line-height: 1.5;
}
.container { max-width: 1400px; margin: 0 auto; padding: 24px; }
h1 { font-size: 28px; margin: 0 0 8px; color: #1a1a1a; }
h2 { font-size: 20px; margin: 16px 0 12px; color: #1a1a1a; }
h3 { font-size: 14px; text-transform: uppercase; letter-spacing: 0.5px;
     color: #666; margin: 18px 0 8px; }
.subtitle { color: #888; margin: 0 0 24px; font-size: 14px; }

.bio-card {
  background: #fff; border-radius: 12px; padding: 20px 24px;
  margin-bottom: 20px; box-shadow: 0 2px 12px rgba(0,0,0,0.04);
}
.bio-row { display: flex; gap: 16px; padding: 4px 0; border-bottom: 1px dotted #eee; }
.bio-row:last-child { border-bottom: none; }
.bio-key { width: 120px; color: #888; font-weight: 600; flex-shrink: 0; }
.bio-val { color: #333; }

.phases { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px;
          margin-bottom: 24px; }
.phase-card {
  background: #fff; border-radius: 12px; padding: 20px 24px;
  box-shadow: 0 2px 12px rgba(0,0,0,0.06);
}
.phase-header h2 { margin-top: 0; }
.verdict-line { margin: 0 0 16px; font-style: italic; font-size: 14px; }

.metric-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px;
              margin: 16px 0 8px; }
.metric { background: #f7f7f9; padding: 10px 12px; border-radius: 8px; }
.metric-label { font-size: 11px; color: #888; text-transform: uppercase;
                letter-spacing: 0.3px; margin-bottom: 4px; }
.metric-value { font-size: 24px; font-weight: 600; line-height: 1; }
.metric-value span { font-size: 12px; color: #aaa; font-weight: 400; }

.bar { height: 4px; background: #eee; border-radius: 2px; margin-top: 8px;
       overflow: hidden; }
.bar-fill { height: 100%; transition: width 0.3s; }
.bar-fill.empty { background: #ddd; width: 0%; }

ul { margin: 4px 0 8px; padding-left: 20px; }
li { margin: 4px 0; }
.quote { color: #444; font-style: italic; }
.quote::before { content: "«"; color: #bbb; }
.quote::after { content: "»"; color: #bbb; }
.empty { color: #bbb; font-style: italic; margin: 4px 0; }
.tag { display: inline-block; background: #ffe9f0; color: #b03060;
       padding: 1px 8px; border-radius: 10px; font-size: 11px; font-weight: 600;
       margin-right: 6px; }

.transcript-details { margin-top: 16px; padding-top: 12px; border-top: 1px solid #eee; }
.transcript-details summary { cursor: pointer; color: #666; font-size: 12px; }
.transcript { margin-top: 12px; max-height: 400px; overflow-y: auto;
              background: #fafafa; padding: 12px; border-radius: 6px; }
.msg { padding: 4px 0; font-size: 13px; }
.msg.u { color: #2c3e50; }
.msg.b { color: #8e3a5d; }

.cross {
  background: #fff; border-radius: 12px; padding: 24px 28px;
  box-shadow: 0 2px 12px rgba(0,0,0,0.06); margin-bottom: 24px;
}
.cross-headline { display: grid; grid-template-columns: 1fr 1fr 2fr;
                  gap: 24px; margin: 16px 0; align-items: center; }
.big-stat { text-align: center; }
.big-num { font-size: 48px; font-weight: 700; line-height: 1; }
.big-num span { font-size: 18px; color: #aaa; font-weight: 400; }
.big-label { font-size: 12px; color: #888; text-transform: uppercase;
             letter-spacing: 0.5px; margin-top: 6px; }
.trajectories { font-size: 14px; }
.trajectories div { margin: 4px 0; }
.cross-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
.verdict-paragraph { background: #f7f7f9; padding: 14px 18px; border-radius: 8px;
                     margin-top: 16px; font-size: 14px; line-height: 1.6; }

@media (max-width: 900px) {
  .phases, .cross-headline, .cross-grid { grid-template-columns: 1fr; }
  .metric-row { grid-template-columns: repeat(2, 1fr); }
}
"""


def _cost_block(cost: dict, message_counts: dict) -> str:
    if not cost:
        return ""
    totals = cost.get("totals", {})
    per_model = cost.get("per_model", {}) or {}
    total_msgs = message_counts.get("total", 0)
    user_msgs = message_counts.get("user", 0)
    bot_msgs = message_counts.get("bot", 0)
    total_cost = totals.get("cost_usd", 0.0)
    per_msg = (total_cost / total_msgs) if total_msgs else 0.0
    per_bot = (total_cost / bot_msgs) if bot_msgs else 0.0

    rows = []
    for model, s in sorted(per_model.items(), key=lambda x: -x[1]["cost_usd"]):
        rows.append(
            f'<tr><td>{_esc(model)}</td>'
            f'<td class="num">{s["calls"]}</td>'
            f'<td class="num">{s["input"]:,}</td>'
            f'<td class="num">{s["output"]:,}</td>'
            f'<td class="num">{s["cache_read"]:,}</td>'
            f'<td class="num">{s["cache_write"]:,}</td>'
            f'<td class="num">${s["cost_usd"]:.4f}</td></tr>'
        )

    return f"""
<div class="cross cost-block">
  <h2>Вартість симуляції</h2>
  <div class="cross-headline">
    <div class="big-stat">
      <div class="big-num">${total_cost:.4f}</div>
      <div class="big-label">за весь прогон дуги</div>
    </div>
    <div class="big-stat">
      <div class="big-num">{total_msgs}</div>
      <div class="big-label">всього повідомлень ({user_msgs} user / {bot_msgs} bot)</div>
    </div>
    <div class="big-stat">
      <div class="big-num">${per_msg*1000:.2f}<span style="font-size:14px">/1k</span></div>
      <div class="big-label">≈ ${per_msg:.5f} за повідомлення<br>(${per_bot:.5f} за відповідь бота)</div>
    </div>
  </div>
  <table class="cost-table">
    <thead><tr>
      <th>модель</th><th>виклики</th><th>input</th><th>output</th>
      <th>cache read</th><th>cache write</th><th>$</th>
    </tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <p class="cost-note">
    Ціни: Sonnet 4.6 — $3/$15 in/out, Haiku 4.5 — $1/$5 in/out, cache hit ~10% від input.
    Symulator-LLM (Andriy через Haiku) і judge-LLM (Sonnet) теж враховані.
  </p>
</div>
"""


def render_dashboard(arc_result: dict, analysis: dict, out_path: str) -> str:
    """Сгенерить self-contained HTML файл и записать на диск."""
    bio = arc_result.get("bio", {})
    segments = arc_result.get("phase_segments", {})
    per_phase = analysis.get("per_phase", {})
    cross = analysis.get("cross_phase", {})
    labels = analysis.get("phase_labels", {1: "знайомство", 2: "зближення", 3: "своя"})
    cost = arc_result.get("cost", {})
    message_counts = arc_result.get("message_counts", {})

    phase_blocks = []
    for ph in (1, 2, 3):
        segment = segments.get(str(ph), [])
        judged = per_phase.get(str(ph), {})
        phase_blocks.append(_phase_card(ph, labels.get(ph, str(ph)), segment, judged))

    extra_css = """
.cost-block { background: #fff8e1; }
.cost-table { width: 100%; border-collapse: collapse; margin-top: 12px;
              font-size: 13px; }
.cost-table th, .cost-table td { padding: 6px 10px; text-align: left;
                                  border-bottom: 1px solid #eee; }
.cost-table th { background: #fdf3c4; font-weight: 600; }
.cost-table td.num { text-align: right; font-family: ui-monospace,
                      "SF Mono", Menlo, monospace; }
.cost-note { font-size: 12px; color: #888; margin-top: 8px; }
"""

    html = f"""<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<title>Дуга стосунків: Мира × {_esc(bio.get('name','?'))}</title>
<style>{CSS}{extra_css}</style>
</head>
<body>
<div class="container">
  <h1>Дуга стосунків Мири</h1>
  <p class="subtitle">
    Симульований прогін 3 фаз з фіксованою біографією користувача.
    Метрики — судья Claude Sonnet 4.6, з ручною рубрикою.
    Час симуляції: {arc_result.get('elapsed_sec', '?')}s. Згенеровано: {_esc(arc_result.get('generated_at', ''))}
  </p>

  {_bio_card(bio)}

  {_cost_block(cost, message_counts)}

  {_cross_block(cross)}

  <div class="phases">
    {''.join(phase_blocks)}
  </div>
</div>
</body>
</html>
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path
