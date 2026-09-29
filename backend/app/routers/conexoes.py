"""Conexões a bancos de clientes: cadastro, teste e perfilamento.

Todo acesso passa por `TenantRepository`, que exige o tenant no construtor — e o
tenant vem do token assinado (`TenantAtual`), nunca da rota ou do corpo.

Conexão de outro tenant responde **404, não 403**: um 403 confirmaria que aquele id
existe em algum lugar, o que já é informação sobre outro cliente.
"""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status

from app.connectors.base import ErroDeConexao
from app.connectors.registry import TipoNaoSuportado, criar_conector
from app.core.crypto import cifrar
from app.core.deps import Db, TenantAtual, UsuarioAtual, exigir_perfil
from app.models.conexao import Conexao, StatusConexao
from app.models.tenant import PerfilUsuario
from app.repositories.base import TenantRepository
from app.schemas.conexao import (
    ConexaoAtualizar,
    ConexaoCriar,
    ConexaoResposta,
    ResultadoPerfilamento,
    ResultadoTeste,
)
from app.semantic.persistencia import persistir_retrato
from app.semantic.profiler import perfilar

logger = structlog.get_logger()

router = APIRouter(prefix="/api/v1/conexoes", tags=["conexoes"])

# Cadastrar e perfilar mexem no que a plataforma enxerga do banco do cliente.
SomenteAdmin = Annotated[object, Depends(exigir_perfil(PerfilUsuario.admin))]


def _nao_encontrada() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"erro": "NAO_ENCONTRADA", "mensagem": "Conexão não encontrada."},
    )


async def _buscar(db, tenant_id: int, conexao_id: int) -> Conexao:
    repo = TenantRepository(Conexao, db, tenant_id)
    conexao = await repo.get(conexao_id)
    if conexao is None:
        raise _nao_encontrada()
    return conexao


# ─── Cadastro ─────────────────────────────────────────────────────────────────


@router.post("", response_model=ConexaoResposta, status_code=status.HTTP_201_CREATED)
async def criar(
    dados: ConexaoCriar,
    db: Db,
    tenant_id: TenantAtual,
    _: SomenteAdmin,
) -> Conexao:
    """Cadastra e **testa antes de salvar**.

    Guardar uma credencial que não funciona só adianta o problema para o momento em
    que alguém tentar perguntar algo. Aqui o cliente ainda está na tela e corrige.
    """
    repo = TenantRepository(Conexao, db, tenant_id)
    if await repo.existe(nome=dados.nome):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "erro": "NOME_EM_USO",
                "mensagem": f"Já existe uma conexão chamada '{dados.nome}'.",
            },
        )

    conexao = await repo.criar(
        {
            "nome": dados.nome,
            "tipo": dados.tipo,
            "host": dados.host,
            "porta": dados.porta_efetiva(),
            "banco": dados.banco,
            "usuario": dados.usuario,
            # A senha em claro vive só nesta linha; daqui em diante é Fernet.
            "senha_cifrada": cifrar(dados.senha.get_secret_value()),
            "usar_ssl": dados.usar_ssl,
            "status": StatusConexao.pendente,
        }
    )

    await _testar_e_registrar(conexao)
    return conexao


@router.get("", response_model=list[ConexaoResposta])
async def listar(db: Db, tenant_id: TenantAtual, _: UsuarioAtual) -> list[Conexao]:
    repo = TenantRepository(Conexao, db, tenant_id)
    itens, _total = await repo.listar(limite=200, ordenar_por=Conexao.nome)
    return list(itens)


@router.get("/{conexao_id}", response_model=ConexaoResposta)
async def obter(conexao_id: int, db: Db, tenant_id: TenantAtual, _: UsuarioAtual) -> Conexao:
    return await _buscar(db, tenant_id, conexao_id)


@router.patch("/{conexao_id}", response_model=ConexaoResposta)
async def atualizar(
    conexao_id: int,
    dados: ConexaoAtualizar,
    db: Db,
    tenant_id: TenantAtual,
    _: SomenteAdmin,
) -> Conexao:
    """Senha ausente mantém a guardada — o formulário não precisa reenviá-la."""
    conexao = await _buscar(db, tenant_id, conexao_id)
    campos = dados.model_dump(exclude_unset=True, exclude={"senha"})
    for chave, valor in campos.items():
        setattr(conexao, chave, valor)
    if dados.senha is not None:
        conexao.senha_cifrada = cifrar(dados.senha.get_secret_value())
    await db.flush()

    await _testar_e_registrar(conexao)
    return conexao


