"""
Marketplace ботов — публикация и установка шаблонов.

Flow:
  1. Юзер у которого есть готовый ChatBot жмёт «📤 Опубликовать в Marketplace»
     → POST /marketplace/listings { bot_id, name, description, category, price_kop }
     Создаётся BotMarketplaceListing со снапшотом system_prompt + workflow_json.
  2. Юзер заходит в /marketplace → видит каталог одобренных листингов.
  3. Жмёт «📥 Установить» → POST /marketplace/listings/{id}/install
     - Если price_kop > 0: списываем с баланса; 70% credit_atomic автору,
       30% остаётся платформе.
     - Создаётся новый ChatBot у установившего (status=off, юзеру нужно
       подключить токен TG/VK — workflow и system_prompt уже на месте).
  4. После использования юзер может оставить рейтинг 1-5 + текстовый отзыв.

Модерация: BotMarketplaceListing.is_approved=False по умолчанию. Админ
видит pending в /admin/marketplace и ставит approve. Только approved и
is_active=True попадают в публичный каталог.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import desc, update, func
from sqlalchemy.exc import IntegrityError

from server.routes.deps import get_db, current_user, optional_user
from server.models import (
    User, ChatBot, Transaction,
    BotMarketplaceListing, BotMarketplaceInstall,
)
from server.billing import deduct_strict, credit_atomic

log = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplace", tags=["marketplace"])


_AUTHOR_REVENUE_PCT = 70   # 70% автору, 30% платформе


# ── Feature-flag: marketplace отключён по продуктовому решению (2026-05-10) ──
# Write-эндпоинты (publish / install / review / approve) возвращают 410 Gone.
# Read-эндпоинты (GET listings, my-listings, pending) остаются — юзер может
# посмотреть свою историю и архив. Установленные ранее боты продолжают
# работать как обычные ChatBot — они уже cloned в свою таблицу.
#
# Чтобы временно вернуть marketplace — установить env MARKETPLACE_ENABLED=1
# и перезапустить сервис.
def _require_marketplace_enabled():
    import os as _os
    if _os.getenv("MARKETPLACE_ENABLED", "").lower() not in ("1", "true", "yes"):
        raise HTTPException(
            410,
            "Marketplace временно отключён. Установленные ранее боты продолжают "
            "работать в разделе «Чат-боты»."
        )


class ListingPublishBody(BaseModel):
    bot_id: int
    name: str = Field(..., max_length=100)
    description: str | None = Field(None, max_length=2000)
    category: str | None = Field(None, max_length=60)
    price_kop: int = Field(0, ge=0, le=1_000_000)  # макс 10 000 ₽
    cover_image_url: str | None = None


@router.post("/listings")
def publish_listing(body: ListingPublishBody, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    """Опубликовать своего бота в marketplace."""
    _require_marketplace_enabled()
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    bot = db.query(ChatBot).filter_by(id=body.bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(404, "Бот не найден или не ваш")
    if not (bot.system_prompt or bot.workflow_json):
        raise HTTPException(400, "Бот должен иметь system_prompt или workflow для публикации")
    if not body.name or len(body.name.strip()) < 3:
        raise HTTPException(400, "Минимум 3 символа в названии")
    # Лимит 10 листингов на юзера
    cnt = db.query(BotMarketplaceListing).filter_by(author_id=user.id).count()
    if cnt >= 10:
        raise HTTPException(409, "Лимит 10 публикаций. Удалите старые.")

    # Валидация cover_image_url: только http/https или относительный путь
    # (защита от javascript:/data:text/html → XSS, если кто-то начнёт
    # рендерить как <a href> или background-image).
    cover = (body.cover_image_url or "").strip()[:500] or None
    if cover:
        low = cover.lower()
        if not (low.startswith("http://") or low.startswith("https://")
                 or low.startswith("/")
                 or (low.startswith("data:image/") and ";base64," in low[:50])):
            raise HTTPException(400,
                "cover_image_url: только http://, https://, относительная или data:image/")

    listing = BotMarketplaceListing(
        author_id=user.id, source_bot_id=bot.id,
        name=body.name.strip()[:100],
        description=(body.description or "").strip()[:2000] or None,
        category=(body.category or "").strip()[:60] or None,
        price_kop=int(body.price_kop or 0),
        system_prompt=bot.system_prompt,
        workflow_json=bot.workflow_json,
        cover_image_url=cover,
        is_approved=False,  # ждёт модерации
    )
    db.add(listing); db.commit(); db.refresh(listing)
    try:
        from server.audit_log import log_action
        log_action("marketplace.published", user_id=user.id,
                   target_type="listing", target_id=str(listing.id),
                   details={"price_kop": listing.price_kop, "bot_id": bot.id})
    except Exception:
        pass
    return {"listing_id": listing.id, "status": "pending_moderation"}


@router.get("/listings")
def list_listings(category: str | None = None,
                    free_only: bool = False,
                    db: Session = Depends(get_db),
                    user=Depends(optional_user)):
    """Публичный каталог одобренных листингов. Без auth."""
    q = (db.query(BotMarketplaceListing)
           .filter_by(is_approved=True, is_active=True))
    if category:
        q = q.filter(BotMarketplaceListing.category == category[:60])
    if free_only:
        q = q.filter(BotMarketplaceListing.price_kop == 0)
    rows = q.order_by(desc(BotMarketplaceListing.installs_count),
                      desc(BotMarketplaceListing.id)).limit(200).all()
    return [{
        "id": r.id, "name": r.name, "description": r.description,
        "category": r.category, "price_kop": r.price_kop,
        "cover_image_url": r.cover_image_url,
        "installs_count": r.installs_count or 0,
        "rating": (r.rating_sum / r.rating_count) if r.rating_count else None,
        "rating_count": r.rating_count or 0,
        "is_mine": bool(user and r.author_id == user.id),
    } for r in rows]


@router.get("/my-listings")
def list_my_listings(db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    rows = (db.query(BotMarketplaceListing)
              .filter_by(author_id=user.id)
              .order_by(desc(BotMarketplaceListing.id)).all())
    return [{
        "id": r.id, "name": r.name, "category": r.category,
        "price_kop": r.price_kop, "is_approved": r.is_approved,
        "is_active": r.is_active,
        "installs_count": r.installs_count or 0,
        "rating": (r.rating_sum / r.rating_count) if r.rating_count else None,
        "rating_count": r.rating_count or 0,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


@router.delete("/listings/{listing_id}")
def delete_listing(listing_id: int, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    """Снять с публикации (свой листинг)."""
    r = (db.query(BotMarketplaceListing)
           .filter_by(id=listing_id, author_id=user.id).first())
    if not r:
        raise HTTPException(404, "Листинг не найден")
    r.is_active = False
    db.commit()
    return {"status": "delisted"}


class InstallReviewBody(BaseModel):
    rating: int | None = Field(None, ge=1, le=5)
    review: str | None = Field(None, max_length=2000)


@router.post("/listings/{listing_id}/install")
def install_listing(listing_id: int, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    """Установить шаблон — создать новый ChatBot у юзера. Платный режим:
    списываем price_kop, 70% credit_atomic автору."""
    _require_marketplace_enabled()
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    listing = (db.query(BotMarketplaceListing)
                 .filter_by(id=listing_id, is_approved=True,
                             is_active=True).first())
    if not listing:
        raise HTTPException(404, "Листинг не найден или не одобрен")
    if listing.author_id == user.id:
        raise HTTPException(400, "Нельзя установить свой собственный бот")

    paid = 0
    # Anti-pump для платных: захватываем slot через UNIQUE INSERT ДО списания
    # денег. Если две параллельные установки одного юзера — выживет один,
    # второй получит IntegrityError → 409. Защита от race на multi-worker
    # (раньше был SELECT → INSERT, между ними ничего не блокировало).
    install_rec: BotMarketplaceInstall | None = None
    if listing.price_kop and listing.price_kop > 0:
        try:
            install_rec = BotMarketplaceInstall(
                listing_id=listing.id, installer_id=user.id,
                installed_bot_id=None, paid_kop=listing.price_kop,
            )
            db.add(install_rec); db.flush()  # триггерит UNIQUE
        except IntegrityError:
            db.rollback()
            raise HTTPException(409,
                "Этот платный шаблон уже установлен. Найдите бота в /chatbots.html.")

        # Списание + revenue split
        if not deduct_strict(db, user.id, listing.price_kop):
            db.rollback()  # откатываем placeholder install_rec
            raise HTTPException(402, f"Недостаточно средств. Цена: {listing.price_kop/100:.0f} ₽")
        paid = listing.price_kop
        author_share = paid * _AUTHOR_REVENUE_PCT // 100
        if author_share > 0:
            credit_atomic(db, listing.author_id, author_share)
            db.add(Transaction(
                user_id=listing.author_id, type="bonus",
                tokens_delta=author_share,
                description=f"Marketplace: установка «{listing.name[:60]}»",
            ))
        db.add(Transaction(
            user_id=user.id, type="usage",
            tokens_delta=-paid,
            description=f"Marketplace: «{listing.name[:60]}»",
        ))

    # Создаём бота у установившего (статус off — юзер сам подключит токены)
    new_bot = ChatBot(
        user_id=user.id,
        name=f"{listing.name[:80]} (из marketplace)",
        system_prompt=listing.system_prompt,
        workflow_json=listing.workflow_json,
        status="off",
    )
    db.add(new_bot); db.flush()
    # Atomic UPDATE — без race на read-then-write при concurrent installs
    db.execute(
        update(BotMarketplaceListing)
        .where(BotMarketplaceListing.id == listing.id)
        .values(installs_count=BotMarketplaceListing.installs_count + 1)
    )
    if install_rec is not None:
        # Платный flow: дописываем installed_bot_id в уже захваченный slot
        install_rec.installed_bot_id = new_bot.id
    else:
        # Бесплатный flow: создаём новую install-запись (повторы разрешены)
        install_rec = BotMarketplaceInstall(
            listing_id=listing.id, installer_id=user.id,
            installed_bot_id=new_bot.id, paid_kop=0,
        )
        db.add(install_rec)
    db.commit(); db.refresh(new_bot)
    try:
        from server.audit_log import log_action
        log_action("marketplace.installed", user_id=user.id,
                   target_type="listing", target_id=str(listing.id),
                   details={"new_bot_id": new_bot.id, "paid_kop": paid})
    except Exception:
        pass
    return {"bot_id": new_bot.id, "listing_id": listing.id, "paid_kop": paid}


@router.post("/listings/{listing_id}/review")
def review_listing(listing_id: int, body: InstallReviewBody,
                     db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    """Оставить рейтинг 1-5 + опц. отзыв. Можно ставить только если
    устанавливал этот листинг.

    Race-safe: rating_sum и rating_count обновляются через atomic UPDATE
    с дельтой. Раньше read-modify-write `listing.rating_sum -= prev; +=
    body.rating` могло потерять одно из двух одновременных review
    (LAST WRITE WINS) — для одного юзера с несколькими installs одного
    бесплатного листинга это даёт корраптный count.
    """
    _require_marketplace_enabled()
    install = (db.query(BotMarketplaceInstall)
                  .filter_by(listing_id=listing_id, installer_id=user.id)
                  .order_by(desc(BotMarketplaceInstall.id)).first())
    if not install:
        raise HTTPException(403, "Можно оценить только установленный бот")
    listing = db.query(BotMarketplaceListing).filter_by(id=listing_id).first()
    if not listing:
        raise HTTPException(404, "Листинг не найден")

    if body.rating is not None:
        prev = install.rating
        delta_sum = int(body.rating) - int(prev or 0)
        delta_count = 0 if prev is not None else 1
        db.execute(
            update(BotMarketplaceListing)
            .where(BotMarketplaceListing.id == listing_id)
            .values(
                rating_sum=func.coalesce(BotMarketplaceListing.rating_sum, 0) + delta_sum,
                rating_count=func.coalesce(BotMarketplaceListing.rating_count, 0) + delta_count,
            )
        )
        install.rating = body.rating
    if body.review is not None:
        install.review = (body.review or "")[:2000]
    db.commit()
    return {"status": "ok"}


# ── Admin endpoints ────────────────────────────────────────────────────────


@router.get("/admin/pending")
def admin_pending_listings(db: Session = Depends(get_db),
                            user: User = Depends(current_user)):
    """Список листингов на модерации (для админа)."""
    from server.security import require_admin
    require_admin(user)
    rows = (db.query(BotMarketplaceListing)
              .filter_by(is_approved=False, is_active=True)
              .order_by(desc(BotMarketplaceListing.id)).all())
    return [{
        "id": r.id, "name": r.name, "description": r.description,
        "category": r.category, "price_kop": r.price_kop,
        "author_id": r.author_id,
        "system_prompt_preview": (r.system_prompt or "")[:300],
        "has_workflow": bool(r.workflow_json),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


@router.post("/admin/listings/{listing_id}/approve")
def admin_approve(listing_id: int, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    _require_marketplace_enabled()
    from server.security import require_admin
    require_admin(user)
    r = db.query(BotMarketplaceListing).filter_by(id=listing_id).first()
    if not r:
        raise HTTPException(404, "Не найдено")
    r.is_approved = True
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("marketplace.approved", user_id=user.id,
                   target_type="listing", target_id=str(listing_id))
    except Exception:
        pass
    return {"status": "approved"}


@router.post("/admin/listings/{listing_id}/reject")
def admin_reject(listing_id: int, db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    _require_marketplace_enabled()
    from server.security import require_admin
    require_admin(user)
    r = db.query(BotMarketplaceListing).filter_by(id=listing_id).first()
    if not r:
        raise HTTPException(404, "Не найдено")
    r.is_active = False
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("marketplace.rejected", user_id=user.id,
                   target_type="listing", target_id=str(listing_id))
    except Exception:
        pass
    return {"status": "rejected"}
