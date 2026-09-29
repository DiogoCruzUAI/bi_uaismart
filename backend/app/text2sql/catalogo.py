"""Visão do catálogo para o construtor de SQL.

Estruturas simples, sem SQLAlchemy: o construtor é lógica pura e precisa ser testável
sem banco. O carregador que vira `Tabela`/`Coluna` nisto fica no fim do arquivo, e é a
única parte que toca a sessão.
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.semantico import Coluna, Tabela, UnidadeColuna


@dataclass(frozen=True, slots=True)
class ColunaCatalogo:
    nome: str
    tipo_sql: str
    unidade: UnidadeColuna = UnidadeColuna.desconhecida
    escala: float = 1.0
    moeda: str | None = None
    descricao: str | None = None
    descricao_negativa: str | None = None
    revisada: bool = False

    @property
    def precisa_escala(self) -> bool:
        return self.escala not in (0, 1.0)


@dataclass(slots=True)
class TabelaCatalogo:
    esquema: str
    nome: str
    colunas: dict[str, ColunaCatalogo] = field(default_factory=dict)
    # Colunas que toda consulta a esta tabela precisa filtrar. Sem isso, uma tabela
    # de centenas de milhões de linhas responde qualquer pergunta com varredura
    # completa — e o `EXPLAIN` recusaria depois, com uma mensagem bem menos útil que
    # "informe o estado ou o município".
    recorte_obrigatorio: list[str] = field(default_factory=list)
    descricao: str | None = None
    revisada: bool = False

    @property
    def qualificado(self) -> str:
        return f"{self.esquema}.{self.nome}"

    def coluna(self, nome: str) -> ColunaCatalogo | None:
        return self.colunas.get(nome.strip().lower())


@dataclass(slots=True)
class Catalogo:
    """O que a plataforma sabe sobre uma conexão."""

    tabelas: dict[str, TabelaCatalogo] = field(default_factory=dict)
    versao_dicionario: int = 1

    def tabela(self, qualificado: str) -> TabelaCatalogo | None:
        return self.tabelas.get(qualificado.strip().lower())

    @property
    def nomes_de_tabela(self) -> set[str]:
        return {t.qualificado.lower() for t in self.tabelas.values()}


async def carregar_catalogo(
    db: AsyncSession, *, tenant_id: int, conexao_id: int, versao_dicionario: int = 1
) -> Catalogo:
    """Lê o catálogo daquela conexão. Só o que não foi removido."""
    tabelas = (
        (
            await db.execute(
                select(Tabela).where(
                    Tabela.tenant_id == tenant_id,
                    Tabela.conexao_id == conexao_id,
                    Tabela.deletado_em.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not tabelas:
        return Catalogo(versao_dicionario=versao_dicionario)

    colunas = (
        (
            await db.execute(
                select(Coluna).where(
                    Coluna.tenant_id == tenant_id,
                    Coluna.tabela_id.in_([t.id for t in tabelas]),
                    Coluna.deletado_em.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    por_tabela: dict[int, list[Coluna]] = {}
    for c in colunas:
        por_tabela.setdefault(c.tabela_id, []).append(c)

    catalogo = Catalogo(versao_dicionario=versao_dicionario)
    for t in tabelas:
        tc = TabelaCatalogo(
            esquema=t.esquema,
            nome=t.nome,
            recorte_obrigatorio=list(t.recorte_obrigatorio or []),
            descricao=t.descricao,
            revisada=t.revisada_em is not None,
        )
        for c in por_tabela.get(t.id, []):
            tc.colunas[c.nome.lower()] = ColunaCatalogo(
                nome=c.nome,
                tipo_sql=c.tipo_sql,
                unidade=c.unidade,
                escala=c.escala or 1.0,
                moeda=c.moeda,
                descricao=c.descricao,
                descricao_negativa=c.descricao_negativa,
                revisada=c.revisada_em is not None,
            )
        catalogo.tabelas[tc.qualificado.lower()] = tc

    return catalogo
