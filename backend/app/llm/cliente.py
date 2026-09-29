"""Cliente da Claude API.

Fino de propósito: concentra modelo, esforço, cache e contabilidade de tokens num
lugar só, para nenhum módulo de domínio precisar saber como se fala com o LLM.

Três decisões que valem explicação:

**O cliente nasce preguiçoso.** Construir no import faria a aplicação inteira morrer
sem `ANTHROPIC_API_KEY` — inclusive o `/health`, o perfilamento e o cadastro de
conexões, que não dependem de LLM nenhum. Falhar só na primeira pergunta é o
comportamento certo.

**Saída estruturada, não texto livre.** `messages.parse` devolve um Pydantic já
validado. É o que permite tratar a resposta do modelo como dado, não como texto a ser
interpretado com expressão regular — e é também por que não usamos `tool_choice`
forçado, que o Opus 5.5 recusa com 400.

**O prefixo estável vai cacheado.** Leitura de cache custa uma fração da entrada, e as
instruções do dicionário são idênticas em toda chamada. Sem isso, reperfilar um banco
de mil tabelas pagaria o mesmo prompt mil vezes.
"""

from dataclasses import dataclass
from typing import TypeVar

import structlog
from pydantic import BaseModel

from app.core.config import settings

logger = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)

_cliente = None


class LlmIndisponivel(Exception):
    """Sem chave configurada, ou a API recusou a requisição."""


def obter_cliente():
    """Instancia o cliente assíncrono na primeira chamada."""
    global _cliente
    if _cliente is None:
        if not settings.anthropic_api_key:
            raise LlmIndisponivel(
                "ANTHROPIC_API_KEY não configurada. O perfilamento e o cadastro de "
                "conexões funcionam sem ela; gerar dicionário e responder perguntas, não."
            )
        from anthropic import AsyncAnthropic

        _cliente = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _cliente


@dataclass(slots=True, frozen=True)
class Uso:
    """Tokens de uma chamada. Vai para a procedência, não só para o log.

    `cache_leitura` é o número a vigiar: se ficar em zero em chamadas seguidas com o
    mesmo prefixo, alguma coisa volátil entrou antes do ponto de cache e a conta
    multiplicou sem ninguém perceber.
    """

    entrada: int = 0
    cache_escrita: int = 0
    cache_leitura: int = 0
    saida: int = 0
    modelo: str = ""

    @property
    def total(self) -> int:
        return self.entrada + self.cache_escrita + self.cache_leitura + self.saida


def _uso_de(resposta, modelo: str) -> Uso:
    u = getattr(resposta, "usage", None)
    if u is None:
        return Uso(modelo=modelo)
    return Uso(
        entrada=getattr(u, "input_tokens", 0) or 0,
        cache_escrita=getattr(u, "cache_creation_input_tokens", 0) or 0,
        cache_leitura=getattr(u, "cache_read_input_tokens", 0) or 0,
        saida=getattr(u, "output_tokens", 0) or 0,
        modelo=modelo,
    )


async def pedir_estruturado(
    *,
    instrucoes: str,
    pedido: str,
    formato: type[T],
    max_tokens: int = 16000,
    esforco: str | None = None,
) -> tuple[T, Uso]:
    """Pede uma resposta que obedece a `formato` e devolve o objeto validado.

    `instrucoes` é o prefixo estável e vai marcado para cache; `pedido` é o que muda
    a cada chamada e fica depois do ponto de cache. Inverter os dois anula o cache
    inteiro — a correspondência é por prefixo, então qualquer byte diferente no começo
    invalida tudo que vem depois.
    """
    cliente = obter_cliente()
    modelo = settings.llm_model

    try:
        resposta = await cliente.messages.parse(
            model=modelo,
            max_tokens=max_tokens,
            # O esforço do Opus 5.5 tem padrão `medium`. Explicitar evita que uma
            # troca de modelo mude a qualidade da saída em silêncio.
            output_config={"effort": esforco or settings.llm_effort},
            system=[
                {
                    "type": "text",
                    "text": instrucoes,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": pedido}],
            output_format=formato,
        )
    except Exception as e:
        # Sem `str(e)` cru na mensagem ao usuário: a exceção do SDK pode carregar
        # trecho do prompt, e o prompt carrega estrutura do banco do cliente.
        logger.error("falha_na_chamada_ao_llm", erro=type(e).__name__, modelo=modelo)
        raise LlmIndisponivel(
            "Não foi possível obter resposta do modelo. Tente novamente em instantes."
        ) from e

    uso = _uso_de(resposta, modelo)
    logger.info(
        "chamada_ao_llm",
        modelo=modelo,
        entrada=uso.entrada,
        cache_leitura=uso.cache_leitura,
        cache_escrita=uso.cache_escrita,
        saida=uso.saida,
    )

    parsed = getattr(resposta, "parsed_output", None)
    if parsed is None:
        raise LlmIndisponivel("O modelo não devolveu uma resposta no formato esperado.")
    return parsed, uso
