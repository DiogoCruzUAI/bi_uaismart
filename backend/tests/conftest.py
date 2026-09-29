"""Configuração comum dos testes.

As variáveis de ambiente são definidas **antes** de qualquer import de `app`, porque
`app.core.config` valida no import e derrubaria a coleta do pytest. Os valores são
gerados na hora: nenhum segredo, nem de teste, fica escrito em arquivo.
"""

import os
import secrets

from cryptography.fernet import Fernet

os.environ.setdefault("DB_PASSWORD", "senha-de-teste")
os.environ.setdefault("SECRET_KEY", secrets.token_hex(32))
os.environ.setdefault("CREDENTIALS_KEY", Fernet.generate_key().decode())
os.environ.setdefault("APP_ENV", "development")


import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


@pytest_asyncio.fixture
async def db():
    """Sessão sobre um SQLite em memória, com o esquema dos modelos.

    `create_all` aqui é o caso que o CLAUDE.md permite: teste. Fora de teste, o
    esquema vem do Alembic — e `tests/unit/test_migracao.py` confere que os dois
    dizem a mesma coisa.

    `StaticPool` é obrigatório: sem ele cada conexão do pool abriria um banco em
    memória **próprio**, e a tabela criada numa não existiria na outra.
    """
    from app.core.base import Base
    import app.models  # noqa: F401  — registra as tabelas no metadata

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sessao = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sessao() as s:
        yield s

    await engine.dispose()


@pytest_asyncio.fixture
async def tenant(db):
    from app.models.tenant import Tenant

    t = Tenant(nome="Cliente de Teste", slug="teste")
    db.add(t)
    await db.flush()
    return t


@pytest_asyncio.fixture
async def conexao(db, tenant):
    from app.core.crypto import cifrar
    from app.models.conexao import Conexao, TipoBanco

    c = Conexao(
        tenant_id=tenant.id,
        nome="Produção",
        tipo=TipoBanco.postgres,
        host="db.exemplo.com",
        porta=5432,
        banco="cliente",
        usuario="nextgen_leitor",
        senha_cifrada=cifrar("segredo-de-teste"),
    )
    db.add(c)
    await db.flush()
    return c
