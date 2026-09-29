"""Geração do dicionário de dados a partir do retrato.

O perfilador entrega evidência; aqui ela vira significado. É a peça que transforma
"`salario` é bigint, 4,2 milhões de distintos, magnitude 10" em "remuneração mensal
contratada, **em centavos**; NÃO é o salário líquido" — e essa frase é a diferença
entre um número certo e um número cem vezes maior com cara de certo.

Três garantias que o módulo sustenta:

1. **O modelo não inventa.** As instruções mandam responder `desconhecida` com
   confiança baixa quando a evidência não basta, e `validar_proposta` descarta tabela
   ou coluna que não foi enviada — alucinação não entra no catálogo.
2. **Nada é aplicado por cima de revisão humana.** `aplicar_proposta` só escreve onde
   `revisada_em` é nulo.
3. **A proposta é proposta.** Tudo entra com `confianca_ia` e sem `revisada_em`. A
   plataforma usa, e diz que ainda não foi revisado por ninguém.
"""

from dataclasses import dataclass, field
from typing import Literal

import structlog
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.cliente import Uso, pedir_estruturado
from app.models.conexao import Conexao
from app.models.semantico import Coluna, Tabela, UnidadeColuna
from app.semantic.retrato import RetratoBanco

logger = structlog.get_logger()

# Escalas plausíveis para `valor_natural = valor_armazenado / escala`. Fora desta
# lista quase sempre é alucinação: bancos guardam valor em centavos (100), em
# milhares (0.001) ou como está (1). Um `escala: 7` não descreve banco nenhum.
ESCALAS_PLAUSIVEIS = frozenset({0.000001, 0.001, 0.01, 1.0, 100.0, 1000.0, 1000000.0})


# ─── Formato da resposta ──────────────────────────────────────────────────────


class PropostaColuna(BaseModel):
    nome: str = Field(description="Nome exato da coluna, como veio no pedido.")
    descricao: str = Field(
        description="O que a coluna significa no negócio, em uma ou duas frases."
    )
    descricao_negativa: str = Field(
        default="",
        description=(
            "A leitura errada mais provável desta coluna. Vazio se não houver uma "
            "confusão plausível."
        ),
    )
    unidade: UnidadeColuna
    escala: float = Field(
        default=1.0,
        description="Divisor para chegar à unidade natural: centavos = 100.",
    )
    moeda: str = Field(default="", description="ISO 4217, só quando unidade=monetaria.")
    confianca: float = Field(ge=0.0, le=1.0)


class PropostaTabela(BaseModel):
    tabela: str = Field(description="esquema.nome, exatamente como veio no pedido.")
    descricao: str
    descricao_negativa: str = ""
    papel: Literal["fato", "dimensao", "ponte", "desconhecido"]
    confianca: float = Field(ge=0.0, le=1.0)
    colunas: list[PropostaColuna]


class PropostaDicionario(BaseModel):
    tabelas: list[PropostaTabela]

    @field_validator("tabelas")
    @classmethod
    def nao_vazia(cls, v):
        if not v:
            raise ValueError("A proposta não descreveu nenhuma tabela.")
        return v


# ─── Instruções: o prefixo estável, cacheado em toda chamada ──────────────────

