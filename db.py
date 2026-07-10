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

# BE-3: изоляция university/school держится на колонке org_type в каждой
# таблице (так работают все роутеры). Отдельные Postgres-схемы через search_path
# были вторым, несовместимым механизмом: на проде схемы university/school пусты,
# все данные лежат в public, а переключение search_path лишь плодило движки/пулы
# (источник утечки из BE-4). Убрано: get_engine/get_session_for_org теперь всегда
# работают с единым движком (public). org_type фильтруется на уровне запросов.
def get_engine(org_type: str = "university"):
    return engine

def get_session_for_org(org_type: str = "university"):
    return SessionLocal()
