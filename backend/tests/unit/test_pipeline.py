"""Testes do pipeline de pergunta, sem chamar a API.

O que é testável sem chave: a recuperação de contexto, a montagem do pedido e o
comportamento do pipeline diante de cada desfecho (modelo recusa, spec inválida,
guardrail barra, EXPLAIN barra, tudo certo). A chamada ao modelo é substituída por um
dublê que devolve a resposta que o teste quiser.

O caso mais importante é `test_recusa_do_modelo_vira_motivo_nao_excecao`: quando o
catálogo não responde a pergunta, a pessoa precisa saber **por quê**. "A tabela não
tem essa informação" e "a consulta ficou grande demais" pedem reações diferentes
dela.
"""

import pytest

from app.connectors.base import (
    Conector,
    Estimativa,
    ResultadoConsulta,
)
from app.llm.cliente import Uso
from app.models.semantico import UnidadeColuna
from app.text2sql.catalogo import Catalogo, ColunaCatalogo, TabelaCatalogo
from app.text2sql.pipeline import (
    RespostaDoModelo,
    montar_pedido,
    responder,
    selecionar_tabelas,
)
from app.text2sql.spec import (
    Agregacao,
    ConsultaSpec,
    FiltroSpec,
    MedidaSpec,
    Operador,
)


def col(nome, tipo="integer", **kw):
    return ColunaCatalogo(nome=nome, tipo_sql=tipo, revisada=True, **kw)


@pytest.fixture
def catalogo():
    cat = Catalogo(versao_dicionario=2)

    mov = TabelaCatalogo(
        esquema="trabalho", nome="movimentacoes", revisada=True,
        descricao="Admissões e desligamentos com carteira assinada.",
    )
    for c in [
        col("uf", "varchar(2)", unidade=UnidadeColuna.codigo),
        col("salario", "bigint", unidade=UnidadeColuna.monetaria, escala=100.0, moeda="BRL",
            descricao="Remuneração mensal, em centavos.",
            descricao_negativa="NÃO é o salário líquido."),
        col("competencia", "date", unidade=UnidadeColuna.data),
    ]:
        mov.colunas[c.nome] = c
    cat.tabelas[mov.qualificado.lower()] = mov

    emp = TabelaCatalogo(
        esquema="cadastro", nome="empresas", revisada=True,
        descricao="Empresas brasileiras registradas na Receita.",
    )
    for c in [col("cnpj", "varchar(14)"), col("capital_social", "numeric(18,2)")]:
        emp.colunas[c.nome] = c
    cat.tabelas[emp.qualificado.lower()] = emp

    return cat


class ConectorDublê(Conector):
    dialeto = "postgres"

    def __init__(self, *, linhas=10, linhas_varridas=1000, custo=100.0, resultado=None):
        self._est = Estimativa(linhas=linhas, linhas_varridas=linhas_varridas, custo=custo)
        self._resultado = resultado or ResultadoConsulta(
            colunas=["Estado", "Salário médio"],
            linhas=[{"Estado": "MG", "Salário médio": 3200.0}],
            duracao_ms=12,
        )
        self.parametros_recebidos: list = []

    async def estimar(self, sql, parametros=()):
        return self._est

    async def executar(self, sql, limite_linhas, parametros=()):
        self.parametros_recebidos = list(parametros)
        return self._resultado

    async def testar(self): return []
    async def coletar_privilegios(self): raise NotImplementedError
    async def listar_tabelas(self): return []
    async def listar_colunas(self, esquema, tabela): return []
    async def listar_relacionamentos(self): return []
    async def fechar(self): ...


def dublê_do_modelo(resposta: RespostaDoModelo, monkeypatch):
    async def falso(**kw):
        return resposta, Uso(entrada=900, cache_leitura=4000, saida=180, modelo="claude-opus-5-5")

    monkeypatch.setattr("app.text2sql.pipeline.pedir_estruturado", falso)


def spec_boa():
    return ConsultaSpec(
        tabela="trabalho.movimentacoes",
        medidas=[MedidaSpec(agregacao=Agregacao.media, coluna="salario", rotulo="Salário médio")],
        filtros=[FiltroSpec(coluna="uf", operador=Operador.igual, valores=["MG"])],
    )


