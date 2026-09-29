"""Testes do gerador de dicionário, sem chamar a API.

Tudo que decide qualidade é testável sem chave: montagem do pedido, lotes, validação
da resposta e aplicação ao catálogo. A chamada em si é uma linha, e é a única parte
que espera a `ANTHROPIC_API_KEY`.

O teste mais importante é `test_coluna_inventada_e_descartada`. Saída estruturada
garante o **formato**, não o **conteúdo**: o modelo pode devolver uma coluna que não
existe com o schema perfeitamente válido. Se ela entrar no catálogo, o Text-to-SQL
passa a gerar SQL referenciando algo que o banco não tem.
"""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.semantico import Coluna, Tabela, UnidadeColuna
from app.semantic.dicionario import (
    INSTRUCOES,
    PropostaColuna,
    PropostaDicionario,
    PropostaTabela,
    agrupar_tabelas,
    aplicar_proposta,
    montar_pedido,
    validar_proposta,
)
from app.semantic.persistencia import persistir_retrato
from app.semantic.retrato import RetratoBanco, RetratoColuna, RetratoTabela
from app.semantic.sinais import classificar_coluna


def rc(nome, tipo="integer", *, pk=False, card=None, maxi=None, nulo=0.0):
    return RetratoColuna(
        nome=nome,
        tipo_sql=tipo,
        aceita_nulo=not pk,
        eh_chave_primaria=pk,
        sinais=classificar_coluna(
            tipo_sql=tipo, cardinalidade=card, fracao_nula=nulo,
            linhas_tabela=1_000_000, maximo=maxi, eh_chave_primaria=pk,
        ),
        cardinalidade=card,
        maximo=maxi,
    )


@pytest.fixture
def retrato():
    return RetratoBanco(
        coletado_em=datetime.now(timezone.utc),
        tabelas=[
            RetratoTabela(
                esquema="public", nome="movimentacoes", eh_view=False, papel="fato",
                linhas_estimadas=293_000_000, bytes_estimados=9_000_000_000,
                colunas=[
                    rc("id", "bigint", pk=True, card=293_000_000),
                    rc("salario", "bigint", card=4_200_000, maxi="10000000000"),
                ],
            ),
            RetratoTabela(
                esquema="public", nome="empresas", eh_view=False, papel="dimensao",
                linhas_estimadas=73_000_000, bytes_estimados=4_000_000_000,
                colunas=[rc("id", "bigint", pk=True, card=73_000_000)],
            ),
        ],
    )


def proposta_boa() -> PropostaDicionario:
    return PropostaDicionario(
        tabelas=[
            PropostaTabela(
                tabela="public.movimentacoes",
                descricao="Movimentações de emprego.",
                descricao_negativa="NÃO é o efetivo atual; é o fluxo do período.",
                papel="fato",
                confianca=0.9,
                colunas=[
                    PropostaColuna(
                        nome="id", descricao="Identificador da movimentação.",
                        unidade=UnidadeColuna.identificador, escala=1.0, confianca=0.95,
                    ),
                    PropostaColuna(
                        nome="salario",
                        descricao="Remuneração mensal contratada, em centavos.",
                        descricao_negativa="NÃO é o salário líquido.",
                        unidade=UnidadeColuna.monetaria, escala=100.0,
                        moeda="BRL", confianca=0.85,
                    ),
                ],
            )
        ]
    )


# ─── Montagem do pedido ───────────────────────────────────────────────────────


def test_pedido_leva_so_as_tabelas_escolhidas(retrato):
    pedido = montar_pedido(retrato, ["public.empresas"])
    assert "public.empresas" in pedido
    assert "public.movimentacoes" not in pedido


def test_pedido_carrega_a_evidencia_de_escala(retrato):
    """Sem a magnitude, o modelo não tem como concluir centavos."""
    pedido = montar_pedido(retrato, ["public.movimentacoes"])
    corpo = json.loads(pedido.split("```json\n")[1].split("\n```")[0])
    salario = next(
        c for c in corpo["tabelas"][0]["colunas"] if c["nome"] == "salario"
    )
    assert salario["magnitude"] == 10


