"""Inferência a partir das estatísticas do catálogo. Funções puras, sem I/O.

Este módulo não decide o que uma coluna **significa** — decide o que ela **parece**,
a partir de números. A diferença importa e define a divisão de trabalho do produto:

- Daqui sai a evidência: esta coluna tem 73 milhões de valores distintos em 73 milhões
  de linhas; aquela tem quatro; esta outra é inteira e chega a dez bilhões.
- Do LLM sai o julgamento: a primeira é um identificador, a segunda é sexo, e a
  terceira é dinheiro **em centavos**, porque dez bilhões de reais de salário mensal
  não existe e cem milhões de centavos é um salário alto porém plausível.

Misturar as duas coisas seria escrever aqui as regras do Leads — e o produto precisa
funcionar em banco de cliente que ninguém nunca viu. Conhecimento de mundo é do modelo;
aritmética sobre estatística é nossa, porque é determinística, barata e testável.

Nenhuma função aqui lê uma linha de dado. Tudo vem de `pg_stats` e `pg_class`, que o
ANALYZE já mantém.
"""

import enum
import re
from dataclasses import dataclass


class FamiliaTipo(str, enum.Enum):
    """Tipo SQL normalizado. `int4`, `bigint` e `smallint` respondem à mesma pergunta."""

    inteiro = "inteiro"
    decimal = "decimal"
    texto = "texto"
    data = "data"
    booleano = "booleano"
    binario = "binario"
    estruturado = "estruturado"  # json, jsonb, array, composto
    outro = "outro"


# Prefixos dos tipos do Postgres. Comparação por prefixo porque o tipo vem com
# modificador — `numeric(10,2)`, `character varying(50)`, `timestamp with time zone`.
_FAMILIAS: tuple[tuple[str, FamiliaTipo], ...] = (
    ("smallint", FamiliaTipo.inteiro),
    ("integer", FamiliaTipo.inteiro),
    ("bigint", FamiliaTipo.inteiro),
    ("int", FamiliaTipo.inteiro),
    ("serial", FamiliaTipo.inteiro),
    ("numeric", FamiliaTipo.decimal),
    ("decimal", FamiliaTipo.decimal),
    ("real", FamiliaTipo.decimal),
    ("double", FamiliaTipo.decimal),
    ("money", FamiliaTipo.decimal),
    ("float", FamiliaTipo.decimal),
    ("character", FamiliaTipo.texto),
    ("varchar", FamiliaTipo.texto),
    ("text", FamiliaTipo.texto),
    ("citext", FamiliaTipo.texto),
    ("uuid", FamiliaTipo.texto),
    ("timestamp", FamiliaTipo.data),
    ("date", FamiliaTipo.data),
    ("time", FamiliaTipo.data),
    ("interval", FamiliaTipo.data),
    ("boolean", FamiliaTipo.booleano),
    ("bool", FamiliaTipo.booleano),
    ("bytea", FamiliaTipo.binario),
    ("json", FamiliaTipo.estruturado),
    ("jsonb", FamiliaTipo.estruturado),
    ("array", FamiliaTipo.estruturado),
    ("hstore", FamiliaTipo.estruturado),
)

# ─── Limiares ─────────────────────────────────────────────────────────────────
# Todos escolhidos para errar para o lado de "não sei". Um sinal errado vira uma
# descrição errada no dicionário, e uma descrição errada vira um número errado na
# tela — que é o pior desfecho possível para este produto. Na dúvida, não sinalizar.

# Acima disso a coluna é praticamente única por linha. Não é 1.0 porque a
# cardinalidade do pg_stats é estimativa e chega a passar do total de linhas.
_FRACAO_IDENTIFICADOR = 0.95
# Abaixo de 100 linhas, "quase tudo distinto" não distingue identificador de
# qualquer coluna de uma tabela pequena.
_LINHAS_MINIMAS_IDENTIFICADOR = 100
# Categórica por contagem absoluta: UF, sexo, situação cadastral, status.
_CARDINALIDADE_CATEGORICA = 50
# Ou por proporção, para tabela grande: 5 mil municípios em 73 milhões de linhas
# é categórico, embora 5 mil passe do limite absoluto.
_FRACAO_CATEGORICA = 0.001
# Coluna que existe mas quase nunca é preenchida. Medida sobre ela é ruído.
_FRACAO_NULA_ALTA = 0.95


