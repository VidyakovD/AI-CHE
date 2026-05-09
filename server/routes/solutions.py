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
    # input_hint вытаскиваем из orchestra_json — для подсказки в форме
    # запуска. Безопасно к отсутствию (не all orchestra-пилоты его задают).
    input_hint = None
    if s.orchestra_json:
        try:
            oj = json.loads(s.orchestra_json)
            input_hint = oj.get("input_hint")
        except Exception:
            pass
    return {
        "id": s.id, "title": s.title, "description": s.description,
        "image_url": s.image_url, "price_tokens": s.price_tokens,
        "category_id": s.category_id,
        "steps_count": len(s.steps) if s.steps else 0,
        "is_orchestra": bool(s.orchestra_json),
        # Новые поля для UI с фильтрами/бейджами:
        "subcategory": s.subcategory,
        "tags": [t.strip() for t in (s.tags or "").split(",") if t.strip()],
        "is_featured": bool(s.is_featured),
        "short_summary": s.short_summary,
        "input_hint": input_hint,
    }


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


class OrchestraCompareBody(BaseModel):
    """Параллельный запуск одного Solution на N моделях для сравнения."""
    input: str
    attachments: list[OrchestraAttachment] | None = None
    models: list[str]   # 2-3 модели: например ["claude-sonnet", "claude-opus"]


def _validate_attachments(atts: list | None) -> list[dict]:
    """Общая валидация attachments из orchestra-запросов.

    Каждый file_url должен указывать на /uploads/*, реально существовать
    на диске, быть в безопасных пределах размера. Возвращает нормализованный
    список dict-ов для записи в SolutionRun.attachments_json.
    """
    if not atts:
        return []
    from pathlib import Path as _P
    import os as _os
    proj_root = _P(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))).resolve()
    uploads_root = (proj_root / "uploads").resolve()
    out: list[dict] = []
    for att in atts[:5]:
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
        out.append({
            "file_url": url, "name": (att.name or "")[:200],
            "mime": (att.mime or "")[:100], "kind": kind, "size": size_bytes,
        })
    return out


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

    attachments_payload = _validate_attachments(body.attachments)

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


# Whitelist моделей для сравнительного запуска. Только Claude/GPT — иначе
# сравнение нерепрезентативно (Perplexity/Grok заточены под другие задачи).
_COMPARE_MODEL_WHITELIST = {
    "claude", "claude-sonnet", "claude-opus", "claude-haiku",
    "gpt", "gpt-4o",
}


