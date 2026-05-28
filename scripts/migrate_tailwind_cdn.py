#!/usr/bin/env python3
"""Миграция Tailwind CDN → собранный /styles.css с CDN-фолбэком.

Заменяет в каждом HTML двусоставный блок:
    <script src="https://cdn.tailwindcss.com/3.4.0"></script>
    <script>tailwind.config={...}</script>

на единый fallback-блок: пытается HEAD /styles.css; если 404 —
динамически подгружает CDN и применяет тот же config.

Для агентов-modular.html уже сделано вручную — этот скрипт пропустит её
если уже видит /styles.css перед CDN.
"""
import re
from pathlib import Path

VIEWS = Path(__file__).resolve().parent.parent / "views"

# pattern 1: пара script-cdn + script-config на соседних строках
PATTERN_PAIR = re.compile(
    r'<script src="https://cdn\.tailwindcss\.com(?:/[\d.]+)?"(?:\s+defer)?></script>\s*\n'
    r'\s*<script>\s*(tailwind\.config\s*=\s*\{.*?\})\s*</script>',
    re.DOTALL,
)

# pattern 2: только CDN, без inline-config (qr_confirm.html)
PATTERN_LONE = re.compile(
    r'<script src="https://cdn\.tailwindcss\.com(?:/[\d.]+)?"(?:\s+defer)?></script>'
)


def build_fallback(config_assign: str | None) -> str:
    """`config_assign` — строка вида 'tailwind.config={...}', либо None.

    Возвращает многострочный fallback-блок: HEAD /styles.css → если есть,
    ничего не делаем; иначе подгружаем CDN и применяем config (если был).
    """
    if config_assign:
        # вытащим объект справа от знака =
        m = re.match(r'tailwind\.config\s*=\s*(\{.*\})\s*$', config_assign.strip(), re.DOTALL)
        cfg_obj = m.group(1) if m else "{}"
        onload = (
            "      s.onload = function(){\n"
            f"        window.tailwind && (window.tailwind.config = {cfg_obj});\n"
            "      };\n"
        )
    else:
        onload = ""

    return (
        "<!-- Tailwind: собранный /styles.css. Fallback на CDN только если файл недоступен (dev). -->\n"
        "<script>\n"
        "(function(){\n"
        "  fetch('/styles.css', {method:'HEAD'}).then(function(r){\n"
        "    if(!r.ok){\n"
        "      var s = document.createElement('script');\n"
        "      s.src = 'https://cdn.tailwindcss.com/3.4.0';\n"
        f"{onload}"
        "      document.head.appendChild(s);\n"
        "    }\n"
        "  }).catch(function(){});\n"
        "})();\n"
        "</script>"
    )


def migrate_file(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    original = text

    # 1) Если в файле НЕТ ни /styles.css ни CDN — ничего не делаем.
    if "cdn.tailwindcss.com" not in text:
        return "skip-no-cdn"

    # 2) Сначала пробуем заменить пару (CDN + config)
    def _pair_replace(m: re.Match) -> str:
        cfg = m.group(1)
        return build_fallback("tailwind.config=" + cfg if not cfg.startswith("tailwind") else cfg)

    text2, n_pair = PATTERN_PAIR.subn(_pair_replace, text, count=1)
    if n_pair == 0:
        # 3) Иначе одиночный <script src="...cdn..."></script>
        text2, n_lone = PATTERN_LONE.subn(build_fallback(None), text, count=1)
        if n_lone == 0:
            return "no-match"

    # Проверяем что styles.css уже есть в head (он во всех уже подключён)
    if "/styles.css" not in text2:
        return "no-styles-link"

    if text2 == original:
        return "noop"

    path.write_text(text2, encoding="utf-8")
    return "ok"


def main() -> None:
    targets = sorted(VIEWS.glob("*.html"))
    for p in targets:
        status = migrate_file(p)
        print(f"{p.name:40s} {status}")


if __name__ == "__main__":
    main()
