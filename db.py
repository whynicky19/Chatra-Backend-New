import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./chatra.db")

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

    # SQLite по умолчанию НЕ применяет ON DELETE CASCADE. Включаем построчно,
    # чтобы поведение внешних ключей (dev/тесты на sqlite) совпадало с Postgres.
    @event.listens_for(engine, "connect")
    def _sqlite_fk_pragma(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
else:
    # Sync-эндпоинты FastAPI выполняются в пуле потоков (по умолчанию ~40), и
    # каждый на время запроса держит соединение из этого пула. Дефолтные
    # pool_size=5 + max_overflow=10 = 15 соединений: под нагрузкой лишние потоки
    # висли на checkout до pool_timeout (30 с) — для клиента это «зависание».
    # Поднимаем пул под конкурентность и включаем pre_ping/recycle, чтобы
    # мёртвые соединения (простой, рестарт БД) не отдавались в запрос как ошибка.
    engine = create_engine(
        DATABASE_URL,
        pool_size=int(os.getenv("DB_POOL_SIZE", "20")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
        pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "10")),
        pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),
        pool_pre_ping=True,
    )

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
