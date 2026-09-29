"""Constrói SQL a partir de uma spec validada. Função pura, sem I/O.

É o coração da garantia do produto. Três propriedades, e nenhuma depende de o prompt
ter dado certo naquela vez:

**Nome nunca é interpolado.** Toda tabela e coluna é resolvida contra o catálogo antes
de entrar no SQL; o que não existe vira erro, não texto. O identificador sai citado a
partir do nome **do catálogo**, não do que o modelo escreveu.

**Valor nunca entra no texto.** Todo valor vira parâmetro vinculado (`$1`, `$2`). Não
há caminho por onde uma string do usuário alcance o SQL.

**A escala é aplicada por construção.** Se o dicionário diz que `salario` está em
centavos, `media(salario)` sai como `avg("salario") / 100.0` e o filtro "acima de
5.000" sai como `"salario" > $1` com `$1 = 500000`. O modelo não precisa lembrar — e é
justamente aí que ele erraria, produzindo um número cem vezes maior com cara de certo.

O SQL daqui ainda passa pelos guardrails e pelo EXPLAIN. Camadas, não confiança.
"""

from dataclasses import dataclass, field
from typing import Any

from app.text2sql.catalogo import Catalogo, ColunaCatalogo, TabelaCatalogo
from app.text2sql.spec import (
    AGREGACOES_ESCALAVEIS,
    Agregacao,
    ConsultaSpec,
    DimensaoSpec,
    FiltroSpec,
    Granularidade,
    MedidaSpec,
    Operador,
)


class SpecInvalida(Exception):
    """A spec não casa com o catálogo. A mensagem vai para o usuário — escreva-a
    dizendo o que falta, não o que o validador detectou."""


@dataclass(slots=True)
class ColunaSaida:
    """Descrição de uma coluna do resultado, para a tela e para a procedência."""

    rotulo: str
    tipo: str  # "dimensao" ou "medida"
    coluna_origem: str | None = None
    unidade: str | None = None
    moeda: str | None = None
    escala_aplicada: float = 1.0
    dicionario_revisado: bool = False


@dataclass(slots=True)
class ConsultaConstruida:
    sql: str
    parametros: list[Any] = field(default_factory=list)
    colunas_saida: list[ColunaSaida] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)


# ─── Citação de identificadores ───────────────────────────────────────────────


def citar(nome: str) -> str:
    """Cita um identificador do Postgres.

    O nome vem do catálogo, não da requisição, então isto não é a defesa principal —
    é o que faz funcionar coluna com maiúscula, espaço ou nome de palavra reservada,
    que existe em banco de cliente mais do que se gostaria. A duplicação da aspa
    fecha o caso patológico de um nome que a contenha.
    """
    return '"' + nome.replace('"', '""') + '"'


_TRUNC = {
    Granularidade.dia: "day",
    Granularidade.mes: "month",
    Granularidade.trimestre: "quarter",
    Granularidade.ano: "year",
}

_COMPARADORES = {
    Operador.igual: "=",
    Operador.diferente: "<>",
    Operador.maior: ">",
    Operador.maior_ou_igual: ">=",
    Operador.menor: "<",
    Operador.menor_ou_igual: "<=",
}


class _Parametros:
    """Acumula os valores e devolve o marcador posicional de cada um."""

    def __init__(self) -> None:
        self.valores: list[Any] = []

    def add(self, valor: Any) -> str:
        self.valores.append(valor)
        return f"${len(self.valores)}"


# ─── Resolução contra o catálogo ──────────────────────────────────────────────


def _resolver_coluna(tabela: TabelaCatalogo, nome: str) -> ColunaCatalogo:
    coluna = tabela.coluna(nome)
    if coluna is None:
        disponiveis = ", ".join(sorted(c.nome for c in tabela.colunas.values())[:12])
        raise SpecInvalida(
            f"A coluna '{nome}' não existe em {tabela.qualificado}. "
            f"Colunas conhecidas: {disponiveis}."
        )
    return coluna


