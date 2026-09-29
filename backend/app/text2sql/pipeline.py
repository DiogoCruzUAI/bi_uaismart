"""Da pergunta em português ao resultado.

    pergunta
       │
       ├─ selecionar_tabelas   recorta o catálogo (não manda o banco inteiro)
       ├─ LLM                  devolve uma ConsultaSpec — nunca SQL
       ├─ construir            NOSSO código monta o SQL, com escala e lista branca
       ├─ guardrails           prova que é leitura, tabela conhecida, um comando só
       ├─ EXPLAIN              recusa o que varre demais, antes de executar
       └─ executar             cursor, transação somente-leitura, prazo

O modelo participa de um passo só, e o que ele produz é estrutura validada, não texto
executável. Errar a spec vira mensagem de erro; errar o SQL viraria um número.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import BaseModel, Field

from app.connectors.base import Conector, LimiteExcedido
from app.llm.cliente import Uso, pedir_estruturado
from app.text2sql.catalogo import Catalogo, TabelaCatalogo
from app.text2sql.construtor import ColunaSaida, SpecInvalida, construir
from app.text2sql.executor import executar_vigiado
from app.text2sql.guardrails import SqlRecusado
from app.text2sql.spec import ConsultaSpec

logger = structlog.get_logger()

# Quantas tabelas do catálogo vão para o prompt. Mandar o banco inteiro é a força
# bruta paga por token: num banco de quatro mil tabelas, a pergunta "quantas empresas
# abriram em MG" não precisa das outras 3.997.
MAX_TABELAS_NO_CONTEXTO = 8

# Palavras que aparecem em toda pergunta e não distinguem tabela nenhuma.
_VAZIAS = frozenset(
    """a o as os de da do das dos e em no na nos nas um uma uns umas por para com sem
    que qual quais quanto quantos quantas quando onde como mais menos maior menor
    todos todas cada entre sobre ate ao aos nao sim me da qual e foi sao ser ter tem
    últimos ultimos ultimo último últimas meses mes ano anos dia dias""".split()
)


def _normalizar(texto: str) -> list[str]:
    """Minúsculas, sem acento, só palavras com 3+ letras.

    Sem remover acento, "salário" na pergunta não casaria com "salario" no nome da
    coluna — e nome de coluna raramente tem acento, justamente o caso que interessa.
    """
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFKD", texto.lower()) if not unicodedata.combining(c)
    )
    return [p for p in re.findall(r"[a-z0-9_]+", sem_acento) if len(p) >= 3 and p not in _VAZIAS]


def _termos_da_tabela(tabela: TabelaCatalogo) -> set[str]:
    termos: set[str] = set()
    termos.update(_normalizar(tabela.nome))
    termos.update(_normalizar(tabela.esquema))
    if tabela.descricao:
        termos.update(_normalizar(tabela.descricao))
    for coluna in tabela.colunas.values():
        termos.update(_normalizar(coluna.nome))
        if coluna.descricao:
            termos.update(_normalizar(coluna.descricao))
    return termos


def selecionar_tabelas(
    pergunta: str, catalogo: Catalogo, *, maximo: int = MAX_TABELAS_NO_CONTEXTO
) -> list[str]:
    """Escolhe as tabelas plausíveis para a pergunta.

    Casamento de palavras, não embeddings: é determinístico, custa nada, não precisa
    de índice vetorial e funciona bem quando as descrições do dicionário existem —
    que é justamente o que o gerador de dicionário produz. Quando ficar insuficiente,
    a troca por busca semântica é local a esta função.

    Sem casamento nenhum devolve as primeiras tabelas em ordem alfabética, para o
    modelo ao menos poder dizer que a pergunta não é respondível com elas.
    """
    termos = set(_normalizar(pergunta))
    if not catalogo.tabelas:
        return []

    pontuadas: list[tuple[int, str]] = []
    for tabela in catalogo.tabelas.values():
        pontos = len(termos & _termos_da_tabela(tabela))
        pontuadas.append((pontos, tabela.qualificado))

    pontuadas.sort(key=lambda x: (-x[0], x[1]))
    com_casamento = [nome for pontos, nome in pontuadas if pontos > 0]
    if com_casamento:
        return com_casamento[:maximo]
    return [nome for _, nome in pontuadas[:maximo]]


# ─── Formato da resposta do modelo ────────────────────────────────────────────


class RespostaDoModelo(BaseModel):
    """O que o modelo devolve.

    `pode_responder` existe para o modelo ter como dizer "não dá". Sem essa saída,
    uma spec obrigatória o forçaria a inventar uma consulta para toda pergunta —
    inclusive as que o catálogo não responde, que é onde a invenção causa mais dano.
    """

    pode_responder: bool
    motivo: str = Field(
        default="", description="Por que não dá para responder. Só quando pode_responder=false."
    )
    explicacao: str = Field(
        default="", description="Como a pergunta foi interpretada, em uma frase."
    )
    consulta: ConsultaSpec | None = None


INSTRUCOES = """\
Você traduz perguntas de negócio em consultas estruturadas, para uma plataforma de BI
em que pessoas perguntam em português e recebem números.

