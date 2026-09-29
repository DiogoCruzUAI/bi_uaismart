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
from collections.abc import Sequence
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


@dataclass(slots=True, frozen=True)
class PrivilegiosDaConexao:
    """Fatos sobre o papel usado para conectar, lidos do catálogo do banco.

    Separado da decisão de propósito. Coletar exige um banco de verdade; decidir é
    lógica pura, e é onde os erros moram. Com a separação, a decisão fica coberta por
    teste sem infraestrutura nenhuma.
    """

    eh_superusuario: bool
    pode_criar_no_banco: bool
    ignora_rls: bool
    # Papéis dos quais o usuário é membro (herança inclusa).
    papeis: frozenset[str]
    # Quantas tabelas aceitam INSERT, UPDATE, DELETE ou TRUNCATE por este papel.
    tabelas_com_escrita: int
    exemplos_com_escrita: tuple[str, ...] = ()


# Papéis predefinidos do Postgres que dão poder além de ler dado. `pg_read_all_data`
# fica de fora de propósito: concede leitura ampla, que é exatamente o que um BI faz.
_PAPEIS_PERIGOSOS: dict[str, str] = {
    "pg_execute_server_program": "executar programas no servidor (COPY TO PROGRAM)",
    "pg_write_server_files": "escrever arquivos no servidor",
    "pg_read_server_files": "ler arquivos do servidor",
    "pg_write_all_data": "escrever em qualquer tabela",
    "pg_signal_backend": "derrubar conexões de outros usuários",
    "pg_maintain": "executar VACUUM, ANALYZE e REINDEX",
}


def avaliar_privilegios(p: PrivilegiosDaConexao) -> tuple[list[str], list[str]]:
    """Decide se a credencial serve. Devolve `(impedimentos, alertas)`.

    **Impedimento** é o que permite escrever ou alcançar o sistema operacional:
    recusamos a conexão. A plataforma nunca escreve no banco do cliente, e a garantia
    disso tem que ser do Postgres dele — não da nossa validação de SQL, que um dia
    terá um bug.

    **Alerta** é privilégio amplo demais que ainda assim só lê. Não impede o cadastro,
    mas o cliente precisa saber que concedeu mais do que precisávamos.

    Função pura: recebe fatos, devolve texto. Nenhuma ida ao banco.
    """
    impedimentos: list[str] = []
    alertas: list[str] = []

    if p.eh_superusuario:
        impedimentos.append(
            "O usuário é superusuário. Superusuário ignora toda verificação de "
            "permissão e pode desligar o modo somente-leitura da própria sessão — "
            "nenhuma proteção nossa vale contra ele."
        )

    if p.tabelas_com_escrita > 0:
        exemplos = ", ".join(p.exemplos_com_escrita)
        sufixo = f" (por exemplo: {exemplos})" if exemplos else ""
        impedimentos.append(
            f"O usuário tem permissão de escrita em {p.tabelas_com_escrita} "
            f"tabela(s){sufixo}. Conceda apenas SELECT."
        )

    if p.pode_criar_no_banco:
        impedimentos.append(
            "O usuário pode criar objetos no banco (CREATE). Com isso ele cria as "
            "próprias tabelas e funções, o que torna o resto das restrições inútil."
        )

    for papel, poder in sorted(_PAPEIS_PERIGOSOS.items()):
        if papel in p.papeis:
            impedimentos.append(f"O usuário é membro de '{papel}', que permite {poder}.")

    if p.ignora_rls:
        alertas.append(
            "O usuário tem BYPASSRLS e enxerga linhas que a segurança em nível de "
            "linha esconderia. Se o banco usa RLS para separar dados, a plataforma "
            "passa por cima dessa separação."
        )

    if "pg_read_all_data" in p.papeis:
        alertas.append(
            "O usuário é membro de 'pg_read_all_data' e lê qualquer tabela do banco, "
            "inclusive as que não pretendia expor."
        )

    return impedimentos, alertas


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
    async def coletar_privilegios(self) -> PrivilegiosDaConexao:
        """Lê do catálogo o que o papel pode fazer. Só consulta catálogo, nunca dado."""

    @abstractmethod
    async def testar(self) -> list[str]:
        """Conecta e confirma que a credencial é de leitura.

        Devolve a lista de alertas (privilégio amplo demais que ainda assim só lê) e
        levanta `ErroDeConexao` quando há impedimento. Alerta não bloqueia o cadastro:
        quem decide se o acesso é amplo demais é o dono do dado, não nós.
        """

    @abstractmethod
    async def listar_tabelas(self) -> list[TabelaBruta]: ...

    @abstractmethod
    async def listar_colunas(self, esquema: str, tabela: str) -> list[ColunaBruta]: ...

    @abstractmethod
    async def listar_relacionamentos(self) -> list[RelacionamentoBruto]: ...

    @abstractmethod
    async def estimar(self, sql: str, parametros: Sequence[Any] = ()) -> Estimativa:
        """`EXPLAIN` sem executar. É a porta que roda antes de toda consulta.

        Os parâmetros entram no EXPLAIN junto com o SQL: o planejador usa o valor real
        para estimar seletividade. Estimar com o SQL sem os valores daria um número
        diferente do da execução, e a porta passaria a proteger outra consulta.
        """

    @abstractmethod
    async def executar(
        self, sql: str, limite_linhas: int, parametros: Sequence[Any] = ()
    ) -> ResultadoConsulta:
        """Executa em transação somente-leitura com prazo. Só chame depois de `estimar`."""

    @abstractmethod
    async def fechar(self) -> None: ...
