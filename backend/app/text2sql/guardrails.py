"""Validação de SQL antes de qualquer execução.

Defesa em camadas. Nenhuma delas sozinha é suficiente, e a ordem importa — cada uma
só recebe o que a anterior aprovou:

1. **Parse em AST** (`sqlglot`). Se não parseia, não roda. Um validador que trabalha
   por regex está sempre um truque de sintaxe atrás; uma AST diz o que a consulta
   *é*, não com o que ela se parece.
2. **Prova de somente-leitura.** Percorre a árvore inteira e recusa qualquer nó de
   escrita ou DDL — inclusive escondido em CTE (`WITH x AS (DELETE ... RETURNING)`),
   que é o caminho que passa por quase toda lista de palavras proibidas.
3. **Comando único.** `;` encadeado é a injeção clássica. Um statement por execução.
4. **Lista branca de tabelas.** A consulta só toca tabelas que o dicionário conhece.
   Isso fecha `pg_catalog`, `information_schema` e as tabelas de outros esquemas de
   graça, sem precisar enumerar o que é proibido.
5. **Lista negra de funções.** Leitura de arquivo, acesso a rede e negação de serviço
   são somente-leitura do ponto de vista do SQL e mesmo assim precisam sair.
6. **LIMIT forçado** quando não há agregação.

O que **não** está aqui, e é igualmente obrigatório (`app.connectors`):

- usuário de banco somente-leitura — a garantia é do Postgres, não nossa;
- `statement_timeout` e `SET TRANSACTION READ ONLY` na sessão;
- porta `EXPLAIN` com teto de custo antes de executar.

Este módulo torna o SQL *provavelmente* seguro. As camadas do conector o tornam
seguro mesmo quando este arquivo tiver um bug — e ele terá, algum dia.
"""

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

# Nós que caracterizam escrita ou mudança de estrutura. A busca é na árvore toda,
# então CTE, subconsulta e statement aninhado caem aqui do mesmo jeito.
_NOS_PROIBIDOS: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Merge,
    # `COPY ... TO PROGRAM` executa comando no servidor de banco. O sqlglot modela
    # COPY como nó próprio, não como `Command`, então precisa constar aqui — a
    # checagem de raiz sozinha não pegaria um COPY aninhado.
    exp.Copy,
    # `Command` é o que o sqlglot devolve para o que ele não modela — COPY, CALL,
    # DO, VACUUM, SET, statement de outro dialeto. Recusar por padrão é o único
    # tratamento correto: não sabemos o que é, então não roda.
    exp.Command,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Use,
)

# Funções somente-leitura em SQL que não são inofensivas no servidor.
_FUNCOES_PROIBIDAS: frozenset[str] = frozenset(
    {
        # Leitura de arquivo do servidor de banco.
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "load_file",
        # Acesso a rede a partir do banco — exfiltração e SSRF.
        "dblink",
        "dblink_connect",
        "dblink_exec",
        "postgres_fdw_handler",
        # Negação de serviço.
        "pg_sleep",
        "pg_sleep_for",
        "sleep",
        "benchmark",
        "generate_series",  # sem recorte, materializa o quanto pedirem
        # Execução dinâmica.
        "query_to_xml",
        "dblink_send_query",
        # Reconhecimento de credencial e privilégio.
        "pg_read_all_settings",
        "current_setting",
        "pg_backend_pid",
        "pg_terminate_backend",
        "pg_cancel_backend",
    }
)

# Esquemas de sistema. Redundante com a lista branca de tabelas, e mantido porque
# redundância barata em controle de segurança é uma virtude, não um defeito.
_ESQUEMAS_PROIBIDOS: frozenset[str] = frozenset(
    {"pg_catalog", "information_schema", "pg_toast", "mysql", "performance_schema", "sys"}
)


class SqlRecusado(Exception):
    """A consulta não passou nos guardrails. A mensagem é mostrada ao usuário —
    escreva-a explicando o que faltou, não o que a defesa detectou."""


@dataclass(slots=True)
class ResultadoValidacao:
    sql: str
    tabelas_referenciadas: set[str] = field(default_factory=set)
    tem_agregacao: bool = False
    tem_limite: bool = False


def _nome_qualificado(tabela: exp.Table) -> str:
    """`esquema.tabela` em minúsculas. Sem esquema, devolve só o nome."""
    nome = (tabela.name or "").lower()
    esquema = (tabela.db or "").lower()
    return f"{esquema}.{nome}" if esquema else nome


