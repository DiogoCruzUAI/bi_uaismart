"""Redis: blacklist de JWT e cache de resultado.

O cache não é otimização de conforto — é parte da tese de custo. Pergunta repetida
não deve custar nem chamada de LLM nem varredura no banco do cliente.

`REDIS_ENABLED=false` permite rodar em dev sem Redis. Nesse modo a blacklist não
funciona, então logout não invalida token antes da expiração — aceitável em
desenvolvimento, bloqueado em produção pelo validador de `config.py`.
"""

import structlog
from redis.asyncio import Redis

from app.core.config import settings

logger = structlog.get_logger()

_redis: Redis | None = None


def get_redis() -> Redis | None:
    global _redis
    if not settings.redis_enabled:
        return None
    if _redis is None:
        _redis = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def revogar_token(jti: str, ttl_segundos: int) -> None:
    """Marca um token como revogado até sua expiração natural.

    O TTL é o tempo que falta para o token expirar sozinho: depois disso a entrada
    é inútil e o Redis a remove. A blacklist não cresce sem limite.
    """
    r = get_redis()
    if r is None:
        return
    await r.setex(f"jwt:revogado:{jti}", max(ttl_segundos, 1), "1")


async def token_revogado(jti: str) -> bool:
    r = get_redis()
    if r is None:
        return False
    try:
        return await r.exists(f"jwt:revogado:{jti}") > 0
    except Exception as e:
        # Redis fora do ar não pode virar "todo token é válido". Na dúvida, recusa.
        logger.error("redis_indisponivel_na_checagem_de_revogacao", erro=str(e))
        return True


async def fechar_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
