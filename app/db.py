"""PostgreSQL 持久化：扫描数组、锚点与结果全部落库，重启后可继续工作。"""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import JSON, String, create_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    sessionmaker,
    Session,
)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://lcs:lcs@db:5432/lcs"
)


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # PostgreSQL 原生 JSON 列；存为 JSON 字符串数组/索引对数组。
    left_data: Mapped[list] = mapped_column("left_data", JSON, nullable=False)
    right_data: Mapped[list] = mapped_column("right_data", JSON, nullable=False)
    anchors: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    result: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """建表（幂等）；容器启动时调用。"""
    Base.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
