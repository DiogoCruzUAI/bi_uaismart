"""Inferência da estrutura do banco: papel das tabelas e ligações não declaradas.

Funções puras, como `sinais`. Nenhuma lê dado.

Duas coisas que o catálogo não entrega prontas:

1. **Qual tabela é fato e qual é dimensão.** Ninguém escreve isso no banco, mas é o
   que separa "o que se mede" de "por que se agrupa". Sai do grafo de chaves
   estrangeiras mais a forma das colunas.

2. **Quais ligações existem sem estar declaradas.** Muito banco de produção não tem
   uma única `FOREIGN KEY` — o ORM cuidava disso, ou a restrição foi removida por
   desempenho. Sem inferir, a plataforma não consegue propor nenhum JOIN e vira uma
   consultora de tabela única.

A inferência é assumidamente palpite, e o palpite vem com `confianca`. Uma FK
declarada vale 1.0 e é fato; uma ligação por nome vale menos e a plataforma diz isso
ao usuário quando ela sustentou a resposta.
"""

from dataclasses import dataclass

from app.semantic.sinais import FamiliaTipo, SinaisColuna


@dataclass(frozen=True, slots=True)
class ColunaParaEstrutura:
    """O mínimo sobre uma coluna para inferir estrutura."""

    esquema: str
    tabela: str
    nome: str
    familia: FamiliaTipo
    eh_chave_primaria: bool
    sinais: SinaisColuna | None = None

    @property
    def qualificado(self) -> str:
        return f"{self.esquema}.{self.tabela}"


@dataclass(frozen=True, slots=True)
class LigacaoInferida:
    esquema_origem: str
    tabela_origem: str
    coluna_origem: str
    esquema_destino: str
    tabela_destino: str
    coluna_destino: str
    confianca: float
    motivo: str


# ─── Papel da tabela ──────────────────────────────────────────────────────────

# Fato costuma ser uma ordem de grandeza maior que dimensão. Não é regra — é o
# desempate quando o grafo de chaves não decide sozinho.
_LINHAS_INDICIO_DE_FATO = 100_000

# Nomes genéricos demais para ligar por conta própria. `id` aparece em toda tabela;
# casar `pedidos.id` com `clientes.id` porque ambos se chamam "id" inventaria uma
# ligação entre tudo e todos.
_NOMES_GENERICOS = frozenset({"id", "codigo", "cod", "key", "chave", "pk", "uuid"})

_SUFIXOS_DE_CHAVE = ("_id", "_codigo", "_cod", "_key", "_sk", "_fk", "_uuid")
_PREFIXOS_DE_CHAVE = ("id_", "cod_", "codigo_", "fk_", "sk_")


def parece_chave_por_nome(nome: str) -> bool:
    """O nome indica referência a outra coisa, não um valor a somar.

    Heurística de nome porque é a única disponível quando a tabela nunca passou por
    ANALYZE e a chave estrangeira não foi declarada — que é justamente o banco de
    cliente típico.
    """
    n = (nome or "").strip().lower()
    if not n:
        return False
    if n in _NOMES_GENERICOS:
        return True
    return n.endswith(_SUFIXOS_DE_CHAVE) or n.startswith(_PREFIXOS_DE_CHAVE)


