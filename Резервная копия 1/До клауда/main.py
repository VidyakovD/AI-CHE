from fastapi import FastAPI, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session
from db import SessionLocal, engine
import models
import os
from dotenv import load_dotenv
from openai import OpenAI
from ai import generate_response
from uuid import uuid4
from fastapi.middleware.cors import CORSMiddleware
import shutil
import uuid

# INIT
load_dotenv()

api_key = os.getenv("OPENAI_API_KEYS")
if not api_key:
    raise ValueError("OPENAI_API_KEYS not set")

models.Base.metadata.create_all(bind=engine)

app = FastAPI()
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # потом ограничим
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# SCHEMA
class MessageRequest(BaseModel):
    chat_id: str
    message: str
    model: str

# CREATE CHAT
class CreateChatRequest(BaseModel):
    model: str


@app.post("/chat/create")
def create_chat(req: CreateChatRequest):
    return {
        "chat_id": str(uuid4()),
        "model": req.model
    }

# GET CHAT HISTORY
@app.get("/chat/{chat_id}")
def get_chat(chat_id: str):
    db: Session = SessionLocal()
    try:
        messages = db.query(models.Message)\
            .filter_by(chat_id=chat_id)\
            .order_by(models.Message.id)\
            .all()

        if not messages:
            return []

        return [{"role": m.role, "content": m.content} for m in messages]
    finally:
        db.close()

from pydantic import BaseModel

class RenameRequest(BaseModel):
    chat_id: str
    title: str


@app.post("/chat/rename")
def rename_chat(req: RenameRequest):
    db = SessionLocal()

    msg = db.query(models.Message).filter_by(chat_id=req.chat_id).first()

    if not msg:
        return {"error": "chat not found"}

    msg.title = req.title
    db.commit()

    return {"status": "ok"}

# SEND MESSAGE
@app.post("/message")
def send_message(req: MessageRequest):
    db: Session = SessionLocal()

    try:
        existing = db.query(models.Message).filter_by(chat_id=req.chat_id).first()

        title = None
        if not existing:
            title = req.message[:40]

        user_msg = models.Message(
            chat_id=req.chat_id,
            role="user",
            content=req.message,
            model=req.model,
            title=title
        )
        db.add(user_msg)
        db.commit()

        messages = db.query(models.Message)\
            .filter_by(chat_id=req.chat_id)\
            .order_by(models.Message.id)\
            .all()

        messages = messages[-20:]

        formatted = [
            {"role": "system", "content": "Ты полезный AI ассистент."}
        ] + [{"role": m.role, "content": m.content} for m in messages]

        try:
            answer = generate_response(req.model, formatted)
        except Exception as e:
            print("AI ERROR:", e)
            return {"error": str(e)}

        ai_msg = models.Message(
            chat_id=req.chat_id,
            role="assistant",
            content=answer if isinstance(answer, str) else answer.get("content", ""),
            model=req.model
        )
        db.add(ai_msg)
        db.commit()

        return {
            "response": {
                "type": "text",
                "content": answer if isinstance(answer, str) else answer.get("content", "")
            }
        }

    finally:
        db.close()

@app.get("/chats/{model}")
def get_chats(model: str):
    db: Session = SessionLocal()
    try:
        chats = db.query(
            models.Message.chat_id,
            models.Message.title
        )
        if model == "gpt":
            chats = db.query(
                models.Message.chat_id,
                models.Message.title
            ).filter(models.Message.model.in_(["gpt", "gpt-4o-mini"])).all()
        else:
            chats = db.query(
                models.Message.chat_id,
                models.Message.title
            ).filter_by(model=model).all()

        result = {}

        for chat_id, title in chats:
            if chat_id not in result:
                result[chat_id] = title or "Новый чат"

        return [{"id": k, "title": v} for k, v in result.items()]

    finally:
        db.close()

@app.delete("/chat/{chat_id}")
def delete_chat(chat_id: str):
    db = SessionLocal()

    try:
        messages = db.query(models.Message).filter_by(chat_id=chat_id).all()

        if not messages:
            raise HTTPException(status_code=404, detail="Chat not found")

        for msg in messages:
            db.delete(msg)

        db.commit()

        return {"status": "deleted"}

    finally:
        db.close()
@app.post("/upload")
def upload_file(file: UploadFile = File(...)):
    file_id = str(uuid.uuid4())
    file_path = f"{UPLOAD_DIR}/{file_id}_{file.filename}"

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    return {
        "url": f"/uploads/{file_id}_{file.filename}"
    }
from fastapi.staticfiles import StaticFiles

app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")