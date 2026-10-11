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
    rmq_depth = Column(Integer, nullable=True)

class ExperimentResult(Base):
    __tablename__ = 'experiment_result'
    
    id = Column(Integer, primary_key=True, index=True)
    intent_id = Column(Integer, nullable=False)
    kill_switch_fired = Column(Boolean, default=False)
    recovery_time_sec = Column(Float, nullable=True)
    resilience_score = Column(Float, nullable=True)
    target_service = Column(String, nullable=True)
    details = Column(String, nullable=True)


def ensure_columns(engine) -> list:
    """create_all() never adds columns to tables that already exist, so a chaos_state.db created before a model
    gained a column fails with "no such column". Add any missing *nullable* columns (SQLite ADD COLUMN); nothing is
    ever dropped or changed. Returns the columns added."""
    from sqlalchemy import inspect, text

    added = []
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have or col.primary_key or not col.nullable:
                    continue
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" '
                                  f'{col.type.compile(dialect=engine.dialect)}'))
                added.append(f"{table.name}.{col.name}")
    return added
