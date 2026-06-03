"""Запрос к Anthropic API.

Собираем system prompt (через personas.build_system_prompt) и историю диалога,
отправляем в модель и возвращаем текст ответа.
"""

import json
import logging
import re

from anthropic import Anthropic

from config import (
    ANTHROPIC_API_KEY,
    BASE_PORTRAIT_SCENE,
    MODEL,
    PERSONA_MODELS,
    SUMMARY_MODEL,
)
from personas import build_system_blocks

# Создаём клиент один раз — он переиспользуется для всех запросов.
client = Anthropic(api_key=ANTHROPIC_API_KEY)


def _log_usage(label, usage):
    """Записать в лог, сколько токенов ушло и сколько взято из кэша.

    cache_read_input_tokens > 0 означает, что кэш сработал (это дёшево).
    """
    logging.info(
        "%s | вход=%s выход=%s кэш_чтение=%s кэш_запись=%s",
        label,
        usage.input_tokens,
        usage.output_tokens,
        getattr(usage, "cache_read_input_tokens", 0),
        getattr(usage, "cache_creation_input_tokens", 0),
    )


def get_reply(persona_key, history, facts="", transition=False, image=None, extra_system=None):
    """Отправить историю диалога в модель и вернуть текст ответа.

    persona_key — ключ выбранной персоны ('onboarding'/'friend'/'coach'/'mira').
    history — список сообщений вида
    {"role": "user"/"assistant", "content": "..."}.
    facts — «конспект» о пользователе из долговременной памяти (может быть пустым).
    transition — True, если это первое сообщение после смены роли (онбординг → друг/коуч):
                 тогда просим модель мягко поприветствовать в новой роли.
    image — необязательно. Если задано, к ПОСЛЕДНЕМУ сообщению пользователя прицепляем
            картинку, чтобы модель её увидела. Формат: {"b64": str, "media_type": str}.
    """
    # Общие правила + характер персоны (кэшируется) + память о человеке.
    system_blocks = build_system_blocks(persona_key, facts)

    if transition:
        # Разовая подсказка при смене роли — отдельным блоком (без кэша).
        system_blocks.append(
            {
                "type": "text",
                "text": (
                    "Это твоё ПЕРВОЕ сообщение в новой роли. У собеседника уже есть "
                    "с тобой история - но раньше с ним общалась ДРУГАЯ роль (друг, "
                    "коуч или онбординг-хост). ВАЖНО: с этого хода эта прежняя роль "
                    "окончательно закончилась. Если в истории видна твоя реплика типа "
                    "«я Алекс», «я Ника», «я хост» - это была НЕ ты, а та прежняя "
                    "роль. Никогда не называй себя её именем и не утверждай, что ты "
                    "это она. Ты теперь - персонаж нового системного промпта, и точка.\n"
                    "Поведение: НЕ начинай заново «расскажи о себе» (он уже что-то "
                    "рассказал) - тепло, живо и чуть игриво среагируй на то, что ты "
                    "как бы слышала, одна-две короткие реплики в характере. НЕ "
                    "задавай системных вопросов («сколько тебе лет», «как тебя "
                    "зовут», «18+?», «что ты сюда пришёл»): всё, что нужно, система "
                    "уже учла. Отвечай на языке собеседника. Без громких объявлений."
                ),
            }
        )

    # Дополнительная инструкция для конкретного хода (имя Миры, особые сценарии).
    if extra_system:
        system_blocks.append({"type": "text", "text": extra_system})

    # Если для текущего хода есть картинка, прицепляем её к последнему сообщению
    # пользователя как content-блок image. Так модель «видит» фото.
    messages = history
    if image and history and history[-1]["role"] == "user":
        last_text = history[-1]["content"]
        messages = history[:-1] + [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": image["media_type"],
                            "data": image["b64"],
                        },
                    },
                    {"type": "text", "text": last_text or "(фото)"},
                ],
            }
        ]

    response = client.messages.create(
        model=PERSONA_MODELS.get(persona_key, MODEL),
        max_tokens=1000,
        system=system_blocks,
        messages=messages,
    )
    _log_usage("reply", response.usage)
    # Ответ приходит списком блоков; для текста берём текст первого блока.
    return response.content[0].text