@router.post("/solutions/{solution_id}/orchestra/start-compare")
async def orchestra_start_compare(solution_id: int, body: OrchestraCompareBody,
                                    db: Session = Depends(get_db),
                                    user: User = Depends(current_user)):
    """Параллельный запуск одного Solution на N моделях для сравнения
    качества. Полезно когда юзер хочет увидеть «а если Opus вместо Sonnet?»

    Создаёт N независимых SolutionRun'ов с переопределённой default_model
    и связывает их через общий compare_group (chat_id с префиксом cmp_).
    Списание: каждый run списывается отдельно (стоимость × N).
    """
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    solution = db.query(Solution).filter_by(id=solution_id, is_active=True).first()
    if not solution or not solution.orchestra_json:
        raise HTTPException(404, "Решение не найдено или нет orchestra")
    user_input = (body.input or "").strip()
    if len(user_input) < 10:
        raise HTTPException(400, "Минимум 10 символов")
    if len(user_input) > 20_000:
        raise HTTPException(413, "Слишком длинный input")

    models = [m.strip() for m in (body.models or []) if m and m.strip()]
    models = [m for m in models if m in _COMPARE_MODEL_WHITELIST]
    if len(models) < 2 or len(models) > 3:
        raise HTTPException(400,
            f"Выбери 2-3 модели из: {sorted(_COMPARE_MODEL_WHITELIST)}")
    if len(set(models)) != len(models):
        raise HTTPException(400, "Модели должны быть разные")

    # N×4 ₽ минимум — пред-проверка
    if get_balance(db, user.id) < 200 * len(models):
        raise HTTPException(402, f"Минимум для compare — {2 * len(models)} ₽")

    attachments_payload = _validate_attachments(body.attachments)

    # Загружаем оригинальный orchestra и для каждой модели делаем копию
    # с переопределённой default_model.
    try:
        orch = json.loads(solution.orchestra_json)
    except Exception:
        raise HTTPException(500, "Некорректная orchestra-конфигурация")

    # compare_group_id — общий идентификатор для UI (отображает все runs
    # вместе как «сравнение»). Кладём в chat_id с префиксом для удобства.
    cmp_group = f"cmp_{uuid.uuid4().hex[:12]}"

    import copy as _copy
    runs_info: list[dict] = []
    for model in models:
        # Делаем deep-copy orchestra с переопределённой моделью
        custom = _copy.deepcopy(orch)
        custom["default_model"] = model
        # Создаём run с custom orchestra прямо в его context (мы не меняем
        # Solution в БД — это плохо для других юзеров). Чтобы run использовал
        # эту orchestra, run_orchestra читает Solution.orchestra_json.
        # Решение: создадим temporary marker и подменим в run_orchestra
        # через custom-load. Пока проще — клонируем orchestra в run.context.
        chat_id = f"{cmp_group}_{model}"
        run = SolutionRun(
            user_id=user.id, solution_id=solution_id,
            chat_id=chat_id, status="running",
            user_input=user_input,
            attachments_json=(json.dumps(attachments_payload, ensure_ascii=False)
                               if attachments_payload else None),
            # Сохраняем custom orchestra в context — runtime прочитает
            context=json.dumps({"_compare_orchestra": custom,
                                 "_compare_group": cmp_group,
                                 "_compare_model": model},
                               ensure_ascii=False),
        )
        db.add(run); db.commit(); db.refresh(run)
        runs_info.append({"run_id": run.id, "model": model, "chat_id": chat_id})

    try:
        from server.audit_log import log_action
        log_action("solution.orchestra_compare", user_id=user.id,
                   target_type="solution", target_id=str(solution_id),
                   details={"group": cmp_group, "models": models,
                             "run_ids": [r["run_id"] for r in runs_info]})
    except Exception:
        pass

    # Запускаем параллельно
    from server.solutions_orchestra import run_orchestra
    for r in runs_info:
        asyncio.create_task(run_orchestra(r["run_id"]))

    return {"compare_group": cmp_group, "runs": runs_info, "status": "running"}


@router.get("/solutions/compare/{compare_group}")
def orchestra_compare_get(compare_group: str,
                            db: Session = Depends(get_db),
                            user: User = Depends(current_user)):
    """Снимок состояния compare-группы: список runs с метаданными."""
    if not compare_group.startswith("cmp_") or len(compare_group) > 32:
        raise HTTPException(400, "Некорректный compare_group")
    runs = (db.query(SolutionRun)
              .filter(SolutionRun.user_id == user.id,
                      SolutionRun.chat_id.like(f"{compare_group}_%"))
              .order_by(SolutionRun.id.asc()).all())
    if not runs:
        raise HTTPException(404, "Compare-группа не найдена")
    out = []
    for r in runs:
        # Извлекаем модель из chat_id (cmp_xxx_<model>)
        model = r.chat_id.replace(f"{compare_group}_", "", 1)
        state = {}
        if r.stages_state:
            try: state = json.loads(r.stages_state)
            except Exception: state = {}
        out.append({
            "run_id": r.id, "model": model, "status": r.status,
            "stages": state.get("stages", {}),
            "final_output": r.final_output,
            "pdf_path": r.pdf_path,
            "total_cost_kop": r.total_cost_kop or 0,
            "user_mark": r.user_mark,
        })
    return {"compare_group": compare_group, "runs": out}


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


# ═══════════════════════════════════════════════════════════════════════════
# Re-run отдельного stage'а
# ═══════════════════════════════════════════════════════════════════════════


class RestageBody(BaseModel):
    stage_id: str
    extra_instruction: str | None = None


