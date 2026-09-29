"""Registro dos modelos.

O Alembic só enxerga uma tabela se a classe dela tiver sido importada antes do
`autogenerate`. Importar aqui é o que evita a migration silenciosamente incompleta —
o modo de falha em que a tabela nova simplesmente não aparece no diff.
"""

from app.models.base import Base, IdMixin, TenantMixin, TimestampMixin
from app.models.chat import (
    Consulta,
    Conversa,
    Mensagem,
    ModoConsulta,
    PapelMensagem,
    StatusConsulta,
)
from app.models.conexao import Conexao, StatusConexao, TipoBanco
from app.models.semantico import (
    Coluna,
    Medida,
    OrigemRelacionamento,
    PapelTabela,
    Relacionamento,
    Tabela,
    UnidadeColuna,
)
from app.models.tenant import PerfilUsuario, PlanoTenant, Tenant, Usuario

__all__ = [
    "Base",
    "IdMixin",
    "TenantMixin",
    "TimestampMixin",
    "Tenant",
    "Usuario",
    "PlanoTenant",
    "PerfilUsuario",
    "Conexao",
    "TipoBanco",
    "StatusConexao",
    "Tabela",
    "Coluna",
    "Relacionamento",
    "Medida",
    "PapelTabela",
    "UnidadeColuna",
    "OrigemRelacionamento",
    "Conversa",
    "Mensagem",
    "Consulta",
    "PapelMensagem",
    "ModoConsulta",
    "StatusConsulta",
]
