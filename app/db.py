"""PostgreSQL 持久化：扫描数组、锚点与结果全部落库，重启后可继续工作。

并发裁决依据见 ``version`` 列：每次成功替换锚点都在同一条条件 UPDATE 内
原子地递增版本并刷新 ``updated_at``；PUT 请求携带期望版本时，只有版本仍
匹配才会写入（compare-and-set），否则整句不命中、状态分毫不改。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    String,
    create_engine,
    text,
)
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

# 首次成功写入时的版本号；每次成功替换递增 1。
INITIAL_VERSION = 1


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    # 乐观锁版本：随每次成功的锚点替换在条件 UPDATE 内原子递增。
    version: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=INITIAL_VERSION,
        server_default=text(f"{INITIAL_VERSION}"),
    )
    # 更新时间只在成功替换时刷新；失败/过期请求不得改变它。
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=text("now()"),
    )


engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """建表（幂等）；容器启动时调用。

    早期库可能缺少 ``version`` / ``updated_at`` 列（命名卷 pgdata 会保留
    旧结构），这里幂等地补齐并回填，保证升级后服务与既有数据继续工作。
    """
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        add_columns = (
            ("version", f"BIGINT NOT NULL DEFAULT {INITIAL_VERSION}"),
            ("updated_at", "TIMESTAMPTZ NOT NULL DEFAULT now()"),
        )
        for column, ddl_type in add_columns:
            exists = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'jobs' AND column_name = :name"
                ),
                {"name": column},
            ).scalar()
            if not exists:
                conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {column} {ddl_type}"))


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
