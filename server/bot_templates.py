"""
Готовые шаблоны бот-workflow для типичных бизнес-задач.

Используются эндпоинтом POST /chatbots/from-template/{slug} — клонирует
шаблон под нового бота юзера, настраивает webhook'и в TG/MAX.

Каждый шаблон — это словарь:
  slug          — id шаблона
  name          — отображаемое имя
  category      — для группировки в галерее
  short         — 1-строчное описание для карточки
  description   — что бот делает (для tooltip / превью)
  who           — для кого (салоны, агентства, магазины…)
  recommended_model — gpt | claude | …
  default_name  — имя нового бота (юзер может переопределить)
  workflow      — JSON графа (wfc_nodes + wfc_edges)
  customizable  — список ключей для онбординг-визарда («Список услуг», «Имя компании»)

Архитектурно: workflow ноды используют богатые UX-возможности (request_contact,
inline-кнопки, save_record, edit_message, chat_action_typing) — все они есть
в server/chatbot_engine.py.

Чтобы юзер мог писать боту в обоих мессенджерах, делаем по два triggers
(trigger_tg + trigger_max) в одном workflow — каждый ведёт к одной AI-ноде.
"""


# ───────────────────────────── 1. Лидогенерация ─────────────────────────────

LEAD_CAPTURE = {
    "slug": "lead_capture",
    "name": "Лидогенерация",
    "category": "Продажи",
    "short": "Захват контактов: имя, телефон, ниша. Уведомление в TG.",
    "description": (
        "Бот собирает контактные данные клиентов через дружелюбный диалог. "
        "Когда клиент готов — просит поделиться номером кнопкой одного нажатия "
        "и сохраняет лид. Владелец сразу получает уведомление в Telegram."
    ),
    "who": "B2B-агентства, услуги, консультанты",
    "recommended_model": "gpt-4o-mini",
    "default_name": "🟢 Лид-бот",
    "customizable": [
        {"key": "company", "label": "Название компании", "placeholder": "ООО \"Ромашка\"", "required": True},
        {"key": "what_we_do", "label": "Чем вы занимаетесь (1-2 предложения)",
         "placeholder": "Делаем Telegram-боты для бизнеса под ключ за 7 дней.",
         "required": True, "multiline": True},
        {"key": "owner_tg_chat_id", "label": "Ваш TG chat_id для уведомлений",
         "placeholder": "Узнать у @userinfobot", "required": False},
    ],
    "workflow": {
        "name": "Лидогенерация",
        "wfc_nodes": [
            {"id": "trg", "type": "trigger_tg", "x": 80, "y": 200, "props": {}},
            {"id": "ai", "type": "node_gpt", "x": 340, "y": 200, "props": {
                "system": (
                    "Ты — менеджер компании {{company}}. Что мы делаем: {{what_we_do}}.\n\n"
                    "Твоя задача — за 2-4 коротких сообщения квалифицировать клиента: "
                    "понять что ему нужно, какой у него запрос. Спрашивай по одному вопросу. "
                    "Когда понятно что клиент заинтересован — скажи: «Отлично, "
                    "оставьте номер — мы вам перезвоним и обсудим детали» и попроси "
                    "поделиться контактом. На команду /start представься.\n\n"
                    "Тон: дружелюбный, по-человечески, без формальностей. Без эмодзи. "
                    "Не больше 2-3 предложений в каждом ответе."
                ),
                "temp": 0.7,
            }},
            {"id": "ask", "type": "request_contact", "x": 600, "y": 200, "props": {
                "prompt": "Поделитесь номером телефона — мы перезвоним:",
                "button": "📞 Поделиться номером",
            }},
            {"id": "save", "type": "save_record", "x": 860, "y": 200, "props": {
                "record_type": "lead",
                "notify_owner": True,
                "owner_tg_chat_id": "{{owner_tg_chat_id}}",
                "ack_text": "Спасибо! 🙌 Передали менеджеру — свяжемся в течение часа.",
            }},
            {"id": "out", "type": "output_tg", "x": 1120, "y": 200, "props": {}},
        ],
        "wfc_edges": [
            {"id": "e1", "from": "trg", "to": "ai"},
            {"id": "e2", "from": "ai", "to": "ask"},
            {"id": "e3", "from": "ask", "to": "save"},
            {"id": "e4", "from": "save", "to": "out"},
        ],
    },
}


