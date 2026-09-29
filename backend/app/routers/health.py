"""Sonda de saúde. É o que o Docker, o Nginx e o monitoramento consultam."""

from fastapi import APIRouter

from app.core.config import settings
from app.core.database import checar_conexao

router = APIRouter(prefix="/api/v1", tags=["saude"])


@router.get("/health")
async def health() -> dict:
    """Responde 200 sempre que o processo está de pé.

    Não checa o banco de propósito: o healthcheck do container decide reiniciar o
    processo, e reiniciar a API não conserta um Postgres fora do ar — só transforma
    uma falha de banco numa cascata de reinícios. Estado de dependência fica em
    `/health/ready`.
    """
    return {"status": "ok", "aplicacao": settings.app_name, "ambiente": settings.app_env}


@router.get("/health/ready")
async def ready() -> dict:
    """Prontidão real: a API consegue falar com o banco de metadados?"""
    banco_ok = await checar_conexao()
    return {"status": "ok" if banco_ok else "degradado", "banco": banco_ok}