@router.delete("/{conexao_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remover(conexao_id: int, db: Db, tenant_id: TenantAtual, _: SomenteAdmin) -> None:
    repo = TenantRepository(Conexao, db, tenant_id)
    if not await repo.remover(conexao_id):
        raise _nao_encontrada()


# ─── Teste e perfilamento ─────────────────────────────────────────────────────


async def _testar_e_registrar(conexao: Conexao) -> list[str]:
    """Testa e grava o desfecho na própria conexão.

    Não levanta quando a conexão falha: o cadastro fica salvo com `status=erro` e
    `ultimo_erro` preenchido, para o cliente poder corrigir o `GRANT` e tentar de novo
    sem redigitar tudo. Falhar aqui apagaria o formulário junto com o erro.
    """
    from datetime import datetime, timezone

    conexao.ultimo_teste_em = datetime.now(timezone.utc)
    try:
        conector = criar_conector(conexao)
    except TipoNaoSuportado as e:
        conexao.status = StatusConexao.erro
        conexao.ultimo_erro = str(e)
        return []

    try:
        alertas = await conector.testar()
    except ErroDeConexao as e:
        conexao.status = StatusConexao.erro
        conexao.ultimo_erro = str(e)
        return []
    finally:
        await conector.fechar()

    conexao.status = StatusConexao.conectada
    conexao.ultimo_erro = None
    return alertas


@router.post("/{conexao_id}/testar", response_model=ResultadoTeste)
async def testar(
    conexao_id: int, db: Db, tenant_id: TenantAtual, _: UsuarioAtual
) -> ResultadoTeste:
    conexao = await _buscar(db, tenant_id, conexao_id)
    alertas = await _testar_e_registrar(conexao)
    await db.flush()
    return ResultadoTeste(conectou=conexao.status is not StatusConexao.erro, alertas=alertas)


@router.post("/{conexao_id}/perfilar", response_model=ResultadoPerfilamento)
async def perfilar_conexao(
    conexao_id: int,
    db: Db,
    tenant_id: TenantAtual,
    _: SomenteAdmin,
    limite_tabelas: int | None = None,
) -> ResultadoPerfilamento:
    """Lê o catálogo do banco do cliente e atualiza o nosso.

    Reperfilar **preserva o que uma pessoa revisou**: só a estatística é sobrescrita.
    O resumo devolve `revisoes_preservadas` para a tela poder dizer isso — quem
    reperfila precisa ver que não perdeu o trabalho de revisão.

    Síncrono nesta versão. Um banco com milhares de tabelas vai estourar o tempo da
    requisição, e é por isso que existe `limite_tabelas`; a fila em segundo plano
    entra quando houver o primeiro cliente grande de verdade.
    """
    conexao = await _buscar(db, tenant_id, conexao_id)

    try:
        conector = criar_conector(conexao)
    except TipoNaoSuportado as e:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={"erro": "TIPO_NAO_SUPORTADO", "mensagem": str(e)},
        ) from e

    try:
        retrato = await perfilar(conector, limite_tabelas=limite_tabelas)
    except ErroDeConexao as e:
        conexao.status = StatusConexao.erro
        conexao.ultimo_erro = str(e)
        await db.flush()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"erro": "FALHA_NO_PERFILAMENTO", "mensagem": str(e)},
        ) from e
    finally:
        await conector.fechar()

    resumo = await persistir_retrato(
        db, tenant_id=tenant_id, conexao=conexao, retrato=retrato
    )
    conexao.status = StatusConexao.catalogada
    conexao.ultimo_erro = None
    await db.flush()

    return ResultadoPerfilamento(
        tabelas_criadas=resumo.tabelas_criadas,
        tabelas_atualizadas=resumo.tabelas_atualizadas,
        tabelas_removidas=resumo.tabelas_removidas,
        colunas_criadas=resumo.colunas_criadas,
        colunas_atualizadas=resumo.colunas_atualizadas,
        colunas_removidas=resumo.colunas_removidas,
        ligacoes_gravadas=resumo.ligacoes_gravadas,
        ligacoes_preservadas=resumo.ligacoes_preservadas,
        revisoes_preservadas=resumo.revisoes_preservadas,
        versao_dicionario=resumo.versao_dicionario,
        avisos=resumo.avisos,
    )