@router.post("/solutions/runs/{run_id}/restage")
async def orchestra_restage(run_id: int, body: RestageBody,
                              db: Session = Depends(get_db),
                              user: User = Depends(current_user)):
    """Перезапустить один stage в завершённом отчёте + перегенерировать
    финал. Стоимость: real-токены × margin (как обычная стадия).

    Используется когда юзер недоволен отдельным разделом — например, хочет
    другой блок «гарантии» или другие 3 варианта CTA.
    """
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    run = db.query(SolutionRun).filter_by(id=run_id).first()
    if not run:
        raise HTTPException(404, "Run не найден")
    if run.user_id != user.id:
        raise HTTPException(403, "Нет доступа")
    if run.status not in ("done", "error"):
        raise HTTPException(400, "Re-run возможен только для завершённого решения")
    sid = (body.stage_id or "").strip()
    if not sid or len(sid) > 64:
        raise HTTPException(400, "Некорректный stage_id")
    extra = (body.extra_instruction or "")[:5000]
    if get_balance(db, user.id) < 200:
        raise HTTPException(402, "Минимум для пере-генерации — 2 ₽")
    run.status = "running"
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("solution.restage", user_id=user.id,
                   target_type="solution_run", target_id=str(run_id),
                   details={"stage_id": sid})
    except Exception:
        pass
    from server.solutions_orchestra import restage as _restage
    asyncio.create_task(_restage(run_id, sid, extra or None))
    return {"run_id": run_id, "status": "running", "stage_id": sid}


# ═══════════════════════════════════════════════════════════════════════════
# Templates: сохранить запуск как шаблон + повторить одним кликом
# ═══════════════════════════════════════════════════════════════════════════


class SaveTemplateBody(BaseModel):
    name: str


@router.post("/solutions/runs/{run_id}/save-template")
def template_save_from_run(run_id: int, body: SaveTemplateBody,
                            db: Session = Depends(get_db),
                            user: User = Depends(current_user)):
    """Сохранить input + attachments запуска как шаблон с именем."""
    from server.models import SolutionRunTemplate
    run = db.query(SolutionRun).filter_by(id=run_id).first()
    if not run:
        raise HTTPException(404, "Run не найден")
    if run.user_id != user.id:
        raise HTTPException(403, "Нет доступа")
    name = (body.name or "").strip()[:100]
    if len(name) < 2:
        raise HTTPException(400, "Имя шаблона минимум 2 символа")
    cnt = db.query(SolutionRunTemplate).filter_by(user_id=user.id).count()
    if cnt >= 50:
        raise HTTPException(409, "Превышен лимит 50 шаблонов")
    t = SolutionRunTemplate(
        user_id=user.id, solution_id=run.solution_id, name=name,
        user_input=run.user_input,
        attachments_json=run.attachments_json,
    )
    db.add(t); db.commit(); db.refresh(t)
    return {"template_id": t.id, "name": t.name}


