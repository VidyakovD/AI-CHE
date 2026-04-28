"""
Генерация коммерческих предложений (КП).

Высокоуровневый pipeline:
  1. parse_client_site(url) — httpx + bs4 → краткий контекст компании клиента
  2. fetch_price_from_bot(bot_id) → текстовый прайс-лист из BotPriceItem
  3. generate_proposal_html(brand, project, ctx) → Claude генерирует HTML
     с применённым брендом (шрифт/цвета/лого/контакты) и контекстом
  4. html_to_pdf(html) → PDF через xhtml2pdf

Используется как из routes/proposals.py (юзер жмёт «Сгенерировать»),
так и из chatbot_engine.auto_proposal_node (email-orchestrator).
"""
import os
import re
import logging
from datetime import datetime
from urllib.parse import urlparse

import httpx

from server.ai import generate_response
from server.models import (
    ProposalProject, ProposalBrand, ChatBot, BotPriceItem,
)

log = logging.getLogger(__name__)

# Лимиты на парсинг сайта клиента (защита от мусора и SSRF на огромный resp).
_SITE_FETCH_TIMEOUT = 12.0
_SITE_FETCH_MAX_BYTES = 2 * 1024 * 1024   # 2 МБ
_SITE_CTX_MAX_CHARS = 4000                # сколько отправляем в Claude

# CIDR-блок-лист (private/loopback/link-local) — защита от SSRF при парсинге
# чужого сайта. Реюзаем подход из http_request ноды.
_PRIVATE_NETS = (
    "10.", "127.", "169.254.", "192.168.", "100.64.",
    "172.16.", "172.17.", "172.18.", "172.19.", "172.20.",
    "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
    "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.",
)


def _is_private_ip(host: str) -> bool:
    if not host:
        return True
    if host in ("localhost", "0.0.0.0"):
        return True
    if any(host.startswith(p) for p in _PRIVATE_NETS):
        return True
    if host.startswith("[") and "::1" in host:  # IPv6 loopback
        return True
    return False


def parse_client_site(url: str) -> str:
    """
    Скачивает главную страницу клиента и извлекает краткий контекст:
    title, h1, мета-описание, первые ~3 КБ видимого текста.
    Возвращает строку до 4 КБ — чтобы вставить в prompt без раздувания.

    Безопасность:
      - HTTPS-only по дефолту (HTTP допускается только если URL явно с http://
        и хост не приватный; SSRF-защита через CIDR блок-лист)
      - timeout 12s + max bytes 2MB
      - no redirects (Location перепроверяется вручную)
    """
    if not url or not url.strip():
        return ""
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https"):
            return ""
        host = parsed.hostname or ""
        if _is_private_ip(host):
            log.warning(f"[proposal] skip private host: {host}")
            return ""
    except Exception:
        return ""

    try:
        with httpx.Client(timeout=_SITE_FETCH_TIMEOUT, follow_redirects=False,
                          headers={"User-Agent": "Mozilla/5.0 AICheBot/1.0"}) as client:
            r = client.get(url.strip())
            # Один шаг redirect — защита от SSRF: revalidate Location
            if 300 <= r.status_code < 400:
                loc = r.headers.get("location", "")
                if loc:
                    parsed2 = urlparse(loc)
                    if parsed2.scheme in ("http", "https") and not _is_private_ip(parsed2.hostname or ""):
                        r = client.get(loc)
            if r.status_code >= 400:
                return ""
            content = r.content[:_SITE_FETCH_MAX_BYTES]
    except (httpx.TimeoutException, httpx.RequestError, httpx.ConnectError) as e:
        log.warning(f"[proposal] site fetch failed: {type(e).__name__}")
        return ""
    except Exception as e:
        log.warning(f"[proposal] site fetch error: {type(e).__name__}")
        return ""

    # Парсинг — простая регулярка, без bs4 чтобы не плодить зависимости
    try:
        html = content.decode("utf-8", errors="ignore")
    except Exception:
        return ""

    parts = []
    # Title
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        parts.append("Title: " + _clean_text(m.group(1))[:200])
    # Meta description
    m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
                  html, re.IGNORECASE)
    if m:
        parts.append("Описание: " + _clean_text(m.group(1))[:300])
    # H1 (первый)
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
    if m:
        parts.append("H1: " + _clean_text(m.group(1))[:200])
    # Видимый текст (отбрасываем script/style)
    body_text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html,
                       flags=re.IGNORECASE | re.DOTALL)
    body_text = re.sub(r"<[^>]+>", " ", body_text)
    body_text = _clean_text(body_text)
    if body_text:
        # Сжимаем — берём первые _SITE_CTX_MAX_CHARS - уже-собранное
        budget = _SITE_CTX_MAX_CHARS - sum(len(p) for p in parts) - 50
        if budget > 200:
            parts.append("\nКонтент:\n" + body_text[:budget])

    return "\n".join(parts)[:_SITE_CTX_MAX_CHARS]


def _clean_text(s: str) -> str:
    """Свернуть пробелы/переносы, убрать HTML-entities, обрезать."""
    if not s:
        return ""
    s = re.sub(r"&[a-zA-Z]+;", " ", s)
    s = re.sub(r"&#\d+;", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _format_price_lines(items) -> str:
    """Общий форматтер списка позиций в plain text для промпта."""
    if not items:
        return ""
    lines = []
    current_cat = None
    for it in items:
        cat = it.category or ""
        if cat and cat != current_cat:
            lines.append(f"\n[{cat}]")
            current_cat = cat
        if it.price_kop:
            price = f"{it.price_kop / 100:,.0f} ₽".replace(",", " ")
        elif it.price_text:
            price = it.price_text
        else:
            price = "цена по запросу"
        desc = f" — {it.description}" if it.description else ""
        lines.append(f"• {it.name}: {price}{desc}")
    return "\n".join(lines)[:8000]


def fetch_price_from_bot(db, bot_id: int, user_id: int) -> str:
    """Прайс из BotPriceItem (legacy, для обратной совместимости)."""
    if not bot_id:
        return ""
    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user_id).first()
    if not bot:
        return ""
    items = (db.query(BotPriceItem)
               .filter_by(bot_id=bot_id, is_active=True)
               .order_by(BotPriceItem.sort_order, BotPriceItem.id)
               .all())
    return _format_price_lines(items)


