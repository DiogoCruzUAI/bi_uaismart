"""JWT e hash de senha.

Adaptado do SessionFlow, que já resolveu isto em produção. O acréscimo aqui é o
`tenant_id` dentro do token: a plataforma é multi-tenant desde a primeira tabela,
então a identidade carrega o tenant e não há caminho em que ele seja esquecido.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Hash pré-computado para normalizar o tempo do login. Sem ele, "e-mail não existe"
# responde mais rápido que "senha errada" e a diferença enumera os usuários.
_HASH_FALSO: str = pwd_context.hash("__nextgen_timing_dummy__")


def hash_senha(senha: str) -> str:
    return pwd_context.hash(senha)


def verificar_senha(senha: str, hash_armazenado: str | None) -> bool:
    """Sempre executa bcrypt, mesmo sem hash real — evita timing attack."""
    return pwd_context.verify(senha, hash_armazenado or _HASH_FALSO) and hash_armazenado is not None


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
