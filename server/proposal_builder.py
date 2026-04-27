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

    ВАЖНО: основной шрифт всегда `DejaVuSans` — он регистрируется в
    pdf_builder._ensure_cyrillic_font_registered и единственный гарантирует
    поддержку кириллицы в xhtml2pdf. Шрифт бренда (Inter/Manrope/...)
    указывается как fallback, но в xhtml2pdf без TTF не будет применён —
    остаётся как «семантическая подсказка».
    """
    if not brand:
        return {
            "primary": "#ff8c42", "accent": "#ffb347", "secondary": "#1C1C1C",
            "font": "DejaVuSans, Inter, sans-serif", "preset": "minimal",
            "company": "", "logo_url": "", "contacts": "",
            "inn": "", "address": "", "signature": "",
        }
    brand_font = brand.font_family or "Inter"
    font = f"DejaVuSans, {brand_font}, sans-serif"
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
    parts = [
        "Ты — копирайтер B2B-агентства. Сделай **коммерческое предложение** в HTML.",
        "Возвращай ТОЛЬКО содержимое <body> в виде серии <section> блоков. "
        "Не пиши <html>, <head>, <body>, <style>, <script> — оформление мы добавим сами.",
        "Используй классы: `.kp-hero` (титульный блок), `.kp-section` (раздел), "
        "`.kp-grid` (карточки 2-3 в ряд), `.kp-card`, `.kp-price-table`, `.kp-cta`.",
        "Структура: 1) hero с обращением к клиенту, 2) понимание задачи, "
        "3) что предлагаем (с фактами/цифрами), 4) состав работ + цены (таблица), "
        "5) гарантии/сроки, 6) призыв к действию.",
        "",
        "=== БРЕНД ===",
        f"Компания: {brand_css.get('company') or '—'}",
        f"Контакты: {brand_css.get('contacts') or '—'}",
        f"Тон: профессиональный, без воды, с цифрами и фактами.",
    ]
    if project.client_name or project.client_email:
        parts += [
            "",
            "=== КЛИЕНТ ===",
            f"Имя: {project.client_name or '—'}",
            f"Email: {project.client_email or '—'}",
        ]
    if project.client_request:
        parts += [
            "",
            "=== ЗАПРОС КЛИЕНТА (адресуйся к нему лично!) ===",
            project.client_request[:5000],
        ]
    if site_ctx:
        parts += [
            "",
            "=== КОНТЕКСТ КЛИЕНТА (с его сайта) — упомяни их сферу/специфику ===",
            site_ctx[:_SITE_CTX_MAX_CHARS],
        ]
    if price_text:
        parts += [
            "",
            "=== НАШ ПРАЙС (бери цены отсюда, релевантные запросу) ===",
            price_text,
        ]
    if project.extra_notes:
        parts += [
            "",
            "=== ДОП. ИНСТРУКЦИИ ===",
            project.extra_notes[:2000],
        ]
    parts += [
        "",
        "Важно: верни ТОЛЬКО HTML-разметку (без markdown ```), "
        "русский язык, на сегодня дата " + datetime.utcnow().strftime("%d.%m.%Y") + ".",
    ]
    return "\n".join(parts)


_BASE_TEMPLATE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"/>
<title>{title}</title>
<style>
  @page {{ size: A4; margin: 18mm 16mm; }}
  body {{ font-family: {font}; color: #1a1a1a; line-height: 1.55; font-size: 11pt; }}
  h1, h2, h3 {{ font-family: {font}; color: {primary}; margin: 0.6em 0 0.3em; }}
  h1 {{ font-size: 22pt; }}
  h2 {{ font-size: 15pt; border-bottom: 2px solid {accent}; padding-bottom: 4pt; }}
  h3 {{ font-size: 12pt; color: {secondary}; }}
  .kp-header {{ display: table; width: 100%; margin-bottom: 18pt; }}
  .kp-header .logo {{ display: table-cell; width: 90pt; vertical-align: middle; }}
  .kp-header .logo img {{ max-width: 80pt; max-height: 60pt; }}
  .kp-header .meta {{ display: table-cell; vertical-align: middle; text-align: right; font-size: 9pt; color: #666; }}
  .kp-hero {{ background: {primary}; color: #fff; padding: 16pt 18pt; border-radius: 8pt; margin-bottom: 14pt; }}
  .kp-hero h1 {{ color: #fff; }}
  .kp-section {{ margin-bottom: 14pt; }}
  .kp-grid {{ display: table; width: 100%; }}
  .kp-card {{ display: table-cell; padding: 10pt; background: #f7f5f1; border-left: 3pt solid {accent}; vertical-align: top; }}
  .kp-card + .kp-card {{ border-left: 8pt solid #fff; padding-left: 10pt; }}
  .kp-price-table {{ width: 100%; border-collapse: collapse; margin: 10pt 0; }}
  .kp-price-table th, .kp-price-table td {{ padding: 7pt 9pt; text-align: left; border-bottom: 1pt solid #ddd; }}
  .kp-price-table th {{ background: {secondary}; color: #fff; font-weight: 600; }}
  .kp-cta {{ background: {accent}; color: {secondary}; padding: 14pt; border-radius: 6pt; text-align: center; font-weight: 700; font-size: 13pt; }}
  .kp-footer {{ border-top: 1pt solid #ccc; margin-top: 18pt; padding-top: 8pt; font-size: 8pt; color: #777; }}
  ul, ol {{ padding-left: 18pt; }}
  li {{ margin-bottom: 3pt; }}
</style></head><body>
<div class="kp-header">
  <div class="logo">{logo_html}</div>
  <div class="meta">
    {company_html}
    <div>Дата: {today}</div>
  </div>
</div>
{ai_content}
<div class="kp-footer">
  {footer_html}
</div>
</body></html>"""


def _wrap_html(brand_css: dict, ai_html: str, project: ProposalProject) -> str:
    """Оборачивает AI-сгенерённый контент в фирменный шаблон."""
    today = datetime.utcnow().strftime("%d.%m.%Y")
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

    title = "Коммерческое предложение"
    if project.client_name:
        title += " для " + project.client_name

    return _BASE_TEMPLATE.format(
        title=_html_escape(title), font=brand_css["font"],
        primary=brand_css["primary"], accent=brand_css["accent"],
        secondary=brand_css["secondary"],
        logo_html=logo_html, company_html=company_html, today=today,
        ai_content=ai_html, footer_html=footer_html,
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
