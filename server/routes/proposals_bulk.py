"""Bulk-генерация КП из CSV + auto-fill из IMAP-письма — для агентств.

Импорт CSV → создание ProposalProject(status='draft') по каждой строке.
Auto-fill письма → LLM парсит поля и возвращает structured dict (не создаёт
проект, только подсказывает значения для формы создания КП).
Генерация запускается отдельно через POST /proposals/projects/{id}/generate.
"""
import csv as _csv
import io as _io
import json as _json
import logging

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server.routes.deps import current_user, get_db
from server.models import (ProposalBrand, ProposalPriceList, ProposalProject,
                            Transaction, User)
from server.audit_log import log_action

log = logging.getLogger(__name__)

router = APIRouter(prefix="/proposals", tags=["proposals"])


@router.post("/bulk-from-csv")
async def proposal_bulk_from_csv(file: UploadFile = File(...),
                                  brand_id: int | None = None,
                                  price_list_id: int | None = None,
                                  extra_notes: str = "",
                                  db: Session = Depends(get_db),
                                  user: User = Depends(current_user)):
    """Bulk-создание КП-драфтов из CSV/TSV.

    Формат CSV (заголовки в первой строке, обязательна `name` или `client_name`):
        name (или client_name), client_email, client_request,
        client_site_url, extra_notes

    Создаёт ProposalProject(status='draft') для каждой строки. Генерация —
    отдельно через POST /proposals/projects/{id}/generate.
    Limit: 100 строк за один CSV, файл ≤5 МБ.
    """
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")

    # Валидация brand/price_list — принадлежат юзеру
    if brand_id is not None:
        b = db.query(ProposalBrand).filter_by(id=brand_id, user_id=user.id).first()
        if not b:
            raise HTTPException(404, "Бренд не найден")
    if price_list_id is not None:
        pl = (db.query(ProposalPriceList)
                .filter_by(id=price_list_id, user_id=user.id).first())
        if not pl:
            raise HTTPException(404, "Прайс не найден")

    fname = (file.filename or "").lower()
    if not fname.endswith((".csv", ".tsv", ".txt")):
        raise HTTPException(400, "Нужен CSV/TSV файл")
    raw = await file.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(413, "Файл больше 5 МБ")
    if not raw:
        raise HTTPException(400, "Пустой файл")

    # Декодируем (UTF-8 с BOM или CP1251)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp1251")
        except UnicodeDecodeError:
            raise HTTPException(400, "Кодировка должна быть UTF-8 или CP1251")

    # Auto-detect delimiter
    sample = text[:2048]
    delim = "\t" if "\t" in sample and sample.count("\t") > sample.count(",") else ","
    reader = _csv.DictReader(_io.StringIO(text), delimiter=delim)
    if not reader.fieldnames:
        raise HTTPException(400, "CSV не содержит заголовков")

    # Нормализация имён колонок (lower, strip)
    field_map = {(f or "").strip().lower(): f
                 for f in reader.fieldnames if f}
    col_name = (field_map.get("name") or field_map.get("client_name")
                or field_map.get("клиент"))
    if not col_name:
        raise HTTPException(400,
            "Нужна колонка `name` (или `client_name` / `клиент`) с именем клиента")
    col_email = field_map.get("client_email") or field_map.get("email")
    col_request = (field_map.get("client_request") or field_map.get("request")
                   or field_map.get("запрос"))
    col_site = (field_map.get("client_site_url") or field_map.get("site")
                or field_map.get("сайт"))
    col_notes = (field_map.get("extra_notes") or field_map.get("notes")
                 or field_map.get("примечания"))

    MAX_ROWS = 100
    rows = list(reader)
    if len(rows) > MAX_ROWS:
        raise HTTPException(400,
            f"CSV содержит {len(rows)} строк, максимум {MAX_ROWS} за раз. "
            f"Разбейте на несколько файлов.")

    created_ids: list[int] = []
    skipped = 0
    errors: list[str] = []
    for i, row in enumerate(rows, start=2):  # +1 за header
        name = (row.get(col_name) or "").strip()[:200]
        if not name:
            skipped += 1
            continue
        try:
            p = ProposalProject(
                user_id=user.id,
                name=name,
                status="draft",
                brand_id=brand_id,
                price_list_id=price_list_id,
                client_name=name,
                client_email=((row.get(col_email) or "").strip()[:200]
                              if col_email else None) or None,
                client_request=((row.get(col_request) or "").strip()[:5000]
                                if col_request else None) or None,
                client_site_url=((row.get(col_site) or "").strip()[:500]
                                 if col_site else None) or None,
                extra_notes=(
                    ((row.get(col_notes) or "").strip()[:2000]
                     if col_notes else "")
                    or extra_notes
                    or None
                ),
            )
            db.add(p)
            db.flush()
            created_ids.append(p.id)
        except Exception as e:
            errors.append(f"row {i}: {type(e).__name__}: {str(e)[:80]}")
            continue
    db.commit()

    log_action("proposal.bulk_csv_import", user_id=user.id,
               target_type="proposal", target_id="bulk",
               details={"created": len(created_ids), "skipped": skipped,
                        "rows": len(rows),
                        "filename": (file.filename or "")[:80]})

    return {
        "status": "ok",
        "created_count": len(created_ids),
        "created_ids": created_ids,
        "skipped": skipped,
        "total_rows": len(rows),
        "errors": errors[:10],
        "next_step": (
            "Драфты созданы. Открой /proposals.html — нажми «Сгенерировать» "
            "у каждого нужного. Каждая генерация стоит ~50 ₽."
        ),
    }


