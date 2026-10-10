from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()

class ExperimentIntent(Base):
    __tablename__ = 'experiment_intent'
    
    id = Column(Integer, primary_key=True, index=True)
    fault_type = Column(String, nullable=False)      # e.g., 'container_crash', 'network_latency'
    target_service = Column(String, nullable=False)  # e.g., 'cart'
    fault_params = Column(String, nullable=True)     # JSON string, e.g., '{"latency_ms": 200}'
    rollback_command = Column(String, nullable=False)
    status = Column(String, default="pending")       # pending, running, success, aborted
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)

class MetricsSnapshot(Base):
    __tablename__ = 'metrics_snapshot'
    
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    error_rate = Column(Float, nullable=False)
    p95_latency = Column(Float, nullable=False)
    is_baseline = Column(Boolean, default=False)

class ExperimentResult(Base):
    __tablename__ = 'experiment_result'
    
    id = Column(Integer, primary_key=True, index=True)
    intent_id = Column(Integer, nullable=False)
    kill_switch_fired = Column(Boolean, default=False)
    recovery_time_sec = Column(Float, nullable=True)
    resilience_score = Column(Float, nullable=True)  # Normalized 0-1
