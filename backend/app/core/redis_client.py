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


# ─── Bloqueio por tentativas de login ─────────────────────────────────────────

# Dez tentativas erradas em quinze minutos bloqueiam a conta por quinze minutos.
# Números escolhidos para não atrapalhar quem esqueceu a senha (três, quatro
# tentativas são normais) e tornar inviável percorrer uma lista de senhas vazadas.
MAX_TENTATIVAS = 10
JANELA_SEGUNDOS = 900


def _chave_tentativas(tenant: str, email: str) -> str:
    """Identifica a conta sem guardar o e-mail.

    O Redis não é o lugar de um cadastro de e-mails de clientes: hash trunca o dado
    pessoal e continua servindo para contar tentativas.
    """
    import hashlib

    digest = hashlib.sha256(f"{tenant}:{email}".lower().encode()).hexdigest()[:32]
    return f"login:falhas:{digest}"


async def login_bloqueado(tenant: str, email: str) -> bool:
    r = get_redis()
    if r is None:
        return False
    try:
        valor = await r.get(_chave_tentativas(tenant, email))
    except Exception as e:
        # Redis fora do ar não pode virar "bloqueia todo mundo": o login é a porta
        # de entrada da plataforma inteira. Aqui a falha abre, e é uma escolha —
        # a alternativa é uma indisponibilidade total por causa do cache.
        logger.error("redis_indisponivel_no_bloqueio_de_login", erro=str(e))
        return False
    return valor is not None and int(valor) >= MAX_TENTATIVAS


async def registrar_falha_de_login(tenant: str, email: str) -> None:
    r = get_redis()
    if r is None:
        return
    chave = _chave_tentativas(tenant, email)
    try:
        # A janela conta a partir da primeira falha e não é estendida pelas
        # seguintes: quem errou dez vezes espera quinze minutos, não quinze minutos
        # depois da última tentativa — o que deixaria a conta presa indefinidamente
        # sob um ataque contínuo.
        atual = await r.incr(chave)
        if atual == 1:
            await r.expire(chave, JANELA_SEGUNDOS)
    except Exception as e:
        logger.error("falha_ao_registrar_tentativa_de_login", erro=str(e))


async def limpar_falhas_de_login(tenant: str, email: str) -> None:
    r = get_redis()
    if r is None:
        return
    try:
        await r.delete(_chave_tentativas(tenant, email))
    except Exception:
        pass
