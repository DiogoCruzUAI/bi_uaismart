"""Conector Postgres.

Perfilamento por estatística, não por varredura. Todo número que este módulo devolve
sobre volume e distribuição sai de `pg_class` e `pg_stats`, que o `ANALYZE` mantém —
custo zero, precisão de alguns por cento. `COUNT(*)` numa tabela de 73 milhões de
linhas leva minutos e entrega uma exatidão que o produto não usa para nada.

`pg_stats` é também a fonte da inteligência do dicionário: `most_common_vals` e
`histogram_bounds` são o que permitem à IA concluir que uma coluna chamada `salario`
com máximo em 10.000.000.000 está em centavos, e que outra com quatro valores
distintos é categórica e não numérica.
"""

import json
import time
from collections.abc import Sequence
from typing import Any

import asyncpg
import structlog

from app.connectors.base import (
    ColunaBruta,
    Conector,
    ErroDeConexao,
    Estimativa,
    LimiteExcedido,
    PrivilegiosDaConexao,
    RelacionamentoBruto,
    ResultadoConsulta,
    TabelaBruta,
    avaliar_privilegios,
)
from app.core.config import settings

logger = structlog.get_logger()

# Esquemas que nunca entram no catálogo.
_ESQUEMAS_IGNORADOS = ("pg_catalog", "information_schema", "pg_toast")

_SQL_PRIVILEGIOS_BASICOS = """
SELECT r.rolsuper                                                      AS eh_superusuario,
       r.rolbypassrls                                                  AS ignora_rls,
       has_database_privilege(current_user, current_database(), 'CREATE') AS pode_criar_no_banco
  FROM pg_roles r
 WHERE r.rolname = current_user
"""

# Papéis dos quais o usuário é membro, herança inclusa. Sem nomear papel predefinido
# nenhum: `pg_has_role` levanta erro se o papel não existir, e o conjunto de papéis
# predefinidos mudou entre versões do Postgres. Perguntar "de quais sou membro" e
# comparar em Python funciona igual da 11 à 18.
_SQL_PAPEIS = """
SELECT r.rolname
  FROM pg_roles r
 WHERE pg_has_role(current_user, r.oid, 'MEMBER') AND r.rolname <> current_user
"""

# `has_table_privilege` com dois argumentos usa o current_user e já leva em conta
# privilégio herdado de papel. Só catálogo: nenhuma linha de dado é lida.
_SQL_TABELAS_COM_ESCRITA = """
WITH gravaveis AS (
    SELECT n.nspname || '.' || c.relname AS nome
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relkind IN ('r', 'p')
       AND n.nspname <> ALL($1::text[])
       AND (has_table_privilege(c.oid, 'INSERT')
         OR has_table_privilege(c.oid, 'UPDATE')
         OR has_table_privilege(c.oid, 'DELETE')
         OR has_table_privilege(c.oid, 'TRUNCATE'))
)
SELECT count(*)                                        AS total,
       (SELECT array_agg(nome) FROM (
            SELECT nome FROM gravaveis ORDER BY nome LIMIT 3
        ) amostra)                                     AS exemplos
  FROM gravaveis
"""

_SQL_TABELAS = """
SELECT n.nspname                         AS esquema,
       c.relname                         AS nome,
       c.relkind = 'v'                   AS eh_view,
       -- reltuples é a estimativa que o ANALYZE mantém. -1 significa "nunca
       -- analisada": devolvemos NULL em vez de fingir que a tabela está vazia.
       NULLIF(c.reltuples, -1)::bigint   AS linhas_estimadas,
       pg_total_relation_size(c.oid)     AS bytes_estimados
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind IN ('r', 'p', 'v', 'm')
   AND n.nspname <> ALL($1::text[])
 ORDER BY n.nspname, c.relname
"""

_SQL_COLUNAS = """
SELECT a.attname                                             AS nome,
       format_type(a.atttypid, a.atttypmod)                  AS tipo_sql,
       NOT a.attnotnull                                      AS aceita_nulo,
       COALESCE(pk.eh_pk, false)                             AS eh_chave_primaria,
       s.null_frac                                           AS fracao_nula,
       s.n_distinct                                          AS n_distinct,
       s.most_common_vals::text                              AS valores_comuns,
       s.histogram_bounds::text                              AS limites
  FROM pg_attribute a
  JOIN pg_class c      ON c.oid = a.attrelid
  JOIN pg_namespace n  ON n.oid = c.relnamespace
  LEFT JOIN pg_stats s ON s.schemaname = n.nspname
                      AND s.tablename  = c.relname
                      AND s.attname    = a.attname
  LEFT JOIN LATERAL (
        SELECT true AS eh_pk
          FROM pg_index i
         WHERE i.indrelid = c.oid AND i.indisprimary AND a.attnum = ANY(i.indkey)
         LIMIT 1
  ) pk ON true
 WHERE n.nspname = $1 AND c.relname = $2
   AND a.attnum > 0 AND NOT a.attisdropped
 ORDER BY a.attnum
"""

