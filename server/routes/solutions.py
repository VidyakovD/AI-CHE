from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
import json, uuid, logging

from server.routes.deps import get_db, optional_user
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
            "steps_count": len(s.steps) if s.steps else 0}


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
