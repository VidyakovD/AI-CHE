"""Site project endpoints — extracted from main.py."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
import json
import os
import logging

from server.routes.deps import get_db, current_user, optional_user
from server.models import SiteProject, User, Transaction, ChatBot
from server.ai import generate_response
from server.billing import deduct_strict

log = logging.getLogger(__name__)

router = APIRouter(tags=["sites"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Цены в КОПЕЙКАХ (1 ₽ = 100 коп)
SPEC_CONVERSATION_CH_COST = 0    # бесплатное обсуждение ТЗ — заложено в фикс цену сайта
SITE_CREATE_FIX_COST    = 150_000  # 1500 ₽ за создание сайта (фикс)
CODE_GEN_CH_COST        = SITE_CREATE_FIX_COST  # legacy alias — первая генерация = фикс
CODE_ITER_CH_COST       = 500    # доработки 5 ₽ за итерацию (по факту изменения)

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
        price = 0  # обсуждение/анализ ТЗ — бесплатное (заложено в фикс цену)
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
        "attached_bot_id": p.attached_bot_id,
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

    # Обсуждение ТЗ бесплатно — стоимость заложена в фикс-цену генерации сайта.
    # Если в будущем нужно ограничивать злоупотребления — добавить rate-limit
    # или мин. баланс 1500 ₽ (стоимость самой генерации).
    cost = SPEC_CONVERSATION_CH_COST  # = 0 в новой модели
    if cost > 0:
        if not deduct_strict(db, user.id, cost):
            raise HTTPException(402, "Недостаточно средств")
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


class AttachBotBody(BaseModel):
    bot_id: int | None = None  # None — отвязать бота от сайта


@router.post("/sites/projects/{project_id}/attach-bot")
def site_project_attach_bot(project_id: int, body: AttachBotBody,
                            db: Session = Depends(get_db),
                            user: User = Depends(current_user)):
    """Привязать чат-бот юзера к сайту. При генерации/публикации виджет
    бота вставится в HTML автоматически."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if body.bot_id is None:
        p.attached_bot_id = None
        db.commit()
        return {"status": "detached"}
    bot = db.query(ChatBot).filter_by(id=body.bot_id, user_id=user.id).first()
    if not bot:
        raise HTTPException(404, "Бот не найден или не ваш")
    if not bot.widget_enabled:
        raise HTTPException(400, "У бота не включён виджет — включите в /chatbots.html")
    p.attached_bot_id = bot.id
    db.commit()
    return {"status": "attached", "bot_id": bot.id, "bot_name": bot.name}