# ───────────────────────── 2. Продажи / прогрев ─────────────────────────────

SALES_WARMUP = {
    "slug": "sales_warmup",
    "name": "Продажи / прогрев",
    "category": "Продажи",
    "short": "AI презентует продукт, отвечает на возражения, ведёт к оплате.",
    "description": (
        "Бот работает как опытный продавец: рассказывает про продукт, "
        "развёрнуто отвечает на любые возражения, и в нужный момент даёт "
        "ссылку на оплату. Использует Claude Sonnet — он лучше всего держит "
        "длинный продающий диалог."
    ),
    "who": "Инфопродукты, услуги, SaaS",
    "recommended_model": "claude",
    "default_name": "💰 Продажный бот",
    "customizable": [
        {"key": "product", "label": "Что вы продаёте", "placeholder": "Курс по таргету ВК — 12 уроков",
         "required": True},
        {"key": "price", "label": "Цена", "placeholder": "9 900 ₽", "required": True},
        {"key": "benefits", "label": "Главные выгоды (3-5 пунктов через запятую)",
         "placeholder": "поддержка кураторов, домашки с проверкой, кейсы выпускников",
         "required": True, "multiline": True},
        {"key": "payment_link", "label": "Ссылка на оплату (ЮKassa / Tinkoff)",
         "placeholder": "https://...", "required": True},
    ],
    "workflow": {
        "name": "Продающий бот",
        "wfc_nodes": [
            {"id": "trg", "type": "trigger_tg", "x": 80, "y": 200, "props": {}},
            {"id": "typing", "type": "chat_action_typing", "x": 280, "y": 200, "props": {}},
            {"id": "ai", "type": "node_claude", "x": 480, "y": 200, "props": {
                "system": (
                    "Ты — продавец-консультант. Продукт: {{product}}. Цена: {{price}}.\n"
                    "Главные выгоды: {{benefits}}.\n"
                    "Ссылка на оплату: {{payment_link}}.\n\n"
                    "Твоя задача — рассказать о продукте, по-человечески ответить на "
                    "возражения («дорого», «подумаю», «не уверен», «у конкурентов дешевле»), "
                    "и в нужный момент дать ссылку на оплату прямо в сообщении.\n\n"
                    "Правила:\n"
                    "- Отвечай развёрнуто (3-7 предложений), но без воды.\n"
                    "- На «дорого» — объясняй ROI, рассрочку, гарантию.\n"
                    "- На «подумаю» — уточняй, что именно вызывает сомнение.\n"
                    "- Когда клиент тёплый — давай ссылку: «Оплатить можно тут: {{payment_link}}».\n"
                    "- Не дави, не используй манипуляции.\n"
                    "- Тон уверенный и заботливый, без воды и эмодзи."
                ),
                "temp": 0.6,
                "max_tokens": 800,
            }},
            {"id": "out", "type": "output_tg", "x": 760, "y": 200, "props": {}},
        ],
        "wfc_edges": [
            {"id": "e1", "from": "trg", "to": "typing"},
            {"id": "e2", "from": "typing", "to": "ai"},
            {"id": "e3", "from": "ai", "to": "out"},
        ],
    },
}


# ─────────────────── 3. FAQ-бот / поддержка клиентов ────────────────────────