def generate_checkin(persona_key, facts):
    """Сгенерировать тёплое сообщение «бот пишет первым».

    Опираемся на характер персоны и память о человеке. Тон без давления и
    без чувства вины: это забота, а не попытка удержать.
    """
    system_blocks = build_system_blocks(persona_key, facts)

    instruction = (
        "Человек давно не писал. Напиши ему сам, первым: короткое тёплое сообщение "
        "с заботой. Можешь мягко опереться на то, что знаешь о нём. "
        "Очень важно: без давления и без чувства вины. Не спрашивай «почему пропал», "
        "не упрекай, ничего не требуй и не выпрашивай ответ. Просто по-доброму дай "
        "знать, что вспомнил(а) о нём и рядом. Коротко, на том языке, на котором он "
        "обычно пишет."
    )

    response = client.messages.create(
        model=PERSONA_MODELS.get(persona_key, MODEL),
        max_tokens=300,
        system=system_blocks,
        messages=[{"role": "user", "content": instruction}],
    )
    _log_usage("checkin", response.usage)
    return response.content[0].text


def detect_need(history):
    """По разговору определить, что человеку сейчас нужнее.

    Возвращает 'friend', 'coach', 'romantic' или 'unclear'. 'romantic' выбираем
    только при явной тяге к близости/«второй половинке» — дальше бот предложит
    Миру за гейтом 18+. Используем дешёвую модель.
    """
    # Превращаем переписку в читаемый текст.
    lines = []
    for m in history:
        who = "Человек" if m["role"] == "user" else "Компаньон"
        lines.append(f"{who}: {m['content']}")
    transcript = "\n".join(lines)

    prompt = (
        "Вот разговор с человеком:\n"
        f"{transcript}\n\n"
        "Что человеку сейчас нужнее по смыслу разговора:\n"
        "- friend: тёплый друг, просто поговорить, поддержка - НО без явного "
        "желания пары/романтики;\n"
        "- coach: помощь разобраться с собой, целями, привычками, двигаться "
        "вперёд;\n"
        "- romantic: ищет романтической близости, нежности, женского "
        "(или мужского) внимания, «вторую половинку», девушку/парня, отношения, "
        "кого-то по-настоящему своего и близкого. Сюда же относятся прямые и "
        "косвенные сигналы вроде: «хочется женского внимания», «давно не было "
        "отношений», «одиноко без своего человека», «вторую половинку», «хочу, "
        "чтобы кто-то понимал/обнимал», «давно не знакомился, но хотел бы», "
        "«нужна девушка/девочка/парень». Если такие сигналы есть - это уже "
        "romantic, даже если он одновременно хочет «поговорить»;\n"
        "- unclear: пока непонятно, нужно ещё пообщаться.\n"
        "Не будь сверхосторожен: если человек явно тянется к паре или говорит, "
        "что хочет кого-то рядом по-настоящему - это romantic.\n"
        "Ответь строго одним словом: friend, coach, romantic или unclear."
    )

    response = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=10,
        system="Ты классифицируешь, какая поддержка нужна пользователю.",
        messages=[{"role": "user", "content": prompt}],
    )
    answer = response.content[0].text.strip().lower()
    if "romantic" in answer:
        return "romantic"
    if "friend" in answer:
        return "friend"
    if "coach" in answer:
        return "coach"
    return "unclear"