INSTRUCOES = """\
Você documenta bancos de dados que nunca viu, para uma plataforma de BI em que
pessoas fazem perguntas em português e recebem números.

O que está em jogo: um número errado com cara de certo é pior que um erro. Se o
dicionário disser que uma coluna é "valor da venda" quando na verdade ela está em
centavos, a plataforma vai responder "R$ 320.000" onde a resposta era "R$ 3.200" — e
ninguém vai desconfiar. Você é a última defesa contra isso.

Você recebe o retrato de algumas tabelas: tipos, volumes, cardinalidade, valores
frequentes, extremos e sinais derivados das estatísticas do banco. Nenhuma linha de
dado real — só estatística.

Para cada tabela e cada coluna, produza:

**descricao** — o que significa no negócio, em uma ou duas frases. Não repita o nome
da coluna ("id é o identificador" não informa nada). Se o nome for opaco e a
evidência não ajudar, diga que o significado não foi determinado.

**descricao_negativa** — a leitura errada mais provável. É o campo mais valioso do
dicionário e o que nenhum schema tem. Exemplos do gênero: "porte vem do cadastro
fiscal e NÃO é faturamento nem número de funcionários"; "é o valor contratado, NÃO o
valor pago"; "é a data de abertura deste estabelecimento, NÃO a fundação da empresa".
Deixe vazio quando não houver confusão plausível — encher por encher gera ruído.

**unidade** — uma de: monetaria, percentual, contagem, duracao, data, texto,
identificador, codigo, desconhecida. Use `codigo` para número ou sigla que só
significa algo via tabela de domínio (CNAE, CBO, situação cadastral, status).

**escala** — o divisor para chegar à unidade natural:
`valor_natural = valor_armazenado / escala`. Use 1 quando o valor já está na unidade
natural, 100 quando está em centavos, 0.001 quando está em milhares.

Como decidir a escala: compare a **magnitude** com o que é plausível para aquele
conceito no mundo real. Um campo de remuneração mensal com magnitude 9 ou 10 não é
dinheiro em unidade natural — nenhum salário mensal chega a um bilhão. Um preço
unitário de produto com magnitude 8, idem. Raciocine a partir do significado, não do
nome: a mesma magnitude pode ser normal num campo de faturamento anual de uma
corporação e absurda num campo de salário.

**moeda** — código ISO 4217, só quando unidade for monetaria e houver evidência. Vazio
se não houver.

**confianca** — de 0 a 1, quanto você confia na sua própria leitura. Seja honesto:
0.9 quando o nome é claro e a evidência confirma; 0.3 quando você está deduzindo de um
nome abreviado sem estatística nenhuma.

REGRAS QUE NÃO SE NEGOCIAM:

1. **Não invente.** Sem evidência suficiente, use `unidade: desconhecida`, `escala: 1`,
   confiança baixa, e diga na descrição que o significado não foi determinado. "Não
   sei" é uma resposta correta e útil; um palpite apresentado com confiança não é.

2. **Só descreva o que foi enviado.** Não acrescente tabela nem coluna que não esteja
   no pedido, mesmo que pareça óbvio que deveria existir.

3. **Use o nome exato** de cada tabela e coluna, como veio no pedido.

4. **Escreva em português do Brasil**, direto, sem rodeio e sem repetir a pergunta.

5. Uma coluna marcada como `quase_sempre_nula` existe mas quase nunca é preenchida —
   diga isso na descrição, porque medida calculada sobre ela é ruído com aparência de
   estatística.
"""


# ─── Montagem do pedido ───────────────────────────────────────────────────────


def montar_pedido(retrato: RetratoBanco, tabelas: list[str]) -> str:
    """Serializa o recorte do retrato que vai no pedido.

    `sort_keys` e `ensure_ascii=False` não são estética: JSON com ordem de chaves
    instável muda o prefixo a cada chamada e derruba o cache de prompt sem que nada
    pareça errado — a conta só aparece na fatura.
    """
    import json

    recorte = retrato.para_prompt(tabelas=tabelas)
    return (
        "Documente as tabelas abaixo.\n\n```json\n"
        + json.dumps(recorte, ensure_ascii=False, sort_keys=True, indent=1, default=str)
        + "\n```"
    )


def agrupar_tabelas(
    retrato: RetratoBanco, *, colunas_por_lote: int = 120
) -> list[list[str]]:
    """Divide as tabelas em lotes para não estourar um pedido só.

    O corte é por número de colunas, não por número de tabelas: uma tabela larga de 300
    colunas pesa mais que trinta tabelas de dez. Cada lote vira uma chamada, e as
    instruções — que são o prefixo estável — ficam cacheadas entre elas.
    """
    lotes: list[list[str]] = []
    atual: list[str] = []
    peso = 0

    for t in sorted(retrato.tabelas, key=lambda t: t.qualificado):
        n = max(1, len(t.colunas))
        if atual and peso + n > colunas_por_lote:
            lotes.append(atual)
            atual, peso = [], 0
        atual.append(t.qualificado)
        peso += n

    if atual:
        lotes.append(atual)
    return lotes


# ─── Validação da resposta ────────────────────────────────────────────────────


@dataclass(slots=True)
class ResultadoValidacao:
    """O que sobrou da proposta depois da conferência.

    Guarda `list[PropostaTabela]`, não `PropostaDicionario`, e a distinção não é
    cosmética: `PropostaDicionario` é o contrato de **saída do modelo**, e exige ao
    menos uma tabela — uma resposta vazia significa que o modelo não fez nada útil.
    Já o resultado da validação pode legitimamente ficar vazio, quando tudo que veio
    era alucinação. Usar o mesmo tipo para os dois fazia o caso mais importante
    (descartar tudo) estourar em vez de ser reportado.
    """

    tabelas: list[PropostaTabela] = field(default_factory=list)
    problemas: list[str] = field(default_factory=list)
    tabelas_descartadas: int = 0
    colunas_descartadas: int = 0
    colunas_sem_proposta: int = 0


