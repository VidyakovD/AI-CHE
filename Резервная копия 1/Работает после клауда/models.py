from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from db import Base
from datetime import datetime


class User(Base):
    __tablename__ = "users"

    id               = Column(Integer, primary_key=True, index=True)
    email            = Column(String, unique=True, index=True, nullable=False)
    password_hash    = Column(String, nullable=False)
    name             = Column(String, nullable=True)
    avatar_url       = Column(String, nullable=True)
    tokens_balance   = Column(Integer, default=0)
    is_active        = Column(Boolean, default=True)
    is_verified      = Column(Boolean, default=False)       # email verified
    agreed_to_terms  = Column(Boolean, default=False)
    referral_code    = Column(String, unique=True, nullable=True)
    referred_by      = Column(String, nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)

    messages      = relationship("Message",      back_populates="user")
    subscriptions = relationship("Subscription", back_populates="user")
    transactions  = relationship("Transaction",  back_populates="user")
    verify_tokens = relationship("VerifyToken",  back_populates="user")


class VerifyToken(Base):
    """Email verification & password-reset tokens."""
    __tablename__ = "verify_tokens"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    token      = Column(String, unique=True, index=True, nullable=False)
    purpose    = Column(String, nullable=False)   # "verify_email" | "reset_password"
    used       = Column(Boolean, default=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="verify_tokens")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id                 = Column(Integer, primary_key=True, index=True)
    user_id            = Column(Integer, ForeignKey("users.id"), nullable=False)
    plan               = Column(String, nullable=False)
    tokens_total       = Column(Integer, nullable=False)
    tokens_used        = Column(Integer, default=0)
    price_rub          = Column(Float, nullable=False)
    status             = Column(String, default="active")   # active / expired / cancelled
    yookassa_payment_id= Column(String, nullable=True)
    started_at         = Column(DateTime, default=datetime.utcnow)
    expires_at         = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="subscriptions")


class Transaction(Base):
    __tablename__ = "transactions"

    id                  = Column(Integer, primary_key=True, index=True)
    user_id             = Column(Integer, ForeignKey("users.id"), nullable=False)
    type                = Column(String, nullable=False)      # payment / usage / refund / bonus
    amount_rub          = Column(Float, nullable=True)
    tokens_delta        = Column(Integer, nullable=False)
    description         = Column(String, nullable=True)
    model               = Column(String, nullable=True)
    yookassa_payment_id = Column(String, nullable=True)
    created_at          = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="transactions")


class Message(Base):
    __tablename__ = "messages"

    id          = Column(Integer, primary_key=True, index=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=True)
    chat_id     = Column(String, index=True)
    role        = Column(String)
    content     = Column(Text)
    model       = Column(String)
    title       = Column(String, nullable=True)
    tokens_used = Column(Integer, default=0)
    created_at  = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="messages")