def test_pedido_e_estavel_entre_chamadas(retrato):
    """Prefixo instável derruba o cache de prompt sem nada parecer errado.

    A conta só aparece na fatura, então a estabilidade vira teste.
    """
    a = montar_pedido(retrato, ["public.movimentacoes"])
    b = montar_pedido(retrato, ["public.movimentacoes"])
    assert a == b


def test_instrucoes_proibem_inventar():
    """As instruções são o produto tanto quanto o código."""
    assert "Não invente" in INSTRUCOES
    assert "desconhecida" in INSTRUCOES
    assert "descricao_negativa" in INSTRUCOES


# ─── Lotes ────────────────────────────────────────────────────────────────────


def test_lotes_cortam_por_coluna_nao_por_tabela(retrato):
    """Uma tabela de 300 colunas pesa mais que trinta de dez."""
    lotes = agrupar_tabelas(retrato, colunas_por_lote=2)
    assert len(lotes) == 2
    assert lotes[0] == ["public.empresas"]  # 1 coluna
    assert lotes[1] == ["public.movimentacoes"]  # 2 colunas


def test_lote_unico_quando_cabe(retrato):
    lotes = agrupar_tabelas(retrato, colunas_por_lote=120)
    assert len(lotes) == 1
    assert set(lotes[0]) == {"public.movimentacoes", "public.empresas"}


def test_tabela_maior_que_o_lote_nao_e_perdida(retrato):
    """Corte agressivo não pode descartar tabela nenhuma."""
    lotes = agrupar_tabelas(retrato, colunas_por_lote=1)
    todas = {t for lote in lotes for t in lote}
    assert todas == {"public.movimentacoes", "public.empresas"}


# ─── Validação ────────────────────────────────────────────────────────────────


def test_proposta_correta_passa_inteira(retrato):
    r = validar_proposta(proposta_boa(), retrato, ["public.movimentacoes"])
    assert r.problemas == []
    assert len(r.tabelas[0].colunas) == 2


def test_coluna_inventada_e_descartada(retrato):
    """Coluna que não existe no catálogo faria o Text-to-SQL gerar SQL quebrado."""
    p = proposta_boa()
    p.tabelas[0].colunas.append(
        PropostaColuna(
            nome="coluna_que_nao_existe", descricao="Inventada.",
            unidade=UnidadeColuna.texto, escala=1.0, confianca=0.9,
        )
    )
    r = validar_proposta(p, retrato, ["public.movimentacoes"])
    assert r.colunas_descartadas == 1
    nomes = {c.nome for c in r.tabelas[0].colunas}
    assert "coluna_que_nao_existe" not in nomes


def test_tabela_inventada_e_descartada(retrato):
    p = proposta_boa()
    p.tabelas.append(
        PropostaTabela(
            tabela="public.inexistente", descricao="Inventada.",
            papel="fato", confianca=0.9, colunas=[],
        )
    )
    r = validar_proposta(p, retrato, ["public.movimentacoes"])
    assert r.tabelas_descartadas == 1
    assert [t.tabela for t in r.tabelas] == ["public.movimentacoes"]


def test_escala_implausivel_vira_um(retrato):
    """Preferimos não converter a converter errado."""
    p = proposta_boa()
    p.tabelas[0].colunas[1] = p.tabelas[0].colunas[1].model_copy(update={"escala": 7.0})
    r = validar_proposta(p, retrato, ["public.movimentacoes"])
    assert r.tabelas[0].colunas[1].escala == 1.0
    assert any("implausível" in m for m in r.problemas)


def test_escala_de_centavos_e_preservada(retrato):
    r = validar_proposta(proposta_boa(), retrato, ["public.movimentacoes"])
    salario = next(c for c in r.tabelas[0].colunas if c.nome == "salario")
    assert salario.escala == 100.0


def test_moeda_em_coluna_nao_monetaria_e_removida(retrato):
    p = proposta_boa()
    p.tabelas[0].colunas[0] = p.tabelas[0].colunas[0].model_copy(
        update={"moeda": "BRL", "unidade": UnidadeColuna.identificador}
    )
    r = validar_proposta(p, retrato, ["public.movimentacoes"])
    assert r.tabelas[0].colunas[0].moeda == ""


