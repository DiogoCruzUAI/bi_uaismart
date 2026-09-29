"""A migration cria de fato o que os modelos declaram?

Migration e modelo divergem em silêncio: alguém adiciona uma coluna, esquece de gerar
a revisão, e tudo continua funcionando na máquina de quem escreveu — porque lá o banco
foi criado a partir do modelo. Quebra no deploy, onde o banco vem da migration.

Este teste roda a migration num SQLite descartável e compara o resultado com
`Base.metadata`. Não substitui rodar contra Postgres (tipo e índice específicos do
dialeto não são conferidos aqui), mas pega a divergência estrutural, que é a comum.

São testes síncronos de propósito: o `env.py` do Alembic chama `asyncio.run`, que
estoura se já houver um laço de eventos rodando.
"""

import argparse
import sqlite3

import pytest
from alembic import command
from alembic.config import Config

import app.models as m


def _migrar(destino: str, revisao: str = "head") -> None:
    cfg = Config("alembic.ini")
    # Forma programática do `-x url=...` que o env.py lê.
    cfg.cmd_opts = argparse.Namespace(x=[f"url=sqlite+aiosqlite:///{destino}"])
    if revisao == "base":
        command.downgrade(cfg, "base")
    else:
        command.upgrade(cfg, revisao)


@pytest.fixture
def banco(tmp_path):
    """Caminho de um SQLite descartável, em formato que o sqlite3 aceita no Windows."""
    return str(tmp_path / "migracao.db").replace("\\", "/")


def test_migration_cria_todas_as_tabelas(banco):
    _migrar(banco)
    con = sqlite3.connect(banco)
    try:
        no_banco = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        con.close()
    no_banco.discard("alembic_version")

    assert no_banco == set(m.Base.metadata.tables)


def test_migration_cria_todas_as_colunas(banco):
    _migrar(banco)
    con = sqlite3.connect(banco)
    try:
        for nome, tabela in m.Base.metadata.tables.items():
            do_banco = {r[1] for r in con.execute(f"PRAGMA table_info('{nome}')")}
            do_modelo = {c.name for c in tabela.columns}
            assert do_banco == do_modelo, f"{nome}: divergência entre modelo e migration"
    finally:
        con.close()


def test_downgrade_desfaz_tudo(banco):
    """Migration sem downgrade testado é migration que não dá para reverter às 3h."""
    _migrar(banco)
    _migrar(banco, "base")

    con = sqlite3.connect(banco)
    try:
        restantes = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        con.close()
    restantes.discard("alembic_version")

    assert restantes == set()


def test_toda_tabela_de_tenant_tem_indice_em_tenant_id():
    """Isolamento por tenant sem índice vira varredura completa sob carga.

    Confere no metadata, não no SQLite: é uma propriedade dos modelos, e vale
    igualmente para o Postgres de produção.
    """
    for nome, tabela in m.Base.metadata.tables.items():
        if "tenant_id" not in tabela.columns:
            continue
        coluna = tabela.columns["tenant_id"]
        indexada = coluna.index or any(
            coluna.name in [c.name for c in idx.columns] for idx in tabela.indexes
        )
        assert indexada, f"{nome}.tenant_id sem índice"
