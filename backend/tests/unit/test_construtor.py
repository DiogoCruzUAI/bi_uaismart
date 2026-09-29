"""Testes do construtor de SQL.

Os dois mais importantes do arquivo são `test_media_de_coluna_em_centavos_divide` e
`test_filtro_em_coluna_escalada_multiplica_o_valor`. Juntos eles provam que a
armadilha mais cara do produto — salário em centavos — deixou de depender da memória
do modelo e passou a ser propriedade do código.

O segundo é o menos óbvio: sem a multiplicação, "salário acima de 5.000" compararia
5.000 contra centavos e traria praticamente a base inteira. Uma resposta errada que
ninguém questiona, porque veio muita linha e não nenhuma.
"""

import pytest
import sqlglot

from app.models.semantico import UnidadeColuna
from app.text2sql.catalogo import Catalogo, ColunaCatalogo, TabelaCatalogo
from app.text2sql.construtor import SpecInvalida, citar, construir
from app.text2sql.spec import (
    Agregacao,
    ConsultaSpec,
    DimensaoSpec,
    FiltroSpec,
    Granularidade,
    MedidaSpec,
    Operador,
    OrdenacaoSpec,
)


def coluna(nome, tipo="integer", *, unidade=UnidadeColuna.desconhecida, escala=1.0,
           moeda=None, revisada=True):
    return ColunaCatalogo(
        nome=nome, tipo_sql=tipo, unidade=unidade, escala=escala,
        moeda=moeda, revisada=revisada,
    )


@pytest.fixture
def catalogo():
    mov = TabelaCatalogo(esquema="trabalho", nome="movimentacoes", revisada=True)
    for c in [
        coluna("id", "bigint"),
        coluna("uf", "character varying(2)", unidade=UnidadeColuna.codigo),
        coluna("municipio", "integer", unidade=UnidadeColuna.codigo),
        coluna("competencia", "date", unidade=UnidadeColuna.data),
        # A armadilha: guardada em centavos.
        coluna("salario", "bigint", unidade=UnidadeColuna.monetaria, escala=100.0, moeda="BRL"),
        coluna("horas", "integer", unidade=UnidadeColuna.duracao),
    ]:
        mov.colunas[c.nome] = c

    cat = Catalogo(versao_dicionario=3)
    cat.tabelas[mov.qualificado.lower()] = mov
    return cat


def spec(**kw):
    base = {
        "tabela": "trabalho.movimentacoes",
        "medidas": [MedidaSpec(agregacao=Agregacao.contagem, rotulo="Total")],
    }
    return ConsultaSpec(**{**base, **kw})


def sql_de(s, cat):
    return construir(s, cat).sql


# ─── A escala: a razão de o módulo existir ────────────────────────────────────


def test_media_de_coluna_em_centavos_divide(catalogo):
    s = spec(medidas=[MedidaSpec(agregacao=Agregacao.media, coluna="salario", rotulo="Média")])
    construida = construir(s, catalogo)
    assert "(avg(\"salario\")) / 100.0" in construida.sql
    assert construida.colunas_saida[0].escala_aplicada == 100.0


def test_filtro_em_coluna_escalada_multiplica_o_valor(catalogo):
    """"Acima de 5.000 reais" precisa virar 500.000 centavos no parâmetro."""
    s = spec(
        filtros=[FiltroSpec(coluna="salario", operador=Operador.maior, valores=[5000])]
    )
    construida = construir(s, catalogo)
    assert construida.parametros == [500000]
    # E o valor não aparece no texto do SQL.
    assert "5000" not in construida.sql


def test_entre_converte_os_dois_extremos(catalogo):
    s = spec(
        filtros=[
            FiltroSpec(coluna="salario", operador=Operador.entre, valores=[2000, 5000])
        ]
    )
    assert construir(s, catalogo).parametros == [200000, 500000]


def test_contagem_nao_e_dividida_pela_escala(catalogo):
    """Contar salários em centavos dá o mesmo número que contar em reais.

    Dividir a contagem por 100 seria um erro grosseiro — e silencioso.
    """
    s = spec(
        medidas=[MedidaSpec(agregacao=Agregacao.contagem, coluna="salario", rotulo="Quantos")]
    )
    construida = construir(s, catalogo)
    assert "/ 100.0" not in construida.sql
    assert construida.colunas_saida[0].escala_aplicada == 1.0


