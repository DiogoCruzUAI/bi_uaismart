"""Testes da avaliação de privilégios da credencial do cliente.

A função sob teste é pura: recebe fatos lidos do catálogo e devolve impedimentos e
alertas. Coletar os fatos exige um Postgres; decidir não — e decidir é onde o erro
custa caro, porque um erro aqui aprova uma credencial que pode escrever.

O caso mais importante do arquivo é `test_superusuario_e_recusado`: é exatamente a
credencial que a verificação anterior aprovava, por conferir a nossa própria
configuração de sessão em vez do privilégio do papel.
"""

import pytest

from app.connectors.base import (
    _PAPEIS_PERIGOSOS,
    PrivilegiosDaConexao,
    avaliar_privilegios,
)


def privilegios(**mudancas) -> PrivilegiosDaConexao:
    """Credencial exemplar — exatamente o que o GRANT de docs/INFRA.md produz."""
    base = {
        "eh_superusuario": False,
        "pode_criar_no_banco": False,
        "ignora_rls": False,
        "papeis": frozenset(),
        "tabelas_com_escrita": 0,
        "exemplos_com_escrita": (),
    }
    return PrivilegiosDaConexao(**{**base, **mudancas})


# ─── O que passa ──────────────────────────────────────────────────────────────


def test_credencial_somente_leitura_passa():
    impedimentos, alertas = avaliar_privilegios(privilegios())
    assert impedimentos == []
    assert alertas == []


def test_papel_inofensivo_nao_impede():
    """Ser membro de um papel qualquer da organização não é problema nosso."""
    impedimentos, _ = avaliar_privilegios(privilegios(papeis=frozenset({"analistas"})))
    assert impedimentos == []


# ─── Impedimentos ─────────────────────────────────────────────────────────────


def test_superusuario_e_recusado():
    """A regressão que este conserto existe para impedir.

    A verificação anterior lia `SHOW transaction_read_only`, que a nossa própria
    conexão força para `on` — então superusuário era aprovado.
    """
    impedimentos, _ = avaliar_privilegios(privilegios(eh_superusuario=True))
    assert len(impedimentos) == 1
    assert "superusuário" in impedimentos[0].lower()


def test_escrita_em_tabela_e_recusada():
    impedimentos, _ = avaliar_privilegios(privilegios(tabelas_com_escrita=1))
    assert len(impedimentos) == 1
    assert "escrita em 1 tabela" in impedimentos[0]


def test_mensagem_de_escrita_nomeia_as_tabelas():
    """Sem os nomes, o cliente não sabe qual GRANT revogar."""
    impedimentos, _ = avaliar_privilegios(
        privilegios(tabelas_com_escrita=2, exemplos_com_escrita=("vendas.pedidos", "cadastro.clientes"))
    )
    assert "vendas.pedidos" in impedimentos[0]
    assert "cadastro.clientes" in impedimentos[0]


def test_create_no_banco_e_recusado():
    impedimentos, _ = avaliar_privilegios(privilegios(pode_criar_no_banco=True))
    assert len(impedimentos) == 1
    assert "CREATE" in impedimentos[0]


@pytest.mark.parametrize("papel", sorted(_PAPEIS_PERIGOSOS))
def test_cada_papel_perigoso_e_recusado(papel):
    impedimentos, _ = avaliar_privilegios(privilegios(papeis=frozenset({papel})))
    assert len(impedimentos) == 1
    assert papel in impedimentos[0]
    # A mensagem diz o que aquele papel permite, não só que foi negado.
    assert _PAPEIS_PERIGOSOS[papel] in impedimentos[0]


# ─── Alertas (não bloqueiam) ──────────────────────────────────────────────────


def test_bypassrls_alerta_mas_nao_impede():
    """Passar por cima de RLS é leitura ampla demais, não escrita.

    Quem decide se o acesso é amplo demais é o dono do dado. A plataforma avisa.
    """
    impedimentos, alertas = avaliar_privilegios(privilegios(ignora_rls=True))
    assert impedimentos == []
    assert len(alertas) == 1
    assert "RLS" in alertas[0]


def test_pg_read_all_data_alerta_mas_nao_impede():
    impedimentos, alertas = avaliar_privilegios(
        privilegios(papeis=frozenset({"pg_read_all_data"}))
    )
    assert impedimentos == []
    assert len(alertas) == 1


def test_pg_read_all_data_nao_esta_na_lista_de_perigosos():
    """Leitura ampla é o que um BI faz. Recusá-la inviabilizaria o produto."""
    assert "pg_read_all_data" not in _PAPEIS_PERIGOSOS


# ─── Vários problemas de uma vez ──────────────────────────────────────────────


def test_todos_os_impedimentos_sao_reportados_juntos():
    """Reportar um por vez faria o cliente corrigir, tentar, falhar de novo."""
    impedimentos, alertas = avaliar_privilegios(
        privilegios(
            eh_superusuario=True,
            pode_criar_no_banco=True,
            ignora_rls=True,
            tabelas_com_escrita=7,
            papeis=frozenset({"pg_write_server_files", "pg_execute_server_program"}),
        )
    )
    assert len(impedimentos) == 5  # superusuário, escrita, create, e os dois papéis
    assert len(alertas) == 1  # bypassrls


def test_avaliacao_nao_altera_a_entrada():
    """Função pura: o dataclass é congelado e nada é mutado."""
    p = privilegios(papeis=frozenset({"pg_maintain"}))
    avaliar_privilegios(p)
    assert p.papeis == frozenset({"pg_maintain"})
    with pytest.raises(Exception):
        p.eh_superusuario = True  # type: ignore[misc]
