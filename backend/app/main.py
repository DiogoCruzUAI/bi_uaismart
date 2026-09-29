"""Ponto de entrada da API do NextGen BI."""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import checar_conexao, engine
from app.core.redis_client import fechar_redis
from app.routers import auth, chat, conexoes, health

logger = structlog.get_logger()


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    logger.info("iniciando", aplicacao=settings.app_name, ambiente=settings.app_env)
    if not await checar_conexao():
        # Aviso, não morte: o container pode subir antes do Postgres ficar pronto e
        # o healthcheck do compose já trata isso. Morrer aqui vira laço de reinício.
        logger.warning("banco_de_metadados_indisponivel_no_startup")
    yield
    await fechar_redis()
    await engine.dispose()
    logger.info("encerrado")


app = FastAPI(
    title=settings.app_name,
    description="Plataforma de BI com perguntas em linguagem natural.",
    version="0.1.0",
    lifespan=ciclo_de_vida,
    # Documentação interativa fica fora de produção: ela expõe o formato de toda
    # rota e é reconhecimento gratuito para quem estiver procurando.
    docs_url=None if settings.app_env == "production" else "/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    # Lista explícita, nunca "*": com credenciais, "*" permitiria a qualquer site
    # fazer requisição autenticada em nome do usuário logado.
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(conexoes.router)
app.include_router(chat.router)