def inferir_papel(
    *,
    linhas_estimadas: int | None,
    colunas: list[ColunaParaEstrutura],
    chaves_saindo: int,
    chaves_entrando: int,
    eh_view: bool = False,
) -> str:
    """Devolve `'fato'`, `'dimensao'`, `'ponte'` ou `'desconhecido'`.

    O grafo manda mais que o tamanho:

    - **Ponte**: quase só chaves estrangeiras e nada para medir. É tabela de
      relacionamento N-para-N, e agrupar por ela não responde pergunta de negócio.
    - **Dimensão**: é apontada por outras. É o "por quê" das perguntas.
    - **Fato**: aponta para várias e tem número para somar. É o "quanto".

    `'desconhecido'` é resposta legítima e frequente — num banco sem FK declarada,
    quase tudo cai aqui até alguém revisar. Chutar fato ou dimensão para não dizer
    "não sei" produziria uma camada semântica confiante e errada.
    """
    if eh_view:
        # View é recorte de outra coisa; herdar papel exigiria analisar a definição.
        return "desconhecido"

    total = len(colunas)
    if total == 0:
        return "desconhecido"

    identificadores = sum(
        1
        for c in colunas
        if c.eh_chave_primaria
        or parece_chave_por_nome(c.nome)
        or (c.sinais and c.sinais.parece_identificador)
    )
    # Candidata a medida: número que não é identificador nem código categórico.
    #
    # O teste por nome não é redundante com os sinais. Numa tabela de ligação, as
    # colunas costumam não ter estatística (nunca analisada) e não ser chave primária
    # declarada — e sem o nome, `produto_id` seria contada como algo a somar. Ninguém
    # soma um produto_id, e contar assim transformava toda tabela-ponte em fato.
    medidas = sum(
        1
        for c in colunas
        if c.familia in (FamiliaTipo.inteiro, FamiliaTipo.decimal)
        and not c.eh_chave_primaria
        and not parece_chave_por_nome(c.nome)
        and not (c.sinais and (c.sinais.parece_identificador or c.sinais.parece_categorico))
    )
    tem_data = any(c.familia is FamiliaTipo.data for c in colunas)

    # Ponte: praticamente só chaves, nada para medir, e aponta para duas ou mais.
    if chaves_saindo >= 2 and medidas == 0 and identificadores >= total - 1:
        return "ponte"

    # Apontada por outras e não aponta para quase ninguém: dimensão.
    if chaves_entrando > 0 and chaves_saindo <= 1:
        return "dimensao"

    # Aponta para várias e tem o que medir: fato.
    if chaves_saindo >= 2 and (medidas > 0 or tem_data):
        return "fato"

    # Sem grafo útil, o tamanho é o que sobra — e só decide em caso claro.
    if chaves_entrando == 0 and chaves_saindo == 0:
        if linhas_estimadas and linhas_estimadas >= _LINHAS_INDICIO_DE_FATO and medidas > 0:
            return "fato"
        return "desconhecido"

    return "desconhecido"


# ─── Ligações não declaradas ──────────────────────────────────────────────────

# Sufixos e prefixos que, em português e inglês, marcam referência a outra tabela.
_PADROES_REFERENCIA: tuple[str, ...] = (
    "{n}_id",
    "id_{n}",
    "{n}_codigo",
    "codigo_{n}",
    "cod_{n}",
    "{n}_cod",
    "{n}_key",
    "fk_{n}",
)


def _singularizar(nome: str) -> set[str]:
    """Variantes plausíveis de um nome de tabela em português.

    Deliberadamente simples e generosa: gerar uma variante a mais custa uma
    comparação de string; deixar de gerar a certa perde a ligação. As regras de
    plural do português têm exceção demais para valer um tratamento completo aqui.
    """
    n = nome.lower().strip()
    variantes = {n}
    # Prefixos comuns de modelagem dimensional: dim_cliente, fato_vendas, tb_produto.
    for prefixo in ("dim_", "d_", "fato_", "fat_", "f_", "tb_", "tbl_"):
        if n.startswith(prefixo):
            variantes.add(n[len(prefixo) :])
    for base in list(variantes):
        if base.endswith("oes"):  # situacoes → situacao
            variantes.add(base[:-3] + "ao")
        if base.endswith("aes"):  # alemaes → alemao
            variantes.add(base[:-3] + "ao")
        if base.endswith("ais"):  # locais → local
            variantes.add(base[:-3] + "al")
        if base.endswith("is"):  # perfis → perfil
            variantes.add(base[:-2] + "il")
        if base.endswith("ns"):  # homens → homem
            variantes.add(base[:-2] + "m")
        if base.endswith("es") and len(base) > 4:  # clientes → client, mes → (não)
            variantes.add(base[:-2])
        if base.endswith("s") and len(base) > 3:  # clientes → cliente
            variantes.add(base[:-1])
    return {v for v in variantes if len(v) >= 3}


