"""Autenticação: login, renovação, logout e gestão de usuários do tenant.

Três propriedades que o código sustenta e os testes guardam:

**O login não conta nada a quem não entrou.** Tenant inexistente, e-mail inexistente,
senha errada e usuário desativado devolvem a mesma resposta, e todos executam bcrypt —
inclusive quando não há hash real para comparar. Resposta ou tempo diferentes
transformam o login num oráculo que enumera clientes e usuários.

**Renovar rotaciona.** O refresh usado é revogado no ato. Sem isso, um refresh token
vazado vale até expirar, mesmo depois de a pessoa ter feito logout e trocado a senha.

**O tenant nunca vem da requisição depois do login.** No login ele é o slug informado;
daí em diante sai do token assinado.
"""

from datetime import datetime, timezone
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.core.config import settings
from app.core.deps import Db, UsuarioAtual, exigir_perfil, oauth2_scheme
from app.core.redis_client import (
    limpar_falhas_de_login,
    login_bloqueado,
    registrar_falha_de_login,
    revogar_token,
)
from app.core.security import (
    criar_access_token,
    criar_refresh_token,
    decodificar_token,
    hash_senha,
    verificar_senha,
)
from app.models.tenant import PerfilUsuario, Tenant, Usuario
from app.schemas.auth import (
    LoginRequisicao,
    RefreshRequisicao,
    SenhaAlterar,
    Tokens,
    UsuarioCriar,
    UsuarioResposta,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/api/v1", tags=["auth"])

SomenteAdmin = Annotated[Usuario, Depends(exigir_perfil(PerfilUsuario.admin))]

# Uma única resposta para toda falha de login. Qualquer variação — "tenant não
# existe", "usuário não encontrado", "senha incorreta" — entrega informação a quem
# está tentando adivinhar.
_CREDENCIAIS_INVALIDAS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail={"erro": "CREDENCIAIS_INVALIDAS", "mensagem": "E-mail ou senha incorretos."},
    headers={"WWW-Authenticate": "Bearer"},
)


def _segundos_ate_expirar(payload: dict) -> int:
    """Quanto falta para o token morrer sozinho.

    É o TTL da entrada na blacklist: depois disso a entrada é inútil, e o Redis a
    remove. Sem esse cálculo, a lista de revogados cresceria para sempre.
    """
    exp = payload.get("exp")
    if not exp:
        return 60
    restante = int(exp - datetime.now(timezone.utc).timestamp())
    return max(restante, 1)


def _emitir(usuario: Usuario) -> Tokens:
    return Tokens(
        access_token=criar_access_token(usuario.id, usuario.tenant_id, usuario.perfil.value),
        refresh_token=criar_refresh_token(usuario.id, usuario.tenant_id),
        expira_em_segundos=settings.access_token_expire_minutes * 60,
    )


# ─── Login ────────────────────────────────────────────────────────────────────


@router.post("/auth/login", response_model=Tokens)
async def login(dados: LoginRequisicao, db: Db) -> Tokens:
    if await login_bloqueado(dados.tenant, dados.email):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "erro": "MUITAS_TENTATIVAS",
                "mensagem": "Tentativas demais. Aguarde alguns minutos e tente de novo.",
            },
        )

    tenant = (
        await db.execute(
            select(Tenant).where(
                Tenant.slug == dados.tenant.lower(),
                Tenant.ativo.is_(True),
                Tenant.deletado_em.is_(None),
            )
        )
    ).scalar_one_or_none()

    usuario: Usuario | None = None
    if tenant is not None:
        usuario = (
            await db.execute(
                select(Usuario).where(
                    Usuario.tenant_id == tenant.id,
                    Usuario.email == dados.email.lower(),
                    Usuario.deletado_em.is_(None),
                )
            )
        ).scalar_one_or_none()

    # bcrypt roda mesmo sem usuário: `verificar_senha` compara contra um hash falso
    # quando recebe None. Sem isso, "tenant não existe" responderia em microssegundos
    # e "senha errada" em centenas de milissegundos — diferença suficiente para
    # enumerar clientes e usuários com um cronômetro.
    senha_confere = verificar_senha(
        dados.senha.get_secret_value(), usuario.senha_hash if usuario else None
    )

    if usuario is None or not senha_confere or not usuario.ativo:
        await registrar_falha_de_login(dados.tenant, dados.email)
        logger.info("login_recusado", tenant=dados.tenant)
        raise _CREDENCIAIS_INVALIDAS

    await limpar_falhas_de_login(dados.tenant, dados.email)
    usuario.ultimo_acesso_em = datetime.now(timezone.utc)
    await db.flush()

    logger.info("login_aceito", usuario_id=usuario.id, tenant_id=usuario.tenant_id)
    return _emitir(usuario)