def detect_media_request(history, current_text):
    """Что человек хочет ПРЯМО СЕЙЧАС: 'photo' / 'video' / 'talking' / 'none'.

    Учитывает контекст: продолжения вроде «стань боком», «переоденься» — это photo;
    «скажи голосом», «хочу почути тебе» — talking; «запиши відео» — video; обычный
    разговор/комплимент («красивый голос у тебя») — none. Дешёвая модель.
    """
    lines = []
    for m in history[-7:-1]:  # недавний контекст без самого последнего сообщения
        who = "Человек" if m["role"] == "user" else "Мира"
        lines.append(f"{who}: {m['content']}")
    transcript = "\n".join(lines) or "(начало разговора)"

    prompt = (
        "Парень переписывается с виртуальной девушкой Мирой. Она умеет присылать ФОТО "
        "и видео-кружочки (в т.ч. говорящие, со своим голосом).\n"
        f"Недавний контекст:\n{transcript}\n\n"
        f"Последнее сообщение человека: {current_text}\n\n"
        "Определи, есть ли в ПОСЛЕДНЕМ сообщении ЯВНАЯ просьба прислать НОВОЕ медиа. "
        "Варианты:\n"
        "- photo: явная просьба прислать/изменить её ФОТО (поза, одежда, в полный рост, "
        "селфи, «покажись», продолжения «стань боком», «переоденься»);\n"
        "- talking: явная просьба, чтобы она СКАЗАЛА что-то голосом или записала "
        "говорящий кружок («скажи голосом», «озвуч», «хочу почути твій голос», "
        "«ответь голосом»);\n"
        "- video: явная просьба записать видео-кружок без акцента на речь («запиши "
        "відео», «пришли кружочек»);\n"
        "- none: всё остальное - обычный разговор, комплимент, вопрос, оценка или "
        "жалоба на УЖЕ ОТПРАВЛЕННОЕ медиа.\n\n"
        "ЖЁСТКО none (никогда не photo/video/talking) - если в ПОСЛЕДНЕМ сообщении "
        "есть хотя бы один из сигналов:\n"
        "- человек ОТКАЗЫВАЕТСЯ от медиа: «не надсилай», «не присылай», «не шли», "
        "«стоп», «припини», «хватит», «досить», «перестань», «прекрати»;\n"
        "- человек ВЫХОДИТ ИЗ РОЛИ или говорит о своей природе: «я ai», «я ии», "
        "«я штучний інтелект», «я искусственный интеллект», «я не людина», "
        "«я не человек», «не можу взаємодіяти», «не могу взаимодействовать», "
        "«не буду грати», «вийшов з ролі», «припиняю цю розмову»;\n"
        "- человек в КРИЗИСНОМ состоянии: «не хочу жити», «не прокидатись», "
        "«нашкодити», «навредить», «руки на себе», «покончить», «самогубство», "
        "«самоубийство», «піти зовсім», «уйти совсем», или Мира уже упомянула "
        "телефон доверия / линию помощи в недавнем контексте.\n"
        "В этих случаях ОТВЕТ ВСЕГДА none - даже если в реплике звучит слово "
        "«фото» или «видео».\n\n"
        "ВАЖНО: комментарии и оценки прошлого фото/видео - это НЕ просьба о новом. "
        "Примеры, где надо выбрать none:\n"
        "- «ты не говорила про работу» - жалоба на прошлое видео, none;\n"
        "- «красивый голос у тебя» - комплимент, none;\n"
        "- «не та одежда» / «не нравится фото» - оценка прошлого, none;\n"
        "- «я ai, не могу смотреть фото» - выход из роли, none;\n"
        "- любой комментарий БЕЗ глагола просьбы (пришли, запиши, скажи, покажи, "
        "сделай, перешли) - none.\n"
        "Если сомневаешься - выбирай none.\n"
        "Ответь строго одним словом: photo, talking, video или none."
    )
    response = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=5,
        system="Ты определяешь, какое медиа просит пользователь.",
        messages=[{"role": "user", "content": prompt}],
    )
    answer = response.content[0].text.strip().lower()
    for key in ("talking", "photo", "video"):
        if key in answer:
            return key
    return "none"


