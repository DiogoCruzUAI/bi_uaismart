"""Execução vigiada: valida, estima, decide, executa.

É o único caminho pelo qual SQL chega ao banco de um cliente. Nenhum outro módulo
deve chamar `conector.executar` diretamente — se precisar, a política deixou de ser
central e passou a ser opcional.

A sequência, e a razão de cada passo:

    validar   → prova que é leitura e que só toca tabela conhecida  (custo zero)
    limitar   → impõe teto de linhas quando não há agregação        (custo zero)
    estimar   → o planejador prevê linhas e custo, sem ler página   (custo ~zero)
    decidir   → recusa com explicação, ou libera                    (custo zero)
    executar  → em transação somente-leitura, com prazo             (custo real)

Os quatro primeiros passos são de graça. Só o quinto custa. Rodar os quatro antes é,
literalmente, a diferença entre inteligência e força bruta.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from app.connectors.base import Conector, Estimativa, LimiteExcedido, ResultadoConsulta
from app.core.config import settings
from app.text2sql.guardrails import SqlRecusado, aplicar_limite, validar

logger = structlog.get_logger()


@dataclass(slots=True)
class ExecucaoVigiada:
    resultado: ResultadoConsulta
    sql_executado: str
    estimativa: Estimativa


def _formatar(n: float) -> str:
    """Número grande em português, para a mensagem de recusa ser legível."""
    return f"{n:,.0f}".replace(",", ".")


async def executar_vigiado(
    sql: str,
    *,
    conector: Conector,
    tabelas_permitidas: set[str] | None,
    parametros: Sequence[Any] = (),
    max_linhas_estimadas: int | None = None,
    max_custo_estimado: float | None = None,
) -> ExecucaoVigiada:
    """Roda a consulta se, e só se, ela passar por todas as portas.

    Levanta `SqlRecusado` (falhou a validação estática) ou `LimiteExcedido` (passou
    da validação mas é grande demais). A distinção importa para o produto: a primeira
    é "essa pergunta não pode ser respondida assim", a segunda é "essa pergunta
    precisa de um recorte" — e só a segunda o usuário consegue resolver sozinho.
    """
    max_linhas = max_linhas_estimadas or settings.max_estimated_rows
    max_custo = max_custo_estimado or settings.max_estimated_cost

    validado = validar(sql, dialeto=conector.dialeto, tabelas_permitidas=tabelas_permitidas)

    sql_final = validado.sql
    if not validado.tem_agregacao and not validado.tem_limite:
        sql_final = aplicar_limite(sql_final, settings.max_result_rows, dialeto=conector.dialeto)

    estimativa = await conector.estimar(sql_final, parametros)

    # Varredura: o maior volume que a consulta toca em qualquer ponto do plano.
    if estimativa.linhas_varridas > max_linhas:
        raise LimiteExcedido(
            f"A consulta leria cerca de {_formatar(estimativa.linhas_varridas)} linhas, acima "
            f"do limite de {_formatar(max_linhas)}. Restrinja o período, a região ou "
            "adicione um filtro — uma pergunta mais específica responde mais rápido "
            "e com o mesmo valor."
        )

    # Agregação não leva LIMIT — limitá-la mudaria a resposta —, então é aqui que
    # o número de grupos é contido. A memória já está garantida pelo cursor do
    # conector; esta porta existe para não fazer o banco do cliente calcular o que
    # ninguém vai ver.
    if estimativa.linhas > settings.max_estimated_output_rows:
        raise LimiteExcedido(
            f"A consulta devolveria cerca de {_formatar(estimativa.linhas)} linhas de "
            f"resultado, acima do limite de {_formatar(settings.max_estimated_output_rows)}. "
            "Agrupe por uma dimensão mais ampla (mês em vez de dia, estado em vez de "
            "município) ou peça os maiores em vez de todos."
        )

    if estimativa.custo > max_custo:
        raise LimiteExcedido(
            f"O banco estimou um custo de {_formatar(estimativa.custo)} para esta consulta, "
            f"acima do limite de {_formatar(max_custo)}. Normalmente isso significa um "
            "JOIN sem índice ou um filtro que não aproveita índice nenhum."
        )

    logger.info(
        "consulta_liberada",
        linhas_estimadas=estimativa.linhas,
        linhas_varridas=estimativa.linhas_varridas,
        custo_estimado=estimativa.custo,
        tabelas=sorted(validado.tabelas_referenciadas),
    )

    resultado = await conector.executar(sql_final, settings.max_result_rows, parametros)
    return ExecucaoVigiada(resultado=resultado, sql_executado=sql_final, estimativa=estimativa)


__all__ = ["executar_vigiado", "ExecucaoVigiada", "SqlRecusado", "LimiteExcedido"]
