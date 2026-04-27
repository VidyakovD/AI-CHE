"""
PDF-отчёты для бизнес-решений.

Принимает Markdown от Claude и рендерит в PDF с фирменными стилями
«AI Студия Че». Использует xhtml2pdf (pure-Python, без system deps).

Если markdown / xhtml2pdf не установлены — функция возвращает None
и юзер видит обычный текст вместо PDF.
"""
import os
import logging
from datetime import datetime
from html import escape as _esc

log = logging.getLogger(__name__)


_BRAND_CSS = """
@page {
    size: a4 portrait;
    margin: 2.2cm 1.8cm 2.2cm 1.8cm;
    @frame header_frame {
        -pdf-frame-content: header_content;
        left: 1.8cm; width: 17.4cm; top: 0.8cm; height: 1cm;
    }
    @frame content_frame {
        left: 1.8cm; width: 17.4cm; top: 2.2cm; height: 24cm;
    }
    @frame footer_frame {
        -pdf-frame-content: footer_content;
        left: 1.8cm; width: 17.4cm; bottom: 0.8cm; height: 1cm;
    }
}
body {
    font-family: 'DejaVu Sans', 'Helvetica', sans-serif;
    color: #1a1a1a;
    font-size: 11pt;
    line-height: 1.55;
}
#header_content { font-size: 9pt; color: #b6915e; text-align: right; }
#footer_content { font-size: 9pt; color: #888; text-align: center; }
.cover {
    text-align: center;
    margin-top: 40mm;
    page-break-after: always;
}
.cover .brand {
    color: #c89052;
    font-size: 14pt;
    /* xhtml2pdf не понимает em для letter-spacing, использует pt:
       0.4em ≈ 5.6pt при 14pt шрифте. */
    letter-spacing: 5.6pt;
    text-transform: uppercase;
    font-weight: 600;
    margin-bottom: 12mm;
}
.cover h1.title {
    font-size: 28pt;
    color: #1a1a1a;
    line-height: 1.2;
    margin-bottom: 6mm;
    border: none;
    padding: 0;
    /* На обложке H1 не должен иметь page-break (наследуется из общего h1) */
    page-break-before: avoid;
}
.cover .subtitle {
    color: #666;
    font-size: 12pt;
    margin-bottom: 35mm;
}
.cover .meta {
    color: #888;
    font-size: 10pt;
    margin-top: 20mm;
}
.cover .stripe {
    height: 3px;
    background: linear-gradient(90deg, #c89052, #ff8c42);
    margin: 4mm auto;
    width: 50mm;
    border-radius: 2px;
}
h1 {
    color: #1a1a1a;
    font-size: 22pt;
    border-bottom: 2px solid #c89052;
    padding-bottom: 4mm;
    margin-top: 8mm;
    margin-bottom: 6mm;
    /* H1 — крупный раздел: всегда с новой страницы для читаемости отчёта */
    page-break-before: always;
    page-break-after: avoid;
}
h2 {
    color: #c89052;
    font-size: 16pt;
    margin-top: 8mm;
    margin-bottom: 3mm;
    page-break-after: avoid;
    page-break-inside: avoid;
}
h3 {
    color: #2a2a2a;
    font-size: 13pt;
    margin-top: 6mm;
    margin-bottom: 2mm;
    page-break-after: avoid;
}
h4 { color: #2a2a2a; font-size: 11pt; font-weight: 700; margin-top: 4mm; }
p { margin: 0 0 3mm 0; text-align: justify; }
ul, ol { margin: 0 0 4mm 6mm; }
li { margin-bottom: 1.5mm; }
strong { color: #111; }
em { color: #444; }
table {
    width: 100%;
    border-collapse: collapse;
    margin: 4mm 0;
    font-size: 10pt;
    page-break-inside: avoid;
}
th, td {
    border: 1px solid #d0c2a8;
    padding: 2mm 3mm;
    text-align: left;
    vertical-align: top;
}
th {
    background: #f6efdf;
    color: #5a4520;
    font-weight: 700;
}
tr:nth-child(even) td { background: #fbf8f0; }
blockquote {
    border-left: 3px solid #c89052;
    padding: 1mm 4mm;
    margin: 3mm 0;
    color: #555;
    background: #fbf8f0;
    font-style: italic;
}
code {
    background: #f4f0e7;
    padding: 0.5mm 1.5mm;
    border-radius: 2px;
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 9.5pt;
    color: #8b5a2b;
}
pre {
    background: #f4f0e7;
    padding: 3mm;
    border-radius: 3px;
    overflow-x: hidden;
    font-size: 9pt;
    page-break-inside: avoid;
}
hr {
    border: none;
    border-top: 1px solid #d8c8a8;
    margin: 5mm 0;
}
.footer-note {
    margin-top: 10mm;
    color: #888;
    font-size: 9pt;
    text-align: center;
    font-style: italic;
}
"""