FAQ_SUPPORT = {
    "slug": "faq_support",
    "name": "FAQ / поддержка",
    "category": "Поддержка",
    "short": "Отвечает по базе знаний, не знает — переводит на оператора.",
    "description": (
        "Бот отвечает на частые вопросы клиентов опираясь на загруженные "
        "вами документы (PDF, инструкции, FAQ). Если не знает ответа — "
        "сохраняет вопрос как тикет и пересылает оператору."
    ),
    "who": "Магазины, сервисы, B2B-поддержка",
    "recommended_model": "gpt-4o-mini",
    "default_name": "🛟 Поддержка",
    "customizable": [
        {"key": "company", "label": "Название компании", "placeholder": "Магазин «Ромашка»", "required": True},
        {"key": "tone", "label": "Тон ответа",
         "options": ["Дружелюбный", "Официальный", "Краткий"],
         "default": "Дружелюбный"},
        {"key": "operator_tg_chat_id", "label": "TG chat_id оператора (для эскалации)",
         "placeholder": "Узнать у @userinfobot", "required": False},
        {"key": "fallback_msg", "label": "Что отвечать когда бот не знает",
         "default": "Я не уверен в ответе на этот вопрос. Передал ваш запрос менеджеру — он скоро ответит.",
         "multiline": True},
    ],
    "workflow": {
        "name": "FAQ-бот",
        "wfc_nodes": [
            {"id": "trg", "type": "trigger_tg", "x": 80, "y": 200, "props": {}},
            {"id": "typing", "type": "chat_action_typing", "x": 280, "y": 200, "props": {}},
            # kb_rag — поиск по базе знаний бота. Если есть — ответит, если нет —
            # вернёт fallback. Юзер загружает файлы через UI «База знаний» бота.
            {"id": "rag", "type": "kb_rag", "x": 500, "y": 200, "props": {
                "model": "gpt-4o-mini",
                "top": 4,
                "system_extra": (
                    "Ты — поддержка компании {{company}}. Тон: {{tone}}. "
                    "Отвечай только на основе предоставленного контекста. "
                    "Если контекста недостаточно — напиши ровно слово: NOFAQ."
                ),
            }},
            # condition: если в ответе есть NOFAQ — эскалация, иначе обычный output
            {"id": "cond", "type": "condition", "x": 760, "y": 200, "props": {
                "check": "NOFAQ",
            }},
            # branch true — эскалация
            {"id": "save_ticket", "type": "save_record", "x": 1020, "y": 80, "props": {
                "record_type": "ticket",
                "notify_owner": True,
                "owner_tg_chat_id": "{{operator_tg_chat_id}}",
                "ack_text": "{{fallback_msg}}",
            }},
            {"id": "out_esc", "type": "output_tg", "x": 1280, "y": 80, "props": {}},
            # branch false — обычный ответ
            {"id": "out_ok", "type": "output_tg", "x": 1020, "y": 320, "props": {}},
        ],
        "wfc_edges": [
            {"id": "e1", "from": "trg", "to": "typing"},
            {"id": "e2", "from": "typing", "to": "rag"},
            {"id": "e3", "from": "rag", "to": "cond"},
            {"id": "e4", "from": "cond", "to": "save_ticket"},
            {"id": "e5", "from": "save_ticket", "to": "out_esc"},
            {"id": "e6", "from": "cond", "to": "out_ok"},
        ],
    },
}


# ─────────────────────── 4. Запись / бронирование ───────────────────────────

