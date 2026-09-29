"""Verificação das consultas de catálogo contra um Postgres de verdade.

**Por que este arquivo existe.** A lógica de decisão está coberta em
`tests/unit/test_privilegios.py`, que roda em qualquer lugar. O que aqueles testes
*não* podem provar é que as consultas de `connectors/postgres.py` executam: se
`pg_has_role` recebe os argumentos certos, se `has_table_privilege` tem a aridade
esperada, se `rolbypassrls` existe na versão em uso. Isso só um servidor responde.

**Como rodar.** Aponte para um Postgres qualquer e execute:

    TEST_PG_DSN=postgres://usuario:senha@host:5432/banco pytest tests/integration -v

Sem a variável, os testes são pulados — não falham. Um teste que exige
infraestrutura e quebra a suíte de quem não a tem é um teste que as pessoas
desativam.

Rode isto na VPS assim que ela subir, e de novo contra o banco de cada cliente novo.
"""

import os

import pytest

from app.connectors.base import avaliar_privilegios
from app.connectors.postgres import ConectorPostgres

DSN = os.environ.get("TEST_PG_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="defina TEST_PG_DSN para rodar os testes de integração"
)


def _conector() -> ConectorPostgres:
    from urllib.parse import unquote, urlparse

    u = urlparse(DSN)
    return ConectorPostgres(
        host=u.hostname or "localhost",
        porta=u.port or 5432,
        banco=(u.path or "/postgres").lstrip("/"),
        usuario=unquote(u.username or "postgres"),
        senha=unquote(u.password or ""),
        usar_ssl=False,
    )


@pytest.fixture
async def conector():
    c = _conector()
    yield c
    await c.fechar()


async def test_consultas_de_privilegio_executam(conector):
    """O que os testes unitários não alcançam: o SQL roda mesmo."""
    p = await conector.coletar_privilegios()

    assert isinstance(p.eh_superusuario, bool)
    assert isinstance(p.pode_criar_no_banco, bool)
    assert isinstance(p.ignora_rls, bool)
    assert isinstance(p.papeis, frozenset)
    assert p.tabelas_com_escrita >= 0
    assert len(p.exemplos_com_escrita) <= 3

    print(f"\n  superusuário:        {p.eh_superusuario}")
    print(f"  CREATE no banco:     {p.pode_criar_no_banco}")
    print(f"  ignora RLS:          {p.ignora_rls}")
    print(f"  papéis:              {sorted(p.papeis) or '(nenhum)'}")
    print(f"  tabelas com escrita: {p.tabelas_com_escrita} {list(p.exemplos_com_escrita)}")

    impedimentos, alertas = avaliar_privilegios(p)
    for i in impedimentos:
        print(f"  IMPEDIMENTO: {i}")
    for a in alertas:
        print(f"  alerta:      {a}")


async def test_superusuario_e_de_fato_recusado(conector):
    """Prova de ponta a ponta do conserto — só roda se o DSN for de superusuário.

    É o caso que a verificação antiga aprovava. Aponte `TEST_PG_DSN` para o usuário
    `postgres` uma vez para ver a recusa acontecer de verdade.
    """
    from app.connectors.base import ErroDeConexao

    p = await conector.coletar_privilegios()
    if not p.eh_superusuario:
        pytest.skip("DSN não é de superusuário; nada a provar aqui")

    with pytest.raises(ErroDeConexao, match="superusuário"):
        await conector.testar()


async def test_credencial_de_leitura_e_aceita(conector):
    """Espelho do anterior: com o GRANT de docs/INFRA.md, `testar()` não levanta."""
    p = await conector.coletar_privilegios()
    impedimentos, _ = avaliar_privilegios(p)
    if impedimentos:
        pytest.skip(f"DSN não é somente-leitura: {impedimentos[0]}")

    alertas = await conector.testar()
    assert isinstance(alertas, list)


async def test_perfilamento_nao_le_dado(conector):
    """As consultas de catálogo funcionam e não dependem de permissão em tabela."""
    tabelas = await conector.listar_tabelas()
    assert isinstance(tabelas, list)
    if tabelas:
        t = tabelas[0]
        colunas = await conector.listar_colunas(t.esquema, t.nome)
        assert isinstance(colunas, list)
        print(f"\n  {t.esquema}.{t.nome}: {len(colunas)} colunas, "
              f"~{t.linhas_estimadas} linhas estimadas")