@router.get("/solutions/templates")
def templates_list(solution_id: int | None = None,
                    db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    from server.models import SolutionRunTemplate
    q = db.query(SolutionRunTemplate).filter_by(user_id=user.id)
    if solution_id:
        q = q.filter_by(solution_id=solution_id)
    rows = q.order_by(SolutionRunTemplate.created_at.desc()).all()
    return [{
        "id": t.id, "solution_id": t.solution_id, "name": t.name,
        "input_preview": (t.user_input or "")[:100],
        "has_attachments": bool(t.attachments_json and t.attachments_json != "[]"),
        "created_at": t.created_at.isoformat() if t.created_at else None,
    } for t in rows]


@router.delete("/solutions/templates/{template_id}")
def template_delete(template_id: int, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    from server.models import SolutionRunTemplate
    t = db.query(SolutionRunTemplate).filter_by(
        id=template_id, user_id=user.id).first()
    if not t:
        raise HTTPException(404, "Шаблон не найден")
    db.delete(t); db.commit()
    return {"status": "deleted"}


@router.post("/solutions/templates/{template_id}/run")
async def template_run(template_id: int, db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    """Запустить orchestra с input + attachments из сохранённого шаблона."""
    from server.models import SolutionRunTemplate
    if not user.is_verified:
        raise HTTPException(403, "Подтвердите email")
    t = db.query(SolutionRunTemplate).filter_by(
        id=template_id, user_id=user.id).first()
    if not t:
        raise HTTPException(404, "Шаблон не найден")
    sol = db.query(Solution).filter_by(id=t.solution_id, is_active=True).first()
    if not sol or not sol.orchestra_json:
        raise HTTPException(400, "Решение шаблона больше недоступно")
    if get_balance(db, user.id) < 200:
        raise HTTPException(402, "Минимум для запуска — 2 ₽")
    chat_id = uuid.uuid4().hex[:16]
    run = SolutionRun(
        user_id=user.id, solution_id=t.solution_id,
        chat_id=chat_id, status="running",
        user_input=t.user_input,
        attachments_json=t.attachments_json,
        context=json.dumps({}, ensure_ascii=False),
    )
    db.add(run); db.commit(); db.refresh(run)
    from server.solutions_orchestra import run_orchestra
    asyncio.create_task(run_orchestra(run.id))
    return {"run_id": run.id, "chat_id": chat_id, "status": "running",
            "from_template": t.id}


# ═══════════════════════════════════════════════════════════════════════════
# Реакция юзера 👍/👎/💡
# ═══════════════════════════════════════════════════════════════════════════


class ReactionBody(BaseModel):
    mark: str
    comment: str | None = None


_AUTO_FLAG_THRESHOLD_DOWNS = 3      # сколько 👎 за окно достаточно для алерта
_AUTO_FLAG_WINDOW_DAYS = 7          # окно


def _check_auto_flag(db: Session, solution_id: int) -> None:
    """Если за последние 7 дней по этому solution_id >=3 👎 → email админу.
    Дедуп через ActionLog (`solution.auto_flagged`) — раз в 24ч на solution_id.
    DB-based чтобы работать корректно при multi-worker (раньше был in-memory dict).
    """
    from datetime import datetime as _dt, timedelta as _td
    from server.models import ActionLog
    # Уже алертили за последние 24ч?
    last_alert = (db.query(ActionLog)
                    .filter(ActionLog.action == "solution.auto_flagged",
                            ActionLog.target_id == str(solution_id),
                            ActionLog.ts >= _dt.utcnow() - _td(hours=24))
                    .first())
    if last_alert:
        return
    cutoff = _dt.utcnow() - _td(days=_AUTO_FLAG_WINDOW_DAYS)
    recent = (db.query(SolutionRun)
                .filter(SolutionRun.solution_id == solution_id,
                        SolutionRun.user_mark == "down",
                        SolutionRun.created_at >= cutoff)
                .all())
    if len(recent) < _AUTO_FLAG_THRESHOLD_DOWNS:
        return
    sol = db.query(Solution).filter_by(id=solution_id).first()
    title = sol.title if sol else f"Solution #{solution_id}"
    log.warning(f"[auto-flag] {len(recent)} 👎 на «{title}» за {_AUTO_FLAG_WINDOW_DAYS}д — алерт админу")
    # Шлём email
    try:
        import os as _os
        admins = [e.strip().lower() for e in _os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()]
        if not admins:
            return
        from server.email_service import _send, _base_template
        comments = [f"<li>{(r.user_comment or 'без комментария')[:200]}</li>"
                    for r in recent if r.user_comment]
        comments_html = ("<ul style='color:rgba(199,196,215,0.8)'>"
                         + "".join(comments[:10]) + "</ul>") if comments else ""
        body_html = (
            f'<p style="color:rgba(199,196,215,0.85);line-height:1.6">'
            f'За последние {_AUTO_FLAG_WINDOW_DAYS} дней пользователи поставили '
            f'<b>{len(recent)} 👎</b> на решение <b>«{title}»</b>.</p>'
            f'<p style="color:rgba(199,196,215,0.7)">Комментарии:</p>'
            f'{comments_html}'
            f'<p style="color:rgba(199,196,215,0.6);font-size:13px">'
            f'Стоит проверить orchestra-промпты и качество ресёрча.</p>'
        )
        for admin_email in admins:
            _send(admin_email, f"⚠️ {len(recent)} 👎 на «{title[:40]}»",
                  _base_template("Низкое качество отчёта", body_html))
        from server.audit_log import log_action
        log_action("solution.auto_flagged", user_id=None,
                   target_type="solution", target_id=str(solution_id),
                   level="warn", success=False,
                   details={"downs": len(recent), "title": title[:80]})
    except Exception as e:
        log.warning(f"[auto-flag] email failed: {type(e).__name__}: {e}")


@router.post("/solutions/runs/{run_id}/reaction")
def orchestra_reaction(run_id: int, body: ReactionBody,
                        db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    """👍/👎/💡 на финальный отчёт + опц. комментарий.
    При 👎 проверяем, не пора ли поднять алерт админу (auto-flagging:
    3+ 👎 за 7 дней по одному решению → email)."""
    run = db.query(SolutionRun).filter_by(id=run_id).first()
    if not run:
        raise HTTPException(404, "Run не найден")
    if run.user_id != user.id:
        raise HTTPException(403, "Нет доступа")
    mark = (body.mark or "").strip().lower()
    if mark not in ("up", "down", "idea", "clear"):
        raise HTTPException(400, "mark должен быть up/down/idea/clear")
    run.user_mark = None if mark == "clear" else mark
    if body.comment is not None:
        run.user_comment = (body.comment or "")[:2000]
    db.commit()
    try:
        from server.audit_log import log_action
        log_action("solution.reaction", user_id=user.id,
                   target_type="solution_run", target_id=str(run_id),
                   details={"mark": mark, "solution_id": run.solution_id,
                             "has_comment": bool(body.comment)})
    except Exception:
        pass
    # Auto-flagging при 👎
    if mark == "down":
        try:
            _check_auto_flag(db, run.solution_id)
        except Exception as e:
            log.warning(f"[auto-flag] check failed: {type(e).__name__}: {e}")
    return {"status": "ok", "mark": run.user_mark}


# ═══════════════════════════════════════════════════════════════════════════
# DOCX-экспорт финального отчёта
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/solutions/runs/{run_id}/docx")
def orchestra_docx(run_id: int, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    """Скачать финальный отчёт в DOCX (on-demand)."""
    from fastapi.responses import FileResponse
    run = db.query(SolutionRun).filter_by(id=run_id).first()
    if not run:
        raise HTTPException(404, "Run не найден")
    if run.user_id != user.id:
        raise HTTPException(403, "Нет доступа")
    if run.status != "done" or not run.final_output:
        raise HTTPException(400, "Отчёт ещё не готов")
    sol = db.query(Solution).filter_by(id=run.solution_id).first()
    title = sol.title if sol else "Бизнес-решение"
    subtitle = sol.description if sol else ""
    import os as _os, uuid as _uuid, re as _re
    base = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    out_dir = _os.path.join(base, "uploads", "solutions")
    _os.makedirs(out_dir, exist_ok=True)
    fid = f"sol_{run_id}_{_uuid.uuid4().hex[:6]}.docx"
    out_path = _os.path.join(out_dir, fid)
    from server.docx_builder import markdown_to_docx
    ok = markdown_to_docx(md_text=run.final_output, title=title,
                           out_path=out_path, subtitle=subtitle)
    if not ok:
        raise HTTPException(503, "DOCX-экспорт временно недоступен")
    safe = _re.sub(r"[^\w\-]", "_", title)[:40]
    return FileResponse(
        out_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"{safe}.docx",
    )


@router.get("/solutions/runs/{run_id}/xlsx")
def orchestra_xlsx(run_id: int, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    """Скачать финальный отчёт в XLSX. Каждая markdown-таблица из отчёта
    идёт на отдельный лист. Главный лист — резюме. Полезно для финансового
    аудита и конкурентного анализа (хочешь обработать таблицу дальше)."""
    from fastapi.responses import FileResponse
    run = db.query(SolutionRun).filter_by(id=run_id).first()
    if not run:
        raise HTTPException(404, "Run не найден")
    if run.user_id != user.id:
        raise HTTPException(403, "Нет доступа")
    if run.status != "done" or not run.final_output:
        raise HTTPException(400, "Отчёт ещё не готов")
    sol = db.query(Solution).filter_by(id=run.solution_id).first()
    title = sol.title if sol else "Бизнес-решение"
    subtitle = sol.description if sol else ""
    import os as _os, uuid as _uuid, re as _re
    base = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    out_dir = _os.path.join(base, "uploads", "solutions")
    _os.makedirs(out_dir, exist_ok=True)
    fid = f"sol_{run_id}_{_uuid.uuid4().hex[:6]}.xlsx"
    out_path = _os.path.join(out_dir, fid)
    from server.xlsx_builder import markdown_to_xlsx
    ok = markdown_to_xlsx(md_text=run.final_output, title=title,
                           out_path=out_path, subtitle=subtitle)
    if not ok:
        raise HTTPException(503, "XLSX-экспорт временно недоступен")
    safe = _re.sub(r"[^\w\-]", "_", title)[:40]
    return FileResponse(
        out_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"{safe}.xlsx",
    )
