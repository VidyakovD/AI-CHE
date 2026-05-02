import asyncio
import secrets as _secrets
from fastapi import APIRouter, Depends, HTTPException, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
import json, uuid, logging

from server.routes.deps import get_db, optional_user, current_user
from server.models import Solution, SolutionCategory, SolutionStep, SolutionRun, User, Message, Transaction
from server.ai import generate_response, get_token_cost, resolve_model
from server.billing import deduct_strict, get_balance

log = logging.getLogger(__name__)

router = APIRouter(tags=["solutions"])


# ─── helpers ───────────────────────────────────────────────────────────────────

def _sol_dict(s: Solution) -> dict:
    return {"id": s.id, "title": s.title, "description": s.description,
            "image_url": s.image_url, "price_tokens": s.price_tokens,
            "category_id": s.category_id,
            "steps_count": len(s.steps) if s.steps else 0,
            "is_orchestra": bool(s.orchestra_json)}


def _step_dict(s: SolutionStep) -> dict:
    return {"id": s.id, "step_number": s.step_number, "title": s.title,
            "model": s.model, "system_prompt": s.system_prompt,
            "user_prompt": s.user_prompt, "wait_for_user": s.wait_for_user,
            "user_hint": s.user_hint,
            "extra_params": json.loads(s.extra_params) if s.extra_params else None}


