"""Contrato de um conector a banco de cliente.

Um conector é responsável por quatro coisas, nesta ordem de importância:

1. **Não deixar escrever.** Sessão em `READ ONLY` e usuário de banco sem permissão
   de escrita. Os guardrails de `app.text2sql` tornam o SQL provavelmente seguro;
   é aqui que ele fica seguro mesmo se aqueles tiverem um bug.
2. **Não deixar varrer.** `EXPLAIN` antes de executar, com teto de linhas e custo.
   O planejador estima sem ler uma página sequer — é a informação mais barata do
   banco e a que evita a consulta de 300 milhões de linhas.
3. **Perfilar sem varrer.** Estatísticas do catálogo (`reltuples`, `pg_stats`) em vez
   de `COUNT(*)` e `SELECT DISTINCT`. Contar a base inteira para descobrir que ela é
   grande é a força bruta que este projeto recusa.
4. **Executar com prazo.** `statement_timeout` no banco, não `asyncio.wait_for` no
   nosso processo: cancelar do nosso lado deixa a consulta rodando lá, consumindo
   CPU do cliente.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Estimativa:
    """O que o planejador prevê antes de a consulta rodar.

    São duas grandezas diferentes e confundi-las inutiliza as duas portas:

    - `linhas` é a saída do nó de topo — quantas linhas o usuário receberia.
      Numa agregação, é o número de grupos. `SELECT count(*) FROM tabela_gigante`
      tem `linhas = 1` e não diz nada sobre o esforço.
    - `linhas_varridas` é o maior `Plan Rows` da árvore inteira — o volume que a
      consulta realmente toca. É o que denuncia a varredura de 300 milhões de linhas
      escondida atrás de um `count(*)`.
    """

    linhas: int
    linhas_varridas: int
    custo: float
    plano: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ResultadoConsulta:
    colunas: list[str]
    linhas: list[dict[str, Any]]
    duracao_ms: int
    truncado: bool = False


@dataclass(slots=True)
class TabelaBruta:
    esquema: str
    nome: str
    eh_view: bool
    linhas_estimadas: int | None
    bytes_estimados: int | None


@dataclass(slots=True)
class ColunaBruta:
    esquema: str
    tabela: str
    nome: str
    tipo_sql: str
    aceita_nulo: bool
    eh_chave_primaria: bool
    # De `pg_stats`, quando disponível. É o que permite à IA perceber unidade e
    # escala — um "máximo" de 10.000.000.000 num campo de salário denuncia centavos.
    fracao_nula: float | None = None
    cardinalidade_estimada: int | None = None
    valores_comuns: list[Any] | None = None
    minimo: Any | None = None
    maximo: Any | None = None


@dataclass(slots=True)
class RelacionamentoBruto:
    esquema_origem: str
    tabela_origem: str
    coluna_origem: str
    esquema_destino: str
    tabela_destino: str
    coluna_destino: str


class ErroDeConexao(Exception):
    """Falha ao conectar ou consultar o banco do cliente.

    A mensagem chega ao usuário. Nunca inclua credencial, DSN completa ou host
    interno — mensagem de erro é canal de vazamento como qualquer outro.
    """


class LimiteExcedido(Exception):
    """A consulta foi recusada pela porta do EXPLAIN.

    Não é erro: é o produto funcionando. A mensagem deve dizer o que falta (um
    recorte, um período, um filtro), não apenas que foi negado.
    """


class Conector(ABC):
    """Interface que todo banco suportado implementa."""

    dialeto: str

    @abstractmethod
    async def testar(self) -> None:
        """Conecta e confirma que o usuário é somente-leitura. Levanta em caso de falha."""

    @abstractmethod
    async def listar_tabelas(self) -> list[TabelaBruta]: ...

    @abstractmethod
    async def listar_colunas(self, esquema: str, tabela: str) -> list[ColunaBruta]: ...

    @abstractmethod
    async def listar_relacionamentos(self) -> list[RelacionamentoBruto]: ...

    @abstractmethod
    async def estimar(self, sql: str) -> Estimativa:
        """`EXPLAIN` sem executar. É a porta que roda antes de toda consulta."""

    @abstractmethod
    async def executar(self, sql: str, limite_linhas: int) -> ResultadoConsulta:
        """Executa em transação somente-leitura com prazo. Só chame depois de `estimar`."""

    @abstractmethod
    async def fechar(self) -> None: ...
