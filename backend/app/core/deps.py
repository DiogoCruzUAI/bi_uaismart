"""Injeção de dependências do FastAPI.

`get_tenant_atual` é a dependência mais importante da aplicação: o `tenant_id` sai do
**token assinado**, nunca de parâmetro de rota, corpo ou cabeçalho. Um `tenant_id`
que o cliente escolhe é um `tenant_id` que o cliente troca.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis_client import token_revogado
from app.core.security import decodificar_token
from app.models.tenant import PerfilUsuario, Usuario

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

_NAO_AUTENTICADO = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail={"erro": "NAO_AUTENTICADO", "mensagem": "Token inválido ou expirado."},
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_usuario_atual(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Usuario:
    payload = decodificar_token(token, tipo_esperado="access")
    if not payload or not payload.get("sub") or not payload.get("tid"):
        raise _NAO_AUTENTICADO

    jti = payload.get("jti")
    if jti and await token_revogado(jti):
        raise _NAO_AUTENTICADO

    # O filtro por tenant_id aqui não é redundante com o id: impede que um token
    # com tenant adulterado — se a chave vazasse — encontrasse o usuário mesmo assim.
    resultado = await db.execute(
        select(Usuario).where(
            Usuario.id == int(payload["sub"]),
            Usuario.tenant_id == int(payload["tid"]),
            Usuario.deletado_em.is_(None),
        )
    )
    usuario = resultado.scalar_one_or_none()
    if usuario is None or not usuario.ativo:
        raise _NAO_AUTENTICADO
    return usuario


async def get_tenant_atual(
    usuario: Annotated[Usuario, Depends(get_usuario_atual)],
) -> int:
    return usuario.tenant_id


def exigir_perfil(*perfis: PerfilUsuario):
    """Restringe uma rota a determinados perfis.

    Uso: `Depends(exigir_perfil(PerfilUsuario.admin))`.
    """

    async def _verificar(
        usuario: Annotated[Usuario, Depends(get_usuario_atual)],
    ) -> Usuario:
        if usuario.perfil not in perfis:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "erro": "ACESSO_NEGADO",
                    "mensagem": "Seu perfil não permite esta operação.",
                },
            )
        return usuario

    return _verificar


UsuarioAtual = Annotated[Usuario, Depends(get_usuario_atual)]
TenantAtual = Annotated[int, Depends(get_tenant_atual)]
Db = Annotated[AsyncSession, Depends(get_db)]
