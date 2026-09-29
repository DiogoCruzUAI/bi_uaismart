"""Testes dos sinais derivados das estatísticas do catálogo.

O viés que estes testes protegem: **na dúvida, não sinalizar**. Um sinal errado vira
descrição errada no dicionário, que vira número errado na tela. Falso negativo custa
uma revisão humana; falso positivo custa a confiança no produto.
"""

import pytest

from app.semantic.sinais import (
    FamiliaTipo,
    classificar_coluna,
    familia_do_tipo,
    magnitude,
)

# ─── Famílias de tipo ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tipo,esperado",
    [
        ("integer", FamiliaTipo.inteiro),
        ("bigint", FamiliaTipo.inteiro),
        ("smallint", FamiliaTipo.inteiro),
        ("numeric(10,2)", FamiliaTipo.decimal),
        ("double precision", FamiliaTipo.decimal),
        ("character varying(50)", FamiliaTipo.texto),
        ("text", FamiliaTipo.texto),
        ("uuid", FamiliaTipo.texto),
        ("timestamp with time zone", FamiliaTipo.data),
        ("date", FamiliaTipo.data),
        ("boolean", FamiliaTipo.booleano),
        ("jsonb", FamiliaTipo.estruturado),
        ("bytea", FamiliaTipo.binario),
        ("tipo_que_nao_existe", FamiliaTipo.outro),
        ("", FamiliaTipo.outro),
    ],
)
def test_familia_do_tipo(tipo, esperado):
    assert familia_do_tipo(tipo) is esperado


def test_array_de_inteiro_e_estruturado_nao_inteiro():
    """`integer[]` começa com 'int' — o sufixo tem que ser testado antes."""
    assert familia_do_tipo("integer[]") is FamiliaTipo.estruturado
    assert familia_do_tipo("text[]") is FamiliaTipo.estruturado


# ─── Magnitude ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "valor,esperado",
    [
        (0, 0),
        (5, 0),
        (42, 1),
        (3200, 3),
        ("3200", 3),
        ("3200.50", 3),
        (10_000_000_000, 10),
        (-3200, 3),  # o que importa é o tamanho, não o sinal
        (0.5, 0),
        ("2026-09-29", None),  # data não é número
        ("abc", None),
        (None, None),
        (True, None),  # booleano não é magnitude
    ],
)
def test_magnitude(valor, esperado):
    assert magnitude(valor) == esperado


def test_magnitude_distingue_reais_de_centavos():
    """O sinal que permite ao LLM julgar escala.

    R$ 3.200,00 guardado em reais tem magnitude 3; em centavos, 5. O salário máximo
    do CAGED (R$ 100 milhões/mês em centavos) chega a 10. Nós damos o número; o
    julgamento de que dez bilhões não é salário mensal é conhecimento de mundo.
    """
    assert magnitude(3200) == 3
    assert magnitude(320000) == 5
    assert magnitude(10_000_000_000) == 10


# ─── Classificação ────────────────────────────────────────────────────────────


def col(**kw):
    base = {
        "tipo_sql": "integer",
        "cardinalidade": None,
        "fracao_nula": None,
        "linhas_tabela": None,
    }
    return classificar_coluna(**{**base, **kw})


def test_chave_primaria_e_identificador_mesmo_sem_estatistica():
    """Tabela pequena não tem estatística que distinga; a PK dispensa a inferência."""
    s = col(eh_chave_primaria=True, linhas_tabela=10)
    assert s.parece_identificador
    assert not s.parece_categorico


def test_coluna_quase_toda_distinta_em_tabela_grande_e_identificador():
    s = col(cardinalidade=72_000_000, linhas_tabela=73_000_000)
    assert s.parece_identificador


def test_tabela_pequena_nao_gera_identificador_por_estatistica():
    """Em 20 linhas, "quase tudo distinto" não distingue nada."""
    s = col(cardinalidade=20, linhas_tabela=20)
    assert not s.parece_identificador


def test_poucos_valores_distintos_e_categorico():
    """UF: 27 valores em qualquer quantidade de linhas."""
    s = col(cardinalidade=27, linhas_tabela=73_000_000)
    assert s.parece_categorico
    assert not s.parece_identificador


def test_categorico_por_proporcao_em_tabela_grande():
    """5.570 municípios passam do limite absoluto, mas são 0,008% das linhas."""
    s = col(cardinalidade=5_570, linhas_tabela=73_000_000)
    assert s.parece_categorico


def test_valor_unico_e_constante_nao_categorico():
    s = col(cardinalidade=1, linhas_tabela=1000)
    assert s.parece_constante
    assert not s.parece_categorico


def test_coluna_quase_sempre_nula_e_sinalizada():
    """Medida sobre coluna 98% nula é ruído com aparência de estatística."""
    s = col(cardinalidade=100, fracao_nula=0.98, linhas_tabela=1000)
    assert s.quase_sempre_nulo


def test_coluna_parcialmente_nula_nao_e_sinalizada():
    s = col(cardinalidade=100, fracao_nula=0.30, linhas_tabela=1000)
    assert not s.quase_sempre_nulo


def test_sem_estatistica_e_declarado_em_voz_alta():
    """Tabela nunca analisada: o dicionário precisa poder dizer "não sei"."""
    s = col(tipo_sql="integer")
    assert s.sem_estatistica
    assert not s.parece_identificador
    assert not s.parece_categorico
    assert s.fracao_distinta is None


def test_magnitude_so_para_coluna_numerica():
    """O 'maior valor' de uma coluna de texto é ordem alfabética, não grandeza."""
    numerica = col(tipo_sql="bigint", maximo="10000000000")
    assert numerica.magnitude_maxima == 10

    texto = col(tipo_sql="text", maximo="zzz")
    assert texto.magnitude_maxima is None


def test_magnitude_usa_o_extremo_de_maior_modulo():
    s = col(tipo_sql="integer", minimo="-500000", maximo="100")
    assert s.magnitude_maxima == 5
