"""
PDF-отчёты для бизнес-решений.

Принимает Markdown от Claude и рендерит в PDF с фирменными стилями
«AI Студия Че». Использует WeasyPrint (HTML+CSS → PDF, поддержка
русских шрифтов, заголовков, списков, таблиц).

Запуск требует system deps:
  apt-get install -y libpango-1.0-0 libpangoft2-1.0-0 libcairo2 \
                     libgdk-pixbuf-2.0-0 libffi-dev shared-mime-info

Если WeasyPrint не установлен — функция возвращает None и юзер
видит обычный текст вместо PDF.
"""
import os
import logging
from datetime import datetime
from html import escape as _esc

log = logging.getLogger(__name__)


_BRAND_CSS = """
@page {
    size: A4;
    margin: 22mm 18mm 22mm 18mm;
    @top-right {
        content: "AI Студия Че";
        font-family: 'Inter', 'Helvetica', sans-serif;
        font-size: 9pt;
        color: #b6915e;
    }
    @bottom-center {
        content: "Стр. " counter(page) " / " counter(pages);
        font-family: 'Inter', 'Helvetica', sans-serif;
        font-size: 9pt;
        color: #888;
    }
    @bottom-right {
        content: "aiche.ru";
        font-family: 'Inter', 'Helvetica', sans-serif;
        font-size: 9pt;
        color: #b6915e;
    }
}
body {
    font-family: 'Inter', 'PT Sans', 'DejaVu Sans', 'Helvetica', sans-serif;
    color: #1a1a1a;
    font-size: 11pt;
    line-height: 1.55;
}
.cover {
    text-align: center;
    margin-top: 40mm;
    page-break-after: always;
}
.cover .brand {
    color: #c89052;
    font-size: 14pt;
    letter-spacing: 0.4em;
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
    page-break-after: avoid;
}
h2 {
    color: #c89052;
    font-size: 16pt;
    margin-top: 8mm;
    margin-bottom: 3mm;
    page-break-after: avoid;
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


def markdown_to_pdf(md_text: str, title: str = "Бизнес-отчёт",
                    out_path: str = None,
                    subtitle: str = "") -> str | None:
    """Конвертирует Markdown в PDF с фирменным стилем.

    Возвращает absolute path сохранённого файла или None при ошибке
    (например, если WeasyPrint не установлен).
    """
    try:
        import markdown as _md
        from weasyprint import HTML, CSS
    except ImportError as e:
        log.warning(f"PDF недоступен: {e}. Установите weasyprint+markdown.")
        return None

    md_html = _md.markdown(
        md_text,
        extensions=["fenced_code", "tables", "nl2br", "sane_lists", "toc"],
    )

    cover_subtitle = _esc(subtitle) if subtitle else "Бизнес-решение от AI Студия Че"
    today = datetime.now().strftime("%d.%m.%Y")

    full_html = f"""<!DOCTYPE html>
<html lang="ru">
<head><meta charset="UTF-8"><title>{_esc(title)}</title></head>
<body>
  <div class="cover">
    <p class="brand">AI Студия Че</p>
    <div class="stripe"></div>
    <h1 class="title">{_esc(title)}</h1>
    <p class="subtitle">{cover_subtitle}</p>
    <p class="meta">Подготовлено: {today}</p>
  </div>
  {md_html}
  <p class="footer-note">Документ сгенерирован AI Студия Че · aiche.ru</p>
</body>
</html>"""

    try:
        if out_path is None:
            from uuid import uuid4
            out_path = f"/tmp/sol_{uuid4().hex[:10]}.pdf"
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        HTML(string=full_html).write_pdf(out_path, stylesheets=[CSS(string=_BRAND_CSS)])
        return out_path
    except Exception as e:
        log.error(f"PDF generation failed: {e}")
        return None