BOOKING = {
    "slug": "booking",
    "name": "Запись на услугу",
    "category": "Запись",
    "short": "Меню услуг → дата → время → телефон → запись + уведомление.",
    "description": (
        "Идеально для салонов, барбершопов, мастерских, репетиторов, врачей. "
        "Клиент выбирает услугу, дату и время через кнопки, оставляет телефон "
        "одним нажатием. Запись попадает в админку и владелец видит её сразу "
        "в Telegram."
    ),
    "who": "Салоны, мастерские, врачи, репетиторы",
    "recommended_model": "gpt-4o-mini",
    "default_name": "📅 Бот записи",
    "customizable": [
        {"key": "company", "label": "Название", "placeholder": "Салон «Орхидея»", "required": True},
        {"key": "services", "label": "Список услуг (по одной в строку)",
         "placeholder": "Маникюр\nПедикюр\nСтрижка", "required": True, "multiline": True},
        {"key": "schedule_hint", "label": "Часы работы",
         "placeholder": "Пн-Сб 10:00–20:00, Вс выходной", "required": True},
        {"key": "owner_tg_chat_id", "label": "TG chat_id мастера (уведомления)",
         "placeholder": "Узнать у @userinfobot", "required": False},
    ],
    "workflow": {
        "name": "Бот записи",
        "wfc_nodes": [
            {"id": "trg", "type": "trigger_tg", "x": 80, "y": 200, "props": {}},
            # AI ведёт диалог: услуга → дата → время → имя.
            # На request_contact он выходит сам, попросив телефон кнопкой.
            {"id": "ai", "type": "node_gpt", "x": 340, "y": 200, "props": {
                "system": (
                    "Ты — администратор {{company}}. Твоя задача — записать клиента на услугу.\n\n"
                    "Услуги: {{services}}.\n"
                    "Часы работы: {{schedule_hint}}.\n\n"
                    "Алгоритм:\n"
                    "1. Поприветствуй и спроси какая услуга нужна.\n"
                    "2. Спроси удобную дату и время (предложи 2-3 варианта если уместно).\n"
                    "3. Спроси имя.\n"
                    "4. Когда есть услуга, дата, время, имя — напиши: «Отлично, "
                    "{имя}! Чтобы подтвердить запись, поделитесь номером телефона:» "
                    "и просто СКАЖИ что нужен номер. Системе автоматически появится "
                    "кнопка «Поделиться номером».\n\n"
                    "Тон: дружелюбный, по-человечески. Один вопрос за раз. Без эмодзи."
                ),
                "temp": 0.6,
            }},
            {"id": "ask_phone", "type": "request_contact", "x": 620, "y": 200, "props": {
                "prompt": "Чтобы подтвердить запись, поделитесь номером:",
                "button": "📞 Поделиться номером",
            }},
            {"id": "save", "type": "save_record", "x": 880, "y": 200, "props": {
                "record_type": "booking",
                "notify_owner": True,
                "owner_tg_chat_id": "{{owner_tg_chat_id}}",
                "ack_text": "✅ Записал вас. До встречи! Если понадобится перенести — просто напишите.",
            }},
            {"id": "out", "type": "output_tg", "x": 1140, "y": 200, "props": {}},
        ],
        "wfc_edges": [
            {"id": "e1", "from": "trg", "to": "ai"},
            {"id": "e2", "from": "ai", "to": "ask_phone"},
            {"id": "e3", "from": "ask_phone", "to": "save"},
            {"id": "e4", "from": "save", "to": "out"},
        ],
    },
}


# ─────────────────────────── 5. Квиз / воронка ──────────────────────────────

QUIZ_FUNNEL = {
    "slug": "quiz_funnel",
    "name": "Квиз / воронка",
    "category": "Маркетинг",
    "short": "Серия вопросов → сегментация → персональный результат.",
    "description": (
        "Бот задаёт 4-6 вопросов, классифицирует клиента (сегмент) и выдаёт "
        "персонализированный результат: подбор продукта, оценку, рекомендацию. "
        "Все ответы и сегмент сохраняются — можно потом сделать рассылку по "
        "сегменту."
    ),
    "who": "Образование, услуги, маркетплейсы",
    "recommended_model": "gpt-4o-mini",
    "default_name": "📝 Квиз-воронка",
    "customizable": [
        {"key": "topic", "label": "Тема квиза", "placeholder": "Какой курс по программированию вам подойдёт",
         "required": True},
        {"key": "questions", "label": "Список вопросов (по одному в строку, 3-7 шт)",
         "placeholder": "Какой ваш уровень: новичок / средний / продвинутый\nСколько часов в неделю готовы учиться\nЧего хотите достичь",
         "required": True, "multiline": True},
        {"key": "segments", "label": "Сегменты и рекомендации",
         "placeholder": "новичок → курс «Python с нуля», 12 недель, 12000 ₽\nсредний → курс «Backend на FastAPI», 8 недель, 18000 ₽",
         "required": True, "multiline": True},
    ],
    "workflow": {
        "name": "Квиз-воронка",
        "wfc_nodes": [
            {"id": "trg", "type": "trigger_tg", "x": 80, "y": 200, "props": {}},
            {"id": "typing", "type": "chat_action_typing", "x": 280, "y": 200, "props": {}},
            {"id": "ai", "type": "node_gpt", "x": 480, "y": 200, "props": {
                "system": (
                    "Ты ведёшь квиз: «{{topic}}».\n\n"
                    "Вопросы по порядку:\n{{questions}}\n\n"
                    "Сегменты и что рекомендовать:\n{{segments}}\n\n"
                    "Алгоритм:\n"
                    "1. На /start или первое сообщение — поприветствуй и задай первый вопрос.\n"
                    "2. После ответа — задай следующий вопрос. По одному за раз.\n"
                    "3. Когда все вопросы заданы — определи сегмент по ответам и "
                    "напиши развёрнутую персональную рекомендацию (5-10 предложений) "
                    "именно под этот сегмент.\n"
                    "4. В конце — призыв к действию: «Хотите узнать подробнее? "
                    "Оставьте номер — мы свяжемся».\n\n"
                    "Тон: дружелюбный эксперт. Без воды. Без эмодзи."
                ),
                "temp": 0.7,
                "max_tokens": 800,
            }},
            {"id": "out", "type": "output_tg", "x": 760, "y": 200, "props": {}},
        ],
        "wfc_edges": [
            {"id": "e1", "from": "trg", "to": "typing"},
            {"id": "e2", "from": "typing", "to": "ai"},
            {"id": "e3", "from": "ai", "to": "out"},
        ],
    },
}