def _execute_step(run: SolutionRun, step: SolutionStep, user_input,
                  db: Session, user) -> dict:
    ctx = json.loads(run.context or "{}")

    # Подставляем переменные в промпт
    prompt = step.user_prompt or ""
    prompt = prompt.replace("{input}", user_input or "")
    prompt = prompt.replace("{prev_result}", ctx.get("prev_result", ""))
    for k, v in ctx.items():
        prompt = prompt.replace(f"{{{k}}}", str(v))

    # Бизнес-решения — расширяем промпт для длинного отчёта в Markdown
    solution = db.query(Solution).filter_by(id=run.solution_id).first()
    is_business = bool(solution and solution.category and solution.category.slug == "business")
    if is_business:
        prompt += (
            "\n\n=== ФОРМАТ ОТВЕТА ===\n"
            "Дай развёрнутый структурированный экспертный отчёт в Markdown:\n"
            "- Заголовок документа (#)\n"
            "- 5-10 содержательных разделов (## H2)\n"
            "- Подразделы (### H3) где уместно\n"
            "- Маркированные/нумерованные списки\n"
            "- Таблицы для сравнений (Markdown table)\n"
            "- **Жирное** для ключевых тезисов, *курсив* для пометок\n"
            "- Каждый раздел — минимум 2-3 абзаца с конкретикой и примерами\n"
            "- Никаких отговорок «нужно уточнить» — давай готовое решение\n"
            "- В конце: «### 🎯 Ключевые выводы» (5-7 буллетов) и "
            "«### 📋 Что делать дальше» (пошаговый план)\n"
            "- Тон: профессиональный, по делу, без воды.\n"
            "- Объём: 3000-6000 слов (плотный, но не водянистый).\n"
        )

    messages = []
    if step.system_prompt:
        messages.append({"role": "system", "content": step.system_prompt})
    messages.append({"role": "user", "content": prompt})

    extra = json.loads(step.extra_params) if step.extra_params else {}
    extra = extra or {}
    # Бизнес-решения требуют большого max_tokens (до 16K) для полного отчёта
    if is_business:
        extra.setdefault("max_tokens", 16000)

    try:
        answer = generate_response(step.model, messages, extra)
    except Exception as e:
        run.status = "error"; db.commit()
        return {"status": "error", "error": str(e)}

    content = answer.get("content", "") if isinstance(answer, dict) else str(answer)
    resp_type = answer.get("type", "text") if isinstance(answer, dict) else "text"

    # Списываем токены за шаг — до сохранения, чтобы при ошибке запрос не прошёл
    if user:
        cost = get_token_cost(resolve_model(step.model)["real_model"] if resolve_model(step.model) else step.model)
        if not deduct_strict(db, user.id, cost):
            run.status = "error"; db.commit()
            return {"status": "error", "error": "Недостаточно токенов для выполнения шага"}
        db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost,
                           description=f"Решение: {step.title or step.step_number}", model=step.model))

    # Сохраняем в чат
    if user_input:
        db.add(Message(chat_id=run.chat_id, role="user", content=user_input,
                       model=step.model, user_id=user.id if user else None))
    db.add(Message(chat_id=run.chat_id, role="assistant", content=content,
                   model=step.model, user_id=user.id if user else None))

    # Обновляем контекст
    ctx["prev_result"] = content
    ctx[f"step_{step.step_number}"] = content
    run.current_step += 1

    solution = db.query(Solution).filter_by(id=run.solution_id).first()
    steps = solution.steps

    # Следующий шаг
    if run.current_step >= len(steps):
        run.status = "done"
        # Списываем фиксированную цену решения (если есть)
        if user and solution.price_tokens > 0:
            if not deduct_strict(db, user.id, solution.price_tokens):
                run.status = "error"; db.commit()
                return {"status": "error", "error": "Недостаточно токенов для завершения решения"}
            db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-solution.price_tokens,
                               description=f"Готовое решение: {solution.title}"))
        # Бизнес-решения — генерируем PDF файл с фирменным оформлением
        pdf_url = None
        if is_business and content.strip():
            try:
                import os as _os, uuid as _uuid
                from server.pdf_builder import markdown_to_pdf
                base = _os.path.dirname(_os.path.abspath(__file__))
                project_root = _os.path.dirname(_os.path.dirname(base))
                upload_dir = _os.path.join(project_root, "uploads", "solutions")
                _os.makedirs(upload_dir, exist_ok=True)
                fid = f"sol_{run.id}_{_uuid.uuid4().hex[:8]}.pdf"
                out_path = _os.path.join(upload_dir, fid)
                ok = markdown_to_pdf(
                    md_text=content,
                    title=solution.title,
                    out_path=out_path,
                    subtitle=solution.description or "",
                )
                if ok:
                    pdf_url = f"/uploads/solutions/{fid}"
                    log.info(f"[Solution] PDF создан: {pdf_url}")
            except Exception as e:
                log.error(f"[Solution] PDF generation failed: {e}")
        db.commit()
        return {"status": "done", "chat_id": run.chat_id,
                "result": {"type": resp_type, "content": content},
                "pdf_url": pdf_url}

    next_step = steps[run.current_step]
    run.context = json.dumps(ctx)
    db.commit()

    # Если следующий шаг не ждёт ввода — выполняем сразу
    if not next_step.wait_for_user:
        return _execute_step(run, next_step, None, db, user)

    return {"status": "waiting_input", "run_id": run.id, "chat_id": run.chat_id,
            "step": _step_dict(next_step),
            "current_result": {"type": resp_type, "content": content}}


# ─── public endpoints ──────────────────────────────────────────────────────────

@router.get("/solutions/categories")
def get_categories(db: Session = Depends(get_db)):
    cats = db.query(SolutionCategory).order_by(SolutionCategory.sort_order).all()
    return [{"id": c.id, "slug": c.slug, "title": c.title} for c in cats]


@router.get("/solutions")
def get_solutions(category: str | None = None, db: Session = Depends(get_db)):
    q = db.query(Solution).filter_by(is_active=True)
    if category:
        cat = db.query(SolutionCategory).filter_by(slug=category).first()
        if not cat:
            return []  # неизвестная категория — пустой список (а не все решения)
        q = q.filter_by(category_id=cat.id)
    return [_sol_dict(s) for s in q.order_by(Solution.sort_order).all()]


