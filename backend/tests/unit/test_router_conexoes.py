"""Testes do endpoint de conexões.

As dependências de autenticação são sobrepostas: o router de login ainda não existe, e
o que está sob teste aqui é o comportamento das conexões, não o JWT — que tem os
próprios testes em `test_config.py` e no módulo de segurança.

O teste que mais importa é `test_senha_nunca_aparece_na_resposta`. Vazar a credencial
do banco de um cliente é o pior desfecho possível da plataforma: dá acesso a **todos**
os dados dele, não a um recorte.
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.connectors.base import ErroDeConexao
from app.core.deps import get_db, get_tenant_atual, get_usuario_atual
from app.main import app
from app.models.conexao import StatusConexao
from app.models.tenant import PerfilUsuario, Usuario
from app.semantic.retrato import RetratoBanco, RetratoColuna, RetratoTabela
from app.semantic.sinais import classificar_coluna

CORPO = {
    "nome": "Produção",
    "tipo": "postgres",
    "host": "db.exemplo.com",
    "banco": "cliente",
    "usuario": "nextgen_leitor",
    "senha": "senha-super-secreta-do-cliente",
}


@pytest_asyncio.fixture
async def cliente(db, tenant, monkeypatch):
    """App com banco e identidade sobrepostos, e sem tocar banco de cliente algum."""
    usuario = Usuario(
        tenant_id=tenant.id,
        nome="Admin",
        email="admin@teste.com",
        senha_hash="x",
        perfil=PerfilUsuario.admin,
    )
    db.add(usuario)
    await db.flush()

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_usuario_atual] = lambda: usuario
    app.dependency_overrides[get_tenant_atual] = lambda: tenant.id

    transporte = ASGITransport(app=app)
    async with AsyncClient(transport=transporte, base_url="http://teste") as c:
        yield c

    app.dependency_overrides.clear()


class ConectorDublê:
    """Substitui o conector real: nenhum teste abre conexão de rede."""

    dialeto = "postgres"
    alertas: list[str] = []
    erro: str | None = None
    retrato: RetratoBanco | None = None

    async def testar(self):
        if type(self).erro:
            raise ErroDeConexao(type(self).erro)
        return list(type(self).alertas)

    async def fechar(self): ...


@pytest.fixture(autouse=True)
def sem_rede(monkeypatch):
    """Nenhum teste deste arquivo pode tentar falar com um banco de verdade."""
    monkeypatch.setattr(
        "app.routers.conexoes.criar_conector", lambda conexao: ConectorDublê()
    )
    ConectorDublê.alertas = []
    ConectorDublê.erro = None
    yield


# ─── A propriedade que não pode quebrar ───────────────────────────────────────


async def test_senha_nunca_aparece_na_resposta(cliente):
    r = await cliente.post("/api/v1/conexoes", json=CORPO)
    assert r.status_code == 201

    bruto = r.text
    assert CORPO["senha"] not in bruto
    assert "senha" not in r.json()
    assert "senha_cifrada" not in r.json()


async def test_senha_nao_vaza_na_listagem_nem_na_leitura(cliente):
    criada = (await cliente.post("/api/v1/conexoes", json=CORPO)).json()

    lista = await cliente.get("/api/v1/conexoes")
    individual = await cliente.get(f"/api/v1/conexoes/{criada['id']}")

    for resposta in (lista, individual):
        assert CORPO["senha"] not in resposta.text
        assert "senha_cifrada" not in resposta.text


async def test_senha_e_guardada_cifrada(cliente, db, tenant):
    """No banco também não: um dump não entrega banco de cliente nenhum."""
    from sqlalchemy import select

    from app.core.crypto import decifrar
    from app.models.conexao import Conexao

    await cliente.post("/api/v1/conexoes", json=CORPO)
    conexao = (await db.execute(select(Conexao))).scalar_one()

    assert conexao.senha_cifrada != CORPO["senha"]
    assert CORPO["senha"] not in conexao.senha_cifrada
    # E é recuperável por quem tem a chave — cifra, não hash.
    assert decifrar(conexao.senha_cifrada) == CORPO["senha"]


# ─── Cadastro ─────────────────────────────────────────────────────────────────


async def test_porta_padrao_e_preenchida(cliente):
    r = await cliente.post("/api/v1/conexoes", json=CORPO)
    assert r.json()["porta"] == 5432


async def test_url_completa_no_campo_host_e_recusada(cliente):
    """O erro de cadastro mais comum, e o de diagnóstico mais confuso."""
    r = await cliente.post(
        "/api/v1/conexoes", json={**CORPO, "host": "postgres://user:pw@host:5432/db"}
    )
    assert r.status_code == 422


async def test_nome_repetido_no_mesmo_tenant_e_recusado(cliente):
    await cliente.post("/api/v1/conexoes", json=CORPO)
    r = await cliente.post("/api/v1/conexoes", json=CORPO)
    assert r.status_code == 409


async def test_conexao_que_falha_fica_salva_com_o_erro(cliente):
    """Falhar o cadastro apagaria o formulário junto com o erro."""
    ConectorDublê.erro = "O usuário é superusuário."

    r = await cliente.post("/api/v1/conexoes", json=CORPO)
    assert r.status_code == 201
    corpo = r.json()
    assert corpo["status"] == StatusConexao.erro.value
    assert "superusuário" in corpo["ultimo_erro"]


async def test_alertas_de_privilegio_chegam_ao_teste(cliente):
    ConectorDublê.alertas = ["O usuário tem BYPASSRLS."]
    criada = (await cliente.post("/api/v1/conexoes", json=CORPO)).json()

    r = await cliente.post(f"/api/v1/conexoes/{criada['id']}/testar")
    assert r.json()["conectou"] is True
    assert r.json()["alertas"] == ["O usuário tem BYPASSRLS."]


# ─── Isolamento entre tenants ─────────────────────────────────────────────────


async def test_conexao_de_outro_tenant_responde_404(cliente, db, tenant):
    """404 e não 403: um 403 confirmaria que aquele id existe em algum lugar."""
    from app.core.crypto import cifrar
    from app.models.conexao import Conexao, TipoBanco
    from app.models.tenant import Tenant

    outro = Tenant(nome="Outro Cliente", slug="outro")
    db.add(outro)
    await db.flush()

    alheia = Conexao(
        tenant_id=outro.id,
        nome="Secreta",
        tipo=TipoBanco.postgres,
        host="h",
        porta=5432,
        banco="b",
        usuario="u",
        senha_cifrada=cifrar("x"),
    )
    db.add(alheia)
    await db.flush()

    r = await cliente.get(f"/api/v1/conexoes/{alheia.id}")
    assert r.status_code == 404


async def test_listagem_so_traz_o_proprio_tenant(cliente, db, tenant):
    from app.core.crypto import cifrar
    from app.models.conexao import Conexao, TipoBanco
    from app.models.tenant import Tenant

    outro = Tenant(nome="Outro", slug="outro2")
    db.add(outro)
    await db.flush()
    db.add(
        Conexao(
            tenant_id=outro.id, nome="Alheia", tipo=TipoBanco.postgres, host="h",
            porta=5432, banco="b", usuario="u", senha_cifrada=cifrar("x"),
        )
    )
    await db.flush()
    await cliente.post("/api/v1/conexoes", json=CORPO)

    corpo = (await cliente.get("/api/v1/conexoes")).json()
    assert [c["nome"] for c in corpo] == ["Produção"]


# ─── Perfilamento ─────────────────────────────────────────────────────────────


async def test_perfilamento_persiste_e_devolve_o_resumo(cliente, monkeypatch):
    retrato = RetratoBanco(
        coletado_em=datetime.now(timezone.utc),
        tabelas=[
            RetratoTabela(
                esquema="public",
                nome="pedidos",
                eh_view=False,
                papel="fato",
                linhas_estimadas=1_000,
                bytes_estimados=10_000,
                colunas=[
                    RetratoColuna(
                        nome="id",
                        tipo_sql="bigint",
                        aceita_nulo=False,
                        eh_chave_primaria=True,
                        sinais=classificar_coluna(
                            tipo_sql="bigint", cardinalidade=1_000,
                            fracao_nula=0.0, linhas_tabela=1_000, eh_chave_primaria=True,
                        ),
                        cardinalidade=1_000,
                    )
                ],
            )
        ],
    )

    async def perfilar_falso(conector, **kw):
        return retrato

    monkeypatch.setattr("app.routers.conexoes.perfilar", perfilar_falso)

    criada = (await cliente.post("/api/v1/conexoes", json=CORPO)).json()
    r = await cliente.post(f"/api/v1/conexoes/{criada['id']}/perfilar")

    assert r.status_code == 200
    corpo = r.json()
    assert corpo["tabelas_criadas"] == 1
    assert corpo["colunas_criadas"] == 1
    assert corpo["versao_dicionario"] == 2  # começou em 1 e subiu com a estrutura nova

    # E a conexão passa a constar como catalogada.
    depois = (await cliente.get(f"/api/v1/conexoes/{criada['id']}")).json()
    assert depois["status"] == StatusConexao.catalogada.value
    assert depois["ultimo_perfilamento_em"] is not None


async def test_falha_no_perfilamento_vira_502_e_fica_registrada(cliente, monkeypatch):
    async def perfilar_que_falha(conector, **kw):
        raise ErroDeConexao("O banco recusou a consulta.")

    monkeypatch.setattr("app.routers.conexoes.perfilar", perfilar_que_falha)

    criada = (await cliente.post("/api/v1/conexoes", json=CORPO)).json()
    r = await cliente.post(f"/api/v1/conexoes/{criada['id']}/perfilar")

    assert r.status_code == 502
    depois = (await cliente.get(f"/api/v1/conexoes/{criada['id']}")).json()
    assert depois["status"] == StatusConexao.erro.value


async def test_perfilar_conexao_inexistente_responde_404(cliente):
    r = await cliente.post("/api/v1/conexoes/99999/perfilar")
    assert r.status_code == 404


async def test_leitor_nao_cadastra_nem_perfila(db, tenant):
    """Perfil `leitor` consome o que existe; não muda o que a plataforma enxerga."""
    from httpx import ASGITransport, AsyncClient

    leitor = Usuario(
        tenant_id=tenant.id,
        nome="Leitor",
        email="leitor@teste.com",
        senha_hash="x",
        perfil=PerfilUsuario.leitor,
    )
    db.add(leitor)
    await db.flush()

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_usuario_atual] = lambda: leitor
    app.dependency_overrides[get_tenant_atual] = lambda: tenant.id
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://teste"
        ) as c:
            assert (await c.post("/api/v1/conexoes", json=CORPO)).status_code == 403
            assert (await c.post("/api/v1/conexoes/1/perfilar")).status_code == 403
            # Mas continua podendo listar.
            assert (await c.get("/api/v1/conexoes")).status_code == 200
    finally:
        app.dependency_overrides.clear()


# ─── Dicionário ───────────────────────────────────────────────────────────────


def _retrato_minimo():
    from app.semantic.retrato import RetratoBanco, RetratoColuna, RetratoTabela
    from app.semantic.sinais import classificar_coluna

    return RetratoBanco(
        coletado_em=datetime.now(timezone.utc),
        tabelas=[
            RetratoTabela(
                esquema="public", nome="pedidos", eh_view=False, papel="fato",
                linhas_estimadas=1_000, bytes_estimados=10_000,
                colunas=[
                    RetratoColuna(
                        nome="total", tipo_sql="bigint", aceita_nulo=True,
                        eh_chave_primaria=False,
                        sinais=classificar_coluna(
                            tipo_sql="bigint", cardinalidade=800, fracao_nula=0.0,
                            linhas_tabela=1_000, maximo="99000000",
                        ),
                        cardinalidade=800, maximo="99000000",
                    )
                ],
            )
        ],
    )


async def test_sem_chave_da_anthropic_responde_503(cliente, monkeypatch):
    """O estado atual do projeto: perfila, mas não gera dicionário.

    503 e não 500: é dependência externa indisponível, e o cliente pode tentar de
    novo depois de configurar a chave.
    """
    from app.llm.cliente import LlmIndisponivel

    async def perfilar_falso(conector, **kw):
        return _retrato_minimo()

    async def sem_chave(retrato, **kw):
        raise LlmIndisponivel("ANTHROPIC_API_KEY não configurada.")

    monkeypatch.setattr("app.routers.conexoes.perfilar", perfilar_falso)
    monkeypatch.setattr("app.routers.conexoes.gerar_dicionario_completo", sem_chave)

    criada = (await cliente.post("/api/v1/conexoes", json=CORPO)).json()
    r = await cliente.post(f"/api/v1/conexoes/{criada['id']}/dicionario")

    assert r.status_code == 503
    assert r.json()["detail"]["erro"] == "LLM_INDISPONIVEL"


async def test_perfilamento_e_salvo_mesmo_se_o_dicionario_falhar(cliente, monkeypatch, db):
    """Perder o catálogo porque o LLM caiu seria jogar fora trabalho já feito."""
    from sqlalchemy import select

    from app.llm.cliente import LlmIndisponivel
    from app.models.semantico import Tabela

    async def perfilar_falso(conector, **kw):
        return _retrato_minimo()

    async def sem_chave(retrato, **kw):
        raise LlmIndisponivel("indisponível")

    monkeypatch.setattr("app.routers.conexoes.perfilar", perfilar_falso)
    monkeypatch.setattr("app.routers.conexoes.gerar_dicionario_completo", sem_chave)

    criada = (await cliente.post("/api/v1/conexoes", json=CORPO)).json()
    await cliente.post(f"/api/v1/conexoes/{criada['id']}/dicionario")

    tabelas = (await db.execute(select(Tabela))).scalars().all()
    assert [t.nome for t in tabelas] == ["pedidos"]


async def test_dicionario_aplica_e_devolve_o_custo(cliente, monkeypatch, db):
    from sqlalchemy import select

    from app.llm.cliente import Uso
    from app.models.semantico import Coluna, UnidadeColuna
    from app.semantic.dicionario import PropostaColuna, PropostaTabela, ResultadoCompleto

    async def perfilar_falso(conector, **kw):
        return _retrato_minimo()

    async def gerar_falso(retrato, **kw):
        return ResultadoCompleto(
            tabelas=[
                PropostaTabela(
                    tabela="public.pedidos", descricao="Pedidos de venda.",
                    papel="fato", confianca=0.9,
                    colunas=[
                        PropostaColuna(
                            nome="total",
                            descricao="Valor total do pedido, em centavos.",
                            descricao_negativa="NÃO inclui frete.",
                            unidade=UnidadeColuna.monetaria, escala=100.0,
                            moeda="BRL", confianca=0.8,
                        )
                    ],
                )
            ],
            lotes=1,
            uso=Uso(entrada=1200, cache_leitura=8000, saida=400, modelo="claude-opus-5-5"),
        )

    monkeypatch.setattr("app.routers.conexoes.perfilar", perfilar_falso)
    monkeypatch.setattr("app.routers.conexoes.gerar_dicionario_completo", gerar_falso)

    criada = (await cliente.post("/api/v1/conexoes", json=CORPO)).json()
    r = await cliente.post(f"/api/v1/conexoes/{criada['id']}/dicionario")

    assert r.status_code == 200
    corpo = r.json()
    assert corpo["tabelas_descritas"] == 1
    assert corpo["colunas_descritas"] == 1
    # O custo vai para a tela: gerar dicionário é a operação mais cara da plataforma.
    assert corpo["tokens_cache_leitura"] == 8000
    assert corpo["modelo"] == "claude-opus-5-5"

    total = (await db.execute(select(Coluna).where(Coluna.nome == "total"))).scalar_one()
    assert total.escala == 100.0
    assert total.descricao_negativa == "NÃO inclui frete."
    assert total.revisada_em is None  # proposta da IA, ainda não revisada
