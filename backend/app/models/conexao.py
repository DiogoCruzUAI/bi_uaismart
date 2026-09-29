"""Conexão a um banco de cliente.

Uma conexão é uma credencial de leitura para um banco que não é nosso. A senha é
cifrada (`app.core.crypto`) antes de chegar ao disco e nunca sai por API.

`versao_dicionario` é incrementada a cada mudança no catálogo daquela conexão. Ela
entra na chave de cache de toda pergunta: quando alguém corrige "salario está em
centavos", as respostas anteriores param de ser servidas na hora, sem invalidação
manual e sem o risco de alguém continuar vendo o número velho.
"""

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, IdMixin, TenantMixin, TimestampMixin


class TipoBanco(str, enum.Enum):
    postgres = "postgres"
    mysql = "mysql"


class StatusConexao(str, enum.Enum):
    # Cadastrada, ainda não testada.
    pendente = "pendente"
    # Testada, respondendo, mas o catálogo ainda não foi levantado.
    conectada = "conectada"
    # Catálogo levantado e dicionário gerado. Pronta para perguntas.
    catalogada = "catalogada"
    # Última tentativa de conexão falhou. `ultimo_erro` diz o motivo.
    erro = "erro"


class Conexao(Base, IdMixin, TenantMixin, TimestampMixin):
    __tablename__ = "conexoes"

    nome: Mapped[str] = mapped_column(String(120), nullable=False)
    tipo: Mapped[TipoBanco] = mapped_column(Enum(TipoBanco, native_enum=False), nullable=False)

    host: Mapped[str] = mapped_column(String(255), nullable=False)
    porta: Mapped[int] = mapped_column(Integer, nullable=False)
    banco: Mapped[str] = mapped_column(String(120), nullable=False)
    usuario: Mapped[str] = mapped_column(String(120), nullable=False)

    # Fernet. Nunca serializada em schema de resposta — ver app/schemas/conexao.py.
    senha_cifrada: Mapped[str] = mapped_column(Text, nullable=False)
    usar_ssl: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    status: Mapped[StatusConexao] = mapped_column(
        Enum(StatusConexao, native_enum=False), default=StatusConexao.pendente, nullable=False
    )
    ultimo_erro: Mapped[str | None] = mapped_column(Text, default=None)
    ultimo_teste_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    ultimo_perfilamento_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    versao_dicionario: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    tabelas: Mapped[list["Tabela"]] = relationship(  # noqa: F821
        back_populates="conexao", cascade="all, delete-orphan"
    )

    # Nome da conexão é único dentro do tenant: dois clientes podem ter "Produção".
    __table_args__ = (UniqueConstraint("tenant_id", "nome", name="uq_conexao_tenant_nome"),)