def fetch_price_from_list(db, price_list_id: int, user_id: int) -> str:
    """Прайс из ProposalPriceList — собственный, для КП.
    Приоритет над bot_id: если у проекта есть price_list_id — используем его."""
    if not price_list_id:
        return ""
    from server.models import ProposalPriceList, ProposalPriceItem
    pl = db.query(ProposalPriceList).filter_by(
        id=price_list_id, user_id=user_id).first()
    if not pl:
        return ""
    items = (db.query(ProposalPriceItem)
               .filter_by(price_list_id=price_list_id, is_active=True)
               .order_by(ProposalPriceItem.sort_order, ProposalPriceItem.id)
               .all())
    return _format_price_lines(items)


# ── HTML/PDF generation ────────────────────────────────────────────────────


def _build_brand_css(brand: ProposalBrand | None) -> dict:
    """Возвращает dict со стилевыми переменными бренда + расширенными
    данными для глубокой персонализации (tagline/usp/guarantees/tone).

    Шрифт через pdf_builder.resolve_pdf_font: маппит web-имя (Inter/Manrope/
    Roboto) на установленный системный TTF (Liberation Sans/Noto Sans/
    DejaVu Sans) — гарантирует поддержку кириллицы в xhtml2pdf.
    """
    from server.pdf_builder import resolve_pdf_font as _resolve
    if not brand:
        pdf_font = _resolve(None)
        return {
            "primary": "#ff8c42", "accent": "#ffb347", "secondary": "#1C1C1C",
            "font": f"{pdf_font}, sans-serif", "preset": "minimal",
            "company": "", "logo_url": "", "contacts": "",
            "inn": "", "address": "", "signature": "",
            "tagline": "", "usp_list": [], "guarantees": [],
            "tone": "business", "intro_phrase": "", "cta_phrase": "",
        }
    brand_font = brand.font_family or "Inter"
    pdf_font = _resolve(brand_font)
    font = f"{pdf_font}, {brand_font}, sans-serif"
    # Парсим списки (по строке = пункт)
    def _split_lines(s):
        if not s:
            return []
        return [x.strip() for x in s.replace("\r\n", "\n").split("\n") if x.strip()][:8]
    return {
        "primary": brand.primary_color or "#ff8c42",
        "accent": brand.accent_color or "#ffb347",
        "secondary": brand.secondary_color or "#1C1C1C",
        "font": font, "preset": brand.style_preset or "minimal",
        "company": brand.company_name or "",
        "logo_url": brand.logo_url or "",
        "contacts": brand.contacts or "",
        "inn": brand.inn or "", "address": brand.address or "",
        "signature": brand.signature_url or "",
        # Расширенная персонализация
        "tagline": (brand.tagline or "")[:200],
        "usp_list": _split_lines(brand.usp_list),
        "guarantees": _split_lines(brand.guarantees),
        "tone": brand.tone or "business",
        "intro_phrase": brand.intro_phrase or "",
        "cta_phrase": brand.cta_phrase or "",
    }


# ─────────────────────────────────────────────────────────────────────────
# JSON-first prompt v3: AI возвращает структурированные данные, мы рендерим
# в HTML по фиксированному шаблону. Преимущества:
#   1. Шапка/подвал/CSS никогда не теряются (мы их НЕ отправляем AI)
#   2. Все КП с одним preset выглядят одинаково
#   3. AI фокусируется на содержании, а не на разметке
#   4. Глубокая персонализация: бренд-данные (tagline/usp/гарантии)
#      сами попадают в шаблон без AI-генерации
# ─────────────────────────────────────────────────────────────────────────


_TONE_HINTS = {
    "business":  "Деловой тон — по делу, с цифрами и фактами, без лишних слов.",
    "friendly":  "Дружелюбный тон — просто и человечно, можно лёгкие междометия. Без панибратства.",
    "premium":   "Премиум-тон — статусно, лаконично, акцент на качестве и эксклюзивности. Никаких эмодзи.",
    "tech":      "Технический тон — детально, структурно, с конкретикой по технологиям/срокам.",
}