def test_coluna_sem_escala_nao_e_dividida(catalogo):
    s = spec(medidas=[MedidaSpec(agregacao=Agregacao.media, coluna="horas", rotulo="Média")])
    assert "/" not in sql_de(s, catalogo)


def test_filtro_em_coluna_sem_escala_nao_converte(catalogo):
    s = spec(filtros=[FiltroSpec(coluna="horas", operador=Operador.maior, valores=[40])])
    assert construir(s, catalogo).parametros == [40]


def test_filtro_de_texto_nao_e_multiplicado(catalogo):
    """Só número converte; multiplicar 'MG' por 100 não faz sentido nenhum."""
    s = spec(filtros=[FiltroSpec(coluna="uf", operador=Operador.igual, valores=["MG"])])
    assert construir(s, catalogo).parametros == ["MG"]


# ─── Segurança: nome e valor ──────────────────────────────────────────────────


def test_tabela_fora_do_catalogo_e_recusada(catalogo):
    with pytest.raises(SpecInvalida, match="não está no catálogo"):
        construir(spec(tabela="publico.secreta"), catalogo)


def test_coluna_fora_do_catalogo_e_recusada(catalogo):
    with pytest.raises(SpecInvalida, match="não existe"):
        construir(spec(dimensoes=[DimensaoSpec(coluna="coluna_inventada")]), catalogo)


def test_nome_malicioso_nao_vira_sql(catalogo):
    """O nome nem chega a ser citado: não existe no catálogo, então é erro."""
    with pytest.raises(SpecInvalida):
        construir(
            spec(dimensoes=[DimensaoSpec(coluna='uf"; DROP TABLE x; --')]), catalogo
        )


def test_valor_nunca_entra_no_texto_do_sql(catalogo):
    malicioso = "'; DROP TABLE movimentacoes; --"
    s = spec(filtros=[FiltroSpec(coluna="uf", operador=Operador.igual, valores=[malicioso])])
    construida = construir(s, catalogo)
    assert "DROP" not in construida.sql.upper()
    assert construida.parametros == [malicioso]


def test_curinga_do_contem_fica_no_parametro(catalogo):
    """`%` digitado pelo usuário é dado, não sintaxe."""
    s = spec(filtros=[FiltroSpec(coluna="uf", operador=Operador.contem, valores=["100%"])])
    construida = construir(s, catalogo)
    assert construida.parametros == ["%100%%"]


def test_citacao_escapa_aspas():
    assert citar('col"estranha') == '"col""estranha"'


# ─── Recorte obrigatório ──────────────────────────────────────────────────────


def test_recorte_obrigatorio_e_exigido(catalogo):
    """A defesa contra a varredura de centenas de milhões de linhas.

    O EXPLAIN recusaria depois, mas com uma mensagem bem menos útil que
    "informe o estado ou o município".
    """
    catalogo.tabela("trabalho.movimentacoes").recorte_obrigatorio = ["uf"]
    with pytest.raises(SpecInvalida, match="precisam de filtro em"):
        construir(spec(), catalogo)


def test_recorte_obrigatorio_satisfeito_passa(catalogo):
    catalogo.tabela("trabalho.movimentacoes").recorte_obrigatorio = ["uf"]
    s = spec(filtros=[FiltroSpec(coluna="uf", operador=Operador.igual, valores=["MG"])])
    assert "WHERE" in sql_de(s, catalogo)


# ─── SQL gerado ───────────────────────────────────────────────────────────────


def test_sql_gerado_e_valido(catalogo):
    """Tudo que sai daqui ainda passa pelos guardrails — então precisa parsear."""
    s = spec(
        medidas=[
            MedidaSpec(agregacao=Agregacao.media, coluna="salario", rotulo="Média"),
            MedidaSpec(agregacao=Agregacao.contagem, rotulo="Total"),
        ],
        dimensoes=[DimensaoSpec(coluna="competencia", granularidade=Granularidade.mes,
                                rotulo="Mês")],
        filtros=[FiltroSpec(coluna="uf", operador=Operador.igual, valores=["MG"])],
        ordenacao=[OrdenacaoSpec(por="Mês", decrescente=False)],
        limite=24,
    )
    construida = construir(s, catalogo)
    arvore = sqlglot.parse_one(construida.sql, read="postgres")
    assert isinstance(arvore, sqlglot.exp.Select)


