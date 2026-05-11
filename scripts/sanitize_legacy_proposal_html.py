#!/usr/bin/env python3
"""
Sanitize legacy ProposalProject.generated_html через bleach.

Зачем: bleach-санитизация генерируемого HTML появилась в коммите `dc7eecf`.
До этого коммита могли быть КП с XSS-вектором в `generated_html` (через
prompt-injection или ручную правку). WYSIWYG-режим рендерит `generated_html`
в sandbox=`allow-same-origin allow-scripts` → embedded <script> может
дёрнуть parent.fetch и слить cookie. Этот скрипт идёт по всем
ProposalProject в БД, прогоняет через тот же bleach.clean что используется
в `server/proposal_builder.py:_strip_ai_wrappers`, и сохраняет результат.

Идемпотентно: повторный запуск ничего не сломает (sanitized HTML на входе
проходит через bleach без изменений). Сохраняет original в action_log
для аудита.

Использование:
    python scripts/sanitize_legacy_proposal_html.py --dry-run    # показать что изменится
    python scripts/sanitize_legacy_proposal_html.py              # применить
    python scripts/sanitize_legacy_proposal_html.py --since 2026-03-01   # только до этой даты

Безопасно для прода: транзакции, batch'ами по 100 записей, rollback при ошибке.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

# Гарантируем что server/ импортируется
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Показать какие записи изменятся, без записи в БД")
    parser.add_argument("--since", type=str, default=None,
                        help="Только записи СОЗДАННЫЕ до этой даты (ISO: 2026-03-01). "
                             "По умолчанию — все.")
    parser.add_argument("--batch", type=int, default=100,
                        help="Размер batch для commit'а (default 100)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    cutoff_date: datetime | None = None
    if args.since:
        try:
            cutoff_date = datetime.fromisoformat(args.since)
        except ValueError:
            print(f"❌ Невалидный формат --since: {args.since}. Используйте 2026-03-01")
            return 2

    from server.db import db_session
    from server.models import ProposalProject
    from server.proposal_builder import _strip_ai_wrappers
    from server.audit_log import log_action

    # Проверим что таблица существует (защита от запуска на свежей dev-БД)
    from sqlalchemy import inspect, exc as sa_exc
    from server.db import engine
    try:
        if "proposal_projects" not in inspect(engine).get_table_names():
            print("❌ Таблица proposal_projects не существует в БД. "
                  "Запустите сервер один раз чтобы Base.metadata.create_all() создал схему.")
            return 3
    except sa_exc.SQLAlchemyError as e:
        print(f"❌ Не удалось прочитать схему БД: {type(e).__name__}: {e}")
        return 3

    print(f"🔍 Сканирую ProposalProject (cutoff={cutoff_date or 'all'})…")

    total = 0
    changed = 0
    unchanged = 0
    errored = 0

    with db_session() as db:
        query = db.query(ProposalProject).filter(ProposalProject.generated_html.isnot(None))
        if cutoff_date is not None:
            query = query.filter(ProposalProject.created_at < cutoff_date)

        batch: list[tuple[ProposalProject, str, str]] = []  # (project, original, sanitized)
        for proj in query.yield_per(200):
            total += 1
            original = proj.generated_html or ""
            if not original.strip():
                unchanged += 1
                continue
            try:
                sanitized = _strip_ai_wrappers(original)
            except Exception as e:
                errored += 1
                print(f"  ⚠️  proposal id={proj.id}: bleach error — {type(e).__name__}: {e}")
                continue

            if sanitized == original:
                unchanged += 1
                continue

            # Поменялось
            changed += 1
            diff_bytes = len(original) - len(sanitized)
            if args.verbose:
                print(f"  📝 id={proj.id} ({proj.client_name or 'без имени'}): "
                      f"размер {len(original)} → {len(sanitized)} (Δ {diff_bytes:+d})")

            batch.append((proj, original, sanitized))

            if len(batch) >= args.batch and not args.dry_run:
                _commit_batch(db, batch, log_action)
                batch = []

        if batch and not args.dry_run:
            _commit_batch(db, batch, log_action)

    print()
    print(f"📊 Готово:")
    print(f"   Всего проектов:    {total}")
    print(f"   Изменено:          {changed}{' (dry-run, не записано)' if args.dry_run else ''}")
    print(f"   Без изменений:     {unchanged}")
    print(f"   Ошибок:            {errored}")
    if args.dry_run:
        print("\n   Запустите без --dry-run чтобы применить изменения.")
    return 0


def _commit_batch(db, batch, log_action):
    """Применить batch + записать в audit_log."""
    for proj, original, sanitized in batch:
        proj.generated_html = sanitized
        log_action(
            "proposal.html_sanitized",
            user_id=proj.user_id,
            target_type="proposal_project",
            target_id=str(proj.id),
            level="info",
            success=True,
            details={
                "original_bytes": len(original),
                "sanitized_bytes": len(sanitized),
                "diff": len(original) - len(sanitized),
            },
        )
    try:
        db.commit()
        print(f"  ✅ Committed batch of {len(batch)}")
    except Exception as e:
        db.rollback()
        print(f"  ❌ Batch commit failed: {type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    sys.exit(main())