_FONTS_REGISTERED = False

# Family → (regular, bold, italic, boldItalic). None если файла нет на системе.
_FONT_FAMILIES = {
    "DejaVuSans": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
    ),
    "Liberation Sans": (
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf",
    ),
    "Liberation Serif": (
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-BoldItalic.ttf",
    ),
    "Noto Sans": (
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Italic.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-BoldItalic.ttf",
    ),
    "Noto Serif": (
        "/usr/share/fonts/truetype/noto/NotoSerif-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSerif-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSerif-Italic.ttf",
        "/usr/share/fonts/truetype/noto/NotoSerif-BoldItalic.ttf",
    ),
}

# Маппинг web-имён шрифтов (Inter/Roboto/Manrope) на доступные системные
# семейства — чтобы пользователь выбирал привычное имя в UI бренда, а
# в PDF подставлялся ближайший аналог с поддержкой кириллицы.
_FONT_FALLBACKS = {
    "Inter": "Liberation Sans",
    "Manrope": "Liberation Sans",
    "Roboto": "Liberation Sans",
    "Open Sans": "Liberation Sans",
    "Lato": "Liberation Sans",
    "Montserrat": "Liberation Sans",
    "PT Sans": "Liberation Sans",
    "Source Sans Pro": "Liberation Sans",
    "Raleway": "Liberation Sans",
    "Nunito": "Liberation Sans",
    "Noto Sans": "Noto Sans",
    "Liberation Sans": "Liberation Sans",
    "Playfair Display": "Liberation Serif",
    "Merriweather": "Liberation Serif",
    "Liberation Serif": "Liberation Serif",
    "Noto Serif": "Noto Serif",
}


def resolve_pdf_font(brand_font: str | None) -> str:
    """Возвращает имя зарегистрированного семейства, подходящего для PDF.
    Имя БЕЗ пробелов — точно как в @font-face и в registerFontFamily
    (xhtml2pdf требует точный матч). Например «Liberation Sans» →
    «LiberationSans». Если выбранный шрифт неизвестен — DejaVuSans
    (гарантированно есть)."""
    if not brand_font:
        return "DejaVuSans"
    fallback = _FONT_FALLBACKS.get(brand_font.strip())
    if fallback and fallback in _FONT_FAMILIES:
        # Проверяем что файл реально доступен
        regular_path = _FONT_FAMILIES[fallback][0]
        if os.path.exists(regular_path):
            return fallback.replace(" ", "")
    return "DejaVuSans"


def _ensure_cyrillic_font_registered() -> str | None:
    """Регистрирует все доступные TTF-семейства с поддержкой кириллицы в
    ReportLab. Без этого xhtml2pdf использует встроенный Helvetica, в
    котором нет русских глифов → «квадратики».

    DejaVu Sans — обязательный fallback (всегда установлен на проде).
    Liberation Sans/Serif и Noto Sans/Serif — дополнительные пресеты для
    выбора пользователем в UI бренда.
    """
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return "DejaVuSans"
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfbase.pdfmetrics import registerFontFamily
    except Exception as e:
        log.warning(f"[pdf] reportlab not available: {type(e).__name__}")
        return None

    registered_count = 0
    for family, (reg, bold, italic, boldit) in _FONT_FAMILIES.items():
        if not os.path.exists(reg):
            continue
        try:
            # Имена в ReportLab не должны содержать пробелов — заменяем
            base = family.replace(" ", "")
            pdfmetrics.registerFont(TTFont(base, reg))
            bold_name = base
            italic_name = base
            boldit_name = base
            if bold and os.path.exists(bold):
                pdfmetrics.registerFont(TTFont(base + "-Bold", bold))
                bold_name = base + "-Bold"
            if italic and os.path.exists(italic):
                pdfmetrics.registerFont(TTFont(base + "-Italic", italic))
                italic_name = base + "-Italic"
            if boldit and os.path.exists(boldit):
                pdfmetrics.registerFont(TTFont(base + "-BoldItalic", boldit))
                boldit_name = base + "-BoldItalic"
            registerFontFamily(base, normal=base, bold=bold_name,
                                italic=italic_name, boldItalic=boldit_name)
            registered_count += 1
        except Exception as e:
            log.warning(f"[pdf] register {family} failed: {type(e).__name__}")

    if registered_count == 0:
        log.warning("[pdf] no cyrillic fonts registered")
        return None
    _FONTS_REGISTERED = True
    log.info(f"[pdf] {registered_count} cyrillic font families registered")
    return "DejaVuSans"