def test_coluna_esquecida_e_contada(retrato):
    """Saber o que ficou sem descrição é o que permite repedir só o que falta."""
    p = proposta_boa()
    p.tabelas[0].colunas = p.tabelas[0].colunas[:1]
    r = validar_proposta(p, retrato, ["public.movimentacoes"])
    assert r.colunas_sem_proposta == 1


def test_tabela_pedida_e_nao_descrita_vira_problema(retrato):
    r = validar_proposta(
        proposta_boa(), retrato, ["public.movimentacoes", "public.empresas"]
    )
    assert any("não foram descritas" in m for m in r.problemas)


# ─── Aplicação ao catálogo ────────────────────────────────────────────────────


@pytest.fixture
async def catalogado(db, tenant, conexao, retrato):
    await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=retrato)
    return conexao


async def test_proposta_e_gravada_no_catalogo(db, tenant, catalogado):
    resumo = await aplicar_proposta(
        db, tenant_id=tenant.id, conexao=catalogado, tabelas=proposta_boa().tabelas
    )
    assert resumo.tabelas_descritas == 1
    assert resumo.colunas_descritas == 2

    salario = (await db.execute(select(Coluna).where(Coluna.nome == "salario"))).scalar_one()
    assert salario.descricao == "Remuneração mensal contratada, em centavos."
    assert salario.descricao_negativa == "NÃO é o salário líquido."
    assert salario.unidade is UnidadeColuna.monetaria
    assert salario.escala == 100.0
    assert salario.moeda == "BRL"
    assert salario.confianca_ia == 0.85


async def test_proposta_nao_marca_como_revisada(db, tenant, catalogado):
    """A plataforma usa a descrição da IA e diz que ninguém revisou ainda."""
    await aplicar_proposta(
        db, tenant_id=tenant.id, conexao=catalogado, tabelas=proposta_boa().tabelas
    )
    salario = (await db.execute(select(Coluna).where(Coluna.nome == "salario"))).scalar_one()
    assert salario.revisada_em is None
    assert salario.confianca_ia is not None


async def test_revisao_humana_nao_e_sobrescrita(db, tenant, catalogado):
    """A IA nunca passa por cima de quem revisou — nem para 'melhorar'."""
    salario = (await db.execute(select(Coluna).where(Coluna.nome == "salario"))).scalar_one()
    salario.descricao = "Descrição escrita por uma pessoa."
    salario.escala = 1.0
    salario.revisada_em = datetime.now(timezone.utc)
    await db.flush()

    resumo = await aplicar_proposta(
        db, tenant_id=tenant.id, conexao=catalogado, tabelas=proposta_boa().tabelas
    )

    depois = (await db.execute(select(Coluna).where(Coluna.nome == "salario"))).scalar_one()
    assert depois.descricao == "Descrição escrita por uma pessoa."
    assert depois.escala == 1.0
    assert resumo.revisoes_respeitadas >= 1


async def test_tabela_fora_do_catalogo_e_contada_nao_criada(db, tenant, catalogado):
    p = PropostaDicionario(
        tabelas=[
            PropostaTabela(
                tabela="public.nao_catalogada", descricao="x",
                papel="fato", confianca=0.5, colunas=[],
            )
        ]
    )
    resumo = await aplicar_proposta(db, tenant_id=tenant.id, conexao=catalogado, tabelas=p.tabelas)
    assert resumo.nao_encontradas == 1
    assert resumo.tabelas_descritas == 0

    nomes = {t.nome for t in (await db.execute(select(Tabela))).scalars().all()}
    assert "nao_catalogada" not in nomes


async def test_descricao_negativa_vazia_vira_nulo(db, tenant, catalogado):
    """Vazio e "não há confusão plausível" são a mesma coisa; nulo diz isso melhor."""
    p = proposta_boa()
    p.tabelas[0].colunas[0] = p.tabelas[0].colunas[0].model_copy(
        update={"descricao_negativa": ""}
    )
    await aplicar_proposta(db, tenant_id=tenant.id, conexao=catalogado, tabelas=p.tabelas)

    coluna = (
        await db.execute(select(Coluna).where(Coluna.nome == "id", Coluna.descricao != ""))
    ).scalars().first()
    assert coluna.descricao_negativa is None
