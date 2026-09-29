"""Conversa, mensagem e — o principal — o registro de procedência de cada consulta.

`Consulta` é o que sustenta a segunda garantia do produto: **confiabilidade do número
na tela**. Todo valor exibido aponta para uma linha aqui, e essa linha responde:

- qual pergunta gerou o número;
- qual spec o LLM produziu e qual SQL o **nosso código** montou a partir dela;
- quantas linhas sustentam o valor, e se houve supressão;
- qual versão do dicionário estava em vigor;
- quanto o planejador estimou antes de executar, e quanto custou de verdade.

Sem esta tabela, "o BI disse" é um argumento de autoridade. Com ela, é uma afirmação
verificável — e quando alguém contesta um número, a discussão tem onde começar.
"""

import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, IdMixin, TenantMixin, TimestampMixin


class PapelMensagem(str, enum.Enum):
    usuario = "usuario"
    assistente = "assistente"


class ModoConsulta(str, enum.Enum):
    # Caminho padrão: LLM gera spec, nosso código monta o SQL a partir do registro.
    semantico = "semantico"
    # Caminho exploratório: LLM gera SQL. Rotulado como tal na tela, sempre.
    exploratorio = "exploratorio"


class StatusConsulta(str, enum.Enum):
    gerada = "gerada"
    # Recusada pelos guardrails antes de tocar o banco. `motivo_recusa` explica.
    recusada = "recusada"
    executada = "executada"
    erro = "erro"
    # Interrompida pelo `statement_timeout` do banco do cliente.
    expirada = "expirada"


class Conversa(Base, IdMixin, TenantMixin, TimestampMixin):
    __tablename__ = "conversas"

    usuario_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conexao_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conexoes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    titulo: Mapped[str] = mapped_column(String(200), nullable=False, default="Nova conversa")

    mensagens: Mapped[list["Mensagem"]] = relationship(
        back_populates="conversa", cascade="all, delete-orphan"
    )


class Mensagem(Base, IdMixin, TenantMixin, TimestampMixin):
    __tablename__ = "mensagens"

    conversa_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversas.id", ondelete="CASCADE"), nullable=False, index=True
    )
    papel: Mapped[PapelMensagem] = mapped_column(
        Enum(PapelMensagem, native_enum=False), nullable=False
    )
    conteudo: Mapped[str] = mapped_column(Text, nullable=False)

    # Especificação do gráfico devolvida ao frontend (tipo, eixos, séries). Guardada
    # para o dashboard poder ser reconstruído sem reexecutar a pergunta.
    grafico: Mapped[dict | None] = mapped_column(JSONB, default=None)

    conversa: Mapped[Conversa] = relationship(back_populates="mensagens")


class Consulta(Base, IdMixin, TenantMixin, TimestampMixin):
    """Procedência de um número. Uma linha por tentativa, inclusive as recusadas.

    As recusas ficam de propósito: elas são o conjunto de treino do produto. A lista
    de perguntas que os guardrails barraram mostra onde o dicionário está incompleto,
    e é a partir dela que a plataforma melhora sem precisar de suposição.
    """

    __tablename__ = "consultas"

    mensagem_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("mensagens.id", ondelete="SET NULL"), default=None, index=True
    )
    conexao_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conexoes.id", ondelete="CASCADE"), nullable=False, index=True
    )

    pergunta: Mapped[str] = mapped_column(Text, nullable=False)
    modo: Mapped[ModoConsulta] = mapped_column(Enum(ModoConsulta, native_enum=False), nullable=False)
    status: Mapped[StatusConsulta] = mapped_column(
        Enum(StatusConsulta, native_enum=False), nullable=False
    )
    motivo_recusa: Mapped[str | None] = mapped_column(Text, default=None)

    # A spec estruturada que o LLM devolveu (dimensões, medidas, filtros).
    spec: Mapped[dict | None] = mapped_column(JSONB, default=None)
    # O SQL que o nosso código montou. Mostrado ao usuário sob demanda — um BI que
    # esconde o SQL está pedindo confiança cega.
    sql_gerado: Mapped[str | None] = mapped_column(Text, default=None)

    # ─── O que o planejador previu (antes de executar) ────────────────────────
    linhas_estimadas: Mapped[int | None] = mapped_column(BigInteger, default=None)
    custo_estimado: Mapped[float | None] = mapped_column(Float, default=None)

    # ─── O que aconteceu de fato ──────────────────────────────────────────────
    linhas_retornadas: Mapped[int | None] = mapped_column(BigInteger, default=None)
    duracao_ms: Mapped[int | None] = mapped_column(Integer, default=None)

    # Alguma célula foi suprimida por base insuficiente? Se sim, a tela precisa dizer.
    houve_supressao: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Dicionário em vigor quando a resposta foi produzida. Se o dicionário mudou
    # desde então, o número guardado pode não ser mais reproduzível — e a plataforma
    # consegue detectar isso comparando com `Conexao.versao_dicionario`.
    versao_dicionario: Mapped[int] = mapped_column(Integer, nullable=False)

    # ─── Custo de LLM ─────────────────────────────────────────────────────────
    modelo_llm: Mapped[str | None] = mapped_column(String(60), default=None)
    tokens_entrada: Mapped[int | None] = mapped_column(Integer, default=None)
    tokens_cache_leitura: Mapped[int | None] = mapped_column(Integer, default=None)
    tokens_saida: Mapped[int | None] = mapped_column(Integer, default=None)
    # Respondida pelo cache, sem chamar o LLM nem o banco.
    veio_do_cache: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
