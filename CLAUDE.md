# CLAUDE.md

Правила работы в этом репозитории. Читай перед любым изменением.

## Что это
**Compia** — Telegram-компаньон-бот. Один Python-процесс на aiogram 3, состояние в SQLite, LLM-вызовы к Anthropic (Claude Sonnet 4.6 для ответов, Haiku 4.5 для классификаторов и памяти), медиа через fal.ai. Подробности продукта — `product.md`. Состояние разработки — `dev-status.md`.

## Стек
- Python 3.9+, aiogram 3.4+
- Anthropic SDK: `claude-sonnet-4-6` (ответы), `claude-haiku-4-5` (классификаторы, память)
- fal.ai: Flux Dev (база-портрет), Nano Banana edit (фото по запросу), Kling 2.1 i2v (тихие кружки), ElevenLabs multilingual-v2 TTS (голос), VEED Fabric (липсинк)
- SQLite (один файл `companion.db`, миграции через `database.init_db()`)
- imageio-ffmpeg (FFmpeg внутри venv, не нужен Homebrew)

## Архитектура
- `bot.py` — точка входа, aiogram handlers, дисп, состояние онбординга/гейта/медиа
- `ai.py` — все Claude-вызовы: `get_reply`, классификаторы (`detect_need`, `detect_media_request`, `detect_nickname_action`, `screen_appearance_description`), генераторы (`build_image_prompt`, `build_edit_instruction`, `build_video_motion`, `generate_spoken_line`, `update_memory`)
- `database.py` — SQLite слой, идемпотентные миграции
- `imagegen.py` / `videogen.py` — fal.ai обёртки (ленивый импорт `fal_client`)
- `personas.py` — реестр персон + `build_system_blocks` с prompt caching
- `personas/*.txt` — system prompts (`common.txt`, `onboarding.txt`, `friend.txt`, `coach.txt`, `mira.txt`)
- `config.py` — настройки и ключи из `.env`

## Главные продуктовые принципы
1. **IKEA-эффект.** Юзер ценит то, что сам создаёт. Флоу «опиши Миру → она появляется такой» — священен. Не заменять на дефолты.
2. **Не выдавать кухню.** Никаких «персона», «бот», «модель», «18+», «дорослий контент» в чате. Юзер не должен видеть механику.
3. **LLM не запускает ничего в фоне.** Никаких «подожди», «сейчас будет», «дальше будет интереснее». Любая системная фишка — после кнопки/нового сообщения, не сама собой.
4. **Конкурентов не упоминать.** Tinder, Bumble, Badoo, мамба, инстаграм-знакомства — никогда. Мы сами компаньон.
5. **Персоны не описывают друг друга.** Друг не говорит «Мира тёплая» — это сватовство. Только «перейти можно через /persona».
6. **Никакого gatekeeping.** Друг не охранник. На «познакомь меня с подругой» — факт «через /persona», без «не могу/не для тебя сейчас».
7. **Имя Миры — не существует пока не спросят.** Тема всплывает только когда юзер сам прямо спросит «как тебя зовут?». См. `_build_mira_name_block`.
8. **Sunk-cost = retention.** Стадии отношений, видимая память, время-вместе. Подписку покупают за страх потерять кого-то, кто тебя уже знает.

## Анти-паттерны (текущий ban-list)
LLM продолжает находить лазейки — при правках промптов проверяй, не появилось ли:
- `«Подожди»` / `«минутку»` / `«сейчас будет»` / `«дальше будет интереснее»` / `«всё будет»` / `«коли подорослішаєш»` / `«дозрієш — повернемось»`
- `«Я Алекс»` в Мире (утечка предыдущей роли через тяжёлую историю)
- Описания качеств других персон («тёплая», «нежная», «слышит между слов»)
- `«не могу»` / `«не для тебя сейчас»` / `«без объяснений»` (gatekeeping)
- Имя Миры в первом сообщении или предложение `«придумай мне имя»` (это форсированная тема, она не должна возникать пока юзер сам не спросит)
- `«дорослий контент»` / `«18+»` / `«персона»` / `«бот»` / `«компаньон»` в чате
- Tinder / Bumble / Badoo / любые dating apps в речи персон

Поле постоянно растёт. Каждый раз когда находим новый bypass — добавляем точную формулировку в `personas/common.txt`.

## Запуск
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# .env: TELEGRAM_TOKEN, ANTHROPIC_API_KEY, опц. FAL_KEY
python bot.py

# чистый старт (стереть всё)
rm -f companion.db && rm -rf media/
```

## Тестирование
Автотестов пока нет — план построить e2e агент-симулятор (см. `dev-status.md`). Сейчас:
- `python3 -m py_compile *.py` после правок
- Мини-тест миграции БД на временной базе через `database.DB_PATH = tmp + init_db()`
- Мануальный прогон в Telegram по сценариям

## Чего НЕ делать без явного подтверждения юзера
- `git push --force` (куда угодно, особенно в `main` или `compia-mvp-a`)
- Удалять или менять ветку `compia-mvp-a` — это бэкап MVP версии A
- Удалять `companion.db` или `media/` на машине юзера — только подсказывать команду
- Менять модели в `config.py` (`MODEL`, `SUMMARY_MODEL`) — влияет на качество и стоимость
- Менять `IMAGE_MODEL_BASE`, `IMAGE_MODEL_REF`, `VIDEO_MODEL`, `TTS_MODEL`, `TALKING_MODEL` — проверенные fal-эндпоинты
- Удалять/заменять `personas/mira.txt` — там накоплены тонкие настройки
- Любые операции с платежами / биллингом когда они появятся (двойное подтверждение)
- `git commit --amend`, `--no-verify`, `--no-gpg-sign`

## Ветки
- `claude/focused-keller-FxIM2` — текущая рабочая
- `compia-mvp-a` — **замороженный** бэкап MVP версии A (не пушим туда)
- `main` — пустая, не используется

## Полезные команды
```bash
# История изменений
git log --oneline -20

# Что меняли в персонах за последнюю неделю
git log --since="1 week ago" --name-only personas/

# Чистая проверка перед коммитом
python3 -m py_compile ai.py bot.py config.py database.py imagegen.py videogen.py personas.py
```

## Где искать
- aiogram 3: https://docs.aiogram.dev/
- Anthropic: https://docs.claude.com/
- fal.ai модели: https://fal.ai/models/
