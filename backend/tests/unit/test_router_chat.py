"""Testes do endpoint de pergunta.

O foco é a **procedência**: toda pergunta, respondida ou recusada, precisa deixar uma
linha em `consultas`. As recusadas são o mapa de onde o dicionário está incompleto —
apagá-las jogaria fora o único sinal de como o produto melhora.
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.deps import get_db, get_tenant_atual, get_usuario_atual
from app.llm.cliente import Uso
from app.main import app
from app.models.chat import Consulta, StatusConsulta
from app.models.semantico import UnidadeColuna
from app.models.tenant import PerfilUsuario, Usuario
from app.semantic.persistencia import persistir_retrato
from app.semantic.retrato import RetratoBanco, RetratoColuna, RetratoTabela
from app.semantic.sinais import classificar_coluna
from app.text2sql.pipeline import Resposta
from app.text2sql.spec import Agregacao, ConsultaSpec, MedidaSpec


def rc(nome, tipo="bigint", **kw):
    return RetratoColuna(
        nome=nome, tipo_sql=tipo, aceita_nulo=True, eh_chave_primaria=False,
        sinais=classificar_coluna(tipo_sql=tipo, cardinalidade=100, fracao_nula=0.0,
                                  linhas_tabela=1000),
        cardinalidade=100, **kw,
    )


@pytest_asyncio.fixture
async def catalogado(db, tenant, conexao):
    retrato = RetratoBanco(
        coletado_em=datetime.now(timezone.utc),
        tabelas=[
            RetratoTabela(
                esquema="trabalho", nome="movimentacoes", eh_view=False, papel="fato",
                linhas_estimadas=1000, bytes_estimados=10_000,
                colunas=[rc("salario"), rc("uf", "varchar(2)")],
            )
        ],
    )
    await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=retrato)
    # Dicionário revisado por uma pessoa: salário em centavos.
    from app.models.semantico import Coluna

    salario = (await db.execute(select(Coluna).where(Coluna.nome == "salario"))).scalar_one()
    salario.unidade = UnidadeColuna.monetaria
    salario.escala = 100.0
    salario.moeda = "BRL"
    salario.revisada_em = datetime.now(timezone.utc)
    await db.flush()
    return conexao


@pytest_asyncio.fixture
async def cliente(db, tenant):
    usuario = Usuario(tenant_id=tenant.id, nome="A", email="a@t.com",
                      senha_hash="x", perfil=PerfilUsuario.analista)
    db.add(usuario)
    await db.flush()

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_usuario_atual] = lambda: usuario
    app.dependency_overrides[get_tenant_atual] = lambda: tenant.id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://teste") as c:
        yield c
    app.dependency_overrides.clear()


def dublê(monkeypatch, resposta: Resposta):
    async def falso(pergunta, **kw):
        return resposta

    monkeypatch.setattr("app.routers.chat.responder", falso)
    monkeypatch.setattr("app.routers.chat.criar_conector", lambda c: _ConectorVazio())


class _ConectorVazio:
    async def fechar(self): ...


@pytest.fixture(autouse=True)
def sem_rede(monkeypatch):
    monkeypatch.setattr("app.routers.chat.criar_conector", lambda c: _ConectorVazio())


async def test_resposta_traz_procedencia(cliente, catalogado, monkeypatch):
    dublê(
        monkeypatch,
        Resposta(
            pergunta="salário médio",
            respondeu=True,
            explicacao="Média salarial.",
            spec=ConsultaSpec(
                tabela="trabalho.movimentacoes",
                medidas=[MedidaSpec(agregacao=Agregacao.media, coluna="salario",
                                    rotulo="Salário médio")],
            ),
            sql='SELECT (AVG("salario")) / 100.0 FROM "trabalho"."movimentacoes"',
            linhas=[{"Salário médio": 3200.0}],
            linhas_estimadas=1,
            custo_estimado=12.5,
            duracao_ms=8,
            uso=Uso(entrada=800, cache_leitura=5000, saida=120, modelo="claude-opus-5-5"),
            tabelas_consideradas=["trabalho.movimentacoes"],
        ),
    )
    r = await cliente.post(
        f"/api/v1/conexoes/{catalogado.id}/perguntar", json={"pergunta": "salário médio"}
    )
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["respondeu"] is True
    assert corpo["linhas"] == [{"Salário médio": 3200.0}]

    p = corpo["procedencia"]
    assert "AVG" in p["sql"]
    assert p["versao_dicionario"] == catalogado.versao_dicionario
    assert p["tokens_cache_leitura"] == 5000
    assert p["tabelas_consideradas"] == ["trabalho.movimentacoes"]


async def test_pergunta_respondida_vira_linha_em_consultas(cliente, catalogado, monkeypatch, db):
    dublê(
        monkeypatch,
        Resposta(pergunta="quantas admissoes", respondeu=True, sql="SELECT 1",
                 linhas=[{"a": 1}], uso=Uso(modelo="claude-opus-5-5")),
    )
    await cliente.post(
        f"/api/v1/conexoes/{catalogado.id}/perguntar", json={"pergunta": "quantas admissoes"}
    )

    consulta = (await db.execute(select(Consulta))).scalar_one()
    assert consulta.status is StatusConsulta.executada
    assert consulta.linhas_retornadas == 1
    assert consulta.tenant_id == catalogado.tenant_id


async def test_pergunta_recusada_tambem_e_registrada(cliente, catalogado, monkeypatch, db):
    """As recusadas são o mapa de onde o dicionário está incompleto."""
    dublê(
        monkeypatch,
        Resposta(pergunta="quanto exportamos?", respondeu=False,
                 motivo="Nenhuma tabela tem informação sobre exportações."),
    )
    r = await cliente.post(
        f"/api/v1/conexoes/{catalogado.id}/perguntar", json={"pergunta": "quanto exportamos?"}
    )
    assert r.status_code == 200
    assert r.json()["respondeu"] is False
    assert "exportações" in r.json()["motivo"]

    consulta = (await db.execute(select(Consulta))).scalar_one()
    assert consulta.status is StatusConsulta.recusada
    assert consulta.motivo_recusa


async def test_conexao_de_outro_tenant_responde_404(cliente, catalogado, db, tenant):
    from app.core.crypto import cifrar
    from app.models.conexao import Conexao, TipoBanco
    from app.models.tenant import Tenant

    outro = Tenant(nome="Outro", slug="outro3")
    db.add(outro)
    await db.flush()
    alheia = Conexao(tenant_id=outro.id, nome="X", tipo=TipoBanco.postgres, host="h",
                     porta=5432, banco="b", usuario="u", senha_cifrada=cifrar("x"))
    db.add(alheia)
    await db.flush()

    r = await cliente.post(f"/api/v1/conexoes/{alheia.id}/perguntar", json={"pergunta": "algo"})
    assert r.status_code == 404


async def test_sem_chave_da_anthropic_responde_503(cliente, catalogado, monkeypatch):
    from app.llm.cliente import LlmIndisponivel

    async def sem_chave(pergunta, **kw):
        raise LlmIndisponivel("ANTHROPIC_API_KEY não configurada.")

    monkeypatch.setattr("app.routers.chat.responder", sem_chave)
    r = await cliente.post(
        f"/api/v1/conexoes/{catalogado.id}/perguntar", json={"pergunta": "salario medio"}
    )
    assert r.status_code == 503


async def test_pergunta_curta_demais_e_recusada(cliente, catalogado):
    """Uma letra não é pergunta; barrar aqui evita gastar uma chamada ao modelo."""
    r = await cliente.post(f"/api/v1/conexoes/{catalogado.id}/perguntar", json={"pergunta": "a"})
    assert r.status_code == 422