def _claude_prompt_json(brand_css: dict, project: ProposalProject,
                         price_text: str, site_ctx: str) -> str:
    """JSON-first промпт. AI возвращает СТРОГИЙ JSON со слотами. Мы потом
    рендерим в HTML по нашему шаблону.

    Если AI всё-таки вернёт мусор — fallback на legacy _claude_prompt.
    """
    from datetime import timedelta as _td
    today = datetime.utcnow()
    valid_until = (today + _td(days=30)).strftime("%d.%m.%Y")
    today_str = today.strftime("%d.%m.%Y")
    tone = brand_css.get("tone") or "business"
    tone_hint = _TONE_HINTS.get(tone, _TONE_HINTS["business"])

    parts = [
        "Ты — старший копирайтер B2B-агентства. Сделай **коммерческое предложение**.",
        "ВАЖНО: верни СТРОГИЙ JSON со слотами (структура ниже). Без markdown-обёртки, "
        "без комментариев, без HTML — ТОЛЬКО валидный JSON. Каждое поле — простой "
        "текст без HTML-тегов (мы сами вставим стили).",
        "",
        "=== СТРУКТУРА JSON ===",
        "{",
        '  "hero": {',
        '    "title": "Короткий заголовок 4-7 слов о ключевой ценности (НЕ «Коммерческое предложение»)",',
        '    "lead": "Персональное обращение к клиенту по имени + одно предложение о понимании задачи"',
        '  },',
        '  "understanding": {',
        '    "intro": "1-2 предложения: что мы услышали из запроса клиента",',
        '    "points": ["конкретный пункт задачи 1", "пункт 2", "пункт 3"]',
        '  },',
        '  "offering": {',
        '    "intro": "1 предложение про подход",',
        '    "cards": [',
        '      {"title": "Название преимущества", "body": "Конкретный факт/цифра (1-2 предложения)"},',
        '      {"title": "...", "body": "..."},',
        '      {"title": "...", "body": "..."}',
        '    ]',
        '  },',
        '  "pricing": {',
        '    "intro": "1 предложение перед таблицей",',
        '    "items": [',
        '      {"name": "Услуга", "description": "Что входит, кратко", "price": "от 50 000 ₽"},',
        '      {"name": "...", "description": "...", "price": "..."}',
        '    ],',
        '    "total": "150 000 ₽" или null,',
        '    "total_note": "Итого / Под ключ / По договору" или null',
        '  },',
        '  "timeline": {',
        '    "intro": "1 предложение",',
        '    "stages": [',
        '      {"label": "Согласование", "duration": "1-2 дня"},',
        '      {"label": "Производство", "duration": "5-7 дней"}',
        '    ]',
        '  },',
        '  "cta": {',
        '    "headline": "Призыв к следующему шагу — 1 короткое предложение",',
        '    "action": "Конкретное действие: «Позвоните...», «Пришлите ТЗ...» или «Подпишем договор сегодня»"',
        '  }',
        "}",
        "",
        "=== ТРЕБОВАНИЯ К СОДЕРЖАНИЮ ===",
        "• Адресуйся к клиенту лично — используй его имя из запроса.",
        f"• {tone_hint}",
        "• Без штампов вроде «надёжный партнёр», «гибкий подход», «команда профессионалов».",
        "• Каждый «card» в offering — конкретика, цифра, факт. Не вода.",
        "• В pricing.items — РОВНО позиции из нашего прайс-листа ниже. Если позиции нет — пиши «по запросу» в price.",
        "• Если совсем нет релевантного прайса — items должен быть [] (пустой), мы тогда покажем «обсудим на созвоне».",
        "• total — только если можешь точно посчитать (сумма позиций). Иначе null.",
        "• 3-4 cards в offering, 2-4 пункта в understanding, 2-3 этапа в timeline.",
        "",
        "=== БРЕНД ===",
        f"Компания: {brand_css.get('company') or '(не указано)'}",
    ]
    if brand_css.get("tagline"):
        parts.append(f"Слоган/позиционирование: {brand_css['tagline']}")
    if brand_css.get("usp_list"):
        parts.append("Наши УТП (используй уместно в offering или понимании):")
        for u in brand_css["usp_list"][:6]:
            parts.append(f"  - {u}")
    if brand_css.get("guarantees"):
        parts.append("Наши гарантии:")
        for g in brand_css["guarantees"][:6]:
            parts.append(f"  - {g}")
    if brand_css.get("intro_phrase"):
        parts.append(f"Предпочитаемая фраза приветствия: {brand_css['intro_phrase']}")
    if brand_css.get("cta_phrase"):
        parts.append(f"Предпочитаемый призыв: {brand_css['cta_phrase']}")

    parts += [
        "",
        f"Сегодня: {today_str}. КП действует до: {valid_until}.",
    ]

    if project.client_name or project.client_email:
        parts += [
            "",
            "=== КЛИЕНТ ===",
            f"Имя: {project.client_name or '(см. в запросе)'}",
            f"Email: {project.client_email or '(не указан)'}",
        ]
    if project.client_request:
        parts += [
            "",
            "=== ЗАПРОС КЛИЕНТА (центральная информация) ===",
            project.client_request[:5000],
        ]
    if site_ctx:
        parts += [
            "",
            "=== КОНТЕКСТ С САЙТА КЛИЕНТА (упомяни их сферу/нишу) ===",
            site_ctx[:_SITE_CTX_MAX_CHARS],
        ]
    if price_text:
        parts += [
            "",
            "=== НАШ ПРАЙС-ЛИСТ (БЕРИ ЦЕНЫ ТОЛЬКО ОТСЮДА) ===",
            price_text,
            "",
            "Если нужной услуги нет — пиши \"по запросу\" в price. Не выдумывай.",
        ]
    else:
        parts += [
            "",
            "(Прайс не привязан — items может быть [] либо с примерными ценами в price-text формате)",
        ]
    if project.extra_notes:
        parts += [
            "",
            "=== ДОПОЛНИТЕЛЬНЫЕ ИНСТРУКЦИИ ВЛАДЕЛЬЦА (учти обязательно) ===",
            project.extra_notes[:2000],
        ]

    parts += [
        "",
        "ВЕРНИ ТОЛЬКО ВАЛИДНЫЙ JSON. БЕЗ ```json. БЕЗ ОБЪЯСНЕНИЙ. БЕЗ HTML.",
    ]
    return "\n".join(parts)