def validar(
    sql: str,
    *,
    dialeto: str = "postgres",
    tabelas_permitidas: set[str] | None = None,
) -> ResultadoValidacao:
    """Valida e devolve o que foi observado na consulta.

    `tabelas_permitidas` vem do catálogo daquela conexão, em `esquema.tabela`
    minúsculo. Passar `None` desliga a camada 4 e só deve acontecer em teste — em
    produção, uma consulta a uma tabela que o dicionário não conhece é uma consulta
    cuja resposta não sabemos interpretar, o que já é razão suficiente para recusar.

    Levanta `SqlRecusado` com mensagem em português. Nunca devolve SQL "corrigido":
    consertar em silêncio o que o LLM escreveu esconde o erro em vez de tratá-lo.
    """
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise SqlRecusado("Consulta vazia.")

    # ─── Camada 1: parse ──────────────────────────────────────────────────────
    try:
        statements = sqlglot.parse(sql, read=dialeto)
    except Exception as e:
        raise SqlRecusado(f"Não foi possível interpretar o SQL gerado: {e}") from e

    statements = [s for s in statements if s is not None]

    # ─── Camada 3: comando único ──────────────────────────────────────────────
    if len(statements) != 1:
        raise SqlRecusado(
            f"Esperado um único comando, encontrados {len(statements)}. "
            "Consultas encadeadas com ';' não são executadas."
        )

    arvore = statements[0]

    # ─── Camada 2: prova de somente-leitura ───────────────────────────────────
    for tipo in _NOS_PROIBIDOS:
        encontrado = arvore.find(tipo)
        if encontrado is not None:
            raise SqlRecusado(
                f"A consulta contém uma operação que não é de leitura "
                f"({tipo.__name__.upper()}). Só SELECT é executado."
            )

    # A raiz precisa ser uma consulta. `WITH ... SELECT` e `UNION` contam.
    if not isinstance(arvore, (exp.Select, exp.Union, exp.Subquery)):
        raise SqlRecusado("A consulta precisa começar com SELECT ou WITH.")

    # ─── Camada 5: funções ────────────────────────────────────────────────────
    for funcao in arvore.find_all(exp.Func):
        nome = (funcao.sql_name() or "").lower()
        if isinstance(funcao, exp.Anonymous):
            nome = str(funcao.this or "").lower()
        if nome in _FUNCOES_PROIBIDAS:
            raise SqlRecusado(f"A função '{nome}' não é permitida em consultas da plataforma.")

    # ─── Camada 4: lista branca de tabelas ────────────────────────────────────
    referenciadas: set[str] = set()
    # Nomes de CTE são tabelas locais da própria consulta, não do banco.
    nomes_cte = {cte.alias_or_name.lower() for cte in arvore.find_all(exp.CTE)}

    for tabela in arvore.find_all(exp.Table):
        esquema = (tabela.db or "").lower()
        if esquema in _ESQUEMAS_PROIBIDOS:
            raise SqlRecusado(f"Acesso ao esquema de sistema '{esquema}' não é permitido.")

        qualificado = _nome_qualificado(tabela)
        if qualificado in nomes_cte or (tabela.name or "").lower() in nomes_cte:
            continue
        referenciadas.add(qualificado)

    if tabelas_permitidas is not None:
        permitidas = {t.lower() for t in tabelas_permitidas}
        # Tolera o nome sem esquema quando o catálogo tem exatamente um casamento.
        sem_esquema = {t.split(".")[-1]: t for t in permitidas}
        desconhecidas = {
            t for t in referenciadas if t not in permitidas and t not in sem_esquema
        }
        if desconhecidas:
            raise SqlRecusado(
                "A consulta referencia tabelas que o dicionário desta conexão não conhece: "
                + ", ".join(sorted(desconhecidas))
                + ". Atualize o catálogo antes de consultá-las."
            )

    tem_agregacao = (
        arvore.find(exp.AggFunc) is not None
        or arvore.find(exp.Group) is not None
        or arvore.find(exp.Distinct) is not None
    )

    # Comentário não sobrevive à normalização.
    #
    # O sqlglot reescreve `-- x` como `/* x */` ao regenerar o SQL. Se o conteúdo do
    # comentário contiver `*/`, o bloco fecharia cedo e o resto viraria comando
    # executável — `SELECT 1 -- */ ; DROP TABLE t` é o ataque. Medido em 29/09/2026,
    # o sqlglot 30.20.0 escapa `*/` como `* /` e o ataque falha; a defesa funciona.
    #
    # Ainda assim, ela é *deles*. Depender do escape de terceiro num caminho de
    # segurança é apostar que ninguém vai regredir aquilo num upgrade menor. Apagar
    # os comentários custa três linhas e remove a classe inteira de risco: o SQL que
    # executa não tem onde carregar carga útil.
    for no in arvore.walk():
        no.comments = None

    return ResultadoValidacao(
        sql=arvore.sql(dialect=dialeto),
        tabelas_referenciadas=referenciadas,
        tem_agregacao=tem_agregacao,
        tem_limite=arvore.args.get("limit") is not None,
    )


def aplicar_limite(sql: str, limite: int, *, dialeto: str = "postgres") -> str:
    """Impõe um LIMIT quando a consulta não tem um.

    Só faz sentido para consulta sem agregação: um `GROUP BY` que devolve 12 linhas
    não precisa de teto, e cortar o resultado de uma agregação **mudaria a resposta**
    em vez de protegê-la — o total passaria a ser o total das primeiras N linhas.
    """
    arvore = sqlglot.parse_one(sql, read=dialeto)
    if arvore.args.get("limit") is not None:
        return arvore.sql(dialect=dialeto)
    if not isinstance(arvore, (exp.Select, exp.Union)):
        return arvore.sql(dialect=dialeto)
    return arvore.limit(limite).sql(dialect=dialeto)
