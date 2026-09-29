"""Perfilamento de um banco de cliente.

Orquestra: lê o catálogo pelo conector, deriva sinais (`sinais`), infere estrutura
(`estrutura`) e monta o retrato (`retrato`). Todo o julgamento fica nas funções puras;
aqui só há coordenação e I/O.

**Nenhuma linha de dado é lida.** Volume vem de `reltuples`, distribuição vem de
`pg_stats` — as duas coisas que o ANALYZE já mantém e que custam zero. `COUNT(*)` numa
tabela de 293 milhões de linhas leva minutos para entregar uma exatidão que o produto
não usa para nada.

Sobre concorrência: as colunas são lidas tabela a tabela, com um teto de requisições
simultâneas. Sequencial, um banco com 4 mil tabelas viraria 4 mil idas e voltas em
série; sem teto, abriríamos conexões demais no banco **do cliente**, que é dele e está
servindo a operação dele. Oito é conservador de propósito.
"""

import asyncio
from collections import Counter
from datetime import datetime, timezone
from typing import Callable

import structlog

from app.connectors.base import Conector
from app.semantic.estrutura import ColunaParaEstrutura, inferir_ligacoes, inferir_papel
from app.semantic.retrato import (
    LigacaoDeclarada,
    RetratoBanco,
    RetratoColuna,
    RetratoTabela,
)
from app.semantic.sinais import classificar_coluna, familia_do_tipo

logger = structlog.get_logger()

CONCORRENCIA_PADRAO = 8

# Confiança mínima para uma ligação inferida contar no grafo que decide o papel das
# tabelas. Heurística alimentando heurística, então o corte é alto: uma ligação
# duvidosa transformaria uma tabela qualquer em "fato" e o erro se propagaria para
# todo o dicionário daquela conexão.
CONFIANCA_PARA_GRAFO = 0.7


