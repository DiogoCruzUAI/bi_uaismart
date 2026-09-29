"""Ambiente do Alembic.

A URL do banco vem de `app.core.config`, não do `alembic.ini`: credencial não mora em
arquivo versionado. O import de `app.models` é o que popula `Base.metadata` — sem ele,
o `autogenerate` produz uma migration vazia sem reclamar de nada.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

from app.core.base import Base
from app.core.config import settings
import app.models  # noqa: F401  — registra todas as tabelas em Base.metadata

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _migrar(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Sem isto, mudar o tipo de uma coluna não entra no diff e a migration passa
        # sem a alteração — o modo de falha mais silencioso do autogenerate.
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(_migrar)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
