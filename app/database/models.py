from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime

Base = declarative_base()


class ApiKey(Base):
    """API key table"""
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_key = Column(String(64), unique=True, nullable=False, index=True)
    usage = Column(Float, default=0)
    limit_value = Column(Float, default=1000000)
    reqs = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.now)
    last_used = Column(DateTime, nullable=True)
    phone = Column(String(20), nullable=True, unique=True)  # Unique phone number, prevents concurrent registration races
    password_hash = Column(String(255), nullable=True)  # Password hash
    created_at_str = Column(String(20), nullable=True)
    last_used_str = Column(String(20), nullable=True)

    # Relationships
    model_usages = relationship("ModelUsage", back_populates="api_key", cascade="all, delete-orphan")

    def to_dict(self):
        """Convert to a dictionary, compatible with the existing JSON structure"""
        return {
            "usage": self.usage,
            "limit": self.limit_value,
            "reqs": self.reqs,
            "created_at": self.created_at_str or self.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "last_used": self.last_used_str or (self.last_used.strftime("%Y-%m-%d %H:%M:%S") if self.last_used else None),
            "phone": self.phone,
            "model_usage": {mu.model_name: mu.to_dict() for mu in self.model_usages}
        }


class ModelUsage(Base):
    """Model usage statistics table"""
    __tablename__ = "model_usage"
    __table_args__ = (
        UniqueConstraint('api_key_id', 'model_name', name='uq_model_usage_api_key_model'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_key_id = Column(Integer, ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False)
    model_name = Column(String(100), nullable=False)
    requests = Column(Integer, default=0)
    tokens = Column(Float, default=0)

    # Relationships
    api_key = relationship("ApiKey", back_populates="model_usages")

    def to_dict(self):
        """Convert to a dictionary"""
        return {
            "requests": self.requests,
            "tokens": self.tokens
        }


class LLMServer(Base):
    """LLM server configuration table"""
    __tablename__ = "llm_servers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    server_url = Column(String(255), unique=True, nullable=False, index=True)
    device = Column(String(100), nullable=True)
    apikey = Column(Text, nullable=True)

    # Relationships
    models = relationship("ServerModel", back_populates="server", cascade="all, delete-orphan")

    def to_dict(self):
        """Convert to a dictionary, compatible with the existing JSON structure"""
        return {
            "device": self.device,
            "apikey": self.apikey,
            "model": {model.client_model_name: model.to_dict() for model in self.models}
        }


class ServerModel(Base):
    """Server model mapping table"""
    __tablename__ = "server_models"
    __table_args__ = (
        UniqueConstraint('server_id', 'actual_model_name', name='uq_server_model'),
        UniqueConstraint('server_id', 'frontend_model_name', name='uq_server_model_new'),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    server_id = Column(Integer, ForeignKey("llm_servers.id", ondelete="CASCADE"), nullable=False)
    client_model_name = Column(String(100), nullable=False)  # Actual backend model name (legacy field, kept for compatibility)
    actual_model_name = Column(String(100), nullable=False)  # Model name used by the frontend (legacy field, kept for compatibility)
    backend_model_name = Column(String(100), nullable=True)  # Actual backend model name (new field)
    frontend_model_name = Column(String(100), nullable=True)  # Model name used by the frontend (new field)
    reqs = Column(Integer, default=0)
    status = Column(Boolean, default=True)
    input_token_weight = Column(Float, default=1.0)  # Input token weight
    output_token_weight = Column(Float, default=1.0)  # Output token weight

    # Relationships
    server = relationship("LLMServer", back_populates="models")

    def to_dict(self):
        """Convert to a dictionary"""
        # Prefer new fields, fall back to legacy fields when empty
        backend_name = self.backend_model_name or self.client_model_name
        frontend_name = self.frontend_model_name or self.actual_model_name
        
        return {
            "name": backend_name,  # Actual backend model name
            "reqs": self.reqs,
            "status": self.status,
            "input_token_weight": self.input_token_weight,
            "output_token_weight": self.output_token_weight,
            "_frontend_name": frontend_name  # Internal use, kept for compatibility
        }