# ──────────────────── 6. Контент-бот / рассылка ─────────────────────────────

CONTENT_BROADCAST = {
    "slug": "content_broadcast",
    "name": "Контент / рассылка",
    "category": "Контент",
    "short": "Подписка, выдача гайдов, регулярная рассылка по расписанию.",
    "description": (
        "Бот выдаёт лид-магниты (PDF, гайды, видео-ссылки) при подписке "
        "и потом регулярно шлёт полезный контент по расписанию. "
        "Подписчики накапливаются — потом по ним можно делать акции."
    ),
    "who": "Эксперты, медиа, образование",
    "recommended_model": "gpt-4o-mini",
    "default_name": "📬 Контент-бот",
    "customizable": [
        {"key": "company", "label": "Имя автора / канала", "placeholder": "Иван Петров", "required": True},
        {"key": "topic", "label": "Тема канала", "placeholder": "Маркетинг для малого бизнеса",
         "required": True},
        {"key": "lead_magnet", "label": "Что выдать сразу при подписке",
         "placeholder": "Ссылка на PDF-гайд или текст приветственного материала",
         "required": True, "multiline": True},
    ],
    "workflow": {
        "name": "Контент-бот",
        "wfc_nodes": [
            {"id": "trg", "type": "trigger_tg", "x": 80, "y": 200, "props": {}},
            {"id": "ai", "type": "node_gpt", "x": 340, "y": 200, "props": {
                "system": (
                    "Ты — бот канала «{{company}}» по теме «{{topic}}».\n\n"
                    "При первом сообщении или /start — поприветствуй и сразу выдай "
                    "лид-магнит:\n\n{{lead_magnet}}\n\n"
                    "На дальнейшие вопросы — отвечай по теме «{{topic}}» как эксперт. "
                    "Тон: дружелюбный, простой язык, без воды. Без эмодзи. "
                    "Если спрашивают что-то вне темы — мягко возвращай в тему."
                ),
                "temp": 0.7,
            }},
            {"id": "save_sub", "type": "save_record", "x": 600, "y": 200, "props": {
                "record_type": "subscriber",
                "notify_owner": False,
                "ack_text": "",
            }},
            {"id": "out", "type": "output_tg", "x": 860, "y": 200, "props": {}},
        ],
        "wfc_edges": [
            {"id": "e1", "from": "trg", "to": "ai"},
            {"id": "e2", "from": "ai", "to": "save_sub"},
            {"id": "e3", "from": "save_sub", "to": "out"},
        ],
    },
}


# ──────────────────────── 7. Авто-КП по входящим письмам ────────────────────