def _nomes_esperados(tabela: str) -> set[str]:
    """Como uma coluna que referencia `tabela` provavelmente se chama."""
    esperados: set[str] = set()
    for variante in _singularizar(tabela):
        esperados.add(variante)
        for padrao in _PADROES_REFERENCIA:
            esperados.add(padrao.format(n=variante))
    return esperados


def _tipos_compativeis(a: FamiliaTipo, b: FamiliaTipo) -> bool:
    """Inteiro liga com inteiro, texto com texto. Decimal não liga com nada.

    Chave em ponto flutuante não existe na prática, e aceitar decimal só produziria
    ligação falsa entre colunas de valor.
    """
    if a is not b:
        return False
    return a in (FamiliaTipo.inteiro, FamiliaTipo.texto)


def inferir_ligacoes(
    colunas: list[ColunaParaEstrutura],
    *,
    declaradas: set[tuple[str, str, str, str]] | None = None,
    confianca_minima: float = 0.6,
) -> list[LigacaoInferida]:
    """Propõe ligações que o banco não declarou.

    `declaradas` traz as FKs reais como `(esquema_origem, tabela_origem, esquema_destino,
    tabela_destino)`: quando duas tabelas já estão ligadas, não propomos um segundo
    caminho entre elas — isso só criaria ambiguidade de JOIN sem ganhar nada.

    Devolve ordenado por confiança decrescente. Nada aqui entra em uso sem revisão
    humana: são candidatas para a tela de aprovação, não verdades.
    """
    declaradas = declaradas or set()

    # Onde está a chave primária de cada tabela.
    chaves: dict[str, ColunaParaEstrutura] = {}
    for c in colunas:
        if c.eh_chave_primaria and c.qualificado not in chaves:
            chaves[c.qualificado] = c

    ligacoes: list[LigacaoInferida] = []
    for coluna in colunas:
        nome = coluna.nome.lower()
        if coluna.eh_chave_primaria or nome in _NOMES_GENERICOS:
            continue

        for destino_qualificado, chave in chaves.items():
            if destino_qualificado == coluna.qualificado:
                continue
            if not _tipos_compativeis(coluna.familia, chave.familia):
                continue

            par = (coluna.esquema, coluna.tabela, chave.esquema, chave.tabela)
            if par in declaradas:
                continue

            esperados = _nomes_esperados(chave.tabela)
            if nome in esperados:
                confianca, motivo = 0.8, (
                    f"o nome '{coluna.nome}' segue o padrão de referência a "
                    f"'{chave.tabela}' e o tipo confere"
                )
            elif any(e in nome for e in esperados if len(e) >= 4):
                confianca, motivo = 0.65, (
                    f"o nome '{coluna.nome}' contém o nome da tabela "
                    f"'{chave.tabela}' e o tipo confere"
                )
            else:
                continue

            # Identificador do lado de cá reforça: é chave apontando para chave.
            if coluna.sinais and coluna.sinais.parece_categorico:
                # Poucos valores distintos apontando para uma tabela grande é mais
                # provável ser um código de domínio do que uma chave estrangeira.
                confianca -= 0.1

            if confianca < confianca_minima:
                continue

            ligacoes.append(
                LigacaoInferida(
                    esquema_origem=coluna.esquema,
                    tabela_origem=coluna.tabela,
                    coluna_origem=coluna.nome,
                    esquema_destino=chave.esquema,
                    tabela_destino=chave.tabela,
                    coluna_destino=chave.nome,
                    confianca=round(confianca, 2),
                    motivo=motivo,
                )
            )

    ligacoes.sort(key=lambda lig: (-lig.confianca, lig.tabela_origem, lig.coluna_origem))
    return ligacoes