def _inject_chatbot_widget(html: str, bot_id: int, app_url: str) -> str:
    """Вставляет <script src='/widget/{bot_id}.js'></script> перед </body>."""
    widget_tag = f'<script src="{app_url}/widget/{bot_id}.js" async></script>'
    if "</body>" in html:
        return html.replace("</body>", f"{widget_tag}\n</body>", 1)
    return html + "\n" + widget_tag


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

    cost = CODE_GEN_CH_COST  # 1500 ₽ фикс
    if not deduct_strict(db, user.id, cost):
        raise HTTPException(402, f"Недостаточно средств (нужно {cost/100:.0f} ₽)")
    p.price_tokens += cost
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost,
                       description=f"Создание сайта ({cost/100:.0f} ₽)"))

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
        f"Ты — опытный веб-разработчик. Создай полный HTML-код одностраничного сайта.\n\n"
        f"⚠️ ВАЖНО: строго следуй ТЗ ниже. Тематика, отрасль, продукт, целевая аудитория, "
        f"названия блоков и разделов — берутся ТОЛЬКО из ТЗ. Не придумывай шаблонные тексты "
        f"про рестораны, кофейни, меню если этого нет в ТЗ. Все заголовки, тексты, призывы — "
        f"строго по теме ТЗ.\n\n"
        f"=== ТЗ ===\n{p.spec_text}\n=== КОНЕЦ ТЗ ===\n"
        f"{img_context}\n"
        f"Технические требования:\n"
        f"- Чистый, современный адаптивный HTML+CSS (mobile-first)\n"
        f"- Без внешних фреймворков (только inline CSS или <style>)\n"
        f"- Семантичная разметка, доступность (alt у картинок, aria-label у иконок)\n"
        f"- Красивый современный дизайн под тематику ТЗ\n"
        f"- Тексты — строго по теме из ТЗ, на русском, без английского lorem ipsum\n"
        f"- Картинки: используй ТОЛЬКО URL из списка выше (если он есть). Иначе — CSS-плейсхолдеры\n"
        f"- Ответ: ТОЛЬКО HTML-код, без markdown-обёрток и объяснений\n"
    )

    p.conversation_phase = "generating_code"
    db.commit()

    # max_tokens=16000 — большие лендинги не влезают в дефолтные 8192
    answer = generate_response("claude", [{"role": "user", "content": prompt}],
                               extra={"max_tokens": 16000})
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

    # Auto-continue: если HTML обрезан, передаём Claude полный контекст
    # (ТЗ + уже сгенерированную часть как assistant turn) и просим продолжить.
    # Без ТЗ модель додумывала тематику с потолка (бывало что в lasting-tail
    # были общие слова → выходил «лендинг ресторана» вместо промышленных труб).
    for attempt in range(2):
        if "</html>" in content.lower():
            break
        log.info(f"[Sites] HTML усечён ({len(content)} симв), продолжение #{attempt+1}")
        cont_messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": content},
            {"role": "user", "content": (
                "Ты не закончил — ответ обрезался. Продолжи строго с того места "
                "где остановился (не повторяй уже написанное), до закрывающего "
                "</html>. Не меняй тематику, следуй ТЗ выше. Ответ — только "
                "продолжение HTML, без markdown и объяснений."
            )},
        ]
        cont = generate_response("claude", cont_messages, extra={"max_tokens": 16000})
        cont_text = cont.get("content", "") if isinstance(cont, dict) else ""
        for marker in ["```html\n", "```\n", "```html", "```"]:
            if cont_text.startswith(marker):
                cont_text = cont_text[len(marker):]
                cont_text = cont_text.rsplit("```", 1)[0] if "```" in cont_text else cont_text
                break
        if not cont_text.strip():
            break
        content += cont_text

    # Если всё равно нет </html> — добавим закрывающие теги вручную
    if "</html>" not in content.lower():
        log.warning(f"[Sites] HTML так и не закрылся после 2 попыток, добавляем теги")
        if "</body>" not in content.lower():
            content += "\n</body>"
        content += "\n</html>"

    p.code_html = content
    p.conversation_phase = "done"
    p.status = "done"
    db.commit()
    return {"code_html": content, "status": p.status, "phase": p.conversation_phase}