# ─── Recuperação de contexto ──────────────────────────────────────────────────


def test_pergunta_sobre_salario_encontra_a_tabela_certa(catalogo):
    escolhidas = selecionar_tabelas("qual o salário médio em MG?", catalogo)
    assert escolhidas[0] == "trabalho.movimentacoes"


def test_pergunta_sobre_empresas_encontra_a_outra(catalogo):
    escolhidas = selecionar_tabelas("quantas empresas existem?", catalogo)
    assert escolhidas[0] == "cadastro.empresas"


def test_acento_na_pergunta_casa_com_coluna_sem_acento(catalogo):
    """`salário` na pergunta precisa casar com `salario` na coluna."""
    assert "trabalho.movimentacoes" in selecionar_tabelas("salário", catalogo)


def test_palavras_vazias_nao_decidem(catalogo):
    """"Quantos" e "de" aparecem em toda pergunta e não distinguem tabela nenhuma."""
    escolhidas = selecionar_tabelas("quantos de os das para", catalogo)
    assert len(escolhidas) == 2  # devolve tudo, sem preferência inventada


def test_contexto_e_limitado(catalogo):
    assert len(selecionar_tabelas("salario empresas", catalogo, maximo=1)) == 1


def test_catalogo_vazio_nao_quebra():
    assert selecionar_tabelas("qualquer coisa", Catalogo()) == []


# ─── Pedido ───────────────────────────────────────────────────────────────────


def test_pedido_leva_o_que_a_coluna_nao_significa(catalogo):
    """É o campo que evita responder 'líquido' com uma coluna de bruto."""
    pedido = montar_pedido("salário médio", catalogo, ["trabalho.movimentacoes"])
    assert "NÃO é o salário líquido" in pedido


def test_pedido_leva_a_escala(catalogo):
    pedido = montar_pedido("salário médio", catalogo, ["trabalho.movimentacoes"])
    assert '"escala": 100.0' in pedido


def test_pedido_e_estavel(catalogo):
    a = montar_pedido("x", catalogo, ["trabalho.movimentacoes"])
    b = montar_pedido("x", catalogo, ["trabalho.movimentacoes"])
    assert a == b


def test_pedido_nao_leva_tabela_fora_do_recorte(catalogo):
    pedido = montar_pedido("x", catalogo, ["trabalho.movimentacoes"])
    assert "cadastro.empresas" not in pedido


# ─── Pipeline ─────────────────────────────────────────────────────────────────


async def test_pergunta_respondida_de_ponta_a_ponta(catalogo, monkeypatch):
    dublê_do_modelo(
        RespostaDoModelo(
            pode_responder=True,
            explicacao="Média salarial das admissões em Minas Gerais.",
            consulta=spec_boa(),
        ),
        monkeypatch,
    )
    conector = ConectorDublê()
    r = await responder("qual o salário médio em MG?", catalogo=catalogo, conector=conector)

    assert r.respondeu
    assert r.linhas == [{"Estado": "MG", "Salário médio": 3200.0}]
    # `r.sql` é o SQL EXECUTADO, regerado da AST pelos guardrails — por isso a
    # comparação ignora caixa: o sqlglot normaliza `avg` para `AVG`. Guardar o
    # executado, e não o construído, é o que faz a procedência valer alguma coisa.
    assert "avg(" in r.sql.lower()
    # A escala do dicionário foi aplicada pelo nosso código, não pelo modelo.
    assert "/ 100.0" in r.sql
    assert r.explicacao.startswith("Média salarial")
    assert r.uso.cache_leitura == 4000


async def test_recusa_do_modelo_vira_motivo_nao_excecao(catalogo, monkeypatch):
    """Quando o catálogo não responde, a pessoa precisa saber por quê."""
    dublê_do_modelo(
        RespostaDoModelo(
            pode_responder=False,
            motivo="Nenhuma tabela disponível tem informação sobre exportações.",
        ),
        monkeypatch,
    )
    r = await responder("quanto exportamos?", catalogo=catalogo, conector=ConectorDublê())

    assert not r.respondeu
    assert "exportações" in r.motivo
    assert r.sql is None


