"""Catálogo semântico: o que a plataforma sabe sobre o banco do cliente.

Este módulo é o produto. Um Text-to-SQL que só lê `information_schema` sabe que
existe uma coluna `salario` do tipo inteiro — e não sabe que ela está em centavos,
que mistura unidades de pagamento, nem que uma média sobre ela sem recorte expõe
pessoas. Ele então devolve um número plausível e errado, com confiança.

O catálogo existe para responder três perguntas que o schema não responde:

1. **O que esta coluna significa?** (`descricao`)
2. **O que ela NÃO significa?** (`descricao_negativa`) — o campo mais valioso, e o
   que nenhum schema tem. Porte não é faturamento; capital social não é patrimônio.
3. **Em que unidade e escala ela está?** (`unidade`, `escala`) — a diferença entre
   R$ 3.200 e R$ 320.000.

A IA preenche tudo isso a partir do perfilamento; um humano do tenant aprova. Enquanto
`revisada_em` for nulo, a plataforma usa a descrição **e diz que ela não foi revisada**.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BigInt, Base, IdMixin, Json, TenantMixin, TimestampMixin


class PapelTabela(str, enum.Enum):
    fato = "fato"
    dimensao = "dimensao"
    ponte = "ponte"
    desconhecido = "desconhecido"


class UnidadeColuna(str, enum.Enum):
    """Unidade do valor armazenado. `escala` complementa: centavos = escala 100."""

    monetaria = "monetaria"
    percentual = "percentual"
    contagem = "contagem"
    duracao = "duracao"
    data = "data"
    texto = "texto"
    identificador = "identificador"
    # Código que só significa algo via tabela de domínio (CNAE, CBO, situação).
    codigo = "codigo"
    desconhecida = "desconhecida"


class OrigemRelacionamento(str, enum.Enum):
    # Chave estrangeira declarada no banco. Confiança máxima.
    fk_declarada = "fk_declarada"
    # Inferida por nome + tipo + sobreposição de valores amostrados.
    inferida = "inferida"
    # Apontada por um humano do tenant.
    manual = "manual"


class Tabela(Base, IdMixin, TenantMixin, TimestampMixin):
    __tablename__ = "cat_tabelas"
    __table_args__ = (
        UniqueConstraint("conexao_id", "esquema", "nome", name="uq_tabela_conexao_esquema_nome"),
    )

    conexao_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("conexoes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    esquema: Mapped[str] = mapped_column(String(120), nullable=False)
    nome: Mapped[str] = mapped_column(String(200), nullable=False)
    eh_view: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Estimativa do catálogo do banco (`reltuples`), não `COUNT(*)`. Contar 300
    # milhões de linhas para saber que são muitas é exatamente a força bruta que
    # este projeto recusa. A estimativa erra por alguns por cento e custa zero.
    linhas_estimadas: Mapped[int | None] = mapped_column(
        BigInt, default=None)
    bytes_estimados: Mapped[int | None] = mapped_column(
        BigInt, default=None)

    papel: Mapped[PapelTabela] = mapped_column(
        Enum(PapelTabela, native_enum=False), default=PapelTabela.desconhecido, nullable=False
    )
    descricao: Mapped[str | None] = mapped_column(Text, default=None)
    descricao_negativa: Mapped[str | None] = mapped_column(Text, default=None)

    # Sem recorte obrigatório, uma tabela grande vira varredura completa. Lista de
    # colunas que toda consulta a esta tabela precisa filtrar (ex.: ["uf"]).
    recorte_obrigatorio: Mapped[list | None] = mapped_column(Json, default=None)

    confianca_ia: Mapped[float | None] = mapped_column(Float, default=None)
    revisada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revisada_por_id: Mapped[int | None] = mapped_column(
        BigInt, ForeignKey("usuarios.id", ondelete="SET NULL"), default=None
    )

    conexao: Mapped["Conexao"] = relationship(back_populates="tabelas")  # noqa: F821
    colunas: Mapped[list["Coluna"]] = relationship(
        back_populates="tabela",
        cascade="all, delete-orphan",
        foreign_keys="Coluna.tabela_id",
    )


class Coluna(Base, IdMixin, TenantMixin, TimestampMixin):
    __tablename__ = "cat_colunas"
    __table_args__ = (UniqueConstraint("tabela_id", "nome", name="uq_coluna_tabela_nome"),)

    tabela_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("cat_tabelas.id", ondelete="CASCADE"), nullable=False, index=True
    )
    nome: Mapped[str] = mapped_column(String(200), nullable=False)
    tipo_sql: Mapped[str] = mapped_column(String(80), nullable=False)
    aceita_nulo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    eh_chave_primaria: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ─── O que o schema não conta ─────────────────────────────────────────────
    descricao: Mapped[str | None] = mapped_column(Text, default=None)
    # O campo que evita a resposta plausível e errada. Ex.: "porte vem da Receita
    # e NÃO é faturamento"; "capital social é o declarado, NÃO é patrimônio".
    descricao_negativa: Mapped[str | None] = mapped_column(Text, default=None)

    unidade: Mapped[UnidadeColuna] = mapped_column(
        Enum(UnidadeColuna, native_enum=False), default=UnidadeColuna.desconhecida, nullable=False
    )
    # Divisor para chegar à unidade natural. Centavos → 100. Milhares → 0.001.
    escala: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    moeda: Mapped[str | None] = mapped_column(String(3), default=None)

    # Quando a coluna é código, onde está a tabela que dá nome a ele.
    dominio_tabela_id: Mapped[int | None] = mapped_column(
        BigInt, ForeignKey("cat_tabelas.id", ondelete="SET NULL"), default=None
    )

    # ─── Perfilamento (amostrado, nunca varredura) ────────────────────────────
    cardinalidade_estimada: Mapped[int | None] = mapped_column(
        BigInt, default=None)
    fracao_nula: Mapped[float | None] = mapped_column(Float, default=None)
    # Valores mais comuns e extremos, de `pg_stats`. É o que permite à IA perceber
    # que um salário "máximo" de 10.000.000.000 está em centavos ou é sentinela.
    amostra: Mapped[dict | None] = mapped_column(Json, default=None)

    confianca_ia: Mapped[float | None] = mapped_column(Float, default=None)
    revisada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revisada_por_id: Mapped[int | None] = mapped_column(
        BigInt, ForeignKey("usuarios.id", ondelete="SET NULL"), default=None
    )

    tabela: Mapped[Tabela] = relationship(back_populates="colunas", foreign_keys=[tabela_id])


class Relacionamento(Base, IdMixin, TenantMixin, TimestampMixin):
    """Ligação entre duas colunas. Um JOIN só é proposto a partir daqui.

    `confianca` importa: uma FK declarada é fato, uma ligação inferida por nome é
    palpite. A plataforma diz ao usuário qual das duas sustentou a resposta.
    """

    __tablename__ = "cat_relacionamentos"

    conexao_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("conexoes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    coluna_origem_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("cat_colunas.id", ondelete="CASCADE"), nullable=False
    )
    coluna_destino_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("cat_colunas.id", ondelete="CASCADE"), nullable=False
    )
    origem: Mapped[OrigemRelacionamento] = mapped_column(
        Enum(OrigemRelacionamento, native_enum=False), nullable=False
    )
    confianca: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    revisada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Medida(Base, IdMixin, TenantMixin, TimestampMixin):
    """Indicador de negócio: a regra mora aqui, uma vez só.

    É o equivalente genérico do registro de medidas do Leads e dos cubos YAML do
    AlumiPremium. A diferença é que aqui ele é **descoberto e versionado por tenant**,
    não escrito à mão por nós.

    `expressao_sql` é um fragmento validado contra lista branca de colunas no momento
    do cadastro, nunca interpolado a partir de entrada do usuário.
    """

    __tablename__ = "cat_medidas"
    __table_args__ = (UniqueConstraint("conexao_id", "chave", name="uq_medida_conexao_chave"),)

    conexao_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("conexoes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tabela_id: Mapped[int] = mapped_column(
        BigInt, ForeignKey("cat_tabelas.id", ondelete="CASCADE"), nullable=False
    )
    chave: Mapped[str] = mapped_column(String(80), nullable=False)
    rotulo: Mapped[str] = mapped_column(String(200), nullable=False)
    descricao: Mapped[str | None] = mapped_column(Text, default=None)

    expressao_sql: Mapped[str] = mapped_column(Text, nullable=False)

    # Filtros que a medida sempre aplica, mesmo que o usuário não peça — é onde
    # moram regras como "transferência não é contratação" e "só salário mensal".
    filtros_implicitos: Mapped[list | None] = mapped_column(Json, default=None)

    # Piso de linhas para o valor poder ser exibido. Abaixo dele a célula sai
    # suprimida, com a contagem e sem o valor. Zero desliga a supressão; só deve
    # ser zero quando a medida não revela nada sobre indivíduos.
    base_minima: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    confianca_ia: Mapped[float | None] = mapped_column(Float, default=None)
    revisada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revisada_por_id: Mapped[int | None] = mapped_column(
        BigInt, ForeignKey("usuarios.id", ondelete="SET NULL"), default=None
    )