@router.get("/solutions/{solution_id}")
def get_solution(solution_id: int, db: Session = Depends(get_db)):
    s = db.query(Solution).filter_by(id=solution_id, is_active=True).first()
    if not s:
        raise HTTPException(404, "Решение не найдено")
    d = _sol_dict(s)
    d["steps"] = [_step_dict(st) for st in s.steps]
    # Для orchestra-режима — отдаём подсказку и список stages для preview
    if s.orchestra_json:
        try:
            orch = json.loads(s.orchestra_json)
            d["orchestra_input_hint"] = orch.get("input_hint", "")
            # requires_attachments: список того что юзер должен загрузить.
            # Каждый: {kind:"doc"|"image", label, accept (mime/ext), required}
            d["orchestra_attachments_spec"] = orch.get("requires_attachments", [])
            d["orchestra_stages"] = [
                {"id": st["id"], "label": st.get("label", st["id"]),
                 "type": st.get("type", "llm")}
                for st in (orch.get("stages") or [])
            ]
        except Exception:
            pass
    return d


@router.post("/solutions/{solution_id}/run")
def run_solution(solution_id: int, db: Session = Depends(get_db),
                 user=Depends(optional_user)):
    s = db.query(Solution).filter_by(id=solution_id, is_active=True).first()
    if not s:
        raise HTTPException(404, "Решение не найдено")
    if user:
        if not user.is_verified:
            raise HTTPException(403, "Подтвердите email")
        if s.price_tokens > 0 and get_balance(db, user.id) < s.price_tokens:
            raise HTTPException(402, "Недостаточно токенов")
    chat_id = str(uuid.uuid4())
    run = SolutionRun(user_id=user.id if user else None,
                      solution_id=solution_id, chat_id=chat_id,
                      current_step=0, status="running", context=json.dumps({}))
    db.add(run)
    db.commit()
    db.refresh(run)

    # Если первый шаг не ждёт ввода — сразу выполняем
    first_step = s.steps[0] if s.steps else None
    if first_step and not first_step.wait_for_user:
        return _execute_step(run, first_step, None, db, user)

    return {"run_id": run.id, "chat_id": chat_id, "status": "waiting_input",
            "step": _step_dict(first_step) if first_step else None}


@router.post("/solutions/runs/{run_id}/continue")
def continue_run(run_id: int, body: dict, db: Session = Depends(get_db),
                 user=Depends(optional_user)):
    run = db.query(SolutionRun).filter_by(id=run_id).first()
    if not run:
        raise HTTPException(404, "Run не найден")
    # IDOR-защита: владелец run должен совпадать с текущим юзером
    # (или оба быть None — анонимные сессии не связаны между юзерами)
    run_owner = run.user_id
    cur_owner = user.id if user else None
    if run_owner != cur_owner:
        raise HTTPException(403, "Нет доступа к этому запуску")
    if run.status == "done":
        return {"status": "done"}

    solution = db.query(Solution).filter_by(id=run.solution_id).first()
    steps = solution.steps
    if run.current_step >= len(steps):
        run.status = "done"
        db.commit()
        return {"status": "done", "chat_id": run.chat_id}

    step = steps[run.current_step]
    user_input = body.get("input", "")
    return _execute_step(run, step, user_input, db, user)



# ═══════════════════════════════════════════════════════════════════════════
# Multi-agent orchestra endpoints
# ═══════════════════════════════════════════════════════════════════════════
#
# Поток работы:
#   1. POST /solutions/{id}/orchestra/start  { input } → возвращает run_id
#      Сразу запускает asyncio task с run_orchestra(run_id) в фоне.
#   2. GET  /solutions/runs/{run_id}/stream — SSE поток обновлений stages_state
#      (UI рисует прогресс по каждому stage'у).
#   3. GET  /solutions/runs/{run_id}        — снимок состояния (для возврата
#      после реконнекта).
#   4. POST /solutions/runs/{run_id}/share  — генерит public_token + URL для шаринга.
#   5. GET  /s/{public_token}               — публичный просмотр результата (без auth).


