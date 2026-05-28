#!/usr/bin/env python3
"""Скрипт добавляет недостающие a11y-атрибуты в HTML views:

  1. Кнопкам с onclick=close*() и иконкой (✕ ✎ ↻ → 🗑) без aria-label
     добавляем aria-label из значения title= если есть, иначе «Закрыть».
  2. Inputs/textareas/selects с placeholder= и без aria-label/label-for:
     добавляем aria-label из placeholder. Это самый дешёвый win — скрин-ридер
     произнесёт что за поле, при этом placeholder остаётся видимым.

Скрипт идемпотентный — повторный запуск ничего не изменит.

Запуск:
    python scripts/add_a11y_attrs.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEWS = ROOT / "views"

TARGET_PAGES = [
    "proposals.html",
    "sites.html",
    "chatbots.html",
    "agents-modular.html",
    "admin.html",
    "api.html",
]


# Маппинг иконок → дефолтный aria-label
ICON_LABELS = {
    "✕": "Закрыть",
    "×": "Закрыть",
    "✎": "Переименовать",
    "↻": "Обновить",
    "🗑": "Удалить",
    "▶": "Запустить",
    "⏸": "Пауза",
    "📋": "Скопировать",
    "+": "Добавить",
}


# Регэксп для иконочных кнопок (<button ... >ICON</button>).
# Захватываем атрибуты до иконки.
BUTTON_RE = re.compile(
    r'(<button\b)([^>]*?)(>)([\s ]*(?:' +
    "|".join(re.escape(k) for k in ICON_LABELS) +
    r')[\s ]*)(</button>)',
    re.UNICODE,
)


def _has_attr(attrs: str, name: str) -> bool:
    return re.search(rf'\b{name}=', attrs) is not None


def _extract_attr(attrs: str, name: str) -> str | None:
    m = re.search(rf'\b{name}="([^"]*)"', attrs)
    return m.group(1) if m else None


def fix_buttons(text: str) -> tuple[str, int]:
    n = 0
    def repl(m: re.Match) -> str:
        nonlocal n
        head, attrs, gt, inner, tail = m.groups()
        # уже есть aria-label — пропускаем
        if _has_attr(attrs, "aria-label"):
            return m.group(0)
        # подбираем label
        icon = inner.strip()
        # title=... приоритетнее
        title = _extract_attr(attrs, "title")
        label = title if title else ICON_LABELS.get(icon, "Действие")
        n += 1
        return f'{head}{attrs} aria-label="{label}"{gt}{inner}{tail}'

    text2 = BUTTON_RE.sub(repl, text)
    return text2, n


# Регэксп для input/textarea/select с placeholder=. Захватываем тэг и атрибуты
# одной строкой (HTML может переноситься, но скрипт работает только на
# одно-строчных тегах — этого достаточно для большинства случаев).
INPUT_RE = re.compile(
    r'(<(?:input|textarea|select)\b)([^>]*?\bplaceholder="([^"]+)"[^>]*?)(/?>)',
    re.IGNORECASE,
)


def fix_inputs(text: str) -> tuple[str, int]:
    n = 0
    def repl(m: re.Match) -> str:
        nonlocal n
        head, attrs, placeholder, tail = m.groups()
        # уже есть aria-label или aria-labelledby — пропускаем
        if _has_attr(attrs, "aria-label") or _has_attr(attrs, "aria-labelledby"):
            return m.group(0)
        # обрезаем длинный placeholder до 80 символов (для скрин-ридера хватит)
        label = placeholder.strip()
        if len(label) > 80:
            label = label[:77] + "..."
        # экранируем кавычки в значении
        label = label.replace('"', "&quot;")
        n += 1
        return f'{head}{attrs} aria-label="{label}"{tail}'

    text2 = INPUT_RE.sub(repl, text)
    return text2, n


def process_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    original = text
    text, n_btn = fix_buttons(text)
    text, n_inp = fix_inputs(text)
    changed = text != original
    if changed:
        path.write_text(text, encoding="utf-8")
    return {"file": path.name, "buttons": n_btn, "inputs": n_inp,
            "changed": changed}


def main() -> int:
    total_btn = 0
    total_inp = 0
    for name in TARGET_PAGES:
        p = VIEWS / name
        if not p.exists():
            print(f"  SKIP (missing): {name}")
            continue
        r = process_file(p)
        total_btn += r["buttons"]
        total_inp += r["inputs"]
        mark = "OK " if r["changed"] else "   "
        print(f"  {mark}{r['file']:30s}  buttons:+{r['buttons']:3d}  inputs:+{r['inputs']:3d}")
    print(f"\nTOTAL: +{total_btn} button aria-label, +{total_inp} input aria-label")
    return 0


if __name__ == "__main__":
    sys.exit(main())
