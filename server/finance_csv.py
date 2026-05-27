"""Парсер банковских CSV-выписок + keyword-категоризатор для модуля finance.

Поддерживает форматы:
  - Tinkoff (operations export): "Дата операции;Дата платежа;Номер карты;
                                  Статус;Сумма операции;Валюта операции;...
                                  Описание;..."
  - Sber (стандартный CSV экспорт)
  - Alfa-Bank
  - Generic: 3+ колонки {date, amount, description} в любом порядке —
    эвристика по заголовкам.

Не используем LLM для категоризации (дорого + медленно для 1000 строк).
Вместо этого — keyword-based правила (_CATEGORY_RULES) распознают типичные
паттерны: «PYATEROCHKA», «YANDEX TAXI», «ZHKH». Юзер может переопределить
категорию транзакции вручную (is_manual_cat=True), и при следующих импортах
наша эвристика её не перепишет.

LLM подключается опционально для unknown-категории — когда модуль finance
получает задачу типа «классифицируй мне эти 50 транзакций», тогда уже
дорогой вызов оправдан. Но при import — никакого LLM.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

log = logging.getLogger(__name__)


# Стандартные категории (slug → human-readable)
CATEGORIES: dict[str, str] = {
    "food":       "🍲 Еда / продукты",
    "cafe":       "☕ Кафе / рестораны",
    "transport":  "🚖 Транспорт",
    "fuel":       "⛽ АЗС / топливо",
    "shopping":   "🛍 Магазины",
    "clothing":   "👕 Одежда",
    "health":     "🏥 Здоровье / аптеки",
    "entertain":  "🎬 Развлечения",
    "subscript":  "📺 Подписки",
    "utility":    "🏠 ЖКХ / связь",
    "travel":     "✈️ Путешествия",
    "education":  "🎓 Образование",
    "transfer":   "💸 Переводы между своими",
    "p2p":        "👥 Переводы людям",
    "income":     "💰 Поступления / зарплата",
    "tax":        "🏛 Налоги / штрафы",
    "fees":       "💳 Комиссии",
    "atm":        "🏧 Снятие наличных",
    "other":      "📦 Прочее",
}


# Ключевые слова → категория. Идём по списку сверху вниз: первое совпадение
# выигрывает. Регистр-независимо (lower).
_CATEGORY_RULES: list[tuple[str, list[str]]] = [
    # Продукты — поиск по сетям
    ("food", [
        "pyaterochka", "пятерочка", "пятёрочка", "magnit", "магнит",
        "lenta", "лента", "perekrestok", "перекрест", "auchan", "ашан",
        "vkusvill", "вкусвилл", "azbuka vkusa", "metro", "globus", "spar",
        "okey", "о'кей", "fix price", "fixprice", "krasnoe i beloe",
        "красное и белое", "vinlab", "winelab",
    ]),
    # Кафе и рестораны
    ("cafe", [
        "coffee", "кофе", "starbucks", "shokoladnica", "шоколадниц",
        "burger", "kfc", "макдоналдс", "mcdonalds", "вкусно и точка",
        "vkusno", "rostic", "ростикс", "papa john", "dodo", "додо",
        "subway", "сабвей", "теремок", "stolovaya", "столовая",
        "kafe ", "ресторан", "restaurant", "yandex eda", "яндекс еда",
        "delivery club", "kuhnya", "кухня",
    ]),
    # Транспорт
    ("transport", [
        "yandex taxi", "яндекс такси", "uber", "uber.ru", "citymobil",
        "ситимоб", "metro ", "метро", "tramvay", "трамвай", "avtobus",
        "автобус", "marshrut", "маршрут", "rzd", "ржд", "russian railway",
        "delimobil", "делимоб", "yandex.go", "яндекс.go", "bolt ",
        "carsharing", "каршеринг",
    ]),
    # АЗС
    ("fuel", [
        "lukoil", "лукойл", "gazprom-neft", "газпром нефть", "rosneft",
        "роснефть", "shell", "bp ", "tatneft", "татнефть", "neftmagistral",
        "нефтьмагистр", "azs ", "азс ",
    ]),
    # Магазины / шопинг
    ("shopping", [
        "ozon", "озон", "wildberries", "wildb", "вайлдб", "yandex market",
        "yandex.market", "яндекс маркет", "leroy", "леруа", "ikea",
        "икея", "mvideo", "м.видео", "eldorado", "эльдорадо", "dns ",
        "днс ", "citilink", "ситилинк", "kazanexpress",
    ]),
    # Одежда
    ("clothing", [
        "h&m", "zara", "uniqlo", "юникло", "lamoda", "ламода",
        "bershka", "pull&bear", "stradivar", "mango ", "tom tailor",
        "kira plastinina",
    ]),
    # Здоровье / аптеки
    ("health", [
        "apteka", "аптека", "аптек", "rigla", "ригла", "stoletnik",
        "столетник", "366", "ozonopt", "vitamir", "витамир", "medsi",
        "медси", "smart-clinic", "klinika ", "клиник ", "fitness",
        "фитнес", "world class", "x-fit", "уорлд класс",
    ]),
    # Развлечения
    ("entertain", [
        "kinoteatr", "кинотеатр", "kinokassa", "okko", "ivi", "иви",
        "kinopoisk", "кинопоиск", "yandex plus", "яндекс плюс",
        "playstation", "playstati", "steam", "steampowered",
        "nintendo", "konsert", "концерт", "ticketland", "kassir",
        "кассир", "afisha",
    ]),
    # Подписки
    ("subscript", [
        "spotify", "apple.com/bill", "netflix", "icloud", "google one",
        "youtube premium", "yandex music", "music subscription",
        "chatgpt", "openai", "anthropic", "github",
    ]),
    # ЖКХ / связь
    ("utility", [
        "zhkh", "жкх", "edinyy schet", "единый счет", "edinij raschetnyj",
        "vodokanal", "водоканал", "gazprom mezhregiongaz", "энергосбыт",
        "energosbyt", "mosenergosbyt", "интернет", "internet",
        "rostelecom", "ростелеком", "мобильн", "мтс", "mts", "beeline",
        "билайн", "megafon", "мегафон", "tele2", "теле2", "domofon",
        "домофон",
    ]),
    # Путешествия
    ("travel", [
        "aviasales", "авиасейл", "booking.com", "booking", "ozon travel",
        "tutu.ru", "tutu", "туту", "hotel ", "отель", "airbnb",
        "aeroflot", "аэрофлот", "s7", "pobeda", "победа", "ural airlines",
        "ural air", "ural ", "rzd", "ржд",
    ]),
    # Образование
    ("education", [
        "skillbox", "geekbrains", "skyeng", "english domain", "uchi.ru",
        "учи.ру", "yaklass", "якласс", "stepik", "степик", "coursera",
        "udemy", "tinkoff journal", "т-ж",
    ]),
    # Переводы между своими счетами (внутрибанк)
    ("transfer", [
        "перевод между своими", "internal transfer", "popolnenie scheta",
        "пополнение счета", "popolnenie kopilki", "копилк",
        "auto-popolnenie", "автопополнение",
    ]),
    # P2P переводы (СБП и т.п.)
    ("p2p", [
        "sbp ", "сбп ", "fast payment system", "перевод по номеру",
        "перевод клиенту", "tinkoff transfer to", "sber transfer",
        "money transfer to",
    ]),
    # Налоги и штрафы
    ("tax", [
        "fns ", "фнс ", "nalog", "налог", "shtraf", "штраф",
        "gibdd", "гибдд", "gosposhlina", "госпошлин",
    ]),
    # Комиссии
    ("fees", [
        "komissiya", "комиссия", "service fee", "плата за обслуж",
    ]),
    # Снятие наличных
    ("atm", [
        "atm ", "банкомат", "snyatie nalichnyh", "снятие наличных",
        "cash withdrawal",
    ]),
]


# ── Парсер CSV ──────────────────────────────────────────────────────────────


# Распространённые имена колонок (lowercased, нормализованные)
_DATE_COL_HINTS = ("дата", "date", "datetime", "operacii", "операц", "transaction")
_AMOUNT_COL_HINTS = ("сумма", "amount", "summa", "оборот")
_DESC_COL_HINTS = ("опис", "description", "назначение", "контрагент",
                   "наимен", "категория")


def _normalize_header(s: str) -> str:
    return (s or "").strip().lower().replace("ё", "е")


def _parse_decimal(s: str) -> Decimal | None:
    """'1 234,56' / '-50.00 ₽' / '1234.5' → Decimal. None если не парсится."""
    if not s:
        return None
    # Удалить валютные символы, NBSP, обычные пробелы
    cleaned = re.sub(r"[₽$€\s ]+", "", s)
    cleaned = cleaned.replace(",", ".")
    # Убрать всё кроме цифр, точки, знака
    cleaned = re.sub(r"[^0-9.\-]", "", cleaned)
    if not cleaned or cleaned in ("-", "."):
        return None
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def _parse_date(s: str) -> datetime | None:
    """Принимает '01.05.2026', '2026-05-01', '01.05.2026 14:30:00' и др."""
    if not s:
        return None
    s = s.strip()
    fmts = [
        "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%d/%m/%Y", "%m/%d/%Y",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def detect_format(header_row: list[str]) -> str:
    """По заголовкам определяет банк. Возвращает source-метку."""
    h = " ".join(_normalize_header(x) for x in header_row)
    if "дата операции" in h and ("сумма операции" in h or "сумма платежа" in h):
        return "csv:tinkoff"
    if "дата" in h and "сумма" in h and ("сбер" in h or "operation" in h):
        return "csv:sber"
    if "alfa" in h or "альфа" in h:
        return "csv:alfa"
    return "csv:generic"


def _find_col(headers: list[str], hints: tuple[str, ...]) -> int:
    """Индекс первой колонки чьё имя содержит любой из hints. -1 если не нашёл."""
    norm = [_normalize_header(h) for h in headers]
    for i, h in enumerate(norm):
        for hint in hints:
            if hint in h:
                return i
    return -1


def categorize(description: str) -> str:
    """Сопоставить описание транзакции с категорией по keyword-правилам.
    Возвращает slug категории (из CATEGORIES) или 'other'."""
    d = (description or "").lower().replace("ё", "е")
    # Поступления (доход) — отдельная эвристика по словам типа «зарплата»,
    # «возврат», «cashback» (но если amount > 0 — упростим, ловит вызвавший).
    income_kw = ("zarplata", "зарплата", "salary", "vozvrat", "возврат",
                 "cashback", "кешбек", "кэшбек", "процент по сбер",
                 "проценты по", "бонусы накоп")
    for kw in income_kw:
        if kw in d:
            return "income"
    for cat, kws in _CATEGORY_RULES:
        for kw in kws:
            if kw in d:
                return cat
    return "other"


def parse_csv_statement(content: bytes | str, filename: str = "") -> dict:
    """Парсит CSV-выписку. Возвращает {"source", "rows": [{...}], "errors": []}.

    Каждый row: {"date": datetime, "amount_kop": int, "currency": "RUB",
                  "description": str, "category": str, "description_hash": str}.

    Не raise'ит — формирует errors для UI. Битые строки пропускаются.
    Лимит 5000 строк за импорт (защита от больших файлов).
    """
    if isinstance(content, bytes):
        # Авто-определение кодировки: utf-8 / utf-8-sig (BOM) / cp1251.
        # Российские банки часто отдают cp1251.
        text = None
        for enc in ("utf-8-sig", "utf-8", "cp1251", "windows-1251"):
            try:
                text = content.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            return {"source": "?", "rows": [], "errors": ["Не удалось декодировать файл (utf-8/cp1251 не подошли)"]}
    else:
        text = content

    # Детектируем разделитель: ; (русские банки часто) или ,
    sample = text[:2048]
    delim = ";" if sample.count(";") > sample.count(",") else ","

    reader = csv.reader(io.StringIO(text), delimiter=delim)
    try:
        rows = list(reader)
    except csv.Error as e:
        return {"source": "?", "rows": [], "errors": [f"CSV parse error: {e}"]}
    if not rows:
        return {"source": "?", "rows": [], "errors": ["Файл пуст"]}

    headers = rows[0]
    source = detect_format(headers)
    date_idx = _find_col(headers, _DATE_COL_HINTS)
    amount_idx = _find_col(headers, _AMOUNT_COL_HINTS)
    desc_idx = _find_col(headers, _DESC_COL_HINTS)

    if date_idx < 0 or amount_idx < 0:
        return {"source": source, "rows": [], "errors": [
            "Не нашлось колонок «Дата» и «Сумма». Заголовки файла: " +
            ", ".join(headers[:10])
        ]}

    parsed: list[dict] = []
    errors: list[str] = []
    skipped = 0
    for i, row in enumerate(rows[1:5001], start=2):
        if not row or len(row) <= max(date_idx, amount_idx):
            skipped += 1
            continue
        d = _parse_date(row[date_idx])
        amt = _parse_decimal(row[amount_idx])
        if d is None or amt is None:
            skipped += 1
            continue
        desc = row[desc_idx] if (desc_idx >= 0 and desc_idx < len(row)) else ""
        desc = (desc or "").strip()[:500]
        # Копейки: умножаем на 100 и округляем
        amount_kop = int(amt * 100)
        # int4 cap: amount_kop хранится как Integer (32-bit). Пропускаем сверх-
        # большие транзакции (>21M ₽) чтобы не словить OverflowError на INSERT.
        if abs(amount_kop) > 2_000_000_000:
            skipped += 1
            continue
        cat = categorize(desc)
        # Доходы — если сумма > 0 и не классифицировалась как income — оставим
        # other (могут быть возвраты, бонусы — не trivial без LLM).
        if amount_kop > 0 and cat == "other":
            cat = "income"
        desc_hash = hashlib.md5(desc.encode("utf-8", errors="ignore")).hexdigest()[:16]
        parsed.append({
            "date": d,
            "amount_kop": amount_kop,
            "currency": "RUB",
            "description": desc,
            "category": cat,
            "description_hash": desc_hash,
        })

    if skipped:
        errors.append(f"Пропущено {skipped} строк (битые/пустые)")
    return {"source": source, "rows": parsed, "errors": errors}


# ── Сводка для module context ───────────────────────────────────────────────


def build_finance_summary(transactions: list[dict],
                          period_days: int = 30) -> str:
    """Сформировать текстовый блок для system-prompt модуля finance.

    transactions — список dicts с полями date(datetime), amount_kop,
    description, category.
    """
    if not transactions:
        return "💰 Транзакций пока нет. Загрузи CSV-выписку — модуль увидит расходы."

    total_in = sum(t["amount_kop"] for t in transactions if t["amount_kop"] > 0)
    total_out = sum(-t["amount_kop"] for t in transactions if t["amount_kop"] < 0)

    by_cat: dict[str, int] = {}
    for t in transactions:
        if t["amount_kop"] < 0:
            cat = t.get("category") or "other"
            by_cat[cat] = by_cat.get(cat, 0) + (-t["amount_kop"])

    cat_lines = []
    for cat, amount in sorted(by_cat.items(), key=lambda kv: -kv[1]):
        label = CATEGORIES.get(cat, cat)
        cat_lines.append(f"  {label}: {amount/100:.2f} ₽")

    parts = [
        f"═══ ФИНАНСЫ (последние {len(transactions)} транзакций) ═══",
        f"Доходы:  {total_in/100:.2f} ₽",
        f"Расходы: {total_out/100:.2f} ₽",
        f"Сальдо:  {(total_in - total_out)/100:.2f} ₽",
        "",
        "По категориям (расходы):",
        *cat_lines,
        "",
        "Последние 10 транзакций:",
    ]
    for t in transactions[:10]:
        sign = "+" if t["amount_kop"] > 0 else "-"
        amount_rub = abs(t["amount_kop"]) / 100
        date_str = t["date"].strftime("%d.%m") if hasattr(t["date"], "strftime") else str(t["date"])
        desc = (t.get("description") or "?")[:60]
        cat_label = CATEGORIES.get(t.get("category") or "other", "?")
        parts.append(f"  {date_str} {sign}{amount_rub:.2f} ₽ · {cat_label} · {desc}")

    return "\n".join(parts)
