import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./chatra.db")

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Изоляция university/school — по колонке org_type, а не по схемам Postgres:
# отдельные схемы плодили движки и пулы (BE-3/BE-4). Движок всегда один.
def get_engine(org_type: str = "university"):
    return engine

def get_session_for_org(org_type: str = "university"):
    return SessionLocal()