def test_sql_passa_pelos_proprios_guardrails(catalogo):
    """Defesa em camadas: o que construímos também é validado."""
    from app.text2sql.guardrails import validar

    s = spec(
        medidas=[MedidaSpec(agregacao=Agregacao.mediana, coluna="salario", rotulo="Mediana")],
        dimensoes=[DimensaoSpec(coluna="uf", rotulo="Estado")],
    )
    construida = construir(s, catalogo)
    resultado = validar(
        construida.sql, tabelas_permitidas={"trabalho.movimentacoes"}
    )
    assert resultado.tem_agregacao


def test_mediana_usa_percentile_cont(catalogo):
    s = spec(medidas=[MedidaSpec(agregacao=Agregacao.mediana, coluna="salario", rotulo="Mediana")])
    assert "percentile_cont(0.5) WITHIN GROUP" in sql_de(s, catalogo)


def test_granularidade_temporal_trunca(catalogo):
    s = spec(dimensoes=[DimensaoSpec(coluna="competencia", granularidade=Granularidade.ano,
                                     rotulo="Ano")])
    assert "date_trunc('year'" in sql_de(s, catalogo)


def test_group_by_usa_posicao(catalogo):
    s = spec(
        dimensoes=[DimensaoSpec(coluna="uf", rotulo="Estado"),
                   DimensaoSpec(coluna="municipio", rotulo="Município")]
    )
    assert "GROUP BY 1, 2" in sql_de(s, catalogo)


def test_sem_dimensao_nao_ha_group_by(catalogo):
    assert "GROUP BY" not in sql_de(spec(), catalogo)


# ─── Procedência ──────────────────────────────────────────────────────────────


def test_colunas_de_saida_descrevem_o_resultado(catalogo):
    s = spec(
        medidas=[MedidaSpec(agregacao=Agregacao.soma, coluna="salario", rotulo="Massa salarial")],
        dimensoes=[DimensaoSpec(coluna="uf", rotulo="Estado")],
    )
    construida = construir(s, catalogo)
    assert [c.tipo for c in construida.colunas_saida] == ["dimensao", "medida"]
    massa = construida.colunas_saida[1]
    assert massa.moeda == "BRL"
    assert massa.escala_aplicada == 100.0


def test_dicionario_nao_revisado_gera_aviso(catalogo):
    """A tela precisa dizer que o número saiu de uma descrição que ninguém conferiu."""
    tabela = catalogo.tabela("trabalho.movimentacoes")
    tabela.colunas["salario"] = coluna(
        "salario", "bigint", unidade=UnidadeColuna.monetaria, escala=100.0, revisada=False
    )
    s = spec(medidas=[MedidaSpec(agregacao=Agregacao.media, coluna="salario", rotulo="Média")])
    construida = construir(s, catalogo)
    assert any("não foi revisado" in a for a in construida.avisos)
    assert not construida.colunas_saida[0].dicionario_revisado


def test_aviso_nao_se_repete(catalogo):
    tabela = catalogo.tabela("trabalho.movimentacoes")
    tabela.colunas["uf"] = coluna("uf", "varchar(2)", revisada=False)
    s = spec(
        dimensoes=[DimensaoSpec(coluna="uf", rotulo="A"), DimensaoSpec(coluna="uf", rotulo="B")]
    )
    construida = construir(s, catalogo)
    assert len(construida.avisos) == 1


# ─── Spec malformada ──────────────────────────────────────────────────────────


def test_agregacao_sem_coluna_e_recusada_na_spec():
    with pytest.raises(ValueError, match="exige uma coluna"):
        MedidaSpec(agregacao=Agregacao.media, rotulo="Média")


def test_entre_com_um_valor_e_recusado_na_spec():
    with pytest.raises(ValueError, match="espera 2"):
        FiltroSpec(coluna="salario", operador=Operador.entre, valores=[100])


def test_ordenacao_por_algo_nao_declarado_e_recusada():
    with pytest.raises(ValueError, match="não é medida nem dimensão"):
        ConsultaSpec(
            tabela="t",
            medidas=[MedidaSpec(agregacao=Agregacao.contagem, rotulo="Total")],
            ordenacao=[OrdenacaoSpec(por="Inexistente")],
        )


def test_consulta_sem_medida_e_recusada():
    with pytest.raises(ValueError):
        ConsultaSpec(tabela="t", medidas=[])