def _parse_proposal_json(content: str) -> dict | None:
    """Парсит ответ AI как JSON. Возвращает dict со слотами или None если
    распарсить не удалось (тогда fallback на старый HTML-путь)."""
    if not content:
        return None
    s = content.strip()
    # Снимаем markdown-fence если есть
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```\s*$", "", s)
    # Иногда AI добавляет пояснения вокруг — выделяем первый {...} блок
    m = re.search(r"\{[\s\S]*\}", s)
    if not m:
        return None
    try:
        import json as _json
        data = _json.loads(m.group(0))
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def _claude_prompt(brand_css: dict, project: ProposalProject, price_text: str,
                    site_ctx: str) -> str:
    """Legacy HTML-prompt (fallback если JSON не сработал)."""
    today = datetime.utcnow()
    valid_until = today.replace(day=min(today.day, 28))
    # +30 дней действия КП
    from datetime import timedelta as _td
    valid_until = (today + _td(days=30)).strftime("%d.%m.%Y")

    parts = [
        "Ты — копирайтер B2B-агентства. Сделай профессиональное **коммерческое предложение** в HTML.",
        "Адресуйся к клиенту лично (по имени/обращению из запроса). Пиши конкретно, "
        "с цифрами и фактами, без воды и общих фраз. Обоснуй каждую цену ценностью для клиента.",
        "",
        "=== ФОРМАТ ОТВЕТА ===",
        "Возвращай ТОЛЬКО содержимое <body> в виде серии <section> блоков. "
        "Не пиши <html>, <head>, <body>, <style>, <script> — оформление мы добавим сами.",
        "Без markdown-обёртки (без ```html), без комментариев — чистый HTML.",
        "",
        "=== СТИЛЬ HTML ===",
        "Используй наши классы:",
        "  • .kp-hero — титульный блок (большой заголовок + персональное обращение)",
        "  • .kp-section — обычный раздел с h2",
        "  • .kp-grid — контейнер для карточек 2-3 в ряд",
        "  • .kp-card — отдельная карточка преимущества/характеристики",
        "  • .kp-price-table — <table> с колонками и строками для прайса",
        "  • .kp-summary — итоговая стоимость / опции",
        "  • .kp-cta — финальный призыв к действию",
        "",
        "=== ОБЯЗАТЕЛЬНАЯ СТРУКТУРА ===",
        "1. <section class=\"kp-hero\">: персональное обращение, краткое формулирование задачи "
        "клиента в одном предложении. Заголовок h1 короткий, не «Коммерческое предложение».",
        "2. <section class=\"kp-section\"> «Понимание задачи»: 2-4 строки — покажи что ты "
        "услышал клиента. Используй конкретику с его сайта/запроса.",
        "3. <section class=\"kp-section\"> «Что мы предлагаем»: <ul> или .kp-grid из 3-4 "
        "карточек с короткими фактами (цифры/сроки/материалы).",
        "4. <section class=\"kp-section\"> «Состав и стоимость» с <table class=\"kp-price-table\"> "
        "(колонки: Услуга/Описание/Стоимость). Берём цены ТОЛЬКО из нашего прайса ниже. "
        "Если позиции нет — пиши «по запросу». В конце таблицы — итоговая строка.",
        "5. <section class=\"kp-section\"> «Сроки и гарантии»: 2-3 пункта.",
        "6. <section class=\"kp-cta\"> с конкретным следующим шагом (созвон/договор/предоплата).",
        "",
        "=== БРЕНД ===",
        f"Компания: {brand_css.get('company') or '(не указано)'}",
        f"Контакты: {brand_css.get('contacts') or '(не указано)'}",
        "",
        "=== ДЕЙСТВИТЕЛЬНОСТЬ ===",
        f"Сегодня: {today.strftime('%d.%m.%Y')}",
        f"КП действует до: {valid_until}",
        "Упомяни срок действия в hero или cta-блоке.",
    ]
    if project.client_name or project.client_email:
        parts += [
            "",
            "=== КЛИЕНТ (адресуйся лично!) ===",
            f"Имя: {project.client_name or '(имя в запросе)'}",
            f"Email: {project.client_email or '(не указан)'}",
        ]
    if project.client_request:
        parts += [
            "",
            "=== ЗАПРОС КЛИЕНТА (это центральная информация — обязательно отрази в КП) ===",
            project.client_request[:5000],
        ]
    if site_ctx:
        parts += [
            "",
            "=== КОНТЕКСТ КЛИЕНТА (с его сайта — упомяни их сферу/нишу/преимущества) ===",
            site_ctx[:_SITE_CTX_MAX_CHARS],
        ]
    if price_text:
        parts += [
            "",
            "=== НАШ ПРАЙС-ЛИСТ (БЕРИ ЦЕНЫ ТОЛЬКО ОТСЮДА, выбирай релевантные запросу) ===",
            price_text,
            "",
            "ВАЖНО: не выдумывай цены. Если нужной услуги нет в прайсе — пиши «по запросу» "
            "и предложи обсудить на созвоне.",
        ]
    else:
        parts += [
            "",
            "(прайс не привязан — указывай примерные цены или «по запросу»)",
        ]
    if project.extra_notes:
        parts += [
            "",
            "=== ДОПОЛНИТЕЛЬНЫЕ ИНСТРУКЦИИ ОТ ВЛАДЕЛЬЦА (учти обязательно) ===",
            project.extra_notes[:2000],
        ]
    parts += [
        "",
        "Объём: 5-7 секций суммарно, без воды. Русский язык. Только HTML, без обёрток.",
    ]
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────
# Style presets — РЕАЛЬНО разные оформления. Каждый имеет свой CSS.
# Применяется через name → подставляется в _BASE_TEMPLATE как preset_css.
# ─────────────────────────────────────────────────────────────────────────