def _expressao_medida(medida: MedidaSpec, tabela: TabelaCatalogo) -> tuple[str, ColunaCatalogo | None]:
    if medida.agregacao is Agregacao.contagem and not medida.coluna:
        return "count(*)", None

    coluna = _resolver_coluna(tabela, medida.coluna)
    ref = citar(coluna.nome)

    if medida.agregacao is Agregacao.contagem:
        expr = f"count({ref})"
    elif medida.agregacao is Agregacao.contagem_distinta:
        expr = f"count(DISTINCT {ref})"
    elif medida.agregacao is Agregacao.soma:
        expr = f"sum({ref})"
    elif medida.agregacao is Agregacao.media:
        expr = f"avg({ref})"
    elif medida.agregacao is Agregacao.mediana:
        expr = f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {ref})"
    elif medida.agregacao is Agregacao.minimo:
        expr = f"min({ref})"
    else:
        expr = f"max({ref})"

    # A divisão pela escala. Contagem fica de fora: contar salários em centavos dá o
    # mesmo número que contar salários em reais, e dividir a contagem por 100 seria
    # um erro grosseiro — e silencioso.
    if coluna.precisa_escala and medida.agregacao in AGREGACOES_ESCALAVEIS:
        expr = f"({expr}) / {coluna.escala}"

    return expr, coluna


def _expressao_dimensao(dim: DimensaoSpec, tabela: TabelaCatalogo) -> tuple[str, ColunaCatalogo]:
    coluna = _resolver_coluna(tabela, dim.coluna)
    ref = citar(coluna.nome)
    if dim.granularidade is Granularidade.nenhuma:
        return ref, coluna
    # `date_trunc` para toda granularidade, inclusive ano: devolve data, que ordena
    # corretamente e deixa a formatação para o frontend. `extract(year)` devolveria
    # um inteiro que ordena igual mas perde o tipo, e aí "2026" vira número num
    # gráfico de série temporal.
    return f"date_trunc('{_TRUNC[dim.granularidade]}', {ref})::date", coluna


def _condicao(filtro: FiltroSpec, tabela: TabelaCatalogo, params: _Parametros) -> str:
    coluna = _resolver_coluna(tabela, filtro.coluna)
    ref = citar(coluna.nome)

    if filtro.operador is Operador.e_nulo:
        return f"{ref} IS NULL"
    if filtro.operador is Operador.nao_e_nulo:
        return f"{ref} IS NOT NULL"

    def valor(v: Any) -> Any:
        """Converte da unidade natural para a armazenada.

        `valor_natural = armazenado / escala`, então o caminho de volta é multiplicar.
        Sem isto, "salário acima de 5.000" compararia 5.000 contra centavos e traria
        praticamente a base inteira — uma resposta errada que ninguém questiona,
        porque veio muita linha e não nenhuma.
        """
        if coluna.precisa_escala and isinstance(v, (int, float)) and not isinstance(v, bool):
            return v * coluna.escala
        return v

    if filtro.operador is Operador.entre:
        a, b = params.add(valor(filtro.valores[0])), params.add(valor(filtro.valores[1]))
        return f"{ref} BETWEEN {a} AND {b}"

    if filtro.operador is Operador.em:
        marcadores = ", ".join(params.add(valor(v)) for v in filtro.valores)
        return f"{ref} IN ({marcadores})"

    if filtro.operador is Operador.contem:
        # ILIKE com os curingas no parâmetro, não no SQL: assim `%` digitado pelo
        # usuário é dado, não sintaxe.
        return f"{ref} ILIKE {params.add(f'%{filtro.valores[0]}%')}"

    return f"{ref} {_COMPARADORES[filtro.operador]} {params.add(valor(filtro.valores[0]))}"


# ─── Construção ───────────────────────────────────────────────────────────────


