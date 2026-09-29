"""Mixins comuns a todo modelo.

`TenantMixin` é o que torna o isolamento estrutural em vez de disciplinar: uma
tabela que herda dele não consegue existir sem `tenant_id`. O mixin declara só a
coluna, deliberadamente: `__table_args__` de mixin é sobrescrito em silêncio pelo
da subclasse, então índice composto é declarado por quem precisa dele.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.core.base import Base


class TimestampMixin:
    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    atualizado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # Exclusão lógica: o histórico de uma conversa ou de um dicionário é prova de
    # procedência. Apagar de verdade destrói a auditoria do número que foi à tela.
    deletado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class TenantMixin:
    @declared_attr
    def tenant_id(cls) -> Mapped[int]:
        return mapped_column(
            BigInteger,
            ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )


class IdMixin:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)


__all__ = ["Base", "TimestampMixin", "TenantMixin", "IdMixin"]
