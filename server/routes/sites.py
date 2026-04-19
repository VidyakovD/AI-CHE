"""Site project endpoints — extracted from main.py."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
import json
import os
import logging

from server.routes.deps import get_db, current_user, optional_user
from server.models import SiteProject, User, Transaction
from server.ai import generate_response
from server.billing import deduct_strict

log = logging.getLogger(__name__)

router = APIRouter(tags=["sites"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SPEC_CONVERSATION_CH_COST = 5   # per chat turn
CODE_GEN_CH_COST = 30           # per code generation call
CODE_ITER_CH_COST = 15          # per iteration call

_sites_host_base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "uploads", "sites")

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class CreateSiteProjectRequest(BaseModel):
    name: str
    creation_mode: str = "create_together"  # "have_spec" or "create_together"
    spec_text: str | None = None             # for "have_spec" mode


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
@router.get("/sites/templates")
def list_site_templates(db: Session = Depends(get_db)):
    """Deprecated -- empty for new flow."""
    return []


# ---------------------------------------------------------------------------
# Projects CRUD
# ---------------------------------------------------------------------------
@router.get("/sites/projects")
def list_sites(db: Session = Depends(get_db), user=Depends(optional_user)):
    if not user:
        return []
    projects = db.query(SiteProject).filter_by(user_id=user.id).order_by(SiteProject.updated_at.desc()).all()
    result = []
    for p in projects:
        result.append({
            "id": p.id, "name": p.name, "status": p.status,
            "price_tokens": p.price_tokens,
            "creation_mode": p.creation_mode or "create_together",
            "conversation_phase": p.conversation_phase or "idle",
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        })
    return result


@router.post("/sites/projects")
def create_site_project(req: CreateSiteProjectRequest, db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    phase = "idle"
    status = "draft"
    chat_history = None
    spec_text = None

    if req.creation_mode == "have_spec" and req.spec_text:
        # User already has specs
        spec_text = req.spec_text
        phase = "collecting_images"
        status = "has_spec"
        # Deduct small amount for spec analysis
        cost = SPEC_CONVERSATION_CH_COST
        if not deduct_strict(db, user.id, cost):
            raise HTTPException(402, "Недостаточно токенов")
        price = cost
    elif req.creation_mode == "create_together":
        phase = "gathering_spec"
        chat_history = json.dumps([])
        price = 0
    else:
        phase = "gathering_spec"
        chat_history = json.dumps([])
        price = 0

    p = SiteProject(
        user_id=user.id, name=req.name,
        creation_mode=req.creation_mode,
        conversation_phase=phase,
        chat_history=chat_history,
        spec_text=spec_text,
        price_tokens=price,
        status=status,
    )
    db.add(p); db.commit(); db.refresh(p)
    return {"id": p.id, "status": p.status, "phase": p.conversation_phase}


@router.get("/sites/projects/{project_id}")
def get_site_project(project_id: int, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    return {
        "id": p.id, "name": p.name, "status": p.status,
        "spec_text": p.spec_text, "code_html": p.code_html,
        "price_tokens": p.price_tokens,
        "creation_mode": p.creation_mode,
        "conversation_phase": p.conversation_phase,
        "chat_history": p.chat_history,
        "image_paths": p.image_paths,
        "hosted_path": p.hosted_path,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.delete("/sites/projects/{project_id}")
def delete_site_project(project_id: int, db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    # Remove hosted files if any
    if p.hosted_path:
        import shutil
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "uploads", "sites", str(project_id))
        if os.path.exists(d):
            shutil.rmtree(d, ignore_errors=True)
    db.delete(p); db.commit()
    return {"status": "deleted"}


@router.post("/sites/projects/{project_id}/rename")
def rename_site_project(project_id: int, body: dict, db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    p.name = body.get("name", p.name)
    db.commit()
    return {"status": "ok", "name": p.name}


# ---------------------------------------------------------------------------
# Spec-building chat
# ---------------------------------------------------------------------------
@router.post("/sites/projects/{project_id}/chat")
def site_project_chat(project_id: int, body: dict, db: Session = Depends(get_db),
                      user: User = Depends(current_user)):
    """Conversational spec builder -- user talks to Claude to build the ТЗ."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if p.conversation_phase not in ("gathering_spec", "spec_ready", "collecting_images"):
        raise HTTPException(400, "Неверная фаза проекта")
    # Reset phase when user goes back to chat from spec_ready
    if p.conversation_phase == "spec_ready":
        p.conversation_phase = "gathering_spec"

    user_message = body.get("message", "").strip()
    if not user_message:
        raise HTTPException(400, "Пустое сообщение")

    # Deduct tokens
    cost = SPEC_CONVERSATION_CH_COST
    if not deduct_strict(db, user.id, cost):
        raise HTTPException(402, "Недостаточно токенов")
    p.price_tokens += cost
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost,
                       description="Чат по ТЗ сайта", model="claude"))

    # Load chat history
    try:
        history = json.loads(p.chat_history) if p.chat_history else []
    except Exception:
        history = []
    history.append({"role": "user", "content": user_message})

    # Build system prompt
    system = (
        "Ты -- профессиональный веб-аналитик. Твоя задача -- помочь пользователю создать "
        "подробное техническое задание (ТЗ) для создания сайта. Задавай вопросы по одному, "
        "узнавай тип сайта (лендинг, магазин, блог, портфолио, сервис), тематику, целевую аудиторию, "
        "желаемые разделы, цветовые предпочтения, функционал. "
        "Когда информации достаточно -- собери всё в единое структурированное ТЗ и отправь его "
        "одним сообщением, начиная со слова 'ТЗ ГОТОВО' чтобы система могла это распознать. "
        "Отвечай на русском языке. Будь лаконичен и дружелюбен."
    )
    if p.spec_text:
        system += f"\n\nТекущее ТЗ: {p.spec_text[:500]} (можешь предложить улучшения)"

    messages = [{"role": "system", "content": system}] + history[-20:]
    answer = generate_response("claude", messages)
    ai_text = answer.get("content", "") if isinstance(answer, dict) else ""
    history.append({"role": "assistant", "content": ai_text})
    p.chat_history = json.dumps(history, ensure_ascii=False)

    # Detect if spec is ready
    if "ТЗ ГОТОВО" in ai_text or "тз готово" in ai_text.lower():
        # Extract the spec content -- remove the trigger phrase
        spec_content = ai_text.replace("ТЗ ГОТОВО", "").strip()
        if not spec_content:
            spec_content = ai_text
        p.spec_text = spec_content
        p.conversation_phase = "spec_ready"
        p.status = "has_spec"
    db.commit()

    return {"response": ai_text, "phase": p.conversation_phase, "spec_text": p.spec_text}


