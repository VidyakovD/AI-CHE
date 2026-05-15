"""Backfill Knowledge Hub категорий для существующих KnowledgeFile.

Запускать один раз после деплоя миграции `category`. Идём по всем файлам
с category='other' (или NULL), классифицируем через Haiku, сохраняем.

Запуск на проде:
    cd /root/AI-CHE && /root/AI-CHE/venv/bin/python scripts/backfill_knowledge_categories.py

С ограничением:
    ... scripts/backfill_knowledge_categories.py --limit 50 --dry-run

Стоимость: ~$0.001 за файл (Haiku, ~200 input tokens). 100 файлов ≈ $0.10.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Нужно чтобы импортировался server.* из корня проекта
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.db import SessionLocal  # noqa: E402
from server.models import KnowledgeFile  # noqa: E402
from server.knowledge_classifier import classify, DEFAULT_CATEGORY  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="Сколько файлов обработать (0 = все)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Только показать классификацию, не писать в БД")
    ap.add_argument("--force", action="store_true",
                    help="Переклассифицировать ВСЕ, не только category='other'/NULL")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        q = db.query(KnowledgeFile)
        if not args.force:
            q = q.filter((KnowledgeFile.category == DEFAULT_CATEGORY)
                         | (KnowledgeFile.category.is_(None)))
        q = q.order_by(KnowledgeFile.created_at.desc())
        if args.limit > 0:
            q = q.limit(args.limit)
        files = q.all()
        print(f"Файлов к обработке: {len(files)}{' [DRY RUN]' if args.dry_run else ''}")

        stats = {"total": len(files), "changed": 0, "unchanged": 0, "errors": 0}
        cat_dist: dict[str, int] = {}

        for i, kf in enumerate(files, 1):
            preview = (kf.content_text or "")[:1500]
            try:
                cat = classify(kf.name or "", kf.mime, preview)
            except Exception as e:
                print(f"[{i}/{len(files)}] ✗ #{kf.id} {kf.name!r}: ERROR {e}")
                stats["errors"] += 1
                continue
            cat_dist[cat] = cat_dist.get(cat, 0) + 1
            old = kf.category or "NULL"
            if cat == old:
                stats["unchanged"] += 1
                marker = "·"
            else:
                stats["changed"] += 1
                marker = "→"
            print(f"[{i}/{len(files)}] {marker} #{kf.id} {kf.name!r}: {old} → {cat}")
            if not args.dry_run and cat != old:
                kf.category = cat
                # Коммитим батчами по 10 для надёжности
                if i % 10 == 0:
                    db.commit()
            # Лёгкий троттл чтобы не упереться в Haiku rate limit
            if i % 5 == 0:
                time.sleep(0.5)

        if not args.dry_run:
            db.commit()

        print()
        print("=" * 60)
        print(f"Готово: {stats['changed']} изменено · {stats['unchanged']} без изменений · {stats['errors']} ошибок")
        print("Распределение по категориям:")
        for cat, n in sorted(cat_dist.items(), key=lambda x: -x[1]):
            print(f"  {cat:12s} {n:4d}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
