"""Repositório com isolamento de tenant obrigatório.

A diferença para o `BaseRepository` do SessionFlow é deliberada: lá o `tenant_id` é
passado em cada método, aqui ele é **exigido no construtor**. A consequência prática é
que não existe caminho de código que consulte sem filtro de tenant — não porque
alguém lembrou de passar, mas porque o objeto não pode ser instanciado sem o tenant.

Vazamento entre clientes quase nunca vem de um ataque. Vem de um `get_by_id` escrito
às pressas numa sexta-feira, sem o `WHERE tenant_id`. Este desenho remove essa
possibilidade em vez de vigiá-la em revisão de código.
"""

import datetime
from typing import Any, Generic, Sequence, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Base

ModelType = TypeVar("ModelType", bound=Base)


class TenantRepository(Generic[ModelType]):
    """CRUD assíncrono restrito a um tenant."""

    def __init__(self, model: type[ModelType], db: AsyncSession, tenant_id: int) -> None:
        if not hasattr(model, "tenant_id"):
            # Erro de programação, não de entrada: falha alto e cedo.
            raise TypeError(
                f"{model.__name__} não tem tenant_id. Use este repositório só com modelos "
                "que herdam TenantMixin — se o modelo é mesmo global, o acesso a ele "
                "precisa ser explícito e revisado, não genérico."
            )
        if tenant_id is None:
            raise ValueError("tenant_id é obrigatório.")
        self.model = model
        self.db = db
        self.tenant_id = tenant_id

    # ─── Consulta base ────────────────────────────────────────────────────────

    def _query(self) -> Select:
        """Toda leitura sai daqui. É o único lugar que aplica o filtro de tenant."""
        q = select(self.model).where(self.model.tenant_id == self.tenant_id)
        if hasattr(self.model, "deletado_em"):
            q = q.where(self.model.deletado_em.is_(None))
        return q

    # ─── Leitura ──────────────────────────────────────────────────────────────

    async def get(self, id: int) -> ModelType | None:
        """Devolve None para registro de outro tenant — igual a inexistente.

        Responder 404 em vez de 403 é proposital: um 403 confirmaria que aquele id
        existe em algum lugar, o que já é informação sobre outro cliente.
        """
        result = await self.db.execute(self._query().where(self.model.id == id))
        return result.scalar_one_or_none()

    async def listar(
        self,
        *,
        pular: int = 0,
        limite: int = 50,
        filtros: list[Any] | None = None,
        ordenar_por: Any | None = None,
    ) -> tuple[Sequence[ModelType], int]:
        q = self._query()
        for f in filtros or []:
            q = q.where(f)

        total = (
            await self.db.execute(select(func.count()).select_from(q.subquery()))
        ).scalar_one()

        if ordenar_por is not None:
            q = q.order_by(ordenar_por)
        itens = (await self.db.execute(q.offset(pular).limit(limite))).scalars().all()
        return itens, total

    async def existe(self, **kwargs: Any) -> bool:
        q = self._query()
        for chave, valor in kwargs.items():
            q = q.where(getattr(self.model, chave) == valor)
        total = (
            await self.db.execute(select(func.count()).select_from(q.subquery()))
        ).scalar_one()
        return total > 0

    # ─── Escrita ──────────────────────────────────────────────────────────────

    async def criar(self, dados: dict[str, Any]) -> ModelType:
        """O tenant do repositório vence qualquer tenant_id vindo nos dados.

        Se a requisição tentou injetar outro tenant_id, ele é descartado em silêncio
        em vez de aceito — o cliente não escolhe em qual tenant escreve.
        """
        obj = self.model(**{**dados, "tenant_id": self.tenant_id})
        self.db.add(obj)
        await self.db.flush()
        await self.db.refresh(obj)
        return obj

    async def atualizar(self, id: int, dados: dict[str, Any]) -> ModelType | None:
        obj = await self.get(id)
        if obj is None:
            return None
        for chave, valor in dados.items():
            # tenant_id e id nunca são editáveis por dados de entrada.
            if chave in {"tenant_id", "id"}:
                continue
            setattr(obj, chave, valor)
        await self.db.flush()
        await self.db.refresh(obj)
        return obj

    async def remover(self, id: int) -> bool:
        """Exclusão lógica quando o modelo suporta; física quando não suporta."""
        obj = await self.get(id)
        if obj is None:
            return False
        if hasattr(obj, "deletado_em"):
            obj.deletado_em = datetime.datetime.now(datetime.timezone.utc)
        else:
            await self.db.delete(obj)
        await self.db.flush()
        return True