def construir(spec: ConsultaSpec, catalogo: Catalogo) -> ConsultaConstruida:
    """Traduz a spec em SQL parametrizado. Levanta `SpecInvalida` se não casar."""
    tabela = catalogo.tabela(spec.tabela)
    if tabela is None:
        conhecidas = ", ".join(sorted(catalogo.nomes_de_tabela)[:12])
        raise SpecInvalida(
            f"A tabela '{spec.tabela}' não está no catálogo desta conexão. "
            f"Tabelas conhecidas: {conhecidas or 'nenhuma — perfile a conexão primeiro'}."
        )

    params = _Parametros()
    avisos: list[str] = []
    colunas_saida: list[ColunaSaida] = []
    selecoes: list[str] = []

    # ─── Recorte obrigatório ──────────────────────────────────────────────────
    if tabela.recorte_obrigatorio:
        filtradas = {f.coluna.strip().lower() for f in spec.filtros}
        faltando = [c for c in tabela.recorte_obrigatorio if c.lower() not in filtradas]
        if faltando:
            raise SpecInvalida(
                f"Consultas a {tabela.qualificado} precisam de filtro em: "
                + ", ".join(faltando)
                + ". Sem esse recorte a consulta varre a tabela inteira; diga o "
                "período, a região ou o recorte que interessa."
            )

    # ─── Dimensões ────────────────────────────────────────────────────────────
    for dim in spec.dimensoes:
        expr, coluna = _expressao_dimensao(dim, tabela)
        rotulo = dim.rotulo or coluna.nome
        selecoes.append(f"{expr} AS {citar(rotulo)}")
        colunas_saida.append(
            ColunaSaida(
                rotulo=rotulo,
                tipo="dimensao",
                coluna_origem=coluna.nome,
                unidade=coluna.unidade.value,
                dicionario_revisado=coluna.revisada,
            )
        )
        if not coluna.revisada:
            avisos.append(
                f"A descrição de '{coluna.nome}' foi gerada por IA e ainda não foi "
                "revisada por ninguém."
            )

    # ─── Medidas ──────────────────────────────────────────────────────────────
    for medida in spec.medidas:
        expr, coluna = _expressao_medida(medida, tabela)
        selecoes.append(f"{expr} AS {citar(medida.rotulo)}")
        escala = coluna.escala if (coluna and coluna.precisa_escala) else 1.0
        colunas_saida.append(
            ColunaSaida(
                rotulo=medida.rotulo,
                tipo="medida",
                coluna_origem=coluna.nome if coluna else None,
                unidade=coluna.unidade.value if coluna else "contagem",
                moeda=coluna.moeda if coluna else None,
                escala_aplicada=escala if medida.agregacao in AGREGACOES_ESCALAVEIS else 1.0,
                dicionario_revisado=coluna.revisada if coluna else True,
            )
        )
        if coluna and coluna.precisa_escala and not coluna.revisada:
            avisos.append(
                f"'{coluna.nome}' foi convertida dividindo por {coluna.escala}, "
                "conforme o dicionário gerado por IA — que ainda não foi revisado."
            )

    # ─── WHERE ────────────────────────────────────────────────────────────────
    condicoes = [_condicao(f, tabela, params) for f in spec.filtros]

    # ─── Montagem ─────────────────────────────────────────────────────────────
    partes = [f"SELECT {', '.join(selecoes)}", f"FROM {citar(tabela.esquema)}.{citar(tabela.nome)}"]
    if condicoes:
        partes.append("WHERE " + " AND ".join(condicoes))
    if spec.dimensoes:
        # Por posição: as dimensões são sempre as primeiras do SELECT, e repetir a
        # expressão de `date_trunc` no GROUP BY só criaria uma segunda cópia para
        # sair de sincronia com a primeira.
        partes.append("GROUP BY " + ", ".join(str(i + 1) for i in range(len(spec.dimensoes))))
    if spec.ordenacao:
        partes.append(
            "ORDER BY "
            + ", ".join(
                f"{citar(o.por)} {'DESC' if o.decrescente else 'ASC'}" for o in spec.ordenacao
            )
        )
    if spec.limite:
        partes.append(f"LIMIT {int(spec.limite)}")

    return ConsultaConstruida(
        sql="\n".join(partes),
        parametros=params.valores,
        colunas_saida=colunas_saida,
        # Aviso repetido vira ruído: a mesma coluna em dimensão e medida diria a
        # mesma frase duas vezes.
        avisos=list(dict.fromkeys(avisos)),
    )
