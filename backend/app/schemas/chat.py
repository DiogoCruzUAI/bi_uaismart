"""Schemas da pergunta e da resposta.

`ColunaResposta` e `procedencia` existem porque a segunda garantia do produto é a
confiabilidade do número na tela. Uma resposta que traz só os valores obriga a
confiar; esta traz o SQL que rodou, quantas linhas o planejador previu, qual versão do
dicionário estava em vigor e se a descrição usada foi revisada por alguém.
"""

from pydantic import BaseModel, Field


class PerguntaRequisicao(BaseModel):
    pergunta: str = Field(min_length=3, max_length=2000)


class ColunaResposta(BaseModel):
    rotulo: str
    tipo: str
    coluna_origem: str | None = None
    unidade: str | None = None
    moeda: str | None = None
    # Divisor aplicado pela plataforma para chegar à unidade natural. Vai para a tela
    # porque é a conversão que o usuário mais precisa poder conferir.
    escala_aplicada: float = 1.0
    dicionario_revisado: bool = False


class Procedencia(BaseModel):
    """De onde veio o número."""

    sql: str | None = None
    spec: dict | None = None
    linhas_estimadas: int | None = None
    custo_estimado: float | None = None
    linhas_retornadas: int = 0
    duracao_ms: int | None = None
    truncado: bool = False
    versao_dicionario: int
    tabelas_consideradas: list[str] = Field(default_factory=list)
    modelo_llm: str | None = None
    tokens_entrada: int = 0
    tokens_cache_leitura: int = 0
    tokens_saida: int = 0


class RespostaChat(BaseModel):
    respondeu: bool
    pergunta: str
    # Como a plataforma entendeu a pergunta. É o que a pessoa lê para conferir se
    # foi entendida antes de acreditar no número.
    explicacao: str = ""
    # Por que não deu, quando não deu. "A tabela não tem essa informação" e "a
    # consulta ficou grande demais" pedem reações diferentes de quem perguntou.
    motivo: str = ""
    colunas: list[ColunaResposta] = Field(default_factory=list)
    linhas: list[dict] = Field(default_factory=list)
    avisos: list[str] = Field(default_factory=list)
    consulta_id: int | None = None
    procedencia: Procedencia