@router.post("/sites/projects/{project_id}/approve-spec")
def site_project_approve_spec(project_id: int, db: Session = Depends(get_db),
                               user: User = Depends(current_user)):
    """User approves the spec, moves to image collection."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    p.conversation_phase = "collecting_images"
    p.status = "has_spec"
    if not p.image_paths:
        p.image_paths = json.dumps([])
    db.commit()
    return {"status": "ok", "phase": p.conversation_phase}


# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------
@router.post("/sites/projects/{project_id}/upload-image")
def site_project_upload_image(project_id: int, db: Session = Depends(get_db),
                               user: User = Depends(current_user)):
    """Get upload URL for images/logos for this project."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    # Return a signed upload URL -- frontend will POST to /upload
    return {"upload_endpoint": "/upload", "project_id": project_id}


@router.post("/sites/projects/{project_id}/attach-image")
def site_project_attach_image(project_id: int, body: dict, db: Session = Depends(get_db),
                               user: User = Depends(current_user)):
    """Attach an uploaded image path to the project."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    try:
        imgs = json.loads(p.image_paths) if p.image_paths else []
    except Exception:
        imgs = []
    imgs.append(body["file_url"])
    p.image_paths = json.dumps(imgs)
    db.commit()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Code generation & iteration
# ---------------------------------------------------------------------------
@router.post("/sites/projects/{project_id}/generate-code")
def site_project_generate_code(project_id: int, body: dict | None = None,
                                db: Session = Depends(get_db), user=Depends(optional_user)):
    """Generate site code from spec + images."""
    if not user:
        raise HTTPException(401, "Нужна авторизация")
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.spec_text:
        raise HTTPException(400, "Сначала создайте ТЗ")

    cost = CODE_GEN_CH_COST
    if not deduct_strict(db, user.id, cost):
        raise HTTPException(402, "Недостаточно токенов")
    p.price_tokens += cost
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost,
                       description="Генерация кода сайта"))

    # Build prompt with images context
    img_context = ""
    full_urls = []
    try:
        imgs = json.loads(p.image_paths) if p.image_paths else []
        if imgs:
            # Даём AI полные URL чтобы картинки работали на любом хостинге
            base_url = os.getenv("APP_URL", "https://aiche.ru").rstrip("/")
            full_urls = [f"{base_url}{u}" if u.startswith("/") else u for u in imgs]
            lines = "\n".join(f"  {i+1}. {u}" for i, u in enumerate(full_urls))
            img_context = (
                f"\n\nЗАГРУЖЕННЫЕ ИЗОБРАЖЕНИЯ ПОЛЬЗОВАТЕЛЯ (используй ИХ, не placeholder'ы):\n{lines}\n"
                f"Обязательно вставь эти URL в <img src=\"...\"> в подходящих местах сайта. "
                f"Не придумывай несуществующие картинки."
            )
    except Exception:
        pass

    prompt = (
        f"Ты -- опытный веб-разработчик. Создай полный HTML-код одностраничного сайта по ТЗ:\n\n"
        f"=== ТЗ ===\n{p.spec_text}\n=== КОНЕЦ ТЗ ===\n"
        f"{img_context}\n"
        f"Требования:\n"
        f"- Чистый, современный адаптивный HTML+CSS\n"
        f"- Без внешних фреймворков (только inline CSS или <style>)\n"
        f"- Семантичная разметка, доступность\n"
        f"- Красивый современный дизайн\n"
        f"- Картинки: используй ТОЛЬКО URL из списка выше (если он есть)\n"
        f"- Ответ: ТОЛЬКО HTML-код, без markdown-обёрток и объяснений\n"
    )

    p.conversation_phase = "generating_code"
    db.commit()

    answer = generate_response("claude", [{"role": "user", "content": prompt}])
    content = answer.get("content", "") if isinstance(answer, dict) else ""

    if not content.strip().startswith("<") or "временно недоступен" in content:
        p.conversation_phase = "spec_approved"
        db.commit()
        raise HTTPException(503, "AI не вернул корректный HTML. Попробуйте ещё раз.")

    # Clean markdown
    for marker in ["```html\n", "```\n", "```html", "```"]:
        if content.startswith(marker):
            content = content[len(marker):]
            content = content.rsplit("```", 1)[0] if "```" in content else content
            break

    p.code_html = content
    p.conversation_phase = "done"
    p.status = "done"
    db.commit()
    return {"code_html": content, "status": p.status, "phase": p.conversation_phase}


@router.put("/sites/projects/{project_id}/save-code")
def site_project_save_code(project_id: int, body: dict, db: Session = Depends(get_db),
                            user: User = Depends(current_user)):
    """Save manually edited code back to the project."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    p.code_html = body.get("code_html", p.code_html)
    if p.conversation_phase not in ("done", "generating_code"):
        p.conversation_phase = "done"
        p.status = "done"
    db.commit()
    return {"status": "ok"}