class OrchestraAttachment(BaseModel):
    """Файл, загруженный юзером для решения. file_url берётся из /upload
    (т.е. файл уже лежит в /uploads/...). kind задаёт семантику для
    stage'ов file_extract / vision_describe."""
    file_url: str
    name: str | None = None
    mime: str | None = None
    kind: str = "doc"   # doc | image | sheet | other
    size: int | None = None


class OrchestraStartBody(BaseModel):
    input: str
    attachments: list[OrchestraAttachment] | None = None


@router.post("/solutions/{solution_id}/orchestra/start")
async def orchestra_start(solution_id: int, body: OrchestraStartBody,
                           db: Session = Depends(get_db),
                           user: User = Depends(current_user)):
    """Запустить multi-agent оркестр для решения. Списание происходит
    по факту работы каждого stage'а (real × margin), не разово фикс-цену."""
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    solution = db.query(Solution).filter_by(id=solution_id, is_active=True).first()
    if not solution:
        raise HTTPException(404, "Решение не найдено")
    if not solution.orchestra_json:
        raise HTTPException(400, "У этого решения нет orchestra-конфигурации. "
                                  "Используйте /solutions/{id}/run для legacy-запуска.")
    user_input = (body.input or "").strip()
    if len(user_input) < 10:
        raise HTTPException(400, "Опиши задачу подробнее (минимум 10 символов).")
    if len(user_input) > 20_000:
        raise HTTPException(413, "Слишком длинный input (макс 20 КБ).")

    # Pre-check: у юзера должен быть какой-то баланс, иначе бессмысленно
    # стартовать (orchestra списывает по stage'ам — первый же stage упадёт).
    if get_balance(db, user.id) < 200:  # минимум 2 ₽ "на пробу"
        raise HTTPException(402, "Недостаточно средств. Минимум для запуска — 2 ₽.")

    # Валидация attachments: каждый file_url должен указывать на /uploads/*,
    # реально существовать на диске и быть в безопасных пределах размера.
    attachments_payload: list[dict] = []
    if body.attachments:
        from pathlib import Path as _P
        import os as _os
        proj_root = _P(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))).resolve()
        uploads_root = (proj_root / "uploads").resolve()
        for att in body.attachments[:5]:  # макс 5 файлов на запуск
            url = (att.file_url or "").strip()
            if not url.startswith("/uploads/") or len(url) > 500:
                raise HTTPException(400, f"Некорректный file_url: {url[:60]}")
            try:
                abs_p = (proj_root / url.lstrip("/")).resolve()
                abs_p.relative_to(uploads_root)
            except (ValueError, OSError):
                raise HTTPException(400, "Файл вне разрешённой директории")
            if not abs_p.is_file():
                raise HTTPException(404, f"Файл не найден: {att.name or url}")
            try:
                size_bytes = abs_p.stat().st_size
            except OSError:
                size_bytes = att.size or 0
            if size_bytes > 25 * 1024 * 1024:
                raise HTTPException(413, f"Файл больше 25 МБ: {att.name or url}")
            kind = (att.kind or "doc").strip()
            if kind not in ("doc", "image", "sheet", "other"):
                kind = "doc"
            attachments_payload.append({
                "file_url": url,
                "name": (att.name or "")[:200],
                "mime": (att.mime or "")[:100],
                "kind": kind,
                "size": size_bytes,
            })

    chat_id = uuid.uuid4().hex[:16]
    run = SolutionRun(
        user_id=user.id, solution_id=solution_id,
        chat_id=chat_id, status="running",
        user_input=user_input,
        attachments_json=(json.dumps(attachments_payload, ensure_ascii=False)
                           if attachments_payload else None),
        context=json.dumps({}, ensure_ascii=False),
    )
    db.add(run); db.commit(); db.refresh(run)

    try:
        from server.audit_log import log_action
        log_action("solution.orchestra_started", user_id=user.id,
                   target_type="solution", target_id=str(solution_id),
                   details={"run_id": run.id, "title": solution.title[:80]})
    except Exception:
        pass

    # Запуск в фоне — НЕ ждём окончания. Фронт подключается к SSE-потоку.
    from server.solutions_orchestra import run_orchestra
    asyncio.create_task(run_orchestra(run.id))

    return {"run_id": run.id, "chat_id": chat_id, "status": "running"}


