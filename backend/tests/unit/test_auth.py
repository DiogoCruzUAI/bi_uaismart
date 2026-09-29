"""Testes de autenticação.

Estes testes rodam contra o app real, **sem sobrepor as dependências de identidade** —
ao contrário de `test_router_conexoes.py`, onde o que estava sob teste eram as
conexões. Aqui o JWT é justamente o que se quer provar.

Três propriedades guardadas:

1. O login não conta nada a quem não entrou (mesma resposta para tenant inexistente,
   e-mail inexistente, senha errada e usuário desativado).
2. Renovar rotaciona: o refresh usado deixa de valer.
3. O tenant vem do token, nunca da requisição — um admin não alcança outro tenant.
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.deps import get_db
from app.core.security import criar_access_token, criar_refresh_token, hash_senha
from app.main import app
from app.models.tenant import PerfilUsuario, Tenant, Usuario

SENHA = "uma-senha-bem-longa"


@pytest_asyncio.fixture
async def cliente(db):
    """App com o banco de teste, mas com a autenticação real em funcionamento."""
    app.dependency_overrides[get_db] = lambda: db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://teste"
    ) as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin(db, tenant):
    u = Usuario(
        tenant_id=tenant.id,
        nome="Admin",
        email="admin@cliente.com",
        senha_hash=hash_senha(SENHA),
        perfil=PerfilUsuario.admin,
    )
    db.add(u)
    await db.flush()
    return u


async def _entrar(cliente, tenant_slug="teste", email="admin@cliente.com", senha=SENHA):
    return await cliente.post(
        "/api/v1/auth/login",
        json={"tenant": tenant_slug, "email": email, "senha": senha},
    )


def _cabecalho(tokens: dict) -> dict:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# ─── Login ────────────────────────────────────────────────────────────────────


async def test_login_valido_devolve_par_de_tokens(cliente, admin):
    r = await _entrar(cliente)
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["access_token"] and corpo["refresh_token"]
    assert corpo["token_type"] == "bearer"
    assert corpo["expira_em_segundos"] > 0


async def test_login_registra_o_acesso(cliente, admin, db):
    assert admin.ultimo_acesso_em is None
    await _entrar(cliente)
    await db.refresh(admin)
    assert admin.ultimo_acesso_em is not None


@pytest.mark.parametrize(
    "caso,kwargs",
    [
        ("senha errada", {"senha": "senha-errada-porem-longa"}),
        ("email inexistente", {"email": "ninguem@cliente.com"}),
        ("tenant inexistente", {"tenant_slug": "nao-existe"}),
    ],
)
async def test_toda_falha_responde_igual(cliente, admin, caso, kwargs):
    """Resposta diferente por caso transforma o login num oráculo.

    Daria para descobrir quais tenants existem, e depois quais e-mails existem em
    cada um, sem nunca acertar uma senha.
    """
    r = await _entrar(cliente, **kwargs)
    assert r.status_code == 401
    assert r.json()["detail"]["erro"] == "CREDENCIAIS_INVALIDAS"


async def test_usuario_desativado_nao_entra(cliente, admin, db):
    admin.ativo = False
    await db.flush()
    r = await _entrar(cliente)
    assert r.status_code == 401


async def test_tenant_desativado_nao_entra(cliente, admin, db, tenant):
    tenant.ativo = False
    await db.flush()
    r = await _entrar(cliente)
    assert r.status_code == 401


async def test_senha_nao_aparece_em_resposta_nenhuma(cliente, admin):
    r = await _entrar(cliente)
    assert SENHA not in r.text
    eu = await cliente.get("/api/v1/auth/eu", headers=_cabecalho(r.json()))
    assert SENHA not in eu.text
    assert "senha_hash" not in eu.text


# ─── Token ────────────────────────────────────────────────────────────────────


async def test_rota_protegida_exige_token(cliente, admin):
    assert (await cliente.get("/api/v1/auth/eu")).status_code == 401


async def test_token_invalido_e_recusado(cliente, admin):
    r = await cliente.get(
        "/api/v1/auth/eu", headers={"Authorization": "Bearer nao-e-um-token"}
    )
    assert r.status_code == 401


async def test_refresh_nao_serve_como_access(cliente, admin):
    """Aceitar o refresh como access burlaria a expiração curta do access.

    Que é exatamente a razão de existirem dois tokens.
    """
    tokens = (await _entrar(cliente)).json()
    r = await cliente.get(
        "/api/v1/auth/eu",
        headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
    )
    assert r.status_code == 401


async def test_token_de_outro_tenant_nao_encontra_o_usuario(cliente, admin):
    """O tid do token é conferido junto com o id do usuário."""
    forjado = criar_access_token(admin.id, admin.tenant_id + 999, "admin")
    r = await cliente.get("/api/v1/auth/eu", headers={"Authorization": f"Bearer {forjado}"})
    assert r.status_code == 401


async def test_eu_devolve_o_usuario_do_token(cliente, admin):
    tokens = (await _entrar(cliente)).json()
    r = await cliente.get("/api/v1/auth/eu", headers=_cabecalho(tokens))
    assert r.status_code == 200
    assert r.json()["email"] == "admin@cliente.com"
    assert r.json()["perfil"] == "admin"


# ─── Renovação ────────────────────────────────────────────────────────────────


async def test_refresh_devolve_par_novo(cliente, admin):
    tokens = (await _entrar(cliente)).json()
    r = await cliente.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert r.status_code == 200
    assert r.json()["access_token"] != tokens["access_token"]


async def test_access_token_nao_serve_como_refresh(cliente, admin):
    tokens = (await _entrar(cliente)).json()
    r = await cliente.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["access_token"]}
    )
    assert r.status_code == 401


async def test_refresh_de_usuario_removido_e_recusado(cliente, admin, db):
    tokens = (await _entrar(cliente)).json()
    admin.ativo = False
    await db.flush()
    r = await cliente.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert r.status_code == 401


async def test_refresh_forjado_para_outro_usuario_e_recusado(cliente, admin):
    forjado = criar_refresh_token(admin.id + 999, admin.tenant_id)
    r = await cliente.post("/api/v1/auth/refresh", json={"refresh_token": forjado})
    assert r.status_code == 401


# ─── Usuários do tenant ───────────────────────────────────────────────────────


async def test_admin_cria_usuario_no_proprio_tenant(cliente, admin):
    tokens = (await _entrar(cliente)).json()
    r = await cliente.post(
        "/api/v1/usuarios",
        headers=_cabecalho(tokens),
        json={
            "nome": "Analista",
            "email": "analista@cliente.com",
            "senha": "outra-senha-longa",
            "perfil": "analista",
        },
    )
    assert r.status_code == 201
    assert r.json()["tenant_id"] == admin.tenant_id


async def test_usuario_criado_consegue_entrar(cliente, admin):
    tokens = (await _entrar(cliente)).json()
    await cliente.post(
        "/api/v1/usuarios",
        headers=_cabecalho(tokens),
        json={
            "nome": "Analista",
            "email": "analista@cliente.com",
            "senha": "outra-senha-longa",
            "perfil": "analista",
        },
    )
    r = await _entrar(cliente, email="analista@cliente.com", senha="outra-senha-longa")
    assert r.status_code == 200


async def test_senha_curta_e_recusada(cliente, admin):
    tokens = (await _entrar(cliente)).json()
    r = await cliente.post(
        "/api/v1/usuarios",
        headers=_cabecalho(tokens),
        json={"nome": "X", "email": "x@cliente.com", "senha": "curta", "perfil": "leitor"},
    )
    assert r.status_code == 422


async def test_email_repetido_no_tenant_e_recusado(cliente, admin):
    tokens = (await _entrar(cliente)).json()
    corpo = {
        "nome": "Outro",
        "email": "admin@cliente.com",
        "senha": "mais-uma-senha-longa",
        "perfil": "leitor",
    }
    r = await cliente.post("/api/v1/usuarios", headers=_cabecalho(tokens), json=corpo)
    assert r.status_code == 409


async def test_mesmo_email_em_tenants_diferentes_e_permitido(cliente, admin, db):
    """A razão de o login pedir o tenant: a mesma pessoa em duas empresas clientes."""
    outro = Tenant(nome="Outra Empresa", slug="outra")
    db.add(outro)
    await db.flush()
    db.add(
        Usuario(
            tenant_id=outro.id,
            nome="Mesma Pessoa",
            email="admin@cliente.com",
            senha_hash=hash_senha("senha-do-outro-tenant"),
            perfil=PerfilUsuario.admin,
        )
    )
    await db.flush()

    aqui = await _entrar(cliente, tenant_slug="teste")
    la = await _entrar(cliente, tenant_slug="outra", senha="senha-do-outro-tenant")
    assert aqui.status_code == 200
    assert la.status_code == 200
    # E cada um recebe o token do seu próprio tenant.
    eu_aqui = await cliente.get("/api/v1/auth/eu", headers=_cabecalho(aqui.json()))
    eu_la = await cliente.get("/api/v1/auth/eu", headers=_cabecalho(la.json()))
    assert eu_aqui.json()["tenant_id"] != eu_la.json()["tenant_id"]


async def test_analista_nao_cria_usuario(cliente, admin, db):
    analista = Usuario(
        tenant_id=admin.tenant_id,
        nome="Analista",
        email="analista@cliente.com",
        senha_hash=hash_senha(SENHA),
        perfil=PerfilUsuario.analista,
    )
    db.add(analista)
    await db.flush()

    tokens = (await _entrar(cliente, email="analista@cliente.com")).json()
    r = await cliente.post(
        "/api/v1/usuarios",
        headers=_cabecalho(tokens),
        json={"nome": "X", "email": "x@cliente.com", "senha": "senha-longa-aqui", "perfil": "leitor"},
    )
    assert r.status_code == 403


async def test_listagem_de_usuarios_nao_vaza_outro_tenant(cliente, admin, db):
    outro = Tenant(nome="Outra", slug="outra2")
    db.add(outro)
    await db.flush()
    db.add(
        Usuario(
            tenant_id=outro.id, nome="Alheio", email="alheio@outro.com",
            senha_hash=hash_senha(SENHA), perfil=PerfilUsuario.admin,
        )
    )
    await db.flush()

    tokens = (await _entrar(cliente)).json()
    r = await cliente.get("/api/v1/usuarios", headers=_cabecalho(tokens))
    assert [u["email"] for u in r.json()] == ["admin@cliente.com"]


# ─── Troca de senha ───────────────────────────────────────────────────────────


async def test_troca_de_senha_exige_a_atual(cliente, admin):
    tokens = (await _entrar(cliente)).json()
    r = await cliente.post(
        "/api/v1/auth/senha",
        headers=_cabecalho(tokens),
        json={"senha_atual": "errada-mas-longa", "senha_nova": "nova-senha-longa"},
    )
    assert r.status_code == 400


async def test_senha_nova_passa_a_valer(cliente, admin):
    tokens = (await _entrar(cliente)).json()
    r = await cliente.post(
        "/api/v1/auth/senha",
        headers=_cabecalho(tokens),
        json={"senha_atual": SENHA, "senha_nova": "nova-senha-bem-longa"},
    )
    assert r.status_code == 204
    assert (await _entrar(cliente, senha=SENHA)).status_code == 401
    assert (await _entrar(cliente, senha="nova-senha-bem-longa")).status_code == 200