_PRESET_CSS = {
    "minimal": """
  /* Minimal: много воздуха, тонкие линии, акценты только в hero/cta */
  body { font-size: 11pt; line-height: 1.6; color: #2a2a2a; }
  h1 { font-size: 24pt; font-weight: 300; letter-spacing: -0.5pt; line-height: 1.15; margin: 0 0 8pt; }
  h2 { font-size: 13pt; font-weight: 600; text-transform: uppercase; letter-spacing: 1.5pt;
       color: {primary}; border-bottom: 0; padding-bottom: 0; margin: 22pt 0 10pt;
       border-left: 3pt solid {accent}; padding-left: 10pt; }
  h3 { font-size: 11pt; font-weight: 600; margin: 8pt 0 4pt; color: {secondary}; }
  .kp-hero { background: transparent; color: {secondary}; padding: 8pt 0 22pt;
             border-bottom: 1pt solid #e5dfd2; margin-bottom: 18pt; }
  .kp-hero h1 { color: {primary}; }
  .kp-hero .lead { font-size: 13pt; line-height: 1.5; color: {secondary}; opacity: 0.85; max-width: 90%; }
  .kp-tagline { font-size: 9pt; text-transform: uppercase; letter-spacing: 2pt;
                color: {accent}; margin-bottom: 8pt; }
  .kp-card { background: #fafaf7; border-left: 2pt solid {accent}; }
  .kp-cta { background: transparent; color: {primary}; border: 2pt solid {primary};
            border-radius: 4pt; padding: 18pt; text-align: center; }
""",
    "classic": """
  /* Classic: серьёзный B2B стиль — двойные линии, заглавные заголовки, табличность */
  body { font-size: 11pt; line-height: 1.55; color: #1a1a1a; }
  h1 { font-size: 22pt; font-weight: 700; line-height: 1.2; }
  h2 { font-size: 14pt; font-weight: 700; color: {secondary}; text-transform: uppercase;
       letter-spacing: 1pt; border-bottom: 3pt double {accent}; padding-bottom: 6pt; margin-top: 18pt; }
  h3 { font-size: 12pt; color: {primary}; }
  .kp-hero { background: linear-gradient(135deg, {primary} 0%, {secondary} 100%);
             color: #fff; padding: 26pt 22pt; border-radius: 0; margin-bottom: 18pt;
             border-left: 5pt solid {accent}; }
  .kp-hero h1, .kp-hero .lead { color: #fff; }
  .kp-hero .lead { font-size: 12pt; opacity: 0.92; margin-top: 8pt; }
  .kp-tagline { font-size: 10pt; color: {accent}; font-weight: 600;
                text-transform: uppercase; letter-spacing: 1.5pt; margin-bottom: 10pt; }
  .kp-card { background: #f7f4ec; border-left: 4pt solid {primary}; padding: 12pt 14pt; }
  .kp-cta { background: {primary}; color: #fff; padding: 18pt; border-radius: 4pt;
            font-size: 14pt; font-weight: 700; text-align: center; }
  .kp-cta * { color: #fff; }
""",
    "bold": """
  /* Bold: контраст, крупная типографика, плотный hero, акцент-плашки */
  body { font-size: 11pt; line-height: 1.5; color: #1a1a1a; }
  h1 { font-size: 30pt; font-weight: 900; line-height: 1.05; letter-spacing: -1pt; margin: 0 0 6pt; }
  h2 { font-size: 18pt; font-weight: 800; color: {primary}; border: 0;
       padding: 6pt 0; margin: 20pt 0 10pt; line-height: 1.2; }
  h2:before { content: "/ "; color: {accent}; }
  h3 { font-size: 13pt; font-weight: 700; color: {secondary}; }
  .kp-hero { background: {secondary}; color: #fff; padding: 32pt 28pt;
             border-radius: 6pt; margin-bottom: 18pt; }
  .kp-hero h1 { color: #fff; }
  .kp-hero .lead { font-size: 14pt; line-height: 1.45; color: {accent}; font-weight: 600; margin-top: 10pt; }
  .kp-tagline { display: inline-block; background: {accent}; color: {secondary};
                font-size: 9pt; font-weight: 700; padding: 3pt 10pt; border-radius: 999pt;
                text-transform: uppercase; letter-spacing: 1pt; margin-bottom: 16pt; }
  .kp-card { background: #ffffff; border: 2pt solid {primary}; padding: 14pt; border-radius: 4pt; }
  .kp-card h3 { color: {primary}; }
  .kp-cta { background: {accent}; color: {secondary}; padding: 22pt; border-radius: 6pt;
            font-size: 16pt; font-weight: 800; text-align: center; }
""",
    "compact": """
  /* Compact: плотный, для длинных КП с большим прайсом */
  body { font-size: 10pt; line-height: 1.45; color: #2a2a2a; }
  h1 { font-size: 20pt; font-weight: 700; line-height: 1.15; }
  h2 { font-size: 12pt; font-weight: 700; color: {primary}; text-transform: uppercase;
       letter-spacing: 0.8pt; border-bottom: 1pt solid {accent}; padding-bottom: 3pt; margin-top: 12pt; }
  h3 { font-size: 10.5pt; }
  .kp-hero { background: #f7f4ec; color: {secondary}; padding: 14pt 16pt;
             border-radius: 4pt; margin-bottom: 10pt; border-left: 4pt solid {primary}; }
  .kp-hero h1 { color: {primary}; }
  .kp-hero .lead { font-size: 11pt; margin-top: 4pt; }
  .kp-tagline { font-size: 9pt; color: {accent}; font-weight: 600; margin-bottom: 4pt; }
  .kp-card { background: #fafaf7; border-left: 2pt solid {accent}; padding: 8pt 10pt; }
  .kp-section { margin-bottom: 8pt; }
  .kp-cta { background: {primary}; color: #fff; padding: 12pt; border-radius: 4pt;
            font-size: 12pt; font-weight: 700; text-align: center; }
""",
}


