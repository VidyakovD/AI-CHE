"""
Одноразовый скрипт: чистит SiteProject.code_html от моих edit-режим артефактов.

Проблема: ранее syncCode() в views/sites.html брал doc.documentElement.outerHTML
ВКЛЮЧАЯ <body contenteditable="true">, <style id="__editmode_css">, data-edit-id
и inline-style cursor:pointer. Эти артефакты записывались в БД через auto-save.

При следующей загрузке iframe body уже contenteditable С ПОРОГА — без клика
юзера. Dashed-outlines применяются всегда → юзер видит «всё в edit-режиме»
даже когда editMode=false в parent.

Этот скрипт чистит все существующие code_html. После очистки и фикса в
syncCode (см. коммит) проблема не возвращается.

Usage:
    DATABASE_URL=... python scripts/sanitize_site_code_html.py [--dry-run]
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.db import db_session
from server.models import SiteProject


# Регексы для очистки. Применяются последовательно к code_html.
RE_EDITMODE_CSS = re.compile(
    r'<style\s+id=["\']__editmode_css["\'][^>]*>.*?</style>\s*',
    re.IGNORECASE | re.DOTALL,
)
RE_BODY_TAG = re.compile(r'<body([^>]*)>', re.IGNORECASE)
RE_CONTENTEDITABLE_ATTR = re.compile(r'\s*contenteditable\s*=\s*"[^"]*"', re.IGNORECASE)
RE_SPELLCHECK_ATTR = re.compile(r'\s*spellcheck\s*=\s*"[^"]*"', re.IGNORECASE)
RE_DATA_EDIT_ID = re.compile(r'\s*data-edit-id\s*=\s*"[^"]*"', re.IGNORECASE)
RE_DATA_EDIT_ICON = re.compile(r'\s*data-edit-icon\s*=\s*"[^"]*"', re.IGNORECASE)
# style="opacity:1" / style="cursor: pointer" — поштучно (только мои inline'ы)
RE_INLINE_CURSOR = re.compile(r'cursor\s*:\s*pointer\s*;?\s*', re.IGNORECASE)
RE_INLINE_OUTLINE = re.compile(r'outline\s*:\s*[^;]*;?\s*', re.IGNORECASE)
RE_EMPTY_STYLE = re.compile(r'\s*style\s*=\s*"\s*"', re.IGNORECASE)


def clean_html(html: str) -> tuple[str, dict]:
    """Очистка от edit-артефактов. Возвращает (cleaned, stats)."""
    stats = {}
    out = html

    # 1. <style id="__editmode_css">...</style>
    out, n = RE_EDITMODE_CSS.subn('', out)
    if n: stats['editmode_css_removed'] = n

    # 2. body contenteditable / spellcheck
    def clean_body(m):
        attrs = m.group(1)
        new_attrs = RE_CONTENTEDITABLE_ATTR.sub('', attrs)
        new_attrs = RE_SPELLCHECK_ATTR.sub('', new_attrs)
        return f'<body{new_attrs}>'
    out, n = RE_BODY_TAG.subn(clean_body, out)
    if n: stats['body_tags_cleaned'] = n

    # 3. Все contenteditable="false" (которые я ставил на медиа)
    out, n = re.subn(r'\s*contenteditable\s*=\s*"false"', '', out, flags=re.IGNORECASE)
    if n: stats['contenteditable_false_removed'] = n

    # 4. data-edit-id / data-edit-icon
    out, n = RE_DATA_EDIT_ID.subn('', out)
    if n: stats['data_edit_id_removed'] = n
    out, n = RE_DATA_EDIT_ICON.subn('', out)
    if n: stats['data_edit_icon_removed'] = n

    # 5. Inline cursor:pointer / outline:... (от моих hover-listeners)
    out, n = RE_INLINE_CURSOR.subn('', out)
    if n: stats['inline_cursor_removed'] = n
    out, n = RE_INLINE_OUTLINE.subn('', out)
    if n: stats['inline_outline_removed'] = n

    # 6. Пустые style="" атрибуты (после удаления inline'ов)
    out, n = RE_EMPTY_STYLE.subn('', out)
    if n: stats['empty_style_removed'] = n

    return out, stats


def main():
    dry = "--dry-run" in sys.argv
    print(f"{'[DRY-RUN] ' if dry else ''}Sanitizing SiteProject.code_html...\n")

    with db_session() as db:
        rows = db.query(SiteProject).filter(SiteProject.code_html.isnot(None)).all()
        affected = 0
        total_chars_saved = 0
        for p in rows:
            old = p.code_html or ''
            if not old:
                continue
            # Quick check — нужна ли чистка?
            if not any(marker in old.lower() for marker in [
                '__editmode_css', 'contenteditable=', 'data-edit-id', 'data-edit-icon',
            ]):
                continue
            new, stats = clean_html(old)
            saved = len(old) - len(new)
            if not stats:
                continue
            affected += 1
            total_chars_saved += saved
            print(f"  #{p.id} {p.name!r}: -{saved} chars, {stats}")
            if not dry:
                p.code_html = new
        if not dry:
            db.commit()
            print(f"\n✅ Cleaned {affected} project(s), saved {total_chars_saved} chars total.")
        else:
            print(f"\n[DRY-RUN] Would clean {affected} project(s), -{total_chars_saved} chars.")
            print("Re-run without --dry-run to apply.")


if __name__ == "__main__":
    main()