def _inject_dejavu_font_face(html: str) -> str:
    """Внедряет @font-face деклараци в <head> для всех зарегистрированных
    семейств. xhtml2pdf использует это чтобы `font-family:'DejaVuSans'`
    или `font-family:'LiberationSans'` в CSS работало предсказуемо.
    """
    decls = []
    for family, paths in _FONT_FAMILIES.items():
        if not os.path.exists(paths[0]):
            continue
        base = family.replace(" ", "")
        decls.append(f"@font-face {{ font-family: '{base}'; src: url('{paths[0]}'); }}")
        if paths[1] and os.path.exists(paths[1]):
            decls.append(f"@font-face {{ font-family: '{base}'; src: url('{paths[1]}'); font-weight: bold; }}")
        if paths[2] and os.path.exists(paths[2]):
            decls.append(f"@font-face {{ font-family: '{base}'; src: url('{paths[2]}'); font-style: italic; }}")
    if not decls:
        return html
    face = "<style>" + "\n".join(decls) + "</style>"
    if "</head>" in html:
        return html.replace("</head>", face + "</head>", 1)
    return face + html


def html_to_pdf_bytes(full_html: str) -> bytes:
    """Конвертит готовый HTML (со своим <style>) в PDF-bytes.
    В отличие от markdown_to_pdf — не добавляет _BRAND_CSS и не заворачивает
    в обложку. Используется в proposal_builder где у нас свой шаблон бренда.

    Регистрирует DejaVu Sans для поддержки кириллицы (без этого вместо
    русских букв — квадратики).

    Кидает RuntimeError при ошибке pisa.
    """
    from xhtml2pdf import pisa
    import io
    _ensure_cyrillic_font_registered()
    full_html = _inject_dejavu_font_face(full_html)
    buf = io.BytesIO()
    res = pisa.CreatePDF(full_html, dest=buf, encoding="utf-8")
    if res.err:
        raise RuntimeError(f"PDF generation failed: {res.err} errors")
    return buf.getvalue()


def markdown_to_pdf(md_text: str, title: str = "Бизнес-отчёт",
                    out_path: str = None,
                    subtitle: str = "") -> str | None:
    """Конвертирует Markdown в PDF с фирменным стилем.
    Возвращает absolute path сохранённого файла или None при ошибке.
    """
    try:
        import markdown as _md
        from xhtml2pdf import pisa
    except ImportError as e:
        log.warning(f"PDF недоступен: {e}. Установите xhtml2pdf+markdown.")
        return None

    md_html = _md.markdown(
        md_text,
        extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
    )

    cover_subtitle = _esc(subtitle) if subtitle else "Бизнес-решение от AI Студия Че"
    today = datetime.now().strftime("%d.%m.%Y")

    full_html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>{_esc(title)}</title>
  <style>{_BRAND_CSS}</style>
</head>
<body>
  <div id="header_content">AI Студия Че</div>
  <div id="footer_content">aiche.ru · стр. <pdf:pagenumber/> из <pdf:pagecount/></div>
  <div class="cover">
    <p class="brand">AI Студия Че</p>
    <div class="stripe"></div>
    <h1 class="title">{_esc(title)}</h1>
    <p class="subtitle">{cover_subtitle}</p>
    <p class="meta">Подготовлено: {today}</p>
  </div>
  <pdf:nextpage/>
  {md_html}
  <p class="footer-note">Документ сгенерирован AI Студия Че · aiche.ru</p>
</body>
</html>"""

    try:
        if out_path is None:
            from uuid import uuid4
            out_path = f"/tmp/sol_{uuid4().hex[:10]}.pdf"
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "wb") as f:
            res = pisa.CreatePDF(full_html, dest=f, encoding="utf-8")
        if res.err:
            log.error(f"PDF pisa errors: {res.err}")
            return None
        return out_path
    except Exception as e:
        log.error(f"PDF generation failed: {e}")
        return None
