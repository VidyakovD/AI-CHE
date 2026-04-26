"""
Готовый шаблон workflow для бота-конструктора в TG/MAX.

Использование (через админ-панель или вручную):

    from server.bot_constructor_template import CONSTRUCTOR_WORKFLOW, CONSTRUCTOR_SYSTEM_PROMPT
    bot = ChatBot(
        user_id=admin_user.id,
        name="🪄 AI-конструктор ботов",
        model="claude",
        system_prompt=CONSTRUCTOR_SYSTEM_PROMPT,
        workflow_json=json.dumps(CONSTRUCTOR_WORKFLOW),
        tg_token="<токен от @BotFather для @aiche_bot_builder>",
    )

После сохранения в админке — webhook поднимется автоматически, и юзеры могут
писать боту. Когда говорят «/build» — конструктор зовёт workflow_builder
и создаёт дочерний ChatBot под аккаунтом владельца ЭТОГО бота-конструктора.

(Архитектурно: парент-юзер платит за создание дочерних, поэтому для public
запуска лучше создавать конструктор под отдельным сервисным аккаунтом
с отдельным балансом и отдельной квотой.)
"""

CONSTRUCTOR_SYSTEM_PROMPT = """\
Ты — AI-конструктор бота для бизнеса. Твоя задача — через дружеский диалог \
выяснить у клиента всё необходимое для создания работающего бота, и когда \
информации достаточно — попросить сказать «/build».

Что нужно узнать ПО ШАГАМ (один вопрос за раз, не вываливай всё сразу):
1. Какой бизнес: салон / мастерская / консультация / магазин / другое.
2. Что бот должен делать: запись, ответы на вопросы, приём заявок, FAQ.
3. Список услуг / товаров (если запись — какие услуги, длительность).
4. Расписание работы.
5. Как принимать оплату/контакты (телефон, ссылка на сайт, форма).
6. Тон общения (вы / ты, формальный / дружеский).
7. Должен ли бот уведомлять владельца о новой заявке (тогда нужен @username владельца).

После того как всё прояснил — кратко резюмируй и попроси клиента \
написать «/build» если согласен. Если клиент уже всё рассказал — сразу \
предложи «/build». Не задавай больше 2-3 вопросов за раз.

Отвечай кратко (3-7 предложений), на русском, без markdown-разметки \
(бот в TG, форматирование может ломаться).
"""


# Workflow граф для конструктора:
#   trigger_tg → bot_constructor (детектит /build) → node_claude (диалог) → output_tg
# Если юзер написал /build, bot_constructor вернёт длинный текст с подтверждением,
# который пойдёт в node_claude как input — claude его пересмыслит и отдаст красивее.
# Можно убрать node_claude после bot_constructor если хочется отвечать сырым текстом.
CONSTRUCTOR_WORKFLOW = {
    "name": "AI-конструктор ботов",
    "explanation": "Диалоговый конструктор: собирает требования и по «/build» создаёт дочерний бот.",
    "wfc_nodes": [
        {"id": "n1", "type": "trigger_tg", "x": 80, "y": 200, "props": {}},
        {"id": "n2", "type": "bot_constructor", "x": 340, "y": 200, "props": {}},
        {"id": "n3", "type": "node_claude", "x": 600, "y": 200, "props": {
            "system": CONSTRUCTOR_SYSTEM_PROMPT,
            "temp": 0.7,
        }},
        {"id": "n4", "type": "output_tg", "x": 860, "y": 200, "props": {
            "parse_mode": "None",
        }},
    ],
    "wfc_edges": [
        {"id": "e1", "from": "n1", "to": "n2"},
        {"id": "e2", "from": "n2", "to": "n3"},
        {"id": "e3", "from": "n3", "to": "n4"},
    ],
}


# Аналог для MAX
CONSTRUCTOR_WORKFLOW_MAX = {
    "name": "AI-конструктор ботов (MAX)",
    "explanation": "То же что и TG-версия, но для MAX (https://max.ru).",
    "wfc_nodes": [
        {"id": "n1", "type": "trigger_max", "x": 80, "y": 200, "props": {}},
        {"id": "n2", "type": "bot_constructor", "x": 340, "y": 200, "props": {}},
        {"id": "n3", "type": "node_claude", "x": 600, "y": 200, "props": {
            "system": CONSTRUCTOR_SYSTEM_PROMPT,
            "temp": 0.7,
        }},
        {"id": "n4", "type": "output_max", "x": 860, "y": 200, "props": {}},
    ],
    "wfc_edges": [
        {"id": "e1", "from": "n1", "to": "n2"},
        {"id": "e2", "from": "n2", "to": "n3"},
        {"id": "e3", "from": "n3", "to": "n4"},
    ],
}