# ── Auto-fill КП из IMAP-письма ─────────────────────────────────────────────


class AutoFillFromEmailBody(BaseModel):
    raw_email: str   # сырое письмо (headers + body) или просто body


@router.post("/autofill-from-email")
def proposal_autofill_from_email(payload: AutoFillFromEmailBody,
                                  db: Session = Depends(get_db),
                                  user: User = Depends(current_user)):
    """LLM-парсинг входящего письма-заявки → structured dict для формы создания КП.

    Юзер копирует raw email от потенциального клиента (например из Outlook /
    Mail.app — "View Source" / "Show Raw"). Мы извлекаем:
      - client_name (из From: или подписи)
      - client_email
      - client_phone
      - client_site_url
      - client_request (краткая суть запроса, что нужно)
      - budget_hint (если упомянут бюджет)
      - urgency (если упомянут срок)

    Стоимость: 5 ₽ (фикс, без real_cost — это короткий запрос). Lazy
    для агентств с потоком заявок — экономит ~5 минут ручного копирования.

    Returns: {fields: {...}, raw_used_len: int}
    """
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")

    raw = (payload.raw_email or "").strip()
    if len(raw) < 30:
        raise HTTPException(400, "Письмо слишком короткое")
    if len(raw) > 50_000:
        raw = raw[:50_000]

    # Биллинг — фикс 5 ₽
    from server.billing import deduct_strict
    cost_kop = 500
    if not deduct_strict(db, user.id, cost_kop):
        raise HTTPException(402, f"Недостаточно средств (нужно {cost_kop/100:.0f} ₽)")
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost_kop,
                       description="КП: auto-fill из письма"))

    # LLM-запрос: structured extraction
    prompt = (
        "Ты — помощник менеджера агентства. Получаешь сырое email-сообщение "
        "от потенциального клиента. Извлеки из него поля для создания КП.\n\n"
        "ВОЗВРАЩАЙ ТОЛЬКО JSON БЕЗ КОММЕНТАРИЕВ И БЕЗ ОБЁРТКИ ```json```!\n\n"
        "Структура:\n"
        "{\n"
        '  "client_name": "Имя клиента или название компании (строка, до 200 символов)",\n'
        '  "client_email": "email если есть",\n'
        '  "client_phone": "телефон если есть",\n'
        '  "client_site_url": "сайт клиента если есть (URL)",\n'
        '  "client_request": "Краткое описание чего хочет клиент (1-3 предложения, до 500 символов)",\n'
        '  "extra_notes": "дополнительные важные детали — бюджет, срок, ограничения, особые требования (до 500 символов)"\n'
        "}\n\n"
        "Если какое-то поле не удалось определить — оставь его пустой строкой ''.\n"
        "Не выдумывай данные которых нет в письме. Не используй placeholder'ы типа 'не указано'.\n\n"
        f"=== ПИСЬМО ===\n{raw}\n=== /ПИСЬМО ===\n\n"
        "Верни JSON:"
    )

    try:
        from server.ai import generate_response
        resp = generate_response("claude",
                                  [{"role": "user", "content": prompt}],
                                  extra={"max_tokens": 1500, "temperature": 0.0,
                                         "_purpose": "proposal_autofill",
                                         "_user_id": user.id})
        content = (resp.get("content") if isinstance(resp, dict) else "") or ""
    except Exception as e:
        # Refund при сбое LLM
        try:
            from server.billing import credit_atomic
            credit_atomic(db, user.id, cost_kop)
            db.add(Transaction(user_id=user.id, type="refund", tokens_delta=cost_kop,
                               description="Refund: КП auto-fill — LLM упал"))
        except Exception:
            pass
        log.error(f"[proposal-autofill] LLM failed: {type(e).__name__}: {e}")
        raise HTTPException(503, "Не удалось разобрать письмо — попробуй ещё раз")

    # Парсим JSON (может прийти с обёрткой ```json — снимем её)
    cleaned = content.strip()
    if cleaned.startswith("```"):
        # ```json\n{...}\n```
        first_nl = cleaned.find("\n")
        if first_nl > 0:
            cleaned = cleaned[first_nl + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()
    try:
        parsed = _json.loads(cleaned)
    except Exception:
        # Попытаемся выдрать первый JSON-объект
        import re as _re
        m = _re.search(r"\{[\s\S]*\}", cleaned)
        if m:
            try:
                parsed = _json.loads(m.group(0))
            except Exception:
                parsed = {}
        else:
            parsed = {}

    if not isinstance(parsed, dict):
        parsed = {}

    # Sanitize + truncate
    def _s(v, n: int) -> str:
        if not isinstance(v, str):
            return ""
        return v.strip()[:n]

    fields = {
        "client_name": _s(parsed.get("client_name"), 200),
        "client_email": _s(parsed.get("client_email"), 200),
        "client_phone": _s(parsed.get("client_phone"), 50),
        "client_site_url": _s(parsed.get("client_site_url"), 500),
        "client_request": _s(parsed.get("client_request"), 5000),
        "extra_notes": _s(parsed.get("extra_notes"), 2000),
    }
    db.commit()

    try:
        log_action("proposal.autofill_from_email", user_id=user.id,
                   target_type="proposal", target_id="autofill",
                   details={"len": len(raw), "filled_fields": [k for k, v in fields.items() if v]})
    except Exception:
        pass

    return {"fields": fields, "raw_used_len": len(raw), "cost_kop": cost_kop}
