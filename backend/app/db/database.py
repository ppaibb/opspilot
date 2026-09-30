"""数据库连接声明及基础模型基类。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import settings


def build_engine_kwargs(database_url: str) -> dict[str, Any]:
    """按数据库类型构造 SQLAlchemy Engine 参数。

    Args:
        database_url: SQLAlchemy 数据库连接串。

    Returns:
        可直接传给 ``create_engine`` 的参数字典。
    """
    backend_name = make_url(database_url).get_backend_name()
    if backend_name == "sqlite":
        # SQLite 仍用于本地开发和演示。FastAPI 后台任务会跨线程访问连接，
        # 因此需要关闭 SQLite 默认的同线程检查。
        return {"connect_args": {"check_same_thread": False}}

    # PostgreSQL 等生产数据库使用连接池健康检查，避免复用被服务端断开的连接。
    return {
        "pool_pre_ping": True,
        "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_recycle": settings.DB_POOL_RECYCLE_SECONDS,
    }


engine = create_engine(settings.DATABASE_URL, **build_engine_kwargs(settings.DATABASE_URL))

# 构建跨文件的独立会话对象工厂
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 构建基础模型类，后续所有 DB Schema 均继承自此类
Base = declarative_base()

def get_db():
    """
    依赖注入: FastAPI 的 Depends() 的依赖函数。
    为每个请求生成独立的数据库 Session，请求结束自动释放。
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
