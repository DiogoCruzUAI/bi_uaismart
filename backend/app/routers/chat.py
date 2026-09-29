"""Perguntar em português a uma conexão catalogada.

Toda pergunta — respondida, recusada ou falha — gera uma linha em `consultas`. As
recusadas ficam de propósito: são o mapa de onde o dicionário está incompleto, e é a
partir delas que a plataforma melhora sem depender de suposição.
"""

import structlog
from fastapi import APIRouter, HTTPException, status

from app.connectors.registry import TipoNaoSuportado, criar_conector
from app.core.deps import Db, TenantAtual, UsuarioAtual
from app.llm.cliente import LlmIndisponivel
from app.models.chat import Consulta, ModoConsulta, StatusConsulta
from app.models.conexao import Conexao
from app.repositories.base import TenantRepository
from app.schemas.chat import ColunaResposta, PerguntaRequisicao, Procedencia, RespostaChat
from app.text2sql.catalogo import carregar_catalogo
from app.text2sql.pipeline import responder

logger = structlog.get_logger()

router = APIRouter(prefix="/api/v1/conexoes", tags=["chat"])


@router.post("/{conexao_id}/perguntar", response_model=RespostaChat)
async def perguntar(
    conexao_id: int,
    dados: PerguntaRequisicao,
    db: Db,
    tenant_id: TenantAtual,
    usuario: UsuarioAtual,
) -> RespostaChat:
    conexao = await TenantRepository(Conexao, db, tenant_id).get(conexao_id)
    if conexao is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"erro": "NAO_ENCONTRADA", "mensagem": "Conexão não encontrada."},
        )

    catalogo = await carregar_catalogo(
        db,
        tenant_id=tenant_id,
        conexao_id=conexao.id,
        versao_dicionario=conexao.versao_dicionario,
    )

    try:
        conector = criar_conector(conexao)
    except TipoNaoSuportado as e:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={"erro": "TIPO_NAO_SUPORTADO", "mensagem": str(e)},
        ) from e

    try:
        r = await responder(dados.pergunta, catalogo=catalogo, conector=conector)
    except LlmIndisponivel as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"erro": "LLM_INDISPONIVEL", "mensagem": str(e)},
        ) from e
    finally:
        await conector.fechar()

    # ─── Procedência ──────────────────────────────────────────────────────────
    consulta = Consulta(
        tenant_id=tenant_id,
        conexao_id=conexao.id,
        pergunta=r.pergunta,
        modo=ModoConsulta.semantico,
        status=StatusConsulta.executada if r.respondeu else StatusConsulta.recusada,
        motivo_recusa=r.motivo or None,
        spec=r.spec.model_dump(mode="json") if r.spec else None,
        sql_gerado=r.sql,
        linhas_estimadas=r.linhas_estimadas,
        custo_estimado=r.custo_estimado,
        linhas_retornadas=len(r.linhas),
        duracao_ms=r.duracao_ms,
        versao_dicionario=conexao.versao_dicionario,
        modelo_llm=r.uso.modelo or None,
        tokens_entrada=r.uso.entrada,
        tokens_cache_leitura=r.uso.cache_leitura,
        tokens_saida=r.uso.saida,
    )
    db.add(consulta)
    await db.flush()

    logger.info(
        "pergunta_respondida" if r.respondeu else "pergunta_recusada",
        consulta_id=consulta.id,
        conexao_id=conexao.id,
        usuario_id=usuario.id,
        linhas=len(r.linhas),
    )

    return RespostaChat(
        respondeu=r.respondeu,
        pergunta=r.pergunta,
        explicacao=r.explicacao,
        motivo=r.motivo,
        colunas=[
            ColunaResposta(
                rotulo=c.rotulo,
                tipo=c.tipo,
                coluna_origem=c.coluna_origem,
                unidade=c.unidade,
                moeda=c.moeda,
                escala_aplicada=c.escala_aplicada,
                dicionario_revisado=c.dicionario_revisado,
            )
            for c in r.colunas
        ],
        linhas=r.linhas,
        avisos=r.avisos,
        consulta_id=consulta.id,
        procedencia=Procedencia(
            sql=r.sql,
            spec=r.spec.model_dump(mode="json") if r.spec else None,
            linhas_estimadas=r.linhas_estimadas,
            custo_estimado=r.custo_estimado,
            linhas_retornadas=len(r.linhas),
            duracao_ms=r.duracao_ms,
            truncado=r.truncado,
            versao_dicionario=conexao.versao_dicionario,
            tabelas_consideradas=r.tabelas_consideradas,
            modelo_llm=r.uso.modelo or None,
            tokens_entrada=r.uso.entrada,
            tokens_cache_leitura=r.uso.cache_leitura,
            tokens_saida=r.uso.saida,
        ),
    )
