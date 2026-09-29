"""Mixins comuns a todo modelo.

`TenantMixin` é o que torna o isolamento estrutural em vez de disciplinar: uma
tabela que herda dele não consegue existir sem `tenant_id`. O mixin declara só a
coluna, deliberadamente: `__table_args__` de mixin é sobrescrito em silêncio pelo
da subclasse, então índice composto é declarado por quem precisa dele.
"""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.core.base import Base

# ─── Tipos portáveis ──────────────────────────────────────────────────────────
# O banco de produção é Postgres e nada muda para ele: `BigInt` compila para BIGINT
# e `Json` para JSONB, exatamente como antes.
#
# A variante existe para o SQLite, e só em teste. Sem ela, testar persistência,
# repositório ou isolamento de tenant exigiria um Postgres de pé — e teste que
# depende de infraestrutura é teste que alguém desativa numa sexta-feira. O SQLite
# não tem BIGINT autoincremento nem JSONB; com as variantes, o mesmo modelo roda
# nos dois.
BigInt = BigInteger().with_variant(Integer, "sqlite")
Json = JSON().with_variant(JSONB, "postgresql")


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
            BigInt,
            ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )


class IdMixin:
    id: Mapped[int] = mapped_column(BigInt, primary_key=True, autoincrement=True)


__all__ = ["Base", "TimestampMixin", "TenantMixin", "IdMixin", "BigInt", "Json"]