def validar_proposta(
    proposta: PropostaDicionario, retrato: RetratoBanco, tabelas_pedidas: list[str]
) -> ResultadoValidacao:
    """Confere a resposta contra o que foi realmente enviado. Pura.

    Saída estruturada garante o **formato**, não o **conteúdo**: o modelo pode devolver
    uma coluna que não existe, com o schema perfeitamente válido. Uma coluna inventada
    no catálogo é pior que uma coluna sem descrição, porque o Text-to-SQL passaria a
    gerar SQL referenciando algo que o banco não tem.
    """
    pedidas = {t.lower() for t in tabelas_pedidas}
    colunas_reais: dict[str, set[str]] = {}
    for t in retrato.tabelas:
        if t.qualificado.lower() in pedidas:
            colunas_reais[t.qualificado.lower()] = {c.nome.lower() for c in t.colunas}

    resultado = ResultadoValidacao()
    aceitas: list[PropostaTabela] = []
    vistas: set[str] = set()

    for pt in proposta.tabelas:
        chave = pt.tabela.lower()
        if chave not in colunas_reais:
            resultado.tabelas_descartadas += 1
            resultado.problemas.append(
                f"Tabela '{pt.tabela}' não estava no pedido e foi descartada."
            )
            continue
        vistas.add(chave)

        colunas_ok: list[PropostaColuna] = []
        nomes_vistos: set[str] = set()
        for pc in pt.colunas:
            if pc.nome.lower() not in colunas_reais[chave]:
                resultado.colunas_descartadas += 1
                resultado.problemas.append(
                    f"Coluna '{pt.tabela}.{pc.nome}' não existe e foi descartada."
                )
                continue
            nomes_vistos.add(pc.nome.lower())

            # Escala fora do plausível vira 1.0 e um aviso: preferimos não converter a
            # converter errado. Dividir por um número inventado estraga o número na tela.
            if pc.escala <= 0 or pc.escala not in ESCALAS_PLAUSIVEIS:
                resultado.problemas.append(
                    f"Escala {pc.escala} implausível em '{pt.tabela}.{pc.nome}'; "
                    "aplicada escala 1."
                )
                pc = pc.model_copy(update={"escala": 1.0})

            # Moeda só faz sentido em coluna monetária.
            if pc.moeda and pc.unidade is not UnidadeColuna.monetaria:
                pc = pc.model_copy(update={"moeda": ""})

            colunas_ok.append(pc)

        faltantes = colunas_reais[chave] - nomes_vistos
        resultado.colunas_sem_proposta += len(faltantes)

        aceitas.append(pt.model_copy(update={"colunas": colunas_ok}))

    nao_descritas = set(colunas_reais) - vistas
    if nao_descritas:
        resultado.problemas.append(
            f"{len(nao_descritas)} tabela(s) pedidas não foram descritas: "
            + ", ".join(sorted(nao_descritas)[:5])
        )

    resultado.tabelas = aceitas
    return resultado


# ─── Geração ──────────────────────────────────────────────────────────────────


async def gerar_dicionario(
    retrato: RetratoBanco, tabelas: list[str]
) -> tuple[ResultadoValidacao, Uso]:
    """Pede ao modelo a descrição das tabelas indicadas e valida a resposta."""
    proposta, uso = await pedir_estruturado(
        instrucoes=INSTRUCOES,
        pedido=montar_pedido(retrato, tabelas),
        formato=PropostaDicionario,
    )
    resultado = validar_proposta(proposta, retrato, tabelas)
    if resultado.problemas:
        logger.warning(
            "proposta_de_dicionario_com_problemas",
            tabelas_descartadas=resultado.tabelas_descartadas,
            colunas_descartadas=resultado.colunas_descartadas,
            colunas_sem_proposta=resultado.colunas_sem_proposta,
        )
    return resultado, uso


# ─── Aplicação ao catálogo ────────────────────────────────────────────────────


