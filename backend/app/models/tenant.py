"""Tenant e usuário.

Um tenant é uma empresa cliente. Tudo que a plataforma guarda pertence a exatamente
um tenant — não há dado global além do catálogo de tenants em si.
"""

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, IdMixin, TimestampMixin


class PlanoTenant(str, enum.Enum):
    trial = "trial"
    basico = "basico"
    profissional = "profissional"
    empresarial = "empresarial"


class PerfilUsuario(str, enum.Enum):
    # Administra o tenant: cadastra conexões, aprova o dicionário, gere usuários.
    admin = "admin"
    # Cria e edita perguntas, dashboards e medidas.
    analista = "analista"
    # Só consome o que já existe. Não dispara SQL novo.
    leitor = "leitor"


class Tenant(Base, IdMixin, TimestampMixin):
    __tablename__ = "tenants"

    nome: Mapped[str] = mapped_column(String(200), nullable=False)
    # Subdomínio/identificador curto. Único entre tenants ativos.
    slug: Mapped[str] = mapped_column(String(60), nullable=False, unique=True, index=True)
    plano: Mapped[PlanoTenant] = mapped_column(
        Enum(PlanoTenant, native_enum=False), default=PlanoTenant.trial, nullable=False
    )
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    usuarios: Mapped[list["Usuario"]] = relationship(back_populates="tenant")


class Usuario(Base, IdMixin, TimestampMixin):
    """Usuário de um tenant.

    Não herda `TenantMixin` porque precisa de uma restrição própria: o e-mail é único
    **dentro** do tenant, não globalmente. A mesma pessoa pode ser usuária de duas
    empresas clientes com o mesmo e-mail.
    """

    __tablename__ = "usuarios"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_usuario_tenant_email"),)

    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    nome: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    senha_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    perfil: Mapped[PerfilUsuario] = mapped_column(
        Enum(PerfilUsuario, native_enum=False), default=PerfilUsuario.analista, nullable=False
    )
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ultimo_acesso_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    tenant: Mapped[Tenant] = relationship(back_populates="usuarios")
