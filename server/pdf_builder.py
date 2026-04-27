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


def html_to_pdf_bytes(full_html: str) -> bytes:
    """Конвертит готовый HTML (со своим <style>) в PDF-bytes.
    В отличие от markdown_to_pdf — не добавляет _BRAND_CSS и не заворачивает
    в обложку. Используется в proposal_builder где у нас свой шаблон бренда.

    Кидает RuntimeError при ошибке pisa.
    """
    from xhtml2pdf import pisa
    import io
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