@router.post("/sites/projects/{project_id}/iterate")
def site_project_iterate(project_id: int, body: dict, db: Session = Depends(get_db),
                          user: User = Depends(current_user)):
    """Iterate on generated code with user instructions."""
    if not user:
        raise HTTPException(401, "Нужна авторизация")
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.code_html:
        raise HTTPException(400, "Сначала сгенерируйте код")

    instructions = body.get("instructions", "").strip()
    if not instructions:
        raise HTTPException(400, "Пустая инструкция")

    cost = CODE_ITER_CH_COST
    if not deduct_strict(db, user.id, cost):
        raise HTTPException(402, "Недостаточно токенов")
    p.price_tokens += cost
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost,
                       description="Доработка сайта"))

    prompt = (
        f"Вот текущий HTML сайта:\n\n{p.code_html}\n\n"
        f"Пользователь просит: {instructions}\n"
        f"Верни ТОЛЬКО обновлённый полный HTML-код. Без markdown, без объяснений."
    )

    answer = generate_response("claude", [{"role": "user", "content": prompt}])
    content = answer.get("content", "") if isinstance(answer, dict) else ""

    # Guard: if AI returned an error message instead of HTML — don't overwrite
    if not content.strip().startswith("<") or "временно недоступен" in content:
        raise HTTPException(503, "AI не вернул корректный HTML. Попробуйте ещё раз.")

    # Clean markdown
    for marker in ["```html\n", "```\n", "```html", "```"]:
        if content.startswith(marker):
            content = content[len(marker):]
            content = content.rsplit("```", 1)[0] if "```" in content else content
            break

    p.code_html = content
    db.commit()
    return {"code_html": content}