@router.post("/sites/projects/{project_id}/repair-code")
def site_project_repair_code(project_id: int, db: Session = Depends(get_db),
                             user: User = Depends(current_user)):
    """Бесплатно дописывает обрезанный HTML до закрывающего </html>.
    Используется когда генерация прошла, но Claude не успел дописать."""
    p = db.query(SiteProject).filter_by(id=project_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Проект не найден")
    if not p.code_html:
        raise HTTPException(400, "Сначала сгенерируйте код")
    if "</html>" in p.code_html.lower():
        return {"status": "ok", "note": "уже закрыт", "code_html": p.code_html}

    content = p.code_html
    base_prompt = (
        f"ТЗ сайта:\n\n=== ТЗ ===\n{p.spec_text or '(нет)'}\n=== КОНЕЦ ТЗ ===\n\n"
        "Сгенерируй полный HTML по этому ТЗ."
    )
    for attempt in range(2):
        if "</html>" in content.lower():
            break
        cont_messages = [
            {"role": "user", "content": base_prompt},
            {"role": "assistant", "content": content},
            {"role": "user", "content": (
                "Ответ обрезался. Продолжи строго с того места, до </html>. "
                "Не меняй тематику, следуй ТЗ. Только HTML."
            )},
        ]
        cont = generate_response("claude", cont_messages, extra={"max_tokens": 16000})
        ctxt = cont.get("content", "") if isinstance(cont, dict) else ""
        for marker in ["```html\n", "```\n", "```html", "```"]:
            if ctxt.startswith(marker):
                ctxt = ctxt[len(marker):]
                ctxt = ctxt.rsplit("```", 1)[0] if "```" in ctxt else ctxt
                break
        if not ctxt.strip():
            break
        content += ctxt
    if "</html>" not in content.lower():
        if "</body>" not in content.lower():
            content += "\n</body>"
        content += "\n</html>"
    p.code_html = content
    db.commit()
    return {"status": "ok", "code_html": content}


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

    cost = CODE_ITER_CH_COST  # 5 ₽ за правку
    if not deduct_strict(db, user.id, cost):
        raise HTTPException(402, f"Недостаточно средств (нужно {cost/100:.0f} ₽)")
    p.price_tokens += cost
    db.add(Transaction(user_id=user.id, type="usage", tokens_delta=-cost,
                       description=f"Правка сайта ({cost/100:.0f} ₽)"))

    prompt = (
        f"Вот текущий HTML сайта:\n\n{p.code_html}\n\n"
        f"Пользователь просит: {instructions}\n"
        f"Верни ТОЛЬКО обновлённый полный HTML-код целиком, от <!DOCTYPE до </html>. "
        f"Без markdown-обёрток, без объяснений, без сокращений."
    )

    answer = generate_response("claude", [{"role": "user", "content": prompt}],
                               extra={"max_tokens": 16000})
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

    # Auto-continue с полным контекстом (тз + уже написанное)
    for attempt in range(2):
        if "</html>" in content.lower():
            break
        cont_messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": content},
            {"role": "user", "content": (
                "Ответ обрезался. Продолжи строго с того места где остановился, "
                "до </html>. Не меняй тематику. Только HTML, без объяснений."
            )},
        ]
        cont = generate_response("claude", cont_messages, extra={"max_tokens": 16000})
        cont_text = cont.get("content", "") if isinstance(cont, dict) else ""
        for marker in ["```html\n", "```\n", "```html", "```"]:
            if cont_text.startswith(marker):
                cont_text = cont_text[len(marker):]
                cont_text = cont_text.rsplit("```", 1)[0] if "```" in cont_text else cont_text
                break
        if not cont_text.strip():
            break
        content += cont_text
    if "</html>" not in content.lower():
        if "</body>" not in content.lower():
            content += "\n</body>"
        content += "\n</html>"

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
    final_html = p.code_html
    if p.attached_bot_id:
        from server.models import ChatBot
        b = db.query(ChatBot).filter_by(id=p.attached_bot_id, user_id=user.id).first()
        if b and b.widget_enabled:
            app_url = os.getenv("APP_URL", "https://aiche.ru").rstrip("/")
            final_html = _inject_chatbot_widget(final_html, b.id, app_url)
    with open(os.path.join(host_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(final_html)

    p.hosted_path = f"/sites/hosted/{project_id}/"
    db.commit()
    return {"url": p.hosted_path, "status": "hosted",
            "widget_attached": bool(p.attached_bot_id)}


@router.get("/sites/hosted/{project_id}/{full_path:path}")
def site_project_serve(project_id: int, full_path: str = ""):
    """Serve hosted site files.

    HTML отдаётся ЧЕРЕЗ SANDBOX IFRAME с null-origin:
      - sandbox="allow-scripts allow-forms allow-popups" — JS работает
      - но origin=null → document.cookie/localStorage основного домена НЕДОСТУПНЫ
      - strict CSP на обёртке как defense-in-depth
      - даже если AI сгенерировал XSS-вектор (`<img onerror=fetch(...)>`) —
        украсть токен пользователя не получится, т.к. sandbox в другом origin
    Path traversal: через Path.resolve() + relative_to (raises на `..` и symlinks).
    """
    from pathlib import Path
    from fastapi.responses import HTMLResponse as _HTMLResponse
    host_dir = Path(_sites_host_base, str(project_id)).resolve()
    try:
        file_path = (host_dir / (full_path or "index.html")).resolve()
        file_path.relative_to(host_dir)
    except (ValueError, OSError):
        raise HTTPException(403, "Доступ запрещён")
    if not file_path.is_file():
        raise HTTPException(404, "Файл не найден")
    ext = file_path.suffix.lower()
    # HTML — в sandbox iframe. Остальные (картинки/css) — напрямую.
    if ext in (".html", ".htm", ""):
        try:
            inner_html = file_path.read_text(encoding="utf-8")
        except Exception:
            raise HTTPException(500, "Не удалось прочитать файл")
        # Экранируем для вставки в srcdoc: кавычки и амперсанды
        escaped = (inner_html
                   .replace("&", "&amp;")
                   .replace('"', "&quot;"))
        wrapper = (
            '<!doctype html><html lang="ru"><head>'
            '<meta charset="utf-8"/>'
            '<title>Site</title>'
            '<style>html,body,iframe{margin:0;padding:0;border:0;width:100%;height:100vh;background:#fff}</style>'
            '</head><body>'
            '<iframe sandbox="allow-scripts allow-forms allow-popups" '
            'referrerpolicy="no-referrer" '
            f'srcdoc="{escaped}"></iframe>'
            '</body></html>'
        )
        return _HTMLResponse(wrapper, headers={
            # На ОБЁРТКЕ — strict CSP (никакого JS, только style и frame).
            # Пользовательский HTML запускается внутри iframe с null origin.
            "Content-Security-Policy": (
                "default-src 'none'; "
                "style-src 'unsafe-inline'; "
                "frame-src data: blob:; "
                "child-src data: blob:; "
                "frame-ancestors 'self'"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        })
    # Картинки, css, прочие ассеты — как есть
    return FileResponse(str(file_path))


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