@router.post("/auth/refresh", response_model=Tokens)
async def renovar(dados: RefreshRequisicao, db: Db) -> Tokens:
    """Troca o refresh por um par novo e **revoga o antigo**.

    Sem a rotação, um refresh token vazado vale até expirar — inclusive depois de a
    pessoa ter feito logout e trocado a senha, que é justamente quando ela acha que
    resolveu o problema.
    """
    payload = decodificar_token(dados.refresh_token, tipo_esperado="refresh")
    if not payload or not payload.get("sub") or not payload.get("tid"):
        raise _CREDENCIAIS_INVALIDAS

    from app.core.redis_client import token_revogado

    jti = payload.get("jti")
    if jti and await token_revogado(jti):
        raise _CREDENCIAIS_INVALIDAS

    usuario = (
        await db.execute(
            select(Usuario).where(
                Usuario.id == int(payload["sub"]),
                Usuario.tenant_id == int(payload["tid"]),
                Usuario.deletado_em.is_(None),
            )
        )
    ).scalar_one_or_none()

    if usuario is None or not usuario.ativo:
        raise _CREDENCIAIS_INVALIDAS

    if jti:
        await revogar_token(jti, _segundos_ate_expirar(payload))
    return _emitir(usuario)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    token: Annotated[str, Depends(oauth2_scheme)],
    _: UsuarioAtual,
    dados: RefreshRequisicao | None = None,
) -> None:
    """Revoga o access token e, se enviado, também o refresh.

    O refresh é opcional no corpo porque nem todo cliente o tem à mão na hora do
    logout — mas sem ele a sessão continua renovável. O frontend deve mandar os dois.
    """
    payload = decodificar_token(token, tipo_esperado="access")
    if payload and payload.get("jti"):
        await revogar_token(payload["jti"], _segundos_ate_expirar(payload))

    if dados and dados.refresh_token:
        refresh = decodificar_token(dados.refresh_token, tipo_esperado="refresh")
        if refresh and refresh.get("jti"):
            await revogar_token(refresh["jti"], _segundos_ate_expirar(refresh))


@router.get("/auth/eu", response_model=UsuarioResposta)
async def eu(usuario: UsuarioAtual) -> Usuario:
    return usuario


@router.post("/auth/senha", status_code=status.HTTP_204_NO_CONTENT)
async def alterar_senha(dados: SenhaAlterar, usuario: UsuarioAtual, db: Db) -> None:
    """Troca a própria senha.

    Não revoga as sessões existentes, e isso é uma lacuna consciente: fazê-lo exige
    rastrear todos os jti do usuário, não só o atual. Está anotado em
    docs/ARQUITETURA.md para ser fechado antes do primeiro cliente real.
    """
    if not verificar_senha(dados.senha_atual.get_secret_value(), usuario.senha_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"erro": "SENHA_INCORRETA", "mensagem": "A senha atual está incorreta."},
        )
    usuario.senha_hash = hash_senha(dados.senha_nova.get_secret_value())
    await db.flush()
    logger.info("senha_alterada", usuario_id=usuario.id)


# ─── Usuários do tenant ───────────────────────────────────────────────────────


@router.post(
    "/usuarios", response_model=UsuarioResposta, status_code=status.HTTP_201_CREATED
)
async def criar_usuario(dados: UsuarioCriar, admin: SomenteAdmin, db: Db) -> Usuario:
    """Cria usuário **no tenant de quem está criando**, nunca em outro."""
    existe = (
        await db.execute(
            select(Usuario).where(
                Usuario.tenant_id == admin.tenant_id,
                Usuario.email == dados.email.lower(),
                Usuario.deletado_em.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existe is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "erro": "EMAIL_EM_USO",
                "mensagem": "Já existe um usuário com este e-mail neste tenant.",
            },
        )

    usuario = Usuario(
        tenant_id=admin.tenant_id,
        nome=dados.nome,
        email=dados.email.lower(),
        senha_hash=hash_senha(dados.senha.get_secret_value()),
        perfil=dados.perfil,
    )
    db.add(usuario)
    await db.flush()
    await db.refresh(usuario)
    logger.info("usuario_criado", usuario_id=usuario.id, tenant_id=admin.tenant_id)
    return usuario


@router.get("/usuarios", response_model=list[UsuarioResposta])
async def listar_usuarios(admin: SomenteAdmin, db: Db) -> list[Usuario]:
    resultado = await db.execute(
        select(Usuario)
        .where(Usuario.tenant_id == admin.tenant_id, Usuario.deletado_em.is_(None))
        .order_by(Usuario.nome)
    )
    return list(resultado.scalars().all())