Você **não escreve SQL**. Você descreve o que quer — medidas, dimensões, filtros — e a
plataforma monta o SQL a partir do catálogo. Isso é proposital: assim o nome de cada
coluna é conferido antes de virar consulta, e as unidades são convertidas por código.

Você recebe as tabelas plausíveis para a pergunta, com descrição, unidade e escala de
cada coluna. Devolva uma consulta que responda o que foi perguntado.

**Medidas** são o que se mede: contagem, soma, média, mediana, mínimo, máximo,
contagem_distinta. `contagem` sem coluna conta linhas.

**Dimensões** são por onde se agrupa. Coluna de data aceita granularidade (dia, mes,
trimestre, ano).

**Filtros** recortam. Operadores: igual, diferente, maior, maior_ou_igual, menor,
menor_ou_igual, entre, em, contem, e_nulo, nao_e_nulo.

REGRAS QUE NÃO SE NEGOCIAM:

1. **Valores na unidade natural.** Se a pergunta diz "salário acima de 5 mil", escreva
   5000 — mesmo que o catálogo informe que a coluna está em centavos. A conversão é
   feita pela plataforma, por código. Converter você mesmo produziria o erro duas
   vezes.

2. **Só tabelas e colunas do catálogo enviado.** Nome exato. Se o que a pergunta pede
   não existe ali, responda `pode_responder: false` e explique o que faltou.

3. **"Não dá" é resposta válida.** Pergunta ambígua, coluna inexistente, dado que a
   tabela não contém: `pode_responder: false` com um motivo claro. Inventar uma
   consulta aproximada produz um número que parece responder a pergunta e não
   responde — o pior desfecho possível.

4. **Leia a descrição do que a coluna NÃO significa.** Ela está no catálogo quando
   existe, e é o que evita responder "faturamento" com uma coluna de porte, ou "valor
   pago" com uma de valor contratado.

5. **Ordenação e limite** quando a pergunta pedir "maiores", "top", "principais".
   Ordene sempre por um rótulo que você declarou.

6. **Rótulos em português**, legíveis, do jeito que apareceriam num cabeçalho de
   tabela: "Salário médio", "Total de admissões", "Estado".

7. Explique em `explicacao`, numa frase, como você entendeu a pergunta. É o que a
   pessoa lê para conferir se você entendeu o que ela quis dizer.
