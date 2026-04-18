"""
База знаний бота:
- add_file: извлекаем текст → AI строит description/tags/summary/facts → сохраняем
- search_file: поиск по name/description/tags (для отправки файла)
- search_kb: полнотекстовый поиск по summary+facts+content (для RAG-ответа)
"""
import os, re, logging
from typing import List, Dict

from server.db import SessionLocal
from server.models import KnowledgeFile

log = logging.getLogger("kb")


def _score(query: str, text: str) -> float:
    """Простой TF-подобный скоринг."""
    if not text: return 0.0
    q = query.lower()
    words = [w for w in re.split(r"\W+", q) if len(w) > 2]
    text_l = text.lower()
    score = 0.0
    for w in words:
        score += text_l.count(w) * (2 if len(w) > 5 else 1)
    if q in text_l:
        score += 10  # точное вхождение
    return score


def add_file(bot_id: int, name: str, path: str, mime: str = None,
             size: int = 0, content_text: str = "",
             model: str = "gpt-4o-mini") -> Dict:
    """Индексирует файл: AI строит description/tags/summary/facts → сохраняет в БД."""
    from server.ai import generate_response

    preview = (content_text or "")[:8000]
    if preview:
        prompt = (
            f"Файл: {name}\n\nСодержимое (начало):\n{preview}\n\n"
            "Сформируй метаданные файла строго в формате:\n"
            "ОПИСАНИЕ: одна строка 10-15 слов\n"
            "ТЕГИ: 3-5 через запятую\n"
            "SUMMARY: 2-4 предложения о содержимом\n"
            "FACTS: 5-15 ключевых фактов через '; '\n"
        )
        try:
            result = generate_response(model, [
                {"role": "system", "content": "Ты индексатор файлов. Отвечай строго по формату."},
                {"role": "user", "content": prompt},
            ])
            raw = result.get("content", "") if isinstance(result, dict) else str(result)
        except Exception as e:
            log.error(f"[KB] indexing error: {e}")
            raw = ""
    else:
        raw = ""

    def extract(label):
        m = re.search(rf"{label}:\s*(.+?)(?=\n[A-ZА-Я]+:|\Z)", raw, re.DOTALL)
        return m.group(1).strip() if m else ""

    description = extract("ОПИСАНИЕ") or name
    tags = extract("ТЕГИ")
    summary = extract("SUMMARY")
    facts = extract("FACTS")

    db = SessionLocal()
    try:
        kf = KnowledgeFile(
            bot_id=bot_id, name=name, path=path, mime=mime, size=size,
            description=description[:500], tags=tags[:500],
            summary=summary[:2000], facts=facts[:3000],
            content_text=(content_text or "")[:50000],
        )
        db.add(kf); db.commit(); db.refresh(kf)
        return {
            "id": kf.id, "name": kf.name, "path": kf.path,
            "description": kf.description, "tags": kf.tags,
            "summary": kf.summary, "facts": kf.facts,
        }
    finally:
        db.close()


def search_file(bot_id: int, query: str, top: int = 5) -> List[Dict]:
    """Поиск файла по name/description/tags — для отправки документа."""
    db = SessionLocal()
    try:
        rows = db.query(KnowledgeFile).filter_by(bot_id=bot_id).all()
    finally:
        db.close()
    scored = []
    for r in rows:
        text = " ".join(filter(None, [r.name, r.description, r.tags]))
        s = _score(query, text)
        if s > 0:
            scored.append((s, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{
        "id": r.id, "name": r.name, "path": r.path,
        "description": r.description or "", "score": s,
    } for s, r in scored[:top]]


def search_kb(bot_id: int, query: str, top: int = 5) -> List[Dict]:
    """Полнотекстовый поиск по summary+facts+content — для RAG-ответа."""
    db = SessionLocal()
    try:
        rows = db.query(KnowledgeFile).filter_by(bot_id=bot_id).all()
    finally:
        db.close()
    scored = []
    for r in rows:
        text = " ".join(filter(None, [
            r.name, r.description, r.tags,
            r.summary, r.facts, r.content_text or "",
        ]))
        s = _score(query, text)
        if s > 0:
            scored.append((s, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{
        "id": r.id, "name": r.name,
        "description": r.description or "",
        "summary": r.summary or "",
        "facts": r.facts or "",
        "score": s,
    } for s, r in scored[:top]]


def get_all_files(bot_id: int) -> List[Dict]:
    db = SessionLocal()
    try:
        rows = db.query(KnowledgeFile).filter_by(bot_id=bot_id)\
                .order_by(KnowledgeFile.created_at.desc()).all()
    finally:
        db.close()
    return [{
        "id": r.id, "name": r.name, "path": r.path,
        "description": r.description or "", "tags": r.tags or "",
        "size": r.size, "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]