@dataclass(frozen=True, slots=True)
class SinaisColuna:
    """O que os números dizem sobre a coluna, antes de qualquer interpretação."""

    familia: FamiliaTipo
    # Cardinalidade dividida pelo total de linhas. `None` quando falta estatística.
    fracao_distinta: float | None
    parece_identificador: bool
    parece_categorico: bool
    parece_constante: bool
    quase_sempre_nulo: bool
    # Ordem de grandeza do maior valor absoluto: 3 significa "chega à casa dos
    # milhares". É o sinal que deixa o LLM julgar unidade e escala — um salário
    # com magnitude 9 não é salário em reais.
    magnitude_maxima: int | None
    # Sem estatística nenhuma, o que sobra é o tipo. Dito em voz alta para o
    # dicionário poder registrar "não sei" em vez de inventar.
    sem_estatistica: bool


def familia_do_tipo(tipo_sql: str) -> FamiliaTipo:
    """Normaliza o tipo do Postgres numa família.

    Array vem como `integer[]` e precisa ser visto como estruturado, não como
    inteiro — por isso o teste de sufixo vem antes da busca por prefixo.
    """
    t = (tipo_sql or "").strip().lower()
    if not t:
        return FamiliaTipo.outro
    if t.endswith("[]"):
        return FamiliaTipo.estruturado
    for prefixo, familia in _FAMILIAS:
        if t.startswith(prefixo):
            return familia
    return FamiliaTipo.outro


def _para_numero(valor: object) -> float | None:
    """Converte o que veio de `pg_stats` em número, quando for número.

    Os extremos chegam como texto (o `anyarray` do pg_stats é lido com `::text`),
    então `'3200'`, `'3200.50'` e `'2026-09-29'` aparecem todos como str. Só o que
    for numérico de verdade interessa.
    """
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor).strip()
    if not re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", texto):
        return None
    try:
        return float(texto)
    except ValueError:
        return None


def magnitude(valor: object) -> int | None:
    """Ordem de grandeza decimal do valor: 3200 → 3, 10_000_000_000 → 10.

    Zero devolve 0. Valor negativo usa o módulo — o que importa é o tamanho.
    """
    n = _para_numero(valor)
    if n is None:
        return None
    n = abs(n)
    if n < 1:
        return 0
    magnitude_atual = 0
    while n >= 10:
        n /= 10
        magnitude_atual += 1
    return magnitude_atual


def classificar_coluna(
    *,
    tipo_sql: str,
    cardinalidade: int | None,
    fracao_nula: float | None,
    linhas_tabela: int | None,
    maximo: object = None,
    minimo: object = None,
    eh_chave_primaria: bool = False,
) -> SinaisColuna:
    """Traduz as estatísticas de uma coluna em sinais.

    Recebe valores soltos em vez de `ColunaBruta` de propósito: mantém o módulo livre
    da camada de conectores e, com isso, testável sem nenhuma dependência de banco.
    """
    familia = familia_do_tipo(tipo_sql)
    sem_estatistica = cardinalidade is None and fracao_nula is None

    fracao_distinta: float | None = None
    if cardinalidade is not None and linhas_tabela:
        fracao_distinta = cardinalidade / linhas_tabela

    parece_constante = cardinalidade is not None and cardinalidade <= 1

    # Chave primária é identificador por definição, não por estatística — e isso
    # cobre a tabela pequena demais para a estatística decidir.
    parece_identificador = eh_chave_primaria or (
        fracao_distinta is not None
        and fracao_distinta >= _FRACAO_IDENTIFICADOR
        and (linhas_tabela or 0) >= _LINHAS_MINIMAS_IDENTIFICADOR
    )

    parece_categorico = False
    if not parece_identificador and not parece_constante and cardinalidade is not None:
        pequena = cardinalidade <= _CARDINALIDADE_CATEGORICA
        proporcionalmente_pequena = (
            fracao_distinta is not None and fracao_distinta <= _FRACAO_CATEGORICA
        )
        parece_categorico = (pequena or proporcionalmente_pequena) and cardinalidade > 1

    quase_sempre_nulo = fracao_nula is not None and fracao_nula >= _FRACAO_NULA_ALTA

    # Magnitude só interessa em coluna numérica: o "maior valor" de uma coluna de
    # texto é ordem alfabética, e converter isso em número não significa nada.
    magnitude_maxima = None
    if familia in (FamiliaTipo.inteiro, FamiliaTipo.decimal):
        candidatos = [m for m in (magnitude(maximo), magnitude(minimo)) if m is not None]
        magnitude_maxima = max(candidatos) if candidatos else None

    return SinaisColuna(
        familia=familia,
        fracao_distinta=fracao_distinta,
        parece_identificador=parece_identificador,
        parece_categorico=parece_categorico,
        parece_constante=parece_constante,
        quase_sempre_nulo=quase_sempre_nulo,
        magnitude_maxima=magnitude_maxima,
        sem_estatistica=sem_estatistica,
    )
