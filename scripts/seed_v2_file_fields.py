"""
Добавляет поля type:'file' / 'textarea' (URLs) в input_schema_json
для решений с orchestra-stages, требующих внешних данных:
  - file_extract → нужен файл (PDF/DOCX/XLSX)
  - vision_describe → нужны картинки/скриншоты
  - parallel_browse → нужен список URL

Идемпотентно: если поле уже есть — пропускает.
Запускается отдельно от seed_v2_solutions.py чтобы не пересоздавать
весь schema (юзер мог добавить кастомные поля).

Usage:
    DATABASE_URL=... python scripts/seed_v2_file_fields.py [--dry-run]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.db import db_session
from server.models import Solution


# id_решения → новые поля, которые ДОЛЖНЫ быть в input_schema (добавляются в начало)
FIELD_UPDATES = {
    # Юр.проверка договора — file_extract читает PDF/DOCX
    32: [
        {
            "name": "contract_file",
            "label": "Файл договора (PDF / DOCX / DOC)",
            "type": "file",
            "kind": "doc",
            "accept": ".pdf,.docx,.doc,.txt",
            "required": True,
            "hint": "Загрузите файл договора, который нужно проверить. До 25 МБ.",
        },
    ],
    # Финаудит Excel — file_extract читает XLSX
    34: [
        {
            "name": "excel_file",
            "label": "Excel-файл с финансовыми данными",
            "type": "file",
            "kind": "sheet",
            "accept": ".xlsx,.xls,.csv",
            "required": True,
            "hint": "Загрузите XLSX/XLS/CSV. До 25 МБ.",
        },
    ],
    # Аудит соцсети канала — vision_describe анализирует скриншоты
    33: [
        {
            "name": "screenshot_1",
            "label": "Скриншот профиля / ленты (картинка 1)",
            "type": "file",
            "kind": "image",
            "accept": ".png,.jpg,.jpeg,.webp",
            "required": True,
            "hint": "Главная страница профиля или последние посты.",
        },
        {
            "name": "screenshot_2",
            "label": "Второй скриншот (опц.)",
            "type": "file",
            "kind": "image",
            "accept": ".png,.jpg,.jpeg,.webp",
            "required": False,
            "hint": "Карточка поста, статистика, оформление — любое полезное.",
        },
    ],
    # Аудит лендинга — parallel_browse + vision_describe.
    # URL'ы вводятся как textarea (по 1 ссылке на строку), скриншоты опц.
    31: [
        {
            "name": "landing_url",
            "label": "URL лендинга для аудита",
            "type": "text",
            "required": True,
            "placeholder": "https://example.com",
            "hint": "Главная страница вашего лендинга",
        },
        {
            "name": "competitor_urls",
            "label": "URL'ы 1-3 конкурентов (опц., по одному на строку)",
            "type": "textarea",
            "rows": 3,
            "required": False,
            "hint": "Сравним ваш лендинг с конкурентами",
        },
    ],
    # Холодная email-рассылка — parallel_browse по списку URL компаний
    35: [
        {
            "name": "companies_urls",
            "label": "URL'ы компаний-получателей (по одному на строку, до 10)",
            "type": "textarea",
            "rows": 6,
            "required": True,
            "hint": "Сайты компаний которым вы хотите написать. AI изучит их и подберёт персональный pitch.",
        },
    ],
}


def merge_fields(existing: list, new_fields: list) -> tuple[list, int]:
    """Добавляет new_fields в начало existing, пропуская уже существующие по name.
    Возвращает (merged_list, count_added)."""
    existing_names = {f.get("name") for f in existing if isinstance(f, dict)}
    to_add = [f for f in new_fields if f.get("name") not in existing_names]
    return to_add + existing, len(to_add)


def main():
    dry = "--dry-run" in sys.argv
    print(f"{'[DRY-RUN] ' if dry else ''}Updating input_schema for {len(FIELD_UPDATES)} solutions...\n")

    with db_session() as db:
        for sol_id, new_fields in FIELD_UPDATES.items():
            s = db.query(Solution).filter_by(id=sol_id).first()
            if not s:
                print(f"  #{sol_id}: NOT FOUND, skip")
                continue
            try:
                existing = json.loads(s.input_schema_json or "[]")
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []
            merged, added = merge_fields(existing, new_fields)
            if added == 0:
                print(f"  #{sol_id} {s.title!r}: all fields already present, skip")
                continue
            print(f"  #{sol_id} {s.title!r}: +{added} field(s) added")
            for f in new_fields[:added]:
                print(f"     - {f['name']}: type={f['type']}, required={f.get('required', False)}")
            if not dry:
                s.input_schema_json = json.dumps(merged, ensure_ascii=False)
        if not dry:
            db.commit()
            print("\n✅ Committed.")
        else:
            print("\n[DRY-RUN] Changes NOT committed. Re-run without --dry-run to apply.")


if __name__ == "__main__":
    main()