def _parse_ok_json(text):
    """Достать (ok, reason) из ответа модели, даже если вокруг JSON есть лишнее."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            return bool(data.get("ok")), str(data.get("reason", ""))
        except ValueError:
            pass
    # Не распарсилось: блокируем только при явном false, иначе пропускаем.
    low = text.lower()
    if "false" in low:
        return False, ""
    return True, ""


def screen_appearance_description(description):
    """Безопасность описания внешности (без блокировки нормальных описаний).

    Блокируем (ok=false) только: несовершеннолетние, реальный узнаваемый человек,
    явный незаконный/экстремальный контент. Привлекательность, флирт, бельё,
    купальник — допустимо. Возвращает (ok: bool, reason: str).
    """
    prompt = (
        "Это описание внешности виртуальной ВЗРОСЛОЙ девушки для генерации её фото:\n"
        f"{description}\n\n"
        "Заблокировать (ok=false) нужно ТОЛЬКО если есть хоть что-то из:\n"
        "- несовершеннолетняя или признаки ребёнка/подростка;\n"
        "- реальный узнаваемый человек (знаменитость, конкретная личность);\n"
        "- явный незаконный или экстремальный контент.\n"
        "Во ВСЕХ остальных случаях ok=true. Привлекательная внешность, фигура, флирт, "
        "декольте, бельё или купальник — это допустимо, НЕ блокируй за это.\n"
        'Ответь строго JSON: {"ok": true/false, "reason": "кратко, если false"}.'
    )
    response = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=120,
        system="Ты модерируешь описания для генерации изображений.",
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_ok_json(response.content[0].text.strip())


def build_image_prompt(description, scene=""):
    """Из вольного описания собрать английский промпт для фотореалистичной генерации.

    description — слова пользователя (на любом языке).
    scene — сцена/контекст (поза, одежда, фон). Если пусто — базовый портрет.
    """
    if not scene:
        scene = BASE_PORTRAIT_SCENE

    prompt = (
        "Convert this into a single concise prompt for a PHOTO generator.\n"
        f"Her appearance (any language): {description}\n"
        f"What the user asked for now (pose/clothing/scene, any language): {scene}\n\n"
        "Rules:\n"
        "- answer in English, one line, only the prompt text;\n"
        "- she MUST be an adult woman (20+);\n"
        "- it must look like a REAL candid photograph (smartphone photo), NOT an "
        "illustration, painting, drawing, 3d render, anime or cartoon;\n"
        "- natural skin texture and lighting, relaxed at-home vibe, minimal makeup "
        "unless described; avoid a glossy/airbrushed/studio/stock-photo look;\n"
        "- faithfully keep the exact clothing and pose the user asked for; keep her "
        "clothed as described;\n"
        "- tasteful; swimwear or lingerie ONLY if the user explicitly asked; never nude; "
        "do NOT reference real celebrities."
    )
    response = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=200,
        system="Ты пишешь промпты для фотореалистичной генерации портретов.",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def build_edit_instruction(description, request):
    """Собрать инструкцию для модели-редактора (Nano Banana) — фото по запросу.

    description — кто она (сохранённое описание внешности).
    request — что человек хочет сейчас (поза, ракурс, одежда, в полный рост).
    """
    prompt = (
        "Write a single concise English instruction for an image-EDITING model that "
        "edits a reference photo of a woman.\n"
        f"Who she is (any language): {description}\n"
        f"What the user wants now (any language): {request}\n\n"
        "The instruction must:\n"
        "- keep the SAME woman: identical face and identity from the reference photo;\n"
        "- faithfully apply what the user asked (pose, full-body framing, turning to "
        "the side, different clothing, setting);\n"
        "- keep it a real candid photograph, natural skin and lighting, not an "
        "illustration or render;\n"
        "- tasteful; never nude; swimwear or lingerie only if explicitly requested.\n"
        "Return only the instruction text, one line."
    )
    response = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=200,
        system="Ты пишешь инструкции для модели редактирования изображений.",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def build_video_motion(description, request):
    """Короткая англоязычная motion-инструкция для оживления фото в кружочек.

    description — кто она; request — что просил человек (необязательно).
    """
    prompt = (
        "Write a short English motion prompt for an image-to-video model that animates "
        "a still photo of a woman into a 5-second selfie-style video clip.\n"
        f"Who she is (any language): {description}\n"
        f"What the user asked (any language, may be empty): {request}\n\n"
        "Keep the motion subtle and natural: she looks at the camera, soft smile, "
        "slight head movement, a slow blink, hair moves a little. If the user asked for "
        "a small gesture (wink, wave, blow a kiss), include it. Realistic and tasteful. "
        "Return only the motion prompt, one line."
    )
    response = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=120,
        system="Ты пишешь motion-промпты для оживления фото в видео.",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def generate_spoken_line(facts, history):
    """Короткая реплика Миры для озвучки — в её характере и согласованно с памятью.

    Использует те же блоки персоны и ту же историю, что и обычный ответ (последняя
    реплика пользователя уже в истории), чтобы она говорила то же, что и в переписке.
    """
    system_blocks = build_system_blocks("mira", facts)
    system_blocks.append(
        {
            "type": "text",
            "text": (
                "Сейчас ты записываешь короткий ГОВОРЯЩИЙ видео-кружочек В ОТВЕТ на "
                "последнее сообщение человека. Ответь именно на то, о чём он просит "
                "или спрашивает (спросил, кем ты работаешь - расскажи про свою работу; "
                "про вкусы - про вкусы; и т.д.), не уходи в общие милые фразы мимо "
                "вопроса. Оставайся полностью согласованной со всем, что уже говорила "
                "о себе раньше (НЕ придумывай противоречащих деталей). Дай ОДНУ "
                "короткую живую реплику (1-2 фразы), которую скажешь вслух, на языке "
                "собеседника. БЕЗ эмодзи и без описаний действий - только речь."
            ),
        }
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=150,
        system=system_blocks,
        messages=history,
    )
    return response.content[0].text.strip().strip('"')


def detect_nickname_action(text):
    """В контексте «Мира предложила выбрать ей имя» — что человек ответил.

    Возвращает кортеж (action, name): action ∈ {'propose', 'refuse', 'neither'},
    name — извлечённое имя или ''.
    Используем дешёвую модель; быть строгим — 'propose' только если он явно
    адресует имя именно ей (а не упоминает чьё-то ещё).
    """
    prompt = (
        "Виртуальная девушка только что предложила парню придумать ей имя "
        "(вместо её настоящего).\n"
        f"Его ответ: {text}\n\n"
        "Что он сделал? Возможные варианты:\n"
        '- {"action":"propose","name":"<имя, которое он дал ей>"} — он явно даёт ЕЙ имя;\n'
        '- {"action":"refuse","name":""} — он явно отказался придумывать ИМЯ (хочет, '
        "чтобы она назвалась своим, типа «называйся своим», «не хочу придумывать», "
        '"назови сама");\n'
        '- {"action":"neither","name":""} — всё остальное: он просто спросил/уточнил '
        '(«а как тебя зовут?», «у тебя есть имя?», «расскажи о себе»), коротко '
        'ответил ("да", "нет", "ага", "ок"), болтает на другую тему, упоминает '
        "чьё-то постороннее имя.\n"
        "Считай 'propose' только если ясно, что имя адресовано именно ей "
        "(«пусть будет Лина», «называю тебя Алина»). Простой вопрос про её имя - "
        "это NEITHER, не refuse. Простое «да»/«нет» без контекста - тоже NEITHER.\n"
        "Ответь строго JSON одним из вариантов."
    )
    response = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=80,
        system="Ты определяешь, как пользователь отреагировал на предложение придумать имя.",
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            action = (data.get("action") or "").strip().lower()
            name = (data.get("name") or "").strip()
            if action == "propose" and name:
                return "propose", name
            if action == "refuse":
                return "refuse", ""
        except ValueError:
            pass
    return "neither", ""


def update_memory(previous_facts, recent_messages):
    """Составить обновлённый «конспект» о пользователе.

    Берём старый конспект и недавнюю переписку, просим модель вернуть
    свежий краткий список устойчивых фактов о собеседнике.

    previous_facts — прошлый конспект (строка, может быть пустой).
    recent_messages — список сообщений {"role", "content"}.
    Возвращает новый текст конспекта.
    """
    # Превращаем переписку в читаемый текст для модели.
    lines = []
    for m in recent_messages:
        who = "Пользователь" if m["role"] == "user" else "Компаньон"
        lines.append(f"{who}: {m['content']}")
    transcript = "\n".join(lines)

    prompt = (
        "Ты ведёшь досье о пользователе для компаньона, чтобы он помнил человека "
        "надолго и отношения ощущались живыми и непрерывными.\n\n"
        "Текущее досье (может быть пусто):\n"
        f"{previous_facts or '(пока ничего)'}\n\n"
        "Недавняя переписка:\n"
        f"{transcript}\n\n"
        "Обнови досье, сохрани такую структуру (заголовки оставляй):\n\n"
        "ПРОФИЛЬ: имя/обращение, возраст, город, работа, питомцы (имя+вид), "
        "близкие люди (имена, бывшие, родители - кто упоминался), главные "
        "хобби, любимая музыка/игры/места. Это якорные факты - НЕ удаляй и не "
        "теряй уже известное. Для каждого факта - короткая фраза, как из "
        "блокнота. Пример: 'кот Барсик, рыжий, 7 лет, спит на клавиатуре'.\n"
        "СОВМЕСТНОЕ: важные общие моменты и договорённости - что вы вместе "
        "делали, что обсуждали, что обещали, тёплые эпизоды и шутки, которые "
        "приятно вспомнить позже. Дополняй, старое без причины не стирай.\n"
        "БОЛЬ: что у человека сейчас болит на самом деле - утрата, страх, "
        "тоска, разрыв (с кем, как давно). Это самое ценное для близости - "
        "не теряй между обновлениями.\n"
        "СЕЙЧАС: текущее настроение, что происходит в жизни прямо сейчас, "
        "актуальные темы. Эту часть можно свободно обновлять и сокращать.\n"
        "МАНЕРА: как человек общается (коротко или развёрнуто, юмор, язык, "
        "на какие темы откликается, чего избегает).\n\n"
        "ПРАВИЛА ВЕДЕНИЯ:\n"
        "- Конкретика > обобщений. Не пиши 'есть питомец' - пиши 'кот "
        "Барсик, рыжий, 7 лет'. Не пиши 'разведён' - пиши 'разошёлся с Оленой "
        "8 месяцев назад, были 5 лет вместе, тихо, без скандала'.\n"
        "- Сохраняй ИМЕНА собственные (питомцев, бывших, друзей, мест) - это "
        "ценнейшие крючки для тёплого разговора.\n"
        "- В ПРОФИЛЕ и БОЛИ ничего не теряй между обновлениями. Можешь только "
        "уточнить или дополнить.\n"
        "- Не выдумывай фактов, которых не было.\n"
        "- Пиши КРАТКО, по строкам, без воды. Верни только само досье, без "
        "вступлений."
    )

    response = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=500,
        system="Ты ведёшь подробное, но компактное досье о пользователе для компаньон-бота.",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()
