from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base
import mapper

app = FastAPI(title="AI Chaos Engine Controller")

# SQLite Database Setup
SQLALCHEMY_DATABASE_URL = "sqlite:///./chaos_state.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create tables
Base.metadata.create_all(bind=engine)

@app.get("/")
def read_root():
    return {"status": "Controller is running", "role": "M2"}

@app.post("/api/v1/map")
def build_map():
    """Triggers the Dependency Mapper to build graph.json"""
    # Assuming M1 puts the compose file in the parent sandbox dir
    graph = mapper.generate_dependency_graph("../sandbox/docker-compose.yaml")
    return {"message": "Graph generated", "graph": graph}

@app.post("/api/v1/plan")
def generate_candidates():
    """Heuristic logic to generate failure candidates based on the graph"""
    # TODO: Implement centrality scoring and optional LLM re-ranking
    return {"candidates": []}

@app.post("/api/v1/inject")
def inject_fault():
    """Triggered by CLI to start a chaos experiment"""
    # TODO: Forward to M3's execution modules
    pass