async def test_spec_com_coluna_inventada_vira_motivo(catalogo, monkeypatch):
    dublê_do_modelo(
        RespostaDoModelo(
            pode_responder=True,
            consulta=ConsultaSpec(
                tabela="trabalho.movimentacoes",
                medidas=[
                    MedidaSpec(agregacao=Agregacao.soma, coluna="nao_existe", rotulo="X")
                ],
            ),
        ),
        monkeypatch,
    )
    r = await responder("qualquer", catalogo=catalogo, conector=ConectorDublê())
    assert not r.respondeu
    assert "não existe" in r.motivo


async def test_consulta_grande_demais_vira_motivo(catalogo, monkeypatch):
    """A porta do EXPLAIN chega ao usuário como orientação, não como erro."""
    dublê_do_modelo(
        RespostaDoModelo(pode_responder=True, consulta=spec_boa()), monkeypatch
    )
    conector = ConectorDublê(linhas_varridas=300_000_000)
    r = await responder("qualquer", catalogo=catalogo, conector=conector)

    assert not r.respondeu
    assert "leria cerca de" in r.motivo


async def test_parametros_chegam_ao_conector_convertidos(catalogo, monkeypatch):
    """O valor vai vinculado e na unidade armazenada — nunca no texto do SQL."""
    dublê_do_modelo(
        RespostaDoModelo(
            pode_responder=True,
            consulta=ConsultaSpec(
                tabela="trabalho.movimentacoes",
                medidas=[MedidaSpec(agregacao=Agregacao.contagem, rotulo="Total")],
                filtros=[
                    FiltroSpec(coluna="salario", operador=Operador.maior, valores=[5000])
                ],
            ),
        ),
        monkeypatch,
    )
    conector = ConectorDublê()
    r = await responder("acima de 5 mil", catalogo=catalogo, conector=conector)

    assert r.respondeu
    assert conector.parametros_recebidos == [500000]


async def test_catalogo_vazio_orienta_a_perfilar(monkeypatch):
    r = await responder("qualquer", catalogo=Catalogo(), conector=ConectorDublê())
    assert not r.respondeu
    assert "Perfile" in r.motivo


async def test_dicionario_nao_revisado_vira_aviso(catalogo, monkeypatch):
    mov = catalogo.tabela("trabalho.movimentacoes")
    mov.colunas["salario"] = ColunaCatalogo(
        nome="salario", tipo_sql="bigint", unidade=UnidadeColuna.monetaria,
        escala=100.0, revisada=False,
    )
    dublê_do_modelo(
        RespostaDoModelo(pode_responder=True, consulta=spec_boa()), monkeypatch
    )
    r = await responder("salário médio", catalogo=catalogo, conector=ConectorDublê())
    assert r.respondeu
    assert any("não foi revisado" in a for a in r.avisos)


async def test_resultado_truncado_avisa(catalogo, monkeypatch):
    dublê_do_modelo(
        RespostaDoModelo(pode_responder=True, consulta=spec_boa()), monkeypatch
    )
    conector = ConectorDublê(
        resultado=ResultadoConsulta(colunas=["x"], linhas=[{"x": 1}], duracao_ms=5, truncado=True)
    )
    r = await responder("x", catalogo=catalogo, conector=conector)
    assert any("cortado" in a for a in r.avisos)


async def test_procedencia_e_registrada(catalogo, monkeypatch):
    """Todo número exibido precisa apontar para o que o produziu."""
    dublê_do_modelo(
        RespostaDoModelo(pode_responder=True, consulta=spec_boa()), monkeypatch
    )
    r = await responder("x", catalogo=catalogo, conector=ConectorDublê(linhas=42, custo=99.5))

    assert r.sql is not None
    assert r.spec is not None
    assert r.linhas_estimadas == 42
    assert r.custo_estimado == 99.5
    assert r.duracao_ms == 12
    assert r.tabelas_consideradas
