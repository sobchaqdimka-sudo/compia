"""Фейковый transport aiogram: ловим всё, что бот пытается отправить.

Подменяет глобальные `bot.bot` и подаёт в `bot.handle_message`/`on_*` callback-ом
фейковые Message/CallbackQuery. Каждый вызов `message.answer`,
`message.answer_photo`, `message.answer_video_note`, `bot.send_message`
записывается в общий журнал событий. Так мы можем восстановить транскрипт без
живого Telegram.
"""

from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class Event:
    """Одно «исходящее» событие от бота в чат."""

    kind: str           # "text" | "photo" | "video_note" | "chat_action"
    text: Optional[str] = None
    caption: Optional[str] = None
    path: Optional[str] = None
    has_keyboard: bool = False
    keyboard_kind: Optional[str] = None   # "persona" | "adult" | "reset" | "start" | "checkin" | None

    def to_dict(self):
        return {
            "kind": self.kind,
            "text": self.text,
            "caption": self.caption,
            "path": self.path,
            "has_keyboard": self.has_keyboard,
            "keyboard_kind": self.keyboard_kind,
        }


def _classify_keyboard(reply_markup) -> Optional[str]:
    """По первой callback_data понять, какая клавиатура показана."""
    if reply_markup is None:
        return None
    try:
        rows = reply_markup.inline_keyboard
        first = rows[0][0].callback_data or ""
        return first.split(":", 1)[0] if first else None
    except Exception:
        return None


class _FakeChat:
    def __init__(self, chat_id: int):
        self.id = chat_id


class _FakeUser:
    def __init__(self, user_id: int):
        self.id = user_id


class FakeMessage:
    """Минимальный заменитель aiogram.types.Message для прогона хэндлеров."""

    def __init__(
        self,
        user_id: int,
        text: Optional[str] = None,
        events: Optional[List[Event]] = None,
    ):
        self.chat = _FakeChat(user_id)
        self.from_user = _FakeUser(user_id)
        self.text = text
        self.photo = None       # тестер пока не симулирует входящие фото
        self.caption = None
        self._events = events if events is not None else []

    async def answer(self, text: str, reply_markup: Any = None):
        self._events.append(Event(
            kind="text",
            text=text,
            has_keyboard=reply_markup is not None,
            keyboard_kind=_classify_keyboard(reply_markup),
        ))

    async def answer_photo(self, file: Any, caption: Optional[str] = None):
        path = getattr(file, "path", None) or str(file)
        self._events.append(Event(kind="photo", path=str(path), caption=caption))

    async def answer_video_note(self, file: Any):
        path = getattr(file, "path", None) or str(file)
        self._events.append(Event(kind="video_note", path=str(path)))


class FakeBot:
    """Заменитель aiogram.Bot. Логирует send_message/send_chat_action."""

    def __init__(self, events: List[Event]):
        self._events = events

    async def send_message(self, chat_id: int, text: str):
        self._events.append(Event(kind="text", text=text))

    async def send_chat_action(self, chat_id: int, action: str):
        # Записываем, но обычно отфильтровываем при печати транскрипта.
        self._events.append(Event(kind="chat_action", text=action))

    async def download(self, photo_size):
        # Тестер не симулирует входящие фото - метод оставлен для совместимости.
        import io
        return io.BytesIO(b"")

    async def set_my_commands(self, *args, **kwargs):
        pass


def install(events: List[Event]):
    """Подменить `bot.bot` фейковым transport. Возвращает оригинал для restore."""
    import bot as bot_module
    original = bot_module.bot
    bot_module.bot = FakeBot(events)
    return original


def restore(original):
    import bot as bot_module
    bot_module.bot = original
