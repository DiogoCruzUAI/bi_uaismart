"""Testes da persistência do retrato no catálogo.

O teste mais importante do arquivo é `test_reperfilar_preserva_revisao_humana`. O modo
de falha que ele impede é silencioso e caro: alguém revisa o dicionário, escreve que
`salario` está em centavos, e um reperfilamento posterior apaga a frase. Nada falha,
nada aparece no log — só as respostas voltam a ficar plausíveis e erradas.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.semantico import Coluna, OrigemRelacionamento, Relacionamento, Tabela
from app.semantic.estrutura import LigacaoInferida
from app.semantic.persistencia import persistir_retrato
from app.semantic.retrato import (
    LigacaoDeclarada,
    RetratoBanco,
    RetratoColuna,
    RetratoTabela,
)
from app.semantic.sinais import classificar_coluna


def rc(nome, tipo="integer", *, pk=False, card=None, maxi=None):
    return RetratoColuna(
        nome=nome,
        tipo_sql=tipo,
        aceita_nulo=not pk,
        eh_chave_primaria=pk,
        sinais=classificar_coluna(
            tipo_sql=tipo,
            cardinalidade=card,
            fracao_nula=0.0,
            linhas_tabela=1_000_000,
            maximo=maxi,
            eh_chave_primaria=pk,
        ),
        cardinalidade=card,
        maximo=maxi,
    )


def retrato_base(*, com_empresas=True) -> RetratoBanco:
    tabelas = [
        RetratoTabela(
            esquema="public",
            nome="movimentacoes",
            eh_view=False,
            papel="fato",
            linhas_estimadas=293_000_000,
            bytes_estimados=90_000_000_000,
            colunas=[
                rc("id", "bigint", pk=True, card=293_000_000),
                rc("salario", "bigint", card=4_200_000, maxi="10000000000"),
            ],
        )
    ]
    if com_empresas:
        tabelas.append(
            RetratoTabela(
                esquema="public",
                nome="empresas",
                eh_view=False,
                papel="dimensao",
                linhas_estimadas=73_000_000,
                bytes_estimados=40_000_000_000,
                colunas=[rc("id", "bigint", pk=True, card=73_000_000)],
            )
        )
    return RetratoBanco(coletado_em=datetime.now(timezone.utc), tabelas=tabelas)


# ─── Primeiro perfilamento ────────────────────────────────────────────────────


async def test_primeiro_perfilamento_cria_o_catalogo(db, tenant, conexao):
    resumo = await persistir_retrato(
        db, tenant_id=tenant.id, conexao=conexao, retrato=retrato_base()
    )
    assert resumo.tabelas_criadas == 2
    assert resumo.colunas_criadas == 3

    tabelas = (await db.execute(select(Tabela))).scalars().all()
    assert {t.nome for t in tabelas} == {"movimentacoes", "empresas"}
    assert all(t.tenant_id == tenant.id for t in tabelas)


async def test_estatistica_e_sinais_chegam_ao_catalogo(db, tenant, conexao):
    """A evidência do salário em centavos precisa sobreviver até o banco."""
    await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=retrato_base())

    salario = (
        await db.execute(select(Coluna).where(Coluna.nome == "salario"))
    ).scalar_one()
    assert salario.cardinalidade_estimada == 4_200_000
    assert salario.amostra["magnitude"] == 10


async def test_conexao_de_outro_tenant_e_recusada(db, tenant, conexao):
    """Erro de programação: falha antes de escrever qualquer coisa."""
    with pytest.raises(ValueError, match="não pertence ao tenant"):
        await persistir_retrato(
            db, tenant_id=tenant.id + 999, conexao=conexao, retrato=retrato_base()
        )


# ─── Reperfilamento ───────────────────────────────────────────────────────────


async def test_reperfilar_atualiza_estatistica_sem_duplicar(db, tenant, conexao):
    await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=retrato_base())

    novo = retrato_base()
    novo.tabelas[0].linhas_estimadas = 300_000_000
    resumo = await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=novo)

    assert resumo.tabelas_criadas == 0
    assert resumo.tabelas_atualizadas == 2

    tabelas = (await db.execute(select(Tabela))).scalars().all()
    assert len(tabelas) == 2
    mov = next(t for t in tabelas if t.nome == "movimentacoes")
    assert mov.linhas_estimadas == 300_000_000


async def test_reperfilar_preserva_revisao_humana(db, tenant, conexao):
    """O teste que protege o dicionário.

    Uma pessoa revisou e escreveu o que a coluna significa e o que ela NÃO significa,
    mais unidade e escala. Reperfilar não pode apagar isso — se apagasse, o produto
    voltaria a errar exatamente onde foi ensinado a acertar.
    """
    from app.models.semantico import UnidadeColuna

    await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=retrato_base())

    salario = (
        await db.execute(select(Coluna).where(Coluna.nome == "salario"))
    ).scalar_one()
    salario.descricao = "Remuneração mensal contratada, em centavos."
    salario.descricao_negativa = "NÃO é o salário líquido nem inclui benefícios."
    salario.unidade = UnidadeColuna.monetaria
    salario.escala = 100.0
    salario.moeda = "BRL"
    salario.revisada_em = datetime.now(timezone.utc)
    await db.flush()

    # Reperfila com estatística diferente.
    novo = retrato_base()
    novo.tabelas[0].colunas[1].cardinalidade = 5_000_000
    await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=novo)

    depois = (
        await db.execute(select(Coluna).where(Coluna.nome == "salario"))
    ).scalar_one()

    # Julgamento: intacto.
    assert depois.descricao == "Remuneração mensal contratada, em centavos."
    assert depois.descricao_negativa == "NÃO é o salário líquido nem inclui benefícios."
    assert depois.unidade is UnidadeColuna.monetaria
    assert depois.escala == 100.0
    assert depois.moeda == "BRL"
    assert depois.revisada_em is not None

    # Estatística: atualizada.
    assert depois.cardinalidade_estimada == 5_000_000


async def test_papel_revisado_nao_e_sobrescrito_pela_heuristica(db, tenant, conexao):
    await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=retrato_base())

    mov = (await db.execute(select(Tabela).where(Tabela.nome == "movimentacoes"))).scalar_one()
    mov.papel = "dimensao"  # a pessoa discordou da heurística
    mov.revisada_em = datetime.now(timezone.utc)
    await db.flush()

    novo = retrato_base()
    novo.tabelas[0].papel = "fato"
    await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=novo)

    depois = (
        await db.execute(select(Tabela).where(Tabela.nome == "movimentacoes"))
    ).scalar_one()
    assert depois.papel == "dimensao"


# ─── Sumiço e retorno ─────────────────────────────────────────────────────────


async def test_tabela_que_sumiu_e_marcada_nao_apagada(db, tenant, conexao):
    """A procedência de uma consulta antiga precisa continuar apontando para algo."""
    await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=retrato_base())

    resumo = await persistir_retrato(
        db, tenant_id=tenant.id, conexao=conexao, retrato=retrato_base(com_empresas=False)
    )
    assert resumo.tabelas_removidas == 1

    empresas = (await db.execute(select(Tabela).where(Tabela.nome == "empresas"))).scalar_one()
    assert empresas.deletado_em is not None  # marcada, ainda existe


async def test_tabela_que_voltou_e_reativada(db, tenant, conexao):
    await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=retrato_base())
    await persistir_retrato(
        db, tenant_id=tenant.id, conexao=conexao, retrato=retrato_base(com_empresas=False)
    )
    await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=retrato_base())

    empresas = (await db.execute(select(Tabela).where(Tabela.nome == "empresas"))).scalar_one()
    assert empresas.deletado_em is None


# ─── Versão do dicionário ─────────────────────────────────────────────────────


async def test_versao_sobe_quando_a_estrutura_muda(db, tenant, conexao):
    inicial = conexao.versao_dicionario
    await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=retrato_base())
    assert conexao.versao_dicionario == inicial + 1


async def test_versao_nao_sobe_sem_mudanca_estrutural(db, tenant, conexao):
    """A versão entra na chave de cache. Reperfilar sem novidade não pode
    invalidar o cache de todo mundo por nada."""
    await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=retrato_base())
    versao = conexao.versao_dicionario

    await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=retrato_base())
    assert conexao.versao_dicionario == versao


async def test_perfilamento_registra_a_data(db, tenant, conexao):
    assert conexao.ultimo_perfilamento_em is None
    await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=retrato_base())
    assert conexao.ultimo_perfilamento_em is not None


# ─── Ligações ─────────────────────────────────────────────────────────────────


async def test_ligacoes_declaradas_e_inferidas_sao_gravadas(db, tenant, conexao):
    r = retrato_base()
    r.tabelas[0].colunas.append(rc("empresa_id", "bigint", card=12_000_000))
    r.ligacoes_declaradas = [
        LigacaoDeclarada("public", "movimentacoes", "empresa_id", "public", "empresas", "id")
    ]
    resumo = await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=r)
    assert resumo.ligacoes_gravadas == 1

    lig = (await db.execute(select(Relacionamento))).scalar_one()
    assert lig.origem is OrigemRelacionamento.fk_declarada
    assert lig.confianca == 1.0


async def test_ligacao_revisada_e_preservada(db, tenant, conexao):
    """Ligação revisada é decisão humana sobre como as tabelas se juntam.

    A inferência roda de novo a cada perfilamento e chegaria com outra confiança, ou
    nem chegaria. Sobrescrever transformaria a revisão em trabalho perdido.
    """
    r = retrato_base()
    r.tabelas[0].colunas.append(rc("empresa_id", "bigint", card=12_000_000))
    r.ligacoes_inferidas = [
        LigacaoInferida(
            "public", "movimentacoes", "empresa_id", "public", "empresas", "id", 0.8, "teste"
        )
    ]
    await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=r)

    lig = (await db.execute(select(Relacionamento))).scalar_one()
    lig.revisada_em = datetime.now(timezone.utc)
    lig.confianca = 1.0
    await db.flush()

    # Reperfila sem a inferência (o nome da coluna mudou, digamos).
    resumo = await persistir_retrato(
        db, tenant_id=tenant.id, conexao=conexao, retrato=retrato_base()
    )
    assert resumo.ligacoes_preservadas == 1

    restantes = (await db.execute(select(Relacionamento))).scalars().all()
    assert len(restantes) == 1
    assert restantes[0].revisada_em is not None


async def test_ligacao_com_ponta_fora_do_retrato_e_descartada(db, tenant, conexao):
    """Referência quebrada é pior que ligação ausente."""
    r = retrato_base()
    r.ligacoes_declaradas = [
        LigacaoDeclarada("public", "movimentacoes", "id", "outro", "inexistente", "id")
    ]
    resumo = await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=r)
    assert resumo.ligacoes_gravadas == 0


async def test_retrato_parcial_gera_aviso(db, tenant, conexao):
    r = retrato_base()
    r.tabelas_nao_perfiladas = 40
    resumo = await persistir_retrato(db, tenant_id=tenant.id, conexao=conexao, retrato=r)
    assert any("40" in a for a in resumo.avisos)
