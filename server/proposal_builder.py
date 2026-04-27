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


def fetch_price_from_bot(db, bot_id: int, user_id: int) -> str:
    """
    Извлекает прайс-лист бота как plain text для вставки в Claude prompt.
    Возвращает «» если бота нет или нет позиций.
    """
    if not bot_id:
        return ""
    bot = db.query(ChatBot).filter_by(id=bot_id, user_id=user_id).first()
    if not bot:
        return ""
    items = (db.query(BotPriceItem)
               .filter_by(bot_id=bot_id, is_active=True)
               .order_by(BotPriceItem.sort_order, BotPriceItem.id)
               .all())
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


# ── HTML/PDF generation ────────────────────────────────────────────────────


def _build_brand_css(brand: ProposalBrand | None) -> dict:
    """Возвращает dict со стилевыми переменными бренда.

    Подбирает PDF-шрифт через pdf_builder.resolve_pdf_font: маппит
    web-имя бренда (Inter/Manrope/Roboto) на установленный системный TTF
    (Liberation Sans/Noto Sans/DejaVu Sans). Это гарантирует, что
    xhtml2pdf отрендерит выбранный шрифт, а не fallback на встроенный
    Helvetica (без кириллицы → квадратики).
    """
    from server.pdf_builder import resolve_pdf_font as _resolve
    if not brand:
        pdf_font = _resolve(None)
        return {
            "primary": "#ff8c42", "accent": "#ffb347", "secondary": "#1C1C1C",
            "font": f"{pdf_font}, sans-serif", "preset": "minimal",
            "company": "", "logo_url": "", "contacts": "",
            "inn": "", "address": "", "signature": "",
        }
    brand_font = brand.font_family or "Inter"
    pdf_font = _resolve(brand_font)
    font = f"{pdf_font}, {brand_font}, sans-serif"
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
    }


def _claude_prompt(brand_css: dict, project: ProposalProject, price_text: str,
                    site_ctx: str) -> str:
    """Собираем промпт. Просим Claude вернуть HTML <div>, мы потом обернём в шаблон."""
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


_BASE_TEMPLATE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"/>
<title>{title}</title>
<style>
  @page {{ size: A4; margin: 18mm 16mm; }}
  body {{ font-family: {font}; color: #1a1a1a; line-height: 1.55; font-size: 11pt; }}
  h1, h2, h3 {{ font-family: {font}; color: {primary}; margin: 0.6em 0 0.3em; }}
  h1 {{ font-size: 22pt; line-height: 1.2; }}
  h2 {{ font-size: 15pt; border-bottom: 2px solid {accent}; padding-bottom: 4pt; margin-top: 10pt; }}
  h3 {{ font-size: 12pt; color: {secondary}; }}
  p {{ margin: 0 0 6pt; }}
  strong {{ color: {primary}; }}
  .kp-header {{ display: table; width: 100%; margin-bottom: 14pt; }}
  .kp-header .logo {{ display: table-cell; width: 90pt; vertical-align: middle; }}
  .kp-header .logo img {{ max-width: 80pt; max-height: 60pt; }}
  .kp-header .meta {{ display: table-cell; vertical-align: middle; text-align: right; font-size: 9pt; color: #666; }}
  .kp-header .meta .valid {{ color: {primary}; font-weight: 600; }}
  .kp-hero {{ background: {primary}; color: #fff; padding: 18pt 20pt; border-radius: 8pt; margin-bottom: 14pt; }}
  .kp-hero h1, .kp-hero h2, .kp-hero h3 {{ color: #fff; }}
  .kp-hero strong {{ color: #fff; }}
  .kp-hero p {{ color: #fff; opacity: 0.95; }}
  .kp-section {{ margin-bottom: 12pt; }}
  .kp-grid {{ display: table; width: 100%; border-collapse: separate; border-spacing: 6pt 0; margin: 6pt 0; }}
  .kp-card {{ display: table-cell; padding: 10pt; background: #f7f5f1; border-left: 3pt solid {accent}; vertical-align: top; border-radius: 4pt; }}
  .kp-card h3 {{ margin-top: 0; }}
  .kp-price-table {{ width: 100%; border-collapse: collapse; margin: 10pt 0; }}
  .kp-price-table th, .kp-price-table td {{ padding: 8pt 10pt; text-align: left; border-bottom: 1pt solid #e0d8c8; vertical-align: top; }}
  .kp-price-table th {{ background: {secondary}; color: #fff; font-weight: 600; font-size: 10pt; text-transform: uppercase; letter-spacing: 0.5pt; }}
  .kp-price-table tr:nth-child(even) td {{ background: #faf7f0; }}
  .kp-price-table td:last-child {{ text-align: right; font-weight: 600; white-space: nowrap; }}
  .kp-price-table tr.total td {{ background: {accent}; color: {secondary}; font-weight: 700; font-size: 12pt; border-bottom: none; }}
  .kp-summary {{ background: #f7f5f1; border-left: 4pt solid {primary}; padding: 10pt 14pt; margin: 10pt 0; border-radius: 4pt; }}
  .kp-cta {{ background: {accent}; color: {secondary}; padding: 16pt; border-radius: 6pt; text-align: center; font-weight: 700; font-size: 13pt; margin: 14pt 0 8pt; }}
  .kp-cta strong {{ color: {primary}; }}
  .kp-footer {{ border-top: 1pt solid #ccc; margin-top: 18pt; padding-top: 8pt; font-size: 8pt; color: #777; }}
  ul, ol {{ padding-left: 18pt; margin: 4pt 0; }}
  li {{ margin-bottom: 3pt; }}
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

    return _BASE_TEMPLATE.format(
        title=_html_escape(title), font=brand_css["font"],
        primary=brand_css["primary"], accent=brand_css["accent"],
        secondary=brand_css["secondary"],
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

    # Прайс из бота
    price_text = ""
    if project.bot_id:
        price_text = fetch_price_from_bot(db, project.bot_id, project.user_id)

    prompt = _claude_prompt(brand_css, project, price_text, site_ctx)
    log.info(f"[proposal] generating for project={project.id} prompt_len={len(prompt)}")
    ans = generate_response("claude", [{"role": "user", "content": prompt}],
                             extra={"max_tokens": 8000}, user_api_key=user_api_key)
    if not isinstance(ans, dict) or not ans.get("content"):
        raise ValueError("AI вернул пустой ответ")
    ai_html = _strip_ai_wrappers(ans.get("content", ""))
    if not ai_html:
        raise ValueError("AI вернул пустой HTML")

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
