"""A linguagem de consulta que o modelo escreve — e que não é SQL.

O LLM não escreve SQL por padrão. Ele descreve **o que quer**: quais medidas, por
quais dimensões, com quais filtros. Nosso código traduz isso em SQL a partir do
catálogo.

A diferença não é estilística. Com SQL livre, a correção de cada resposta depende de o
prompt ter dado certo naquela vez. Com uma spec validada contra o catálogo:

- nome de tabela e coluna **nunca** é interpolado — só resolve se existir no catálogo;
- valor **nunca** entra no texto do SQL — vira parâmetro vinculado;
- `escala` do dicionário é aplicada por construção. O modelo pede "média de salario" e
  não precisa lembrar que a coluna está em centavos: quem divide por 100 é o
  construtor, sempre, inclusive nos filtros. É onde a armadilha mais cara do produto
  deixa de depender da memória do modelo.

Spec malformada é recusada antes de virar SQL; SQL gerado a partir dela ainda passa
pelos guardrails e pelo EXPLAIN. Camadas.
"""

import enum

from pydantic import BaseModel, Field, model_validator


class Agregacao(str, enum.Enum):
    contagem = "contagem"
    contagem_distinta = "contagem_distinta"
    soma = "soma"
    media = "media"
    mediana = "mediana"
    minimo = "minimo"
    maximo = "maximo"


# Agregações que produzem um valor na mesma unidade da coluna e por isso precisam da
# escala aplicada. Contagem não: contar salários em centavos dá o mesmo número que
# contar salários em reais, e dividir a contagem por 100 seria um erro grosseiro.
AGREGACOES_ESCALAVEIS = frozenset(
    {
        Agregacao.soma,
        Agregacao.media,
        Agregacao.mediana,
        Agregacao.minimo,
        Agregacao.maximo,
    }
)

# Agregações que exigem coluna. `contagem` é a única que aceita a tabela inteira.
AGREGACOES_COM_COLUNA = frozenset(set(Agregacao) - {Agregacao.contagem})


class Granularidade(str, enum.Enum):
    """Recorte temporal de uma dimensão de data."""

    nenhuma = "nenhuma"
    dia = "dia"
    mes = "mes"
    trimestre = "trimestre"
    ano = "ano"


class Operador(str, enum.Enum):
    igual = "igual"
    diferente = "diferente"
    maior = "maior"
    maior_ou_igual = "maior_ou_igual"
    menor = "menor"
    menor_ou_igual = "menor_ou_igual"
    entre = "entre"
    em = "em"
    contem = "contem"
    e_nulo = "e_nulo"
    nao_e_nulo = "nao_e_nulo"


# Quantos valores cada operador espera. É conferido na spec, antes de qualquer SQL:
# um `entre` com três valores é erro de quem escreveu, não uma consulta a executar.
VALORES_ESPERADOS: dict[Operador, tuple[int, int | None]] = {
    Operador.igual: (1, 1),
    Operador.diferente: (1, 1),
    Operador.maior: (1, 1),
    Operador.maior_ou_igual: (1, 1),
    Operador.menor: (1, 1),
    Operador.menor_ou_igual: (1, 1),
    Operador.entre: (2, 2),
    Operador.em: (1, None),
    Operador.contem: (1, 1),
    Operador.e_nulo: (0, 0),
    Operador.nao_e_nulo: (0, 0),
}


class MedidaSpec(BaseModel):
    """O que se mede."""

    agregacao: Agregacao
    coluna: str = Field(
        default="",
        description="Coluna a agregar. Vazio apenas com agregacao=contagem.",
    )
    rotulo: str = Field(description="Nome legível da medida, para o cabeçalho.")

    @model_validator(mode="after")
    def coluna_quando_necessaria(self) -> "MedidaSpec":
        if self.agregacao in AGREGACOES_COM_COLUNA and not self.coluna:
            raise ValueError(f"A agregação '{self.agregacao.value}' exige uma coluna.")
        return self


class DimensaoSpec(BaseModel):
    """Por que se agrupa."""

    coluna: str
    granularidade: Granularidade = Granularidade.nenhuma
    rotulo: str = ""


class FiltroSpec(BaseModel):
    """O recorte.

    `valores` carrega o que o usuário pediu na **unidade natural** — "salário acima de
    5.000" é 5000, não 500000. A conversão para a unidade armazenada é do construtor,
    que conhece a escala. Pedir ao modelo que converta seria devolver a ele exatamente
    a responsabilidade que a camada semântica existe para tirar.
    """

    coluna: str
    operador: Operador
    valores: list[str | float | int] = Field(default_factory=list)

    @model_validator(mode="after")
    def quantidade_de_valores(self) -> "FiltroSpec":
        minimo, maximo = VALORES_ESPERADOS[self.operador]
        n = len(self.valores)
        if n < minimo or (maximo is not None and n > maximo):
            esperado = f"{minimo}" if minimo == maximo else f"{minimo} ou mais"
            raise ValueError(
                f"O operador '{self.operador.value}' espera {esperado} valor(es), "
                f"recebeu {n}."
            )
        return self


class OrdenacaoSpec(BaseModel):
    # Rótulo de uma medida ou dimensão já declarada na spec — nunca uma coluna solta.
    # Ordenar por coluna fora do SELECT agrupado não é SQL válido, e aceitar isso só
    # produziria erro do banco em vez de mensagem clara.
    por: str
    decrescente: bool = True


class ConsultaSpec(BaseModel):
    """A pergunta traduzida em estrutura."""

    tabela: str = Field(description="esquema.tabela da tabela principal.")
    medidas: list[MedidaSpec] = Field(min_length=1)
    dimensoes: list[DimensaoSpec] = Field(default_factory=list)
    filtros: list[FiltroSpec] = Field(default_factory=list)
    ordenacao: list[OrdenacaoSpec] = Field(default_factory=list)
    limite: int | None = Field(default=None, ge=1, le=10_000)

    @model_validator(mode="after")
    def ordenacao_referencia_algo_declarado(self) -> "ConsultaSpec":
        conhecidos = {m.rotulo for m in self.medidas} | {
            d.rotulo or d.coluna for d in self.dimensoes
        }
        for o in self.ordenacao:
            if o.por not in conhecidos:
                raise ValueError(
                    f"A ordenação usa '{o.por}', que não é medida nem dimensão desta "
                    "consulta."
                )
        return self