AUTO_PROPOSAL_EMAIL = {
    "slug": "auto_proposal_email",
    "name": "Авто-КП по запросам в почту",
    "category": "Продажи",
    "short": "Читает письма с запросами → AI составляет КП → отправляет PDF в ответ.",
    "description": (
        "Бот-оркестратор: каждые 60 сек проверяет inbox через IMAP. На каждое "
        "новое письмо генерирует персонализированное коммерческое предложение "
        "(под бренд + прайс из бота), сохраняет PDF и автоматически отвечает "
        "клиенту вложением. Идеально для отдела продаж B2B — менеджер только "
        "правит отправленные КП и закрывает сделки."
    ),
    "who": "B2B-продажи, агентства, консультанты",
    "recommended_model": "claude",
    "default_name": "📨 Авто-КП по почте",
    "customizable": [
        {"key": "imap_cred_id", "label": "IMAP-credential id (создать в /admin)",
         "placeholder": "1", "required": True},
        {"key": "brand_id", "label": "Бренд (id из /proposals.html)",
         "placeholder": "1", "required": True},
        {"key": "bot_id_for_price", "label": "Бот с прайсом (id, опц.)",
         "placeholder": "оставьте пустым — возьмём текущего бота", "required": False},
        {"key": "email_subject", "label": "Тема ответа",
         "placeholder": "Re: {{subject}} — наше КП",
         "default": "Re: {{subject}} — коммерческое предложение"},
        {"key": "reply_body", "label": "Текст письма",
         "placeholder": "Здравствуйте{{salut}}! Спасибо за запрос. Во вложении КП.",
         "multiline": True,
         "default": "Здравствуйте{{salut}}!\n\nСпасибо за ваш запрос. Во вложении — наше коммерческое предложение, подготовленное специально под вашу задачу.\n\nЕсли возникнут вопросы — мы на связи."},
    ],
    "workflow": {
        "name": "Авто-КП по почте",
        "wfc_nodes": [
            {"id": "trg", "type": "trigger_imap", "x": 80, "y": 200, "props": {
                "cfg": {"cred_id": "{{imap_cred_id}}"}
            }},
            {"id": "kp", "type": "auto_proposal", "x": 340, "y": 200, "props": {
                "cfg": {
                    "brand_id": "{{brand_id}}",
                    "bot_id_for_price": "{{bot_id_for_price}}",
                    "email_subject": "{{email_subject}}",
                    "reply_body": "{{reply_body}}",
                    "send_email": True,
                }
            }},
            {"id": "rec", "type": "save_record", "x": 600, "y": 200, "props": {
                "cfg": {
                    "record_type": "proposal_sent",
                    "notify_owner": False,
                    "ack_text": "✓ КП отправлено",
                }
            }},
        ],
        "wfc_edges": [
            {"id": "e1", "from": "trg", "to": "kp"},
            {"id": "e2", "from": "kp", "to": "rec"},
        ],
    },
}


# ──────────────────────── Регистрация шаблонов ──────────────────────────────

TEMPLATES = [
    LEAD_CAPTURE,
    SALES_WARMUP,
    FAQ_SUPPORT,
    BOOKING,
    QUIZ_FUNNEL,
    CONTENT_BROADCAST,
    AUTO_PROPOSAL_EMAIL,
]

TEMPLATES_BY_SLUG = {t["slug"]: t for t in TEMPLATES}


def list_templates() -> list[dict]:
    """Список шаблонов для UI-галереи (без полного workflow — только мета)."""
    return [{
        "slug": t["slug"],
        "name": t["name"],
        "category": t["category"],
        "short": t["short"],
        "description": t["description"],
        "who": t["who"],
        "recommended_model": t["recommended_model"],
        "default_name": t["default_name"],
        "customizable": t["customizable"],
    } for t in TEMPLATES]


def render_template(slug: str, params: dict) -> dict | None:
    """Подставляет {{ключ}} в workflow по списку params.

    Возвращает {name, workflow_json (str), recommended_model} или None.
    """
    import json, re
    tpl = TEMPLATES_BY_SLUG.get(slug)
    if not tpl:
        return None
    # Сериализуем workflow и подменяем плейсхолдеры в строках
    raw = json.dumps(tpl["workflow"], ensure_ascii=False)
    # Заполняем дефолтами для не-заданных полей чтобы не получить «{{x}}» в проде
    full_params = {}
    for f in tpl["customizable"]:
        full_params[f["key"]] = params.get(f["key"]) or f.get("default") or ""
    for k, v in full_params.items():
        # Эскейпим " чтобы не порвать JSON
        v_safe = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        raw = re.sub(r"\{\{\s*" + re.escape(k) + r"\s*\}\}", v_safe, raw)
    return {
        "name": params.get("bot_name") or tpl["default_name"],
        "workflow_json": raw,
        "recommended_model": tpl["recommended_model"],
        "template_slug": slug,
    }