async def perfilar(
    conector: Conector,
    *,
    limite_tabelas: int | None = None,
    concorrencia: int = CONCORRENCIA_PADRAO,
    ao_progredir: Callable[[int, int], None] | None = None,
) -> RetratoBanco:
    """Monta o retrato do banco.

    `limite_tabelas` permite perfilar em etapas um banco muito grande — o que fica de
    fora é contado em `tabelas_nao_perfiladas`, para a tela dizer que o retrato está
    incompleto em vez de dar a impressão de que o banco é menor do que é.

    `ao_progredir(feitas, total)` é chamado a cada tabela concluída.
    """
    inicio = datetime.now(timezone.utc)

    brutas = await conector.listar_tabelas()
    total_no_banco = len(brutas)
    if limite_tabelas is not None and total_no_banco > limite_tabelas:
        # As maiores primeiro: num perfilamento parcial, é onde estão as perguntas.
        brutas = sorted(brutas, key=lambda t: t.linhas_estimadas or 0, reverse=True)
        brutas = brutas[:limite_tabelas]

    logger.info("perfilamento_iniciado", tabelas=len(brutas), no_banco=total_no_banco)

    relacionamentos = await conector.listar_relacionamentos()

    # ─── Colunas, com concorrência limitada ───────────────────────────────────
    limite = asyncio.Semaphore(max(1, concorrencia))
    feitas = 0

    async def colunas_de(bruta):
        nonlocal feitas
        async with limite:
            colunas = await conector.listar_colunas(bruta.esquema, bruta.nome)
        feitas += 1
        if ao_progredir:
            ao_progredir(feitas, len(brutas))
        return bruta, colunas

    resultados = await asyncio.gather(
        *(colunas_de(b) for b in brutas), return_exceptions=True
    )

    declaradas_por_par: set[tuple[str, str, str, str]] = set()
    for r in relacionamentos:
        declaradas_por_par.add(
            (r.esquema_origem, r.tabela_origem, r.esquema_destino, r.tabela_destino)
        )

    # ─── Colunas de todas as tabelas, antes de qualquer inferência ────────────
    colunas_por_tabela: dict[str, list[RetratoColuna]] = {}
    brutas_ok: list = []
    para_estrutura: list[ColunaParaEstrutura] = []
    falhas = 0

    for resultado in resultados:
        if isinstance(resultado, BaseException):
            # Uma tabela sem permissão de leitura não pode derrubar o perfilamento
            # inteiro: o retrato parcial ainda serve, e o que falhou fica no log.
            falhas += 1
            logger.warning("falha_ao_perfilar_tabela", erro=type(resultado).__name__)
            continue

        bruta, colunas_brutas = resultado
        qualificado = f"{bruta.esquema}.{bruta.nome}"
        brutas_ok.append(bruta)

        colunas: list[RetratoColuna] = []
        for cb in colunas_brutas:
            sinais = classificar_coluna(
                tipo_sql=cb.tipo_sql,
                cardinalidade=cb.cardinalidade_estimada,
                fracao_nula=cb.fracao_nula,
                linhas_tabela=bruta.linhas_estimadas,
                minimo=cb.minimo,
                maximo=cb.maximo,
                eh_chave_primaria=cb.eh_chave_primaria,
            )
            colunas.append(
                RetratoColuna(
                    nome=cb.nome,
                    tipo_sql=cb.tipo_sql,
                    aceita_nulo=cb.aceita_nulo,
                    eh_chave_primaria=cb.eh_chave_primaria,
                    sinais=sinais,
                    cardinalidade=cb.cardinalidade_estimada,
                    fracao_nula=cb.fracao_nula,
                    valores_comuns=cb.valores_comuns,
                    minimo=cb.minimo,
                    maximo=cb.maximo,
                )
            )
            para_estrutura.append(
                ColunaParaEstrutura(
                    esquema=bruta.esquema,
                    tabela=bruta.nome,
                    nome=cb.nome,
                    familia=familia_do_tipo(cb.tipo_sql),
                    eh_chave_primaria=cb.eh_chave_primaria,
                    sinais=sinais,
                )
            )

        colunas_por_tabela[qualificado] = colunas

    # ─── Ligações primeiro, papéis depois ─────────────────────────────────────
    #
    # A ordem importa e já esteve errada aqui. Inferindo papel antes das ligações, um
    # banco sem `FOREIGN KEY` declarada — que é o banco de cliente típico, porque o
    # ORM cuidava disso ou a restrição saiu por desempenho — tem grafo vazio, e
    # **tudo** vira "desconhecido". As ligações inferidas são justamente a informação
    # que permite classificar nesse caso.
    ligacoes_inferidas = inferir_ligacoes(para_estrutura, declaradas=declaradas_por_par)

    saindo: Counter[str] = Counter()
    entrando: Counter[str] = Counter()
    for r in relacionamentos:
        saindo[f"{r.esquema_origem}.{r.tabela_origem}"] += 1
        entrando[f"{r.esquema_destino}.{r.tabela_destino}"] += 1
    for lig in ligacoes_inferidas:
        if lig.confianca < CONFIANCA_PARA_GRAFO:
            continue
        saindo[f"{lig.esquema_origem}.{lig.tabela_origem}"] += 1
        entrando[f"{lig.esquema_destino}.{lig.tabela_destino}"] += 1

    tabelas: list[RetratoTabela] = []
    for bruta in brutas_ok:
        qualificado = f"{bruta.esquema}.{bruta.nome}"
        tabelas.append(
            RetratoTabela(
                esquema=bruta.esquema,
                nome=bruta.nome,
                eh_view=bruta.eh_view,
                papel=inferir_papel(
                    linhas_estimadas=bruta.linhas_estimadas,
                    colunas=[c for c in para_estrutura if c.qualificado == qualificado],
                    chaves_saindo=saindo.get(qualificado, 0),
                    chaves_entrando=entrando.get(qualificado, 0),
                    eh_view=bruta.eh_view,
                ),
                linhas_estimadas=bruta.linhas_estimadas,
                bytes_estimados=bruta.bytes_estimados,
                colunas=colunas_por_tabela[qualificado],
            )
        )

    retrato = RetratoBanco(
        coletado_em=inicio,
        tabelas=sorted(tabelas, key=lambda t: t.qualificado),
        ligacoes_declaradas=[
            LigacaoDeclarada(
                esquema_origem=r.esquema_origem,
                tabela_origem=r.tabela_origem,
                coluna_origem=r.coluna_origem,
                esquema_destino=r.esquema_destino,
                tabela_destino=r.tabela_destino,
                coluna_destino=r.coluna_destino,
            )
            for r in relacionamentos
        ],
        ligacoes_inferidas=ligacoes_inferidas,
        tabelas_nao_perfiladas=total_no_banco - len(tabelas),
    )

    logger.info(
        "perfilamento_concluido",
        tabelas=len(retrato.tabelas),
        colunas=retrato.total_colunas,
        ligacoes_declaradas=len(retrato.ligacoes_declaradas),
        ligacoes_inferidas=len(retrato.ligacoes_inferidas),
        nao_perfiladas=retrato.tabelas_nao_perfiladas,
        falhas=falhas,
        duracao_s=round((datetime.now(timezone.utc) - inicio).total_seconds(), 1),
    )
    return retrato
