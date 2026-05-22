"""Тесты PATCH /creators/items/{id}/reschedule.

Покрывают:
  - Успешный перенос на будущую дату
  - 404 если item чужого юзера
  - 400 на published-item (история неизменна)
  - 400 на дату в прошлом
  - 400 на невалидный формат datetime
"""
import json
import time
from datetime import datetime, timedelta

import pytest


def _make_brand_with_item(status: str = "planned",
                          schedule_offset_hours: int = 24):
    """Создать User + Brand + Calendar + Item для теста.
    Возвращает (user_id, brand_id, calendar_id, item_id)."""
    from server.db import db_session
    from server.models import (User, CreatorBrand, ContentCalendar, ContentItem)
    with db_session() as db:
        u = User(
            email=f"reschedule-{time.time_ns()}@x.x",
            password_hash="h", is_verified=True,
            agreed_to_terms=True, tokens_balance=10000,
        )
        db.add(u); db.commit(); db.refresh(u)
        b = CreatorBrand(user_id=u.id, name="Test", niche="test", tone="business")
        db.add(b); db.commit(); db.refresh(b)
        cal = ContentCalendar(brand_id=b.id, period_start=datetime.utcnow().date(),
                               period_end=(datetime.utcnow() + timedelta(days=30)).date(),
                               status="active")
        db.add(cal); db.commit(); db.refresh(cal)
        item = ContentItem(
            calendar_id=cal.id,
            platform="tg",
            type="text",
            schedule_at=datetime.utcnow() + timedelta(hours=schedule_offset_hours),
            brief="test brief",
            status=status,
        )
        db.add(item); db.commit(); db.refresh(item)
        return u.id, b.id, cal.id, item.id


def _cleanup(user_id: int):
    from server.db import db_session
    from server.models import (User, CreatorBrand, ContentCalendar, ContentItem)
    with db_session() as db:
        for b in db.query(CreatorBrand).filter_by(user_id=user_id).all():
            for cal in db.query(ContentCalendar).filter_by(brand_id=b.id).all():
                db.query(ContentItem).filter_by(calendar_id=cal.id).delete()
                db.delete(cal)
            db.delete(b)
        db.query(User).filter_by(id=user_id).delete()
        db.commit()


class TestRescheduleItem:

    def test_successful_reschedule(self):
        """Перенос planned-item на будущую дату — schedule_at обновляется."""
        from server.db import db_session
        from server.models import User, ContentItem
        from server.routes.creators import reschedule_item, RescheduleIn

        uid, bid, cid, iid = _make_brand_with_item()
        try:
            new_dt = datetime.utcnow() + timedelta(days=7)
            payload = RescheduleIn(schedule_at=new_dt.isoformat() + "Z")
            with db_session() as db:
                u = db.query(User).filter_by(id=uid).first()
                result = reschedule_item(iid, payload, user=u, db=db)
            assert result["ok"] is True

            # Проверим в БД что schedule_at реально изменился
            with db_session() as db:
                item = db.query(ContentItem).filter_by(id=iid).first()
                # Округление до секунды — формат iso → datetime
                assert abs((item.schedule_at - new_dt).total_seconds()) < 2
        finally:
            _cleanup(uid)

    def test_published_blocks_reschedule(self):
        """published-item нельзя перенести."""
        from server.db import db_session
        from server.models import User
        from server.routes.creators import reschedule_item, RescheduleIn
        from fastapi import HTTPException

        uid, bid, cid, iid = _make_brand_with_item(status="published")
        try:
            new_dt = datetime.utcnow() + timedelta(days=7)
            payload = RescheduleIn(schedule_at=new_dt.isoformat() + "Z")
            with db_session() as db:
                u = db.query(User).filter_by(id=uid).first()
                with pytest.raises(HTTPException) as exc:
                    reschedule_item(iid, payload, user=u, db=db)
                assert exc.value.status_code == 400
                assert "Опубликованный" in exc.value.detail
        finally:
            _cleanup(uid)

    def test_past_date_rejected(self):
        """Дата в прошлом > 10 мин назад — 400."""
        from server.db import db_session
        from server.models import User
        from server.routes.creators import reschedule_item, RescheduleIn
        from fastapi import HTTPException

        uid, bid, cid, iid = _make_brand_with_item()
        try:
            past_dt = datetime.utcnow() - timedelta(days=1)
            payload = RescheduleIn(schedule_at=past_dt.isoformat() + "Z")
            with db_session() as db:
                u = db.query(User).filter_by(id=uid).first()
                with pytest.raises(HTTPException) as exc:
                    reschedule_item(iid, payload, user=u, db=db)
                assert exc.value.status_code == 400
                assert "прошлое" in exc.value.detail.lower()
        finally:
            _cleanup(uid)

    def test_invalid_datetime_format_rejected(self):
        """Невалидная дата — 400."""
        from server.db import db_session
        from server.models import User
        from server.routes.creators import reschedule_item, RescheduleIn
        from fastapi import HTTPException

        uid, bid, cid, iid = _make_brand_with_item()
        try:
            payload = RescheduleIn(schedule_at="not-a-date")
            with db_session() as db:
                u = db.query(User).filter_by(id=uid).first()
                with pytest.raises(HTTPException) as exc:
                    reschedule_item(iid, payload, user=u, db=db)
                assert exc.value.status_code == 400
        finally:
            _cleanup(uid)

    def test_foreign_user_404(self):
        """Юзер не может переносить чужие посты."""
        from server.db import db_session
        from server.models import User
        from server.routes.creators import reschedule_item, RescheduleIn
        from fastapi import HTTPException

        # Юзер A — владелец item
        uid_a, _, _, iid = _make_brand_with_item()
        # Юзер B — не имеет доступа
        with db_session() as db:
            u_b = User(
                email=f"foreign-{time.time_ns()}@x.x",
                password_hash="h", is_verified=True,
                agreed_to_terms=True, tokens_balance=0,
            )
            db.add(u_b); db.commit(); db.refresh(u_b)
            uid_b = u_b.id
        try:
            payload = RescheduleIn(schedule_at=(datetime.utcnow() + timedelta(days=1)).isoformat() + "Z")
            with db_session() as db:
                u_b = db.query(User).filter_by(id=uid_b).first()
                with pytest.raises(HTTPException) as exc:
                    reschedule_item(iid, payload, user=u_b, db=db)
                assert exc.value.status_code == 404
        finally:
            _cleanup(uid_a)
            with db_session() as db:
                db.query(User).filter_by(id=uid_b).delete()
                db.commit()
