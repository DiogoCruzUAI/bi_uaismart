"""JWT e hash de senha.

Adaptado do SessionFlow, que já resolveu isto em produção. O acréscimo aqui é o
`tenant_id` dentro do token: a plataforma é multi-tenant desde a primeira tabela,
então a identidade carrega o tenant e não há caminho em que ele seja esquecido.

O hash usa a biblioteca `bcrypt` direto, sem `passlib`. O passlib está sem
manutenção desde 2020 e lê `bcrypt.__about__.__version__`, atributo removido no
bcrypt 4.1 — o que imprimia um traceback capturado a cada import. O uso aqui era
só `hash`/`verify`, então a camada não pagava por si.
"""

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings

# Mesmo custo que o passlib aplicava por padrão: os hashes já gravados continuam
# válidos e os novos saem idênticos aos antigos em formato e preço.
_CUSTO_BCRYPT = 12

# `bcrypt.checkpw` **entra em pânico no Rust** (`pyo3_runtime.PanicException`) se o
# hash for curto demais para fatiar — e PanicException herda de BaseException, então
# `except Exception` não segura, e nem o middleware de erro do FastAPI. Conferir a
# forma antes é o que mantém um hash corrompido no banco como um login recusado em
# vez de um worker derrubado. Medido em 29/09/2026: 3000 entradas que casam esta
# expressão, zero pânicos.
_FORMATO_BCRYPT = re.compile(r"\$2[aby]\$(?:0[4-9]|[12][0-9]|3[01])\$[./A-Za-z0-9]{53}")

# Hash pré-computado para normalizar o tempo do login. Sem ele, "e-mail não existe"
# responde mais rápido que "senha errada" e a diferença enumera os usuários.
_HASH_FALSO: str = bcrypt.hashpw(
    b"__nextgen_timing_dummy__", bcrypt.gensalt(_CUSTO_BCRYPT)
).decode("ascii")


def hash_senha(senha: str) -> str:
    return bcrypt.hashpw(senha.encode("utf-8"), bcrypt.gensalt(_CUSTO_BCRYPT)).decode("ascii")


def verificar_senha(senha: str, hash_armazenado: str | None) -> bool:
    """Sempre executa bcrypt, mesmo sem hash real — evita timing attack."""
    utilizavel = (
        hash_armazenado is not None and _FORMATO_BCRYPT.fullmatch(hash_armazenado) is not None
    )
    alvo = hash_armazenado if utilizavel else _HASH_FALSO
    try:
        confere = bcrypt.checkpw(senha.encode("utf-8"), alvo.encode("ascii"))
    except ValueError:
        # Forma válida, conteúdo não (salt corrompido). Gasta o mesmo tempo do
        # caminho normal antes de recusar, senão o erro vira um canal de tempo.
        bcrypt.checkpw(senha.encode("utf-8"), _HASH_FALSO.encode("ascii"))
        return False
    return confere and utilizavel


def _criar_token(dados: dict[str, Any], tipo: str, expira_em: timedelta) -> str:
    return jwt.encode(
        {
            **dados,
            "exp": datetime.now(timezone.utc) + expira_em,
            "type": tipo,
            "jti": str(uuid.uuid4()),
        },
        settings.secret_key,
        algorithm=settings.algorithm,
    )


def criar_access_token(usuario_id: int, tenant_id: int, perfil: str) -> str:
    return _criar_token(
        {"sub": str(usuario_id), "tid": tenant_id, "perfil": perfil},
        "access",
        timedelta(minutes=settings.access_token_expire_minutes),
    )


def criar_refresh_token(usuario_id: int, tenant_id: int) -> str:
    return _criar_token(
        {"sub": str(usuario_id), "tid": tenant_id},
        "refresh",
        timedelta(days=settings.refresh_token_expire_days),
    )


def decodificar_token(token: str, tipo_esperado: str = "access") -> dict[str, Any] | None:
    """Verifica assinatura, validade e **tipo**.

    O tipo é conferido explicitamente porque um refresh token aceito como access
    burlaria a expiração curta — que é a razão de existirem dois tokens.
    """
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError:
        return None
    if payload.get("type") != tipo_esperado:
        return None
    return payload