@dataclass(slots=True)
class ResumoDicionario:
    tabelas_descritas: int = 0
    colunas_descritas: int = 0
    revisoes_respeitadas: int = 0
    nao_encontradas: int = 0


async def aplicar_proposta(
    db: AsyncSession,
    *,
    tenant_id: int,
    conexao: Conexao,
    tabelas: list[PropostaTabela],
) -> ResumoDicionario:
    """Escreve a proposta no catálogo, sem tocar no que foi revisado.

    A descrição gerada entra com `confianca_ia` preenchida e `revisada_em` nulo — a
    plataforma usa e **diz na resposta** que aquilo ainda não passou por ninguém.
    """
    resumo = ResumoDicionario()

    no_catalogo = (
        (
            await db.execute(
                select(Tabela).where(
                    Tabela.tenant_id == tenant_id,
                    Tabela.conexao_id == conexao.id,
                    Tabela.deletado_em.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    por_nome = {f"{t.esquema}.{t.nome}".lower(): t for t in no_catalogo}

    for pt in tabelas:
        tabela = por_nome.get(pt.tabela.lower())
        if tabela is None:
            resumo.nao_encontradas += 1
            continue

        if tabela.revisada_em is None:
            tabela.descricao = pt.descricao
            tabela.descricao_negativa = pt.descricao_negativa or None
            tabela.confianca_ia = pt.confianca
            resumo.tabelas_descritas += 1
        else:
            resumo.revisoes_respeitadas += 1

        colunas_no_catalogo = (
            (
                await db.execute(
                    select(Coluna).where(
                        Coluna.tenant_id == tenant_id,
                        Coluna.tabela_id == tabela.id,
                        Coluna.deletado_em.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        por_coluna = {c.nome.lower(): c for c in colunas_no_catalogo}

        for pc in pt.colunas:
            coluna = por_coluna.get(pc.nome.lower())
            if coluna is None:
                resumo.nao_encontradas += 1
                continue
            if coluna.revisada_em is not None:
                resumo.revisoes_respeitadas += 1
                continue

            coluna.descricao = pc.descricao
            coluna.descricao_negativa = pc.descricao_negativa or None
            coluna.unidade = pc.unidade
            coluna.escala = pc.escala
            coluna.moeda = pc.moeda or None
            coluna.confianca_ia = pc.confianca
            resumo.colunas_descritas += 1

    await db.flush()
    logger.info(
        "dicionario_aplicado",
        conexao_id=conexao.id,
        tabelas=resumo.tabelas_descritas,
        colunas=resumo.colunas_descritas,
        revisoes_respeitadas=resumo.revisoes_respeitadas,
    )
    return resumo


@dataclass(slots=True)
class ResultadoCompleto:
    tabelas: list[PropostaTabela] = field(default_factory=list)
    problemas: list[str] = field(default_factory=list)
    lotes: int = 0
    lotes_com_falha: int = 0
    uso: Uso = field(default_factory=Uso)


async def gerar_dicionario_completo(
    retrato: RetratoBanco, *, colunas_por_lote: int = 120
) -> ResultadoCompleto:
    """Documenta o retrato inteiro, em lotes.

    Um lote que falha não derruba os outros. Num banco de mil tabelas, perder tudo
    porque a décima chamada expirou seria trocar um dicionário 99% pronto por nenhum —
    e a próxima execução só precisa repetir o que faltou.
    """
    completo = ResultadoCompleto()
    entrada = cache_escrita = cache_leitura = saida = 0
    modelo = ""

    for lote in agrupar_tabelas(retrato, colunas_por_lote=colunas_por_lote):
        completo.lotes += 1
        try:
            resultado, uso = await gerar_dicionario(retrato, lote)
        except Exception as e:
            completo.lotes_com_falha += 1
            completo.problemas.append(
                f"Falha ao documentar {len(lote)} tabela(s): {type(e).__name__}."
            )
            logger.warning("lote_de_dicionario_falhou", tabelas=len(lote), erro=type(e).__name__)
            continue

        completo.tabelas.extend(resultado.tabelas)
        completo.problemas.extend(resultado.problemas)
        entrada += uso.entrada
        cache_escrita += uso.cache_escrita
        cache_leitura += uso.cache_leitura
        saida += uso.saida
        modelo = uso.modelo or modelo

    completo.uso = Uso(
        entrada=entrada,
        cache_escrita=cache_escrita,
        cache_leitura=cache_leitura,
        saida=saida,
        modelo=modelo,
    )
    return completo
