from sqlalchemy import Column, Integer, String, Text
from db import Base

class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(String, index=True)
    role = Column(String)  # user / assistant
    content = Column(Text)
    model = Column(String)
    title = Column(String, nullable=True)