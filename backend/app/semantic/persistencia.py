"""Grava o retrato no catálogo, sem destruir o trabalho humano.

A regra que este módulo existe para sustentar: **reperfilar atualiza a estatística e
preserva o julgamento**.

Um humano do tenant revisou e escreveu "salario está em centavos; NÃO é o salário
líquido". Amanhã a tabela ganha uma coluna, alguém reperfila, e um `DELETE` seguido de
`INSERT` apagaria essa frase. O dicionário voltaria ao zero, em silêncio, e as
respostas voltariam a ficar plausíveis e erradas — sem ninguém perceber, porque nada
falhou.

Por isso o caminho é upsert, campo a campo:

- **Estatística** (linhas, cardinalidade, fração nula, amostra) é sempre sobrescrita:
  é o retrato de agora.
- **Julgamento** (descrição, o que não é, unidade, escala, revisão) só é escrito
  quando ainda está vazio. O que uma pessoa revisou não é tocado.
- **Sumido do banco de origem** é marcado como removido, não apagado: a procedência de
  uma consulta antiga precisa continuar apontando para algo.
"""

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conexao import Conexao
from app.models.semantico import Coluna, OrigemRelacionamento, Relacionamento, Tabela
from app.semantic.retrato import RetratoBanco

logger = structlog.get_logger()


@dataclass(slots=True)
class ResumoPersistencia:
    tabelas_criadas: int = 0
    tabelas_atualizadas: int = 0
    tabelas_removidas: int = 0
    colunas_criadas: int = 0
    colunas_atualizadas: int = 0
    colunas_removidas: int = 0
    ligacoes_gravadas: int = 0
    ligacoes_preservadas: int = 0
    versao_dicionario: int = 0
    # Quantos campos de julgamento humano foram mantidos intactos. Vai para a tela:
    # quem reperfila precisa ver que não perdeu o que revisou.
    revisoes_preservadas: int = 0
    avisos: list[str] = field(default_factory=list)


def _agora() -> datetime:
    return datetime.now(timezone.utc)


async def persistir_retrato(
    db: AsyncSession,
    *,
    tenant_id: int,
    conexao: Conexao,
    retrato: RetratoBanco,
) -> ResumoPersistencia:
    """Reconcilia o catálogo daquela conexão com o retrato recém-coletado."""
    resumo = ResumoPersistencia()

    if conexao.tenant_id != tenant_id:
        # Erro de programação, não de entrada: falha alto antes de escrever nada.
        raise ValueError("Conexão não pertence ao tenant informado.")

    # ─── Tabelas ──────────────────────────────────────────────────────────────
    existentes = {
        f"{t.esquema}.{t.nome}": t
        for t in (
            await db.execute(
                select(Tabela).where(
                    Tabela.tenant_id == tenant_id, Tabela.conexao_id == conexao.id
                )
            )
        )
        .scalars()
        .all()
    }
    vistas: set[str] = set()
    por_qualificado: dict[str, Tabela] = {}

    for rt in retrato.tabelas:
        chave = rt.qualificado
        vistas.add(chave)
        tabela = existentes.get(chave)

        if tabela is None:
            tabela = Tabela(
                tenant_id=tenant_id,
                conexao_id=conexao.id,
                esquema=rt.esquema,
                nome=rt.nome,
            )
            db.add(tabela)
            resumo.tabelas_criadas += 1
        else:
            resumo.tabelas_atualizadas += 1
            # Voltou a existir depois de ter sumido.
            tabela.deletado_em = None

        # Estatística: sempre o retrato de agora.
        tabela.eh_view = rt.eh_view
        tabela.linhas_estimadas = rt.linhas_estimadas
        tabela.bytes_estimados = rt.bytes_estimados

        # Papel é inferência nossa, não julgamento humano — mas se alguém revisou a
        # tabela, a escolha dela vale mais que a heurística.
        if tabela.revisada_em is None:
            tabela.papel = rt.papel
        else:
            resumo.revisoes_preservadas += 1

        por_qualificado[chave] = tabela

    for chave, tabela in existentes.items():
        if chave not in vistas and tabela.deletado_em is None:
            tabela.deletado_em = _agora()
            resumo.tabelas_removidas += 1

    # `flush` para as tabelas novas ganharem id antes de as colunas apontarem para elas.
    await db.flush()

    # ─── Colunas ──────────────────────────────────────────────────────────────
    ids = [t.id for t in por_qualificado.values()]
    colunas_existentes: dict[tuple[int, str], Coluna] = {}
    if ids:
        for c in (
            (
                await db.execute(
                    select(Coluna).where(
                        Coluna.tenant_id == tenant_id, Coluna.tabela_id.in_(ids)
                    )
                )
            )
            .scalars()
            .all()
        ):
            colunas_existentes[(c.tabela_id, c.nome)] = c

    vistas_colunas: set[tuple[int, str]] = set()

    for rt in retrato.tabelas:
        tabela = por_qualificado[rt.qualificado]
        for rc in rt.colunas:
            chave = (tabela.id, rc.nome)
            vistas_colunas.add(chave)
            coluna = colunas_existentes.get(chave)

            if coluna is None:
                coluna = Coluna(
                    tenant_id=tenant_id,
                    tabela_id=tabela.id,
                    nome=rc.nome,
                    tipo_sql=rc.tipo_sql,
                )
                db.add(coluna)
                resumo.colunas_criadas += 1
            else:
                resumo.colunas_atualizadas += 1
                coluna.deletado_em = None

            # Estatística: sempre sobrescrita.
            coluna.tipo_sql = rc.tipo_sql
            coluna.aceita_nulo = rc.aceita_nulo
            coluna.eh_chave_primaria = rc.eh_chave_primaria
            coluna.cardinalidade_estimada = rc.cardinalidade
            coluna.fracao_nula = rc.fracao_nula
            coluna.amostra = {
                "valores_comuns": rc.valores_comuns,
                "minimo": rc.minimo,
                "maximo": rc.maximo,
                "magnitude": rc.sinais.magnitude_maxima,
                "sinais": [
                    marca
                    for marca, ligada in (
                        ("identificador", rc.sinais.parece_identificador),
                        ("categorica", rc.sinais.parece_categorico),
                        ("constante", rc.sinais.parece_constante),
                        ("quase_sempre_nula", rc.sinais.quase_sempre_nulo),
                        ("sem_estatistica", rc.sinais.sem_estatistica),
                    )
                    if ligada
                ],
            }

            # Julgamento: nunca sobrescrito. `unidade` e `escala` são o que separa
            # R$ 3.200 de R$ 320.000 — perder isso num reperfilamento reintroduziria
            # exatamente o erro que a plataforma existe para evitar.
            if coluna.revisada_em is not None:
                resumo.revisoes_preservadas += 1

    for chave, coluna in colunas_existentes.items():
        if chave not in vistas_colunas and coluna.deletado_em is None:
            coluna.deletado_em = _agora()
            resumo.colunas_removidas += 1

    await db.flush()

    # ─── Ligações ─────────────────────────────────────────────────────────────
    resumo.ligacoes_gravadas, resumo.ligacoes_preservadas = await _persistir_ligacoes(
        db, tenant_id=tenant_id, conexao=conexao, retrato=retrato
    )

    # ─── Versão do dicionário ─────────────────────────────────────────────────
    # A versão entra na chave de cache de toda pergunta. Só muda quando a estrutura
    # mudou: reperfilar sem novidade não pode invalidar o cache de todo mundo.
    houve_mudanca_estrutural = any(
        (
            resumo.tabelas_criadas,
            resumo.tabelas_removidas,
            resumo.colunas_criadas,
            resumo.colunas_removidas,
        )
    )
    if houve_mudanca_estrutural:
        conexao.versao_dicionario += 1
    conexao.ultimo_perfilamento_em = _agora()
    resumo.versao_dicionario = conexao.versao_dicionario

    if retrato.tabelas_nao_perfiladas:
        resumo.avisos.append(
            f"{retrato.tabelas_nao_perfiladas} tabela(s) ficaram fora do retrato. "
            "O catálogo está incompleto."
        )

    await db.flush()
    # `vars()` não serve: o dataclass usa `slots=True` e não tem `__dict__`.
    contadores = {
        campo.name: getattr(resumo, campo.name)
        for campo in fields(resumo)
        if isinstance(getattr(resumo, campo.name), int)
    }
    logger.info(
        "retrato_persistido", conexao_id=conexao.id, tenant_id=tenant_id, **contadores
    )
    return resumo