# ---------------------------------------------------------------------------
# Hosting & serving
# ---------------------------------------------------------------------------
@router.post("/sites/projects/{project_id}/host")
def site_project_host(project_id: int, db: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    """Publish site -- save files and return public URL."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.code_html:
        raise HTTPException(400, "Сначала сгенерируйте код")

    host_dir = os.path.join(_sites_host_base, str(project_id))
    os.makedirs(host_dir, exist_ok=True)
    with open(os.path.join(host_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(p.code_html)

    p.hosted_path = f"/sites/hosted/{project_id}/"
    db.commit()
    return {"url": p.hosted_path, "status": "hosted"}


@router.get("/sites/hosted/{project_id}/{full_path:path}")
def site_project_serve(project_id: int, full_path: str = ""):
    """Serve hosted site files (защита от path traversal через Path.resolve)."""
    from pathlib import Path
    host_dir = Path(_sites_host_base, str(project_id)).resolve()
    try:
        file_path = (host_dir / (full_path or "index.html")).resolve()
        # is_relative_to проверяет реальный путь после раскрытия .. и symlinks
        file_path.relative_to(host_dir)
    except (ValueError, OSError):
        raise HTTPException(403, "Доступ запрещён")
    if not file_path.is_file():
        raise HTTPException(404, "Файл не найден")
    return FileResponse(str(file_path), media_type="text/html; charset=utf-8")


@router.post("/sites/projects/{project_id}/download")
def site_project_download(project_id: int, db: Session = Depends(get_db),
                           user: User = Depends(current_user)):
    """Trigger hosted save + return download URL."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.code_html:
        raise HTTPException(400, "Сначала сгенерируйте код")

    host_dir = os.path.join(_sites_host_base, str(project_id))
    os.makedirs(host_dir, exist_ok=True)
    with open(os.path.join(host_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(p.code_html)

    p.hosted_path = f"/sites/hosted/{project_id}/"
    db.commit()
    return {"url": f"/sites/hosted/{project_id}/", "status": "ready"}


@router.get("/sites/projects/{project_id}/zip")
def site_project_zip(project_id: int, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    """Скачать сайт целиком ZIP-ом: index.html + все картинки в /images/."""
    import io, zipfile, re
    from fastapi.responses import StreamingResponse

    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.code_html:
        raise HTTPException(400, "Сначала сгенерируйте код")

    html = p.code_html
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    project_root = os.path.dirname(base_dir)
    app_url = os.getenv("APP_URL", "https://aiche.ru").rstrip("/")

    # Собираем все URL картинок из HTML (полные и /uploads/...)
    pattern = re.compile(r'(?:src|href)=["\']([^"\']+)["\']', re.IGNORECASE)
    found = set()
    for m in pattern.finditer(html):
        url = m.group(1)
        if "/uploads/" in url:
            # Извлекаем локальный путь
            idx = url.find("/uploads/")
            local_rel = url[idx:]  # /uploads/xxx.png
            found.add((url, local_rel))

    # Заменяем URL в HTML на images/filename
    html_zip = html
    for orig_url, local_rel in found:
        fname = os.path.basename(local_rel)
        html_zip = html_zip.replace(orig_url, f"images/{fname}")

    # Пакуем ZIP
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", html_zip)
        for orig_url, local_rel in found:
            local_abs = os.path.join(project_root, local_rel.lstrip("/"))
            if os.path.exists(local_abs):
                fname = os.path.basename(local_rel)
                zf.write(local_abs, f"images/{fname}")
    buf.seek(0)

    safe_name = re.sub(r'[^\w\-]', '_', p.name or 'site')[:40]
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'},
    )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------
@router.post("/sites/code")
def site_decode_code(body: dict, db: Session = Depends(get_db), user=Depends(optional_user)):
    """Utility: return clean code without markdown. Used internally."""
    content = body.get("content", "")
    for marker in ["```html\n", "```\n", "```html", "```"]:
        if content.startswith(marker):
            content = content[len(marker):]
            if "```" in content:
                content = content.rsplit("```", 1)[0]
            break
    return {"clean": content.strip()}