@router.get("/solutions/runs/{run_id}")
def orchestra_run_get(run_id: int, db: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    """Снимок состояния run'а (без auth-bypass через шаринг — здесь только владелец)."""
    run = db.query(SolutionRun).filter_by(id=run_id).first()
    if not run:
        raise HTTPException(404, "Run не найден")
    if run.user_id != user.id:
        raise HTTPException(403, "Нет доступа")
    state = {}
    if run.stages_state:
        try: state = json.loads(run.stages_state)
        except Exception: state = {}
    attachments = []
    if run.attachments_json:
        try: attachments = json.loads(run.attachments_json) or []
        except Exception: attachments = []
    return {
        "run_id": run.id,
        "solution_id": run.solution_id,
        "status": run.status,
        "stages": state.get("stages", {}),
        "final_output": run.final_output,
        "pdf_path": run.pdf_path,
        "total_cost_kop": run.total_cost_kop or 0,
        "public_token": run.public_token,
        "user_input": run.user_input,
        "attachments": attachments,
    }


@router.get("/solutions/runs/{run_id}/stream")
async def orchestra_run_stream(run_id: int, db: Session = Depends(get_db),
                                 user: User = Depends(current_user)):
    """SSE-поток обновлений run'а. Сначала шлёт текущее состояние, потом
    push'ит каждый delta при изменении (через subscribe_run)."""
    run = db.query(SolutionRun).filter_by(id=run_id).first()
    if not run:
        raise HTTPException(404, "Run не найден")
    if run.user_id != user.id:
        raise HTTPException(403, "Нет доступа")

    from server.solutions_orchestra import subscribe_run, unsubscribe_run

    async def event_gen():
        # Сразу — снимок состояния
        snap = {}
        if run.stages_state:
            try: snap = json.loads(run.stages_state)
            except Exception: snap = {}
        snap["status"] = run.status
        snap["final_output"] = run.final_output
        snap["pdf_path"] = run.pdf_path
        yield f"data: {json.dumps(snap, ensure_ascii=False)}\n\n"

        if run.status in ("done", "error"):
            return

        q = subscribe_run(run_id)
        try:
            for _ in range(600):  # макс 10 минут (600 × 1 сек idle)
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    # heartbeat — чтобы прокси не закрыл соединение
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                if msg.get("status") in ("done", "error"):
                    return
        finally:
            unsubscribe_run(run_id, q)

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                              headers={"Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})


@router.post("/solutions/runs/{run_id}/share")
def orchestra_run_share(run_id: int, db: Session = Depends(get_db),
                         user: User = Depends(current_user)):
    """Генерит public_token для шаринга результата. Идемпотентно: если уже
    есть — возвращает тот же."""
    run = db.query(SolutionRun).filter_by(id=run_id).first()
    if not run:
        raise HTTPException(404, "Run не найден")
    if run.user_id != user.id:
        raise HTTPException(403, "Нет доступа")
    if run.status != "done":
        raise HTTPException(400, "Можно шарить только завершённый отчёт")
    if not run.public_token:
        run.public_token = _secrets.token_urlsafe(20)
        db.commit()
    return {"public_token": run.public_token,
            "share_url": f"/s/{run.public_token}"}


@router.delete("/solutions/runs/{run_id}/share")
def orchestra_run_unshare(run_id: int, db: Session = Depends(get_db),
                           user: User = Depends(current_user)):
    """Отменить шаринг (revoke token)."""
    run = db.query(SolutionRun).filter_by(id=run_id).first()
    if not run:
        raise HTTPException(404, "Run не найден")
    if run.user_id != user.id:
        raise HTTPException(403, "Нет доступа")
    run.public_token = None
    db.commit()
    return {"status": "revoked"}