_SQL_RELACIONAMENTOS = """
SELECT ns_o.nspname AS esquema_origem,  t_o.relname AS tabela_origem,  a_o.attname AS coluna_origem,
       ns_d.nspname AS esquema_destino, t_d.relname AS tabela_destino, a_d.attname AS coluna_destino
  FROM pg_constraint ct
  JOIN pg_class     t_o  ON t_o.oid = ct.conrelid
  JOIN pg_namespace ns_o ON ns_o.oid = t_o.relnamespace
  JOIN pg_class     t_d  ON t_d.oid = ct.confrelid
  JOIN pg_namespace ns_d ON ns_d.oid = t_d.relnamespace
  JOIN pg_attribute a_o  ON a_o.attrelid = ct.conrelid  AND a_o.attnum = ct.conkey[1]
  JOIN pg_attribute a_d  ON a_d.attrelid = ct.confrelid AND a_d.attnum = ct.confkey[1]
 WHERE ct.contype = 'f'
   AND ns_o.nspname <> ALL($1::text[])
"""


class ConectorPostgres(Conector):
    dialeto = "postgres"

    def __init__(
        self,
        *,
        host: str,
        porta: int,
        banco: str,
        usuario: str,
        senha: str,
        usar_ssl: bool = True,
    ) -> None:
        self._dsn_partes = {
            "host": host,
            "port": porta,
            "database": banco,
            "user": usuario,
            "password": senha,
            "ssl": "prefer" if usar_ssl else False,
        }
        self._pool: asyncpg.Pool | None = None

    # ─── Ciclo de vida ────────────────────────────────────────────────────────

    async def _obter_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            try:
                self._pool = await asyncpg.create_pool(
                    **self._dsn_partes,
                    min_size=1,
                    max_size=4,
                    command_timeout=settings.query_timeout_seconds,
                    # Toda sessão nasce somente-leitura e com prazo. Não depende de
                    # o chamador lembrar de configurar.
                    server_settings={
                        "default_transaction_read_only": "on",
                        "statement_timeout": str(settings.query_timeout_seconds * 1000),
                        "idle_in_transaction_session_timeout": "10000",
                        "application_name": "nextgen-bi",
                    },
                )
            except Exception as e:
                # Sem `str(e)` cru: a exceção do asyncpg pode conter host e usuário.
                logger.error("falha_conexao_cliente", tipo=type(e).__name__)
                raise ErroDeConexao(
                    "Não foi possível conectar ao banco. Verifique host, porta, "
                    "credenciais e se o servidor aceita conexão da nossa origem."
                ) from e
        return self._pool

    async def fechar(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ─── Teste ────────────────────────────────────────────────────────────────

    async def coletar_privilegios(self) -> PrivilegiosDaConexao:
        """Pergunta ao catálogo o que este papel pode fazer.

        Substitui uma checagem que era uma tautologia: a versão anterior conferia
        `SHOW transaction_read_only`, mas a própria conexão envia
        `default_transaction_read_only=on` em `server_settings` — a resposta era `on`
        até para superusuário. Ela validava a nossa configuração, não o privilégio do
        papel, e teria aprovado exatamente a credencial que existia para recusar.

        Tudo aqui é consulta a catálogo: nenhuma tabela de dado é tocada.
        """
        pool = await self._obter_pool()
        async with pool.acquire() as conn:
            basico = await conn.fetchrow(_SQL_PRIVILEGIOS_BASICOS)
            papeis = await conn.fetch(_SQL_PAPEIS)
            escrita = await conn.fetchrow(_SQL_TABELAS_COM_ESCRITA, list(_ESQUEMAS_IGNORADOS))

        return PrivilegiosDaConexao(
            eh_superusuario=bool(basico["eh_superusuario"]),
            pode_criar_no_banco=bool(basico["pode_criar_no_banco"]),
            ignora_rls=bool(basico["ignora_rls"]),
            papeis=frozenset(r["rolname"] for r in papeis),
            tabelas_com_escrita=int(escrita["total"] or 0),
            exemplos_com_escrita=tuple(escrita["exemplos"] or ()),
        )

    async def testar(self) -> list[str]:
        """Conecta e recusa a credencial que possa escrever.

        Um usuário com permissão de escrita conectaria normalmente e a plataforma
        seguiria feliz — até o dia em que um bug nos guardrails encontrasse um
        `DELETE`. Melhor recusar no cadastro, quando o cliente ainda está na tela e
        pode corrigir o `GRANT`.
        """
        pool = await self._obter_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")

        privilegios = await self.coletar_privilegios()
        impedimentos, alertas = avaliar_privilegios(privilegios)

        if impedimentos:
            raise ErroDeConexao(
                "Esta credencial tem mais permissão do que a plataforma aceita. "
                "A plataforma nunca escreve no banco do cliente, e essa garantia "
                "precisa ser do Postgres.\n\n"
                + "\n".join(f"• {i}" for i in impedimentos)
                + "\n\nO GRANT recomendado está em docs/INFRA.md."
            )

        return alertas

    # ─── Perfilamento ─────────────────────────────────────────────────────────

    async def listar_tabelas(self) -> list[TabelaBruta]:
        pool = await self._obter_pool()
        async with pool.acquire() as conn:
            linhas = await conn.fetch(_SQL_TABELAS, list(_ESQUEMAS_IGNORADOS))
        return [
            TabelaBruta(
                esquema=r["esquema"],
                nome=r["nome"],
                eh_view=r["eh_view"],
                linhas_estimadas=r["linhas_estimadas"],
                bytes_estimados=r["bytes_estimados"],
            )
            for r in linhas
        ]

    async def listar_colunas(self, esquema: str, tabela: str) -> list[ColunaBruta]:
        pool = await self._obter_pool()
        async with pool.acquire() as conn:
            linhas = await conn.fetch(_SQL_COLUNAS, esquema, tabela)

        colunas: list[ColunaBruta] = []
        for r in linhas:
            limites = _lista_de_texto_pg(r["limites"])
            colunas.append(
                ColunaBruta(
                    esquema=esquema,
                    tabela=tabela,
                    nome=r["nome"],
                    tipo_sql=r["tipo_sql"],
                    aceita_nulo=r["aceita_nulo"],
                    eh_chave_primaria=r["eh_chave_primaria"],
                    fracao_nula=r["fracao_nula"],
                    cardinalidade_estimada=_cardinalidade(r["n_distinct"], None),
                    valores_comuns=_lista_de_texto_pg(r["valores_comuns"])[:20] or None,
                    # Extremos do histograma. É o sinal mais forte de unidade e escala.
                    minimo=limites[0] if limites else None,
                    maximo=limites[-1] if limites else None,
                )
            )
        return colunas

    async def listar_relacionamentos(self) -> list[RelacionamentoBruto]:
        pool = await self._obter_pool()
        async with pool.acquire() as conn:
            linhas = await conn.fetch(_SQL_RELACIONAMENTOS, list(_ESQUEMAS_IGNORADOS))
        return [
            RelacionamentoBruto(
                esquema_origem=r["esquema_origem"],
                tabela_origem=r["tabela_origem"],
                coluna_origem=r["coluna_origem"],
                esquema_destino=r["esquema_destino"],
                tabela_destino=r["tabela_destino"],
                coluna_destino=r["coluna_destino"],
            )
            for r in linhas
        ]

    # ─── Estimativa e execução ────────────────────────────────────────────────

    async def estimar(self, sql: str, parametros: Sequence[Any] = ()) -> Estimativa:
        """`EXPLAIN` puro — sem `ANALYZE`, que executaria a consulta.

        Confusão que custa caro: `EXPLAIN ANALYZE` **roda** o comando. Aqui a
        estimativa precisa custar nada, então é `EXPLAIN (FORMAT JSON)` e ponto.
        """
        pool = await self._obter_pool()
        try:
            async with pool.acquire() as conn:
                bruto = await conn.fetchval(f"EXPLAIN (FORMAT JSON) {sql}", *parametros)
        except asyncpg.PostgresError as e:
            raise ErroDeConexao(f"O banco recusou a consulta: {e.args[0] if e.args else e}") from e

        plano = json.loads(bruto)[0]["Plan"] if isinstance(bruto, str) else bruto[0]["Plan"]
        return Estimativa(
            linhas=int(plano.get("Plan Rows", 0)),
            linhas_varridas=_maior_plan_rows(plano),
            custo=float(plano.get("Total Cost", 0.0)),
            plano=plano,
        )

    async def executar(
        self, sql: str, limite_linhas: int, parametros: Sequence[Any] = ()
    ) -> ResultadoConsulta:
        pool = await self._obter_pool()
        inicio = time.perf_counter()
        try:
            async with pool.acquire() as conn:
                # Transação explícita e somente-leitura: mesmo que o
                # `default_transaction_read_only` seja perdido numa reconexão, a
                # transação continua incapaz de escrever.
                async with conn.transaction(readonly=True):
                    # Cursor, não `conn.fetch`.
                    #
                    # `conn.fetch` traz o resultado inteiro para a memória antes de
                    # qualquer corte. Uma agregação não leva LIMIT (limitá-la mudaria
                    # a resposta), então um `GROUP BY` de alta cardinalidade — cnpj,
                    # competência — materializaria milhões de linhas aqui e mataria o
                    # processo por OOM. A porta do EXPLAIN não salva: ela trabalha com
                    # *estimativa*, e estimativa erra por ordens de grandeza.
                    #
                    # Com cursor, o consumo de memória é `limite_linhas + 1` linhas,
                    # sempre, independentemente do que a consulta devolva. É o que
                    # torna a RAM da máquina uma conta fechada em vez de uma aposta.
                    cursor = await conn.cursor(sql, *parametros)
                    # +1 para distinguir "deu exatamente o limite" de "tem mais".
                    registros = await cursor.fetch(limite_linhas + 1)
        except asyncpg.QueryCanceledError as e:
            raise LimiteExcedido(
                f"A consulta passou de {settings.query_timeout_seconds}s e foi interrompida. "
                "Reduza o período ou aplique um filtro mais específico."
            ) from e
        except asyncpg.PostgresError as e:
            raise ErroDeConexao(f"O banco recusou a consulta: {e.args[0] if e.args else e}") from e

        duracao_ms = int((time.perf_counter() - inicio) * 1000)
        truncado = len(registros) > limite_linhas
        registros = registros[:limite_linhas]
        colunas = list(registros[0].keys()) if registros else []
        return ResultadoConsulta(
            colunas=colunas,
            linhas=[dict(r) for r in registros],
            duracao_ms=duracao_ms,
            truncado=truncado,
        )


# ─── Auxiliares ───────────────────────────────────────────────────────────────


def _cardinalidade(n_distinct: float | None, linhas: int | None) -> int | None:
    """Traduz o `n_distinct` do `pg_stats`.

    A convenção do Postgres: positivo é a contagem absoluta de valores distintos;
    **negativo é a fração** em relação ao total de linhas (-1 = todo valor é único,
    típico de chave). Tratar -1 como "um valor distinto" inverteria completamente a
    leitura da coluna — de identificador para constante.
    """
    if n_distinct is None:
        return None
    if n_distinct >= 0:
        return int(n_distinct)
    if linhas:
        return int(abs(n_distinct) * linhas)
    return None


def _lista_de_texto_pg(valor: str | None) -> list[Any]:
    """Converte a representação textual de array do Postgres (`{a,b,c}`) em lista.

    `most_common_vals` e `histogram_bounds` são do tipo `anyarray`, que o asyncpg não
    decodifica — daí o `::text` na consulta e este parser. É deliberadamente simples:
    o conteúdo só alimenta o prompt do dicionário, então valor com vírgula interna
    saindo partido é ruído tolerável, não bug.
    """
    if not valor:
        return []
    interno = valor.strip()
    if interno.startswith("{") and interno.endswith("}"):
        interno = interno[1:-1]
    if not interno:
        return []
    return [p.strip().strip('"') for p in interno.split(",")]


def _maior_plan_rows(plano: dict[str, Any]) -> int:
    """Maior `Plan Rows` da árvore inteira do plano.

    O nó de topo só conta a saída. `SELECT count(*) FROM caged` tem `Plan Rows = 1`
    no topo e um `Seq Scan` de 293 milhões logo abaixo — olhar só o topo deixaria
    passar exatamente a consulta que o limite existe para barrar.

    Percurso iterativo, não recursivo: plano de consulta com muitos JOINs aninhados
    chega fácil a dezenas de níveis, e um `RecursionError` aqui derrubaria a porta
    de segurança em vez de fechá-la.
    """
    maior = 0
    pilha = [plano]
    while pilha:
        no = pilha.pop()
        maior = max(maior, int(no.get("Plan Rows", 0) or 0))
        pilha.extend(no.get("Plans", []) or [])
    return maior
