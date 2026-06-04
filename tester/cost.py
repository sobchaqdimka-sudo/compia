"""Pricing и подсчёт стоимости вызовов Anthropic.

Цены — на момент конца 2025/начало 2026 (USD за 1M токенов).
Если поменяются — править здесь.

ПОЛЬЗОВАНИЕ:
    from tester.cost import install_tracker, summary
    install_tracker()
    ... делаешь обычные вызовы anthropic.Anthropic().messages.create(...) ...
    s = summary()  # dict с totals и breakdown по моделям

Трекер - monkey-patch базового SDK-метода. Невидим для остального кода.
Никаких сетевых эффектов - просто счёт.
"""

import logging
from collections import defaultdict
from threading import Lock
from typing import Any, Callable, Dict, Optional

LOG = logging.getLogger("tester.cost")

# USD за 1М токенов. Источник: docs.anthropic.com (опубликовано к выпуску
# моделей 4.x). Cache hit стоит ~10% от input, cache write ~125%.
PRICING = {
    "claude-sonnet-4-6": {
        "input":       3.00,
        "output":     15.00,
        "cache_read":  0.30,
        "cache_write": 3.75,
    },
    "claude-haiku-4-5": {
        "input":       1.00,
        "output":      5.00,
        "cache_read":  0.10,
        "cache_write": 1.25,
    },
    # Fallback на случай других моделей в системе.
    "_default": {
        "input":       3.00,
        "output":     15.00,
        "cache_read":  0.30,
        "cache_write": 3.75,
    },
}


def _price_for_model(model_name: str) -> Dict[str, float]:
    """Вернуть прайс по модели; подходит по точному совпадению или дефолту."""
    if not model_name:
        return PRICING["_default"]
    if model_name in PRICING:
        return PRICING[model_name]
    # На случай если кто-то пишет '20250101'-суффикс или другое.
    for key in PRICING:
        if key != "_default" and model_name.startswith(key):
            return PRICING[key]
    return PRICING["_default"]


def _cost_usd(model_name: str, in_t: int, out_t: int,
              cache_read: int, cache_write: int) -> float:
    """Стоимость одного вызова в долларах."""
    p = _price_for_model(model_name)
    return (
        in_t       * p["input"]       / 1_000_000
        + out_t    * p["output"]      / 1_000_000
        + cache_read  * p["cache_read"]  / 1_000_000
        + cache_write * p["cache_write"] / 1_000_000
    )


# ---------------- runtime state -----------------

_LOCK = Lock()
_STATS: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
    "calls": 0,
    "input": 0,
    "output": 0,
    "cache_read": 0,
    "cache_write": 0,
    "cost_usd": 0.0,
})
_INSTALLED = False
_ORIGINAL_CREATE = None
# Внешний callback, вызывается после каждой записи. Подпись:
# (model: str, in_t: int, out_t: int, cache_read: int, cache_write: int, cost_usd: float)
# Используется продакшен-ботом для записи в БД per-user. Падения коллбека
# не должны рушить вызов модели - ловим всё.
_RECORD_CALLBACK: Optional[Callable[..., None]] = None


def reset() -> None:
    """Обнулить счётчики (между прогонами)."""
    with _LOCK:
        _STATS.clear()


def _record(model: str, usage) -> None:
    in_t = getattr(usage, "input_tokens", 0) or 0
    out_t = getattr(usage, "output_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cost = _cost_usd(model, in_t, out_t, cr, cw)
    with _LOCK:
        s = _STATS[model]
        s["calls"] += 1
        s["input"] += in_t
        s["output"] += out_t
        s["cache_read"] += cr
        s["cache_write"] += cw
        s["cost_usd"] += cost
    # Внешний слушатель (продакшен пишет per-user в БД). Вне lock'а,
    # чтобы коллбек случайно не задержал следующий вызов модели.
    cb = _RECORD_CALLBACK
    if cb is not None:
        try:
            cb(model, in_t, out_t, cr, cw, cost)
        except Exception:
            LOG.exception("cost tracker record_callback failed")


def install_tracker(record_callback: Optional[Callable[..., None]] = None) -> None:
    """Monkey-patch anthropic SDK чтобы считать токены каждого вызова.

    record_callback (опц.) — внешний слушатель, вызывается после каждой
    записи с (model, in_t, out_t, cache_read, cache_write, cost_usd).
    Можно поставить позже через set_record_callback() — install_tracker()
    идемпотентен и не сбрасывает уже работающий патч.
    """
    global _INSTALLED, _ORIGINAL_CREATE
    if record_callback is not None:
        set_record_callback(record_callback)
    if _INSTALLED:
        return
    import anthropic  # noqa: F401  - убедимся что пакет есть
    from anthropic.resources.messages.messages import Messages

    _ORIGINAL_CREATE = Messages.create

    def _wrapped(self, *args, **kwargs):
        resp = _ORIGINAL_CREATE(self, *args, **kwargs)
        try:
            model = getattr(resp, "model", None) or kwargs.get("model", "")
            if hasattr(resp, "usage"):
                _record(model, resp.usage)
        except Exception:
            LOG.exception("cost tracker failed to record call")
        return resp

    Messages.create = _wrapped
    _INSTALLED = True
    LOG.info("Cost tracker installed (monkey-patched anthropic.Messages.create)")


def set_record_callback(cb: Optional[Callable[..., None]]) -> None:
    """Установить (или снять) внешний слушатель записей."""
    global _RECORD_CALLBACK
    _RECORD_CALLBACK = cb


def uninstall_tracker() -> None:
    """Восстановить оригинал (нужно для тестов изоляции, обычно не вызываем)."""
    global _INSTALLED, _ORIGINAL_CREATE
    if not _INSTALLED:
        return
    from anthropic.resources.messages.messages import Messages
    Messages.create = _ORIGINAL_CREATE
    _INSTALLED = False


def summary() -> Dict[str, Any]:
    """Снимок текущей статистики (для дашборда/отчёта)."""
    with _LOCK:
        per_model = {m: dict(s) for m, s in _STATS.items()}
    totals = {
        "calls": sum(s["calls"] for s in per_model.values()),
        "input": sum(s["input"] for s in per_model.values()),
        "output": sum(s["output"] for s in per_model.values()),
        "cache_read": sum(s["cache_read"] for s in per_model.values()),
        "cache_write": sum(s["cache_write"] for s in per_model.values()),
        "cost_usd": sum(s["cost_usd"] for s in per_model.values()),
    }
    return {"per_model": per_model, "totals": totals}