def _render_proposal_json(data: dict, brand_css: dict) -> str:
    """Рендерит структурированный JSON в HTML по нашему фиксированному шаблону.
    Шапка/подвал/CSS — всегда наши, AI на них не влияет."""
    out = []
    p = brand_css

    # 1. HERO с тэглайном бренда (если есть)
    hero = data.get("hero") or {}
    out.append('<section class="kp-hero">')
    if p.get("tagline"):
        out.append(f'<div class="kp-tagline">{_html_escape(p["tagline"])}</div>')
    title = hero.get("title", "").strip()
    if title:
        out.append(f'<h1>{_html_escape(title)}</h1>')
    lead = hero.get("lead", "").strip()
    if lead:
        out.append(f'<p class="lead">{_html_escape(lead)}</p>')
    out.append('</section>')

    # 2. UNDERSTANDING — что мы услышали
    u = data.get("understanding") or {}
    if u.get("intro") or u.get("points"):
        out.append('<section class="kp-section">')
        out.append('<h2>Понимание задачи</h2>')
        if u.get("intro"):
            out.append(f'<p>{_html_escape(u["intro"])}</p>')
        if u.get("points") and isinstance(u["points"], list):
            out.append('<ul>')
            for pt in u["points"][:6]:
                if pt:
                    out.append(f'<li>{_html_escape(str(pt))}</li>')
            out.append('</ul>')
        out.append('</section>')

    # 3. OFFERING — что предлагаем (карточки)
    o = data.get("offering") or {}
    cards = o.get("cards") if isinstance(o.get("cards"), list) else []
    if o.get("intro") or cards:
        out.append('<section class="kp-section">')
        out.append('<h2>Что мы предлагаем</h2>')
        if o.get("intro"):
            out.append(f'<p>{_html_escape(o["intro"])}</p>')
        if cards:
            out.append('<div class="kp-grid">')
            for c in cards[:4]:
                if not isinstance(c, dict):
                    continue
                title = c.get("title", "").strip()
                body = c.get("body", "").strip()
                if not title and not body:
                    continue
                out.append('<div class="kp-card">')
                if title:
                    out.append(f'<h3>{_html_escape(title)}</h3>')
                if body:
                    out.append(f'<p>{_html_escape(body)}</p>')
                out.append('</div>')
            out.append('</div>')
        out.append('</section>')

    # 4. PRICING — таблица
    pr = data.get("pricing") or {}
    items = pr.get("items") if isinstance(pr.get("items"), list) else []
    if pr.get("intro") or items:
        out.append('<section class="kp-section">')
        out.append('<h2>Состав и стоимость</h2>')
        if pr.get("intro"):
            out.append(f'<p>{_html_escape(pr["intro"])}</p>')
        if items:
            out.append('<table class="kp-price-table"><thead><tr>')
            out.append('<th>Услуга</th><th>Что входит</th><th>Стоимость</th>')
            out.append('</tr></thead><tbody>')
            for it in items[:20]:
                if not isinstance(it, dict):
                    continue
                name = (it.get("name") or "").strip()
                desc = (it.get("description") or "").strip()
                price = (it.get("price") or "по запросу").strip()
                if not name:
                    continue
                out.append(
                    f'<tr><td>{_html_escape(name)}</td>'
                    f'<td>{_html_escape(desc)}</td>'
                    f'<td>{_html_escape(price)}</td></tr>'
                )
            total = (pr.get("total") or "").strip() if pr.get("total") else ""
            total_note = (pr.get("total_note") or "Итого").strip() if pr.get("total_note") else "Итого"
            if total:
                out.append(
                    f'<tr class="total"><td colspan="2">{_html_escape(total_note)}</td>'
                    f'<td>{_html_escape(total)}</td></tr>'
                )
            out.append('</tbody></table>')
        else:
            out.append('<p><em>Точную стоимость рассчитаем после обсуждения деталей.</em></p>')
        out.append('</section>')

    # 5. TIMELINE — этапы
    t = data.get("timeline") or {}
    stages = t.get("stages") if isinstance(t.get("stages"), list) else []
    if t.get("intro") or stages:
        out.append('<section class="kp-section">')
        out.append('<h2>Сроки и этапы</h2>')
        if t.get("intro"):
            out.append(f'<p>{_html_escape(t["intro"])}</p>')
        if stages:
            out.append('<ul>')
            for s in stages[:6]:
                if not isinstance(s, dict):
                    continue
                lbl = (s.get("label") or "").strip()
                dur = (s.get("duration") or "").strip()
                if lbl:
                    line = f'<strong>{_html_escape(lbl)}</strong>'
                    if dur:
                        line += f' — {_html_escape(dur)}'
                    out.append(f'<li>{line}</li>')
            out.append('</ul>')
        out.append('</section>')

    # 6. GUARANTEES — гарантии бренда (всегда из бренда, AI не трогает)
    if p.get("guarantees"):
        out.append('<section class="kp-section">')
        out.append('<h2>Гарантии</h2>')
        out.append('<ul>')
        for g in p["guarantees"][:6]:
            out.append(f'<li>{_html_escape(g)}</li>')
        out.append('</ul>')
        out.append('</section>')

    # 7. CTA — призыв к действию
    cta = data.get("cta") or {}
    headline = (cta.get("headline") or "").strip()
    action = (cta.get("action") or "").strip()
    if headline or action:
        out.append('<section class="kp-cta">')
        if headline:
            out.append(f'<div>{_html_escape(headline)}</div>')
        if action:
            out.append(f'<div style="margin-top:6pt;font-size:11pt;font-weight:600;opacity:0.95">{_html_escape(action)}</div>')
        out.append('</section>')

    return "\n".join(out)