async def _persistir_ligacoes(
    db: AsyncSession, *, tenant_id: int, conexao: Conexao, retrato: RetratoBanco
) -> tuple[int, int]:
    """Regrava as ligações, preservando as que uma pessoa revisou.

    Ligação revisada é decisão humana sobre como as tabelas se juntam. A inferência
    roda de novo a cada perfilamento e chegaria com outra confiança, ou nem chegaria —
    sobrescrever transformaria a revisão em trabalho perdido toda semana.
    """
    atuais = (
        (
            await db.execute(
                select(Relacionamento).where(
                    Relacionamento.tenant_id == tenant_id,
                    Relacionamento.conexao_id == conexao.id,
                )
            )
        )
        .scalars()
        .all()
    )

    preservadas = 0
    for r in atuais:
        if r.revisada_em is not None:
            preservadas += 1
        else:
            await db.delete(r)
    await db.flush()

    # Mapa coluna → id, para resolver as pontas das ligações.
    colunas = (
        (
            await db.execute(
                select(Coluna, Tabela)
                .join(Tabela, Coluna.tabela_id == Tabela.id)
                .where(Tabela.tenant_id == tenant_id, Tabela.conexao_id == conexao.id)
            )
        )
        .all()
    )
    por_nome: dict[tuple[str, str, str], int] = {
        (t.esquema.lower(), t.nome.lower(), c.nome.lower()): c.id for c, t in colunas
    }

    def id_de(esquema: str, tabela: str, coluna: str) -> int | None:
        return por_nome.get((esquema.lower(), tabela.lower(), coluna.lower()))

    gravadas = 0
    candidatas = [
        (lig.esquema_origem, lig.tabela_origem, lig.coluna_origem,
         lig.esquema_destino, lig.tabela_destino, lig.coluna_destino,
         OrigemRelacionamento.fk_declarada, 1.0)
        for lig in retrato.ligacoes_declaradas
    ] + [
        (lig.esquema_origem, lig.tabela_origem, lig.coluna_origem,
         lig.esquema_destino, lig.tabela_destino, lig.coluna_destino,
         OrigemRelacionamento.inferida, lig.confianca)
        for lig in retrato.ligacoes_inferidas
    ]

    for eo, to, co, ed, td, cd, origem, confianca in candidatas:
        origem_id, destino_id = id_de(eo, to, co), id_de(ed, td, cd)
        # Ponta fora do retrato (tabela não perfilada, por exemplo): a ligação não
        # tem onde se apoiar e é descartada em vez de virar referência quebrada.
        if origem_id is None or destino_id is None:
            continue
        db.add(
            Relacionamento(
                tenant_id=tenant_id,
                conexao_id=conexao.id,
                coluna_origem_id=origem_id,
                coluna_destino_id=destino_id,
                origem=origem,
                confianca=confianca,
            )
        )
        gravadas += 1

    await db.flush()
    return gravadas, preservadas
