"""Testes da execução vigiada.

Um conector falso substitui o Postgres: o que está sob teste aqui é a **política** —
o que é liberado, o que é recusado e com qual explicação —, não o driver.
"""

import pytest

from app.connectors.base import Conector, Estimativa, LimiteExcedido, ResultadoConsulta
from app.connectors.postgres import _maior_plan_rows
from app.core.config import settings
from app.text2sql.executor import executar_vigiado
from app.text2sql.guardrails import SqlRecusado

TABELAS = {"vendas.pedidos", "cadastro.clientes"}


class ConectorFalso(Conector):
    """Devolve a estimativa que o teste mandar e registra o que foi executado."""

    dialeto = "postgres"

    def __init__(self, *, linhas=10, linhas_varridas=1000, custo=100.0):
        self._estimativa = Estimativa(
            linhas=linhas, linhas_varridas=linhas_varridas, custo=custo
        )
        self.sql_executado: str | None = None

    async def estimar(self, sql: str) -> Estimativa:
        return self._estimativa

    async def executar(self, sql: str, limite_linhas: int) -> ResultadoConsulta:
        self.sql_executado = sql
        return ResultadoConsulta(colunas=["x"], linhas=[{"x": 1}], duracao_ms=5)

    async def testar(self) -> None: ...
    async def listar_tabelas(self): return []
    async def listar_colunas(self, esquema, tabela): return []
    async def listar_relacionamentos(self): return []
    async def fechar(self) -> None: ...


async def _executar(sql, conector, tabelas=TABELAS):
    return await executar_vigiado(sql, conector=conector, tabelas_permitidas=tabelas)


# ─── O que passa ──────────────────────────────────────────────────────────────


async def test_consulta_pequena_executa():
    c = ConectorFalso()
    r = await _executar("SELECT id FROM vendas.pedidos", c)
    assert r.resultado.linhas == [{"x": 1}]
    assert c.sql_executado is not None


async def test_limite_e_imposto_em_consulta_sem_agregacao():
    c = ConectorFalso()
    await _executar("SELECT id FROM vendas.pedidos", c)
    assert f"LIMIT {settings.max_result_rows}" in c.sql_executado.upper()


async def test_agregacao_nao_recebe_limite():
    """Limitar uma agregação mudaria a resposta; a memória é contida pelo cursor."""
    c = ConectorFalso()
    await _executar("SELECT uf, count(*) FROM vendas.pedidos GROUP BY uf", c)
    assert "LIMIT" not in c.sql_executado.upper()


# ─── As três portas ───────────────────────────────────────────────────────────


async def test_varredura_grande_demais_e_recusada():
    """`count(*)` numa tabela gigante: 1 linha de saída, 300 milhões varridas."""
    c = ConectorFalso(linhas=1, linhas_varridas=300_000_000, custo=10.0)
    with pytest.raises(LimiteExcedido, match="leria cerca de"):
        await _executar("SELECT count(*) FROM vendas.pedidos", c)
    assert c.sql_executado is None, "a consulta não pode ter chegado ao banco"


async def test_saida_grande_demais_e_recusada():
    """Agregação de alta cardinalidade: varre pouco, devolve milhões de grupos."""
    c = ConectorFalso(linhas=8_000_000, linhas_varridas=9_000_000, custo=100.0)
    with pytest.raises(LimiteExcedido, match="devolveria cerca de"):
        await _executar("SELECT id, count(*) FROM vendas.pedidos GROUP BY id", c)
    assert c.sql_executado is None


async def test_custo_alto_e_recusado():
    c = ConectorFalso(linhas=10, linhas_varridas=100, custo=9_999_999.0)
    with pytest.raises(LimiteExcedido, match="custo"):
        await _executar("SELECT id FROM vendas.pedidos", c)
    assert c.sql_executado is None


async def test_sql_invalido_nao_chega_a_estimativa():
    """Guardrail estático roda antes de qualquer ida ao banco — inclusive o EXPLAIN."""
    c = ConectorFalso()
    with pytest.raises(SqlRecusado):
        await _executar("DROP TABLE vendas.pedidos", c)
    assert c.sql_executado is None


async def test_tabela_desconhecida_nao_chega_ao_banco():
    c = ConectorFalso()
    with pytest.raises(SqlRecusado):
        await _executar("SELECT * FROM secreto.folha", c)
    assert c.sql_executado is None


# ─── Leitura do plano ─────────────────────────────────────────────────────────


def test_maior_plan_rows_desce_a_arvore():
    """O nó de topo de um count() diz 1; a varredura abaixo dele diz 293 milhões."""
    plano = {
        "Node Type": "Aggregate",
        "Plan Rows": 1,
        "Plans": [
            {
                "Node Type": "Gather",
                "Plan Rows": 4,
                "Plans": [{"Node Type": "Seq Scan", "Plan Rows": 293_000_000}],
            }
        ],
    }
    assert _maior_plan_rows(plano) == 293_000_000


def test_maior_plan_rows_em_plano_raso():
    assert _maior_plan_rows({"Node Type": "Seq Scan", "Plan Rows": 42}) == 42


def test_maior_plan_rows_aguenta_plano_profundo():
    """Plano com muitos JOINs não pode estourar a pilha — a porta cairia aberta."""
    plano = {"Plan Rows": 1}
    for i in range(2, 2002):
        plano = {"Plan Rows": i, "Plans": [plano]}
    assert _maior_plan_rows(plano) == 2001