_BASE_TEMPLATE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"/>
<title>{title}</title>
<style>
  /* ── База — общие правила, одинаковые для всех пресетов ────────── */
  @page {{ size: A4; margin: 18mm 16mm; }}
  body {{ font-family: {font}; }}
  h1, h2, h3 {{ font-family: {font}; }}
  p {{ margin: 0 0 6pt; }}
  strong {{ color: {primary}; }}
  .kp-header {{ display: table; width: 100%; margin-bottom: 14pt; }}
  .kp-header .logo {{ display: table-cell; width: 90pt; vertical-align: middle; }}
  .kp-header .logo img {{ max-width: 80pt; max-height: 60pt; }}
  .kp-header .meta {{ display: table-cell; vertical-align: middle; text-align: right;
                      font-size: 9pt; color: #666; }}
  .kp-header .meta .valid {{ color: {primary}; font-weight: 600; }}
  .kp-section {{ margin-bottom: 12pt; }}
  .kp-grid {{ display: table; width: 100%; border-collapse: separate; border-spacing: 6pt 0; margin: 6pt 0; }}
  .kp-card {{ display: table-cell; padding: 10pt; vertical-align: top; border-radius: 4pt; }}
  .kp-card h3 {{ margin-top: 0; }}
  .kp-price-table {{ width: 100%; border-collapse: collapse; margin: 10pt 0; }}
  .kp-price-table th, .kp-price-table td {{ padding: 8pt 10pt; text-align: left;
                                             border-bottom: 1pt solid #e0d8c8; vertical-align: top; }}
  .kp-price-table th {{ background: {secondary}; color: #fff; font-weight: 600;
                        font-size: 10pt; text-transform: uppercase; letter-spacing: 0.5pt; }}
  .kp-price-table tr:nth-child(even) td {{ background: #faf7f0; }}
  .kp-price-table td:last-child {{ text-align: right; font-weight: 600; white-space: nowrap; }}
  .kp-price-table tr.total td {{ background: {accent}; color: {secondary}; font-weight: 700;
                                  font-size: 12pt; border-bottom: none; }}
  .kp-footer {{ border-top: 1pt solid #ccc; margin-top: 18pt; padding-top: 8pt;
                font-size: 8pt; color: #777; }}
  ul, ol {{ padding-left: 18pt; margin: 4pt 0; }}
  li {{ margin-bottom: 3pt; }}
  /* ── Пресет «{preset_name}» — переопределяет цвета/типографику/hero/cta ── */
  {preset_css}
</style></head><body>
<div class="kp-header">
  <div class="logo">{logo_html}</div>
  <div class="meta">
    {company_html}
    <div>Дата: {today}</div>
    <div class="valid">КП действует до: {valid_until}</div>
  </div>
</div>
{ai_content}
{signature_html}
<div class="kp-footer">
  {footer_html}
</div>
</body></html>"""


def _wrap_html(brand_css: dict, ai_html: str, project: ProposalProject) -> str:
    """Оборачивает AI-сгенерённый контент в фирменный шаблон."""
    from datetime import timedelta as _td
    today_dt = datetime.utcnow()
    today = today_dt.strftime("%d.%m.%Y")
    valid_until = (today_dt + _td(days=30)).strftime("%d.%m.%Y")

    logo_html = ""
    if brand_css.get("logo_url"):
        logo_url = brand_css["logo_url"]
        # Относительный путь /uploads/... должен резолвиться абсолютно для PDF
        if logo_url.startswith("/"):
            app_url = os.getenv("APP_URL", "https://aiche.ru").rstrip("/")
            logo_url = app_url + logo_url
        logo_html = f'<img src="{_html_escape(logo_url)}" alt=""/>'
    company_html = ""
    if brand_css.get("company"):
        company_html = f'<div><strong>{_html_escape(brand_css["company"])}</strong></div>'
    footer_lines = []
    if brand_css.get("contacts"):
        footer_lines.append(_html_escape(brand_css["contacts"]).replace("\n", "<br/>"))
    if brand_css.get("address"):
        footer_lines.append(_html_escape(brand_css["address"]))
    if brand_css.get("inn"):
        footer_lines.append(f"ИНН {_html_escape(brand_css['inn'])}")
    footer_html = "<br/>".join(footer_lines) or f"Создано в AI Студия Че · {today}"

    # Подпись (если задана signature_url) — вставляем перед подвалом
    signature_html = ""
    if brand_css.get("signature"):
        sig_url = brand_css["signature"]
        if sig_url.startswith("/"):
            app_url = os.getenv("APP_URL", "https://aiche.ru").rstrip("/")
            sig_url = app_url + sig_url
        signature_html = (
            f'<div style="margin-top:20pt;padding-top:8pt;border-top:1pt dashed #ccc">'
            f'<p style="font-size:10pt;color:#666;margin:0 0 4pt">С уважением,</p>'
            f'<img src="{_html_escape(sig_url)}" alt="" style="max-width:140pt;max-height:50pt"/>'
            f'</div>'
        )

    title = "Коммерческое предложение"
    if project.client_name:
        title += " для " + project.client_name

    # Выбираем preset CSS. По умолчанию minimal.
    preset_name = (brand_css.get("preset") or "minimal").strip().lower()
    preset_template = _PRESET_CSS.get(preset_name) or _PRESET_CSS["minimal"]
    # Подставляем цвета бренда в preset CSS — заменяем плейсхолдеры
    preset_css = (preset_template
                   .replace("{primary}", brand_css["primary"])
                   .replace("{accent}", brand_css["accent"])
                   .replace("{secondary}", brand_css["secondary"]))

    return _BASE_TEMPLATE.format(
        title=_html_escape(title), font=brand_css["font"],
        primary=brand_css["primary"], accent=brand_css["accent"],
        secondary=brand_css["secondary"],
        preset_name=_html_escape(preset_name), preset_css=preset_css,
        logo_html=logo_html, company_html=company_html,
        today=today, valid_until=valid_until,
        ai_content=ai_html, footer_html=footer_html,
        signature_html=signature_html,
    )


def _html_escape(s: str | None) -> str:
    if not s:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


def _strip_ai_wrappers(content: str) -> str:
    """AI иногда возвращает с обёртками — снимаем."""
    if not content:
        return ""
    s = content.strip()
    # Снимаем markdown code-fence если есть
    s = re.sub(r"^```(?:html)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```\s*$", "", s)
    # Снимаем <html>/<body> если AI всё-таки вернул их
    s = re.sub(r"</?html[^>]*>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"</?head[^>]*>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"</?body[^>]*>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"<style[^>]*>.*?</style>", "", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<script[^>]*>.*?</script>", "", s, flags=re.IGNORECASE | re.DOTALL)
    return s.strip()


def generate_proposal(db, project: ProposalProject, user_api_key: str | None = None) -> dict:
    """
    Генерирует HTML+PDF для проекта КП.
    Возвращает {html, pdf_path, usage}. Кидает ValueError при ошибке AI.
    """
    brand = None
    if project.brand_id:
        brand = db.query(ProposalBrand).filter_by(
            id=project.brand_id, user_id=project.user_id).first()
    brand_css = _build_brand_css(brand)

    # Контекст с сайта (если URL задан)
    site_ctx = ""
    if project.client_site_url:
        site_ctx = parse_client_site(project.client_site_url)
        # Кэшируем результат в БД — чтобы при ре-генерации не парсить заново
        if site_ctx and site_ctx != (project.client_site_ctx or ""):
            project.client_site_ctx = site_ctx

    # Прайс. Приоритет: собственный price_list (новый способ) → бот (legacy).
    price_text = ""
    if getattr(project, "price_list_id", None):
        price_text = fetch_price_from_list(db, project.price_list_id, project.user_id)
    if not price_text and project.bot_id:
        price_text = fetch_price_from_bot(db, project.bot_id, project.user_id)

    # JSON-first: AI возвращает структурированные данные → мы рендерим
    # в HTML по нашему шаблону (стабильное оформление). Fallback на
    # legacy HTML-генерацию если JSON не сработал.
    json_prompt = _claude_prompt_json(brand_css, project, price_text, site_ctx)
    log.info(f"[proposal] generating (JSON-first) for project={project.id} prompt_len={len(json_prompt)}")
    ans = generate_response("claude", [{"role": "user", "content": json_prompt}],
                             extra={"max_tokens": 6000}, user_api_key=user_api_key)
    if not isinstance(ans, dict) or not ans.get("content"):
        raise ValueError("AI вернул пустой ответ")

    raw_content = ans.get("content", "")
    parsed = _parse_proposal_json(raw_content)
    if parsed and (parsed.get("hero") or parsed.get("offering")):
        # Успех: рендерим JSON в HTML по нашему шаблону
        ai_html = _render_proposal_json(parsed, brand_css)
        log.info(f"[proposal] JSON parsed OK: {len(ai_html)} chars")
    else:
        # Fallback: AI вернул не-JSON → пробуем как HTML (legacy путь)
        log.warning(f"[proposal] JSON parse failed, falling back to legacy HTML")
        ai_html = _strip_ai_wrappers(raw_content)
        if not ai_html:
            raise ValueError("AI вернул пустой контент")

    full_html = _wrap_html(brand_css, ai_html, project)

    # PDF
    pdf_rel_path = _save_pdf(full_html, project.id)

    return {
        "html": full_html,
        "pdf_path": pdf_rel_path,
        "usage": ans.get("usage", {}) or {},
    }


def edit_section(section_html: str, instruction: str, brand_css: dict,
                  user_api_key: str | None = None) -> dict:
    """Точечная правка одной <section> блока КП.
    Цена: реальные токены × 5 (ai.improve_margin_pct), без фикс-минимума.

    Возвращает {'html': str, 'usage': dict}. ValueError при пустом ответе AI.
    """
    prompt = (
        "Ты редактируешь ОДИН блок коммерческого предложения (HTML <section>). "
        "Верни обновлённый HTML того же блока — только сам <section>, без обёрток. "
        "Сохраняй фирменные классы (.kp-section/.kp-hero/.kp-grid/.kp-card/"
        ".kp-price-table/.kp-cta/.kp-summary). Без markdown-кода, без <html>/<body>. "
        "Русский язык, без воды.\n\n"
        f"=== ИНСТРУКЦИЯ ОТ ПОЛЬЗОВАТЕЛЯ ===\n{instruction}\n\n"
        f"=== ТЕКУЩИЙ БЛОК ===\n{section_html}\n\n"
        "Верни ТОЛЬКО обновлённый <section>...</section>."
    )
    ans = generate_response("claude", [{"role": "user", "content": prompt}],
                             extra={"max_tokens": 4000}, user_api_key=user_api_key)
    if not isinstance(ans, dict) or not ans.get("content"):
        raise ValueError("AI вернул пустой ответ")
    new_html = _strip_ai_wrappers(ans.get("content", ""))
    if not new_html:
        raise ValueError("AI вернул пустой HTML")
    return {"html": new_html, "usage": ans.get("usage", {}) or {}}


def _save_pdf(html: str, project_id: int) -> str:
    """Конвертит HTML в PDF и сохраняет в /uploads/proposals/. Возвращает относительный путь."""
    from server.pdf_builder import html_to_pdf_bytes  # переиспользуем helper
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(base, "uploads", "proposals")
    os.makedirs(out_dir, exist_ok=True)
    fname = f"kp_{project_id}_{int(datetime.utcnow().timestamp())}.pdf"
    out_path = os.path.join(out_dir, fname)
    pdf_bytes = html_to_pdf_bytes(html)
    with open(out_path, "wb") as f:
        f.write(pdf_bytes)
    return f"/uploads/proposals/{fname}"
