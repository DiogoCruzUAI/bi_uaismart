"""Conexão com o banco de metadados da plataforma.

Só metadados: tenants, usuários, conexões, dicionário e chat. O dado analítico do
cliente nunca passa por aqui — ele fica no banco do cliente e é lido sob demanda
pelos conectores (`app.connectors`), com usuário somente-leitura.
"""

from collections.abc import AsyncGenerator

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.base import Base
from app.core.config import settings

logger = structlog.get_logger()

engine = create_async_engine(
    settings.database_url,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=settings.app_debug,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def checar_conexao() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error("falha_conexao_banco", erro=str(e))
        return False


__all__ = ["Base", "engine", "AsyncSessionLocal", "get_db", "checar_conexao"]