"""


def montar_pedido(pergunta: str, catalogo: Catalogo, tabelas: list[str]) -> str:
    """Serializa a pergunta e o recorte do catálogo.

    `sort_keys` mantém o prefixo estável entre chamadas — JSON com ordem instável
    derruba o cache de prompt sem nada parecer errado.
    """
    import json

    recorte: list[dict] = []
    for nome in tabelas:
        tabela = catalogo.tabela(nome)
        if tabela is None:
            continue
        colunas = []
        for coluna in sorted(tabela.colunas.values(), key=lambda c: c.nome):
            d: dict[str, Any] = {"nome": coluna.nome, "tipo": coluna.tipo_sql}
            if coluna.descricao:
                d["descricao"] = coluna.descricao
            if coluna.descricao_negativa:
                d["nao_significa"] = coluna.descricao_negativa
            if coluna.unidade.value != "desconhecida":
                d["unidade"] = coluna.unidade.value
            if coluna.precisa_escala:
                d["escala"] = coluna.escala
            if coluna.moeda:
                d["moeda"] = coluna.moeda
            colunas.append(d)

        entrada: dict[str, Any] = {"tabela": tabela.qualificado, "colunas": colunas}
        if tabela.descricao:
            entrada["descricao"] = tabela.descricao
        if tabela.recorte_obrigatorio:
            entrada["filtro_obrigatorio_em"] = tabela.recorte_obrigatorio
        recorte.append(entrada)

    return (
        f"Pergunta: {pergunta}\n\nCatálogo disponível:\n```json\n"
        + json.dumps(recorte, ensure_ascii=False, sort_keys=True, indent=1)
        + "\n```"
    )


# ─── Resultado ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Resposta:
    pergunta: str
    respondeu: bool
    motivo: str = ""
    explicacao: str = ""
    spec: ConsultaSpec | None = None
    sql: str | None = None
    colunas: list[ColunaSaida] = field(default_factory=list)
    linhas: list[dict] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    linhas_estimadas: int | None = None
    custo_estimado: float | None = None
    duracao_ms: int | None = None
    truncado: bool = False
    uso: Uso = field(default_factory=Uso)
    tabelas_consideradas: list[str] = field(default_factory=list)


async def responder(
    pergunta: str, *, catalogo: Catalogo, conector: Conector
) -> Resposta:
    """Executa o pipeline inteiro para uma pergunta.

    Nunca levanta por causa da pergunta: recusa vira `respondeu=False` com motivo. A
    pessoa precisa saber **por que** não deu — "a tabela não tem essa informação" e
    "a consulta ficou grande demais" pedem reações diferentes dela.
    """
    tabelas = selecionar_tabelas(pergunta, catalogo)
    if not tabelas:
        return Resposta(
            pergunta=pergunta,
            respondeu=False,
            motivo=(
                "Esta conexão ainda não tem catálogo. Perfile o banco antes de "
                "fazer perguntas."
            ),
        )

    resposta_modelo, uso = await pedir_estruturado(
        instrucoes=INSTRUCOES,
        pedido=montar_pedido(pergunta, catalogo, tabelas),
        formato=RespostaDoModelo,
    )

    base = Resposta(
        pergunta=pergunta,
        respondeu=False,
        explicacao=resposta_modelo.explicacao,
        uso=uso,
        tabelas_consideradas=tabelas,
    )

    if not resposta_modelo.pode_responder or resposta_modelo.consulta is None:
        base.motivo = resposta_modelo.motivo or (
            "Não foi possível responder com as tabelas disponíveis."
        )
        return base

    base.spec = resposta_modelo.consulta

    try:
        construida = construir(resposta_modelo.consulta, catalogo)
    except SpecInvalida as e:
        base.motivo = str(e)
        logger.info("spec_recusada", pergunta=pergunta[:120])
        return base

    base.sql = construida.sql
    base.colunas = construida.colunas_saida
    base.avisos = list(construida.avisos)

    try:
        execucao = await executar_vigiado(
            construida.sql,
            conector=conector,
            tabelas_permitidas=catalogo.nomes_de_tabela,
            parametros=construida.parametros,
        )
    except (SqlRecusado, LimiteExcedido) as e:
        base.motivo = str(e)
        return base

    base.respondeu = True
    base.sql = execucao.sql_executado
    base.linhas = execucao.resultado.linhas
    base.truncado = execucao.resultado.truncado
    base.duracao_ms = execucao.resultado.duracao_ms
    base.linhas_estimadas = execucao.estimativa.linhas
    base.custo_estimado = execucao.estimativa.custo
    if execucao.resultado.truncado:
        base.avisos.append(
            "O resultado foi cortado no limite de linhas; há mais dados do que o "
            "mostrado."
        )
    return base
