"""Testes do perfilamento de ponta a ponta, com um banco falso.

O banco falso imita o que se encontra em cliente real: uma tabela de fato grande, uma
dimensão, uma chave estrangeira declarada e outra que ninguém declarou. Inclui também
a armadilha do salário em centavos — não para o perfilador resolvê-la (resolver é
julgamento, e julgamento é do LLM), mas para provar que a evidência que permite
resolvê-la chega ao retrato.
"""

import asyncio

import pytest

from app.connectors.base import (
    ColunaBruta,
    Conector,
    Estimativa,
    RelacionamentoBruto,
    ResultadoConsulta,
    TabelaBruta,
)
from app.semantic.profiler import perfilar


class BancoFalso(Conector):
    """Conector que serve um catálogo fixo e conta as chamadas feitas."""

    dialeto = "postgres"

    def __init__(self, tabelas, colunas, relacionamentos=(), falhar_em=()):
        self._tabelas = list(tabelas)
        self._colunas = dict(colunas)
        self._relacionamentos = list(relacionamentos)
        self._falhar_em = set(falhar_em)
        self.chamadas_de_coluna = 0
        self.concorrencia_maxima = 0
        self._em_voo = 0

    async def listar_tabelas(self):
        return list(self._tabelas)

    async def listar_colunas(self, esquema, tabela):
        self._em_voo += 1
        self.concorrencia_maxima = max(self.concorrencia_maxima, self._em_voo)
        try:
            # Cede o controle para as outras corrotinas realmente se sobreporem.
            await asyncio.sleep(0)
            self.chamadas_de_coluna += 1
            if f"{esquema}.{tabela}" in self._falhar_em:
                raise PermissionError("sem permissão de leitura nesta tabela")
            return list(self._colunas.get(f"{esquema}.{tabela}", []))
        finally:
            self._em_voo -= 1

    async def listar_relacionamentos(self):
        return list(self._relacionamentos)

    async def testar(self) -> list[str]:
        return []

    async def coletar_privilegios(self):
        raise NotImplementedError

    async def estimar(self, sql) -> Estimativa:
        raise NotImplementedError

    async def executar(self, sql, limite_linhas) -> ResultadoConsulta:
        raise NotImplementedError

    async def fechar(self) -> None: ...


def coluna(nome, tipo="integer", *, pk=False, card=None, nulo=0.0, mini=None, maxi=None):
    return ColunaBruta(
        esquema="public",
        tabela="",
        nome=nome,
        tipo_sql=tipo,
        aceita_nulo=not pk,
        eh_chave_primaria=pk,
        fracao_nula=nulo,
        cardinalidade_estimada=card,
        minimo=mini,
        maximo=maxi,
    )


@pytest.fixture
def banco():
    """Fato `movimentacoes`, dimensão `municipios`, e `empresas` sem FK declarada."""
    tabelas = [
        TabelaBruta("public", "movimentacoes", False, 293_000_000, 90_000_000_000),
        TabelaBruta("public", "municipios", False, 5_570, 800_000),
        TabelaBruta("public", "empresas", False, 73_000_000, 40_000_000_000),
    ]
    colunas = {
        "public.movimentacoes": [
            coluna("id", "bigint", pk=True, card=293_000_000),
            coluna("municipio_id", card=5_570),
            coluna("empresa_id", "bigint", card=12_000_000),
            coluna("competencia", card=79),
            # A armadilha: salário em centavos, máximo de R$ 100 milhões/mês.
            coluna("salario", "bigint", card=4_200_000, mini="0", maxi="10000000000"),
            coluna("data_movimentacao", "date"),
        ],
        "public.municipios": [
            coluna("id", pk=True, card=5_570),
            coluna("nome", "character varying(120)", card=5_570),
            coluna("uf", "character varying(2)", card=27),
        ],
        "public.empresas": [
            coluna("id", "bigint", pk=True, card=73_000_000),
            coluna("razao_social", "text", card=60_000_000),
            coluna("capital_social", "numeric(18,2)", card=900_000, maxi="50000000.00"),
        ],
    }
    # Só a ligação com municipios está declarada; empresa_id ficou sem FK.
    relacionamentos = [
        RelacionamentoBruto(
            "public", "movimentacoes", "municipio_id", "public", "municipios", "id"
        )
    ]
    return BancoFalso(tabelas, colunas, relacionamentos)


# ─── Retrato ──────────────────────────────────────────────────────────────────


async def test_perfila_todas_as_tabelas(banco):
    r = await perfilar(banco)
    assert len(r.tabelas) == 3
    assert r.total_colunas == 12
    assert r.tabelas_nao_perfiladas == 0


async def test_papeis_sao_inferidos(banco):
    r = await perfilar(banco)
    papeis = {t.nome: t.papel for t in r.tabelas}
    # Aponta para duas e tem número a somar.
    assert papeis["movimentacoes"] == "fato"
    # É apontada e não aponta.
    assert papeis["municipios"] == "dimensao"


async def test_evidencia_do_salario_em_centavos_chega_ao_retrato(banco):
    """O perfilador não conclui 'centavos' — ele entrega o número que permite concluir.

    Magnitude 10 num campo de salário mensal: ou é dez bilhões de reais, que não
    existe, ou são centavos. Quem decide é o LLM, com conhecimento de mundo.
    """
    r = await perfilar(banco)
    mov = r.tabela("public.movimentacoes")
    salario = next(c for c in mov.colunas if c.nome == "salario")

    assert salario.sinais.magnitude_maxima == 10
    assert not salario.sinais.parece_identificador
    assert not salario.sinais.parece_categorico

    # E sobrevive à compactação para o prompt — de nada adianta coletar e descartar.
    assert salario.para_prompt()["magnitude"] == 10


async def test_coluna_categorica_e_reconhecida(banco):
    r = await perfilar(banco)
    mun = r.tabela("public.municipios")
    uf = next(c for c in mun.colunas if c.nome == "uf")
    assert uf.sinais.parece_categorico


# ─── Ligações ─────────────────────────────────────────────────────────────────


async def test_ligacao_nao_declarada_e_inferida(banco):
    """`empresa_id` não tem FK e sem inferência não haveria JOIN possível."""
    r = await perfilar(banco)
    inferidas = {(lig.coluna_origem, lig.tabela_destino) for lig in r.ligacoes_inferidas}
    assert ("empresa_id", "empresas") in inferidas


async def test_ligacao_declarada_nao_e_reproposta(banco):
    r = await perfilar(banco)
    assert len(r.ligacoes_declaradas) == 1
    inferidas = {(lig.coluna_origem, lig.tabela_destino) for lig in r.ligacoes_inferidas}
    assert ("municipio_id", "municipios") not in inferidas


# ─── Robustez e limites ───────────────────────────────────────────────────────


async def test_tabela_sem_permissao_nao_derruba_o_perfilamento():
    """Retrato parcial serve; perfilamento abortado não serve para nada."""
    tabelas = [
        TabelaBruta("public", "boa", False, 100, 1000),
        TabelaBruta("public", "proibida", False, 100, 1000),
    ]
    colunas = {"public.boa": [coluna("id", pk=True, card=100)]}
    banco = BancoFalso(tabelas, colunas, falhar_em={"public.proibida"})

    r = await perfilar(banco)
    assert len(r.tabelas) == 1
    assert r.tabelas[0].nome == "boa"
    # A que falhou é contada como não perfilada, e não some em silêncio.
    assert r.tabelas_nao_perfiladas == 1


async def test_limite_mantem_as_maiores(banco):
    r = await perfilar(banco, limite_tabelas=2)
    nomes = {t.nome for t in r.tabelas}
    assert nomes == {"movimentacoes", "empresas"}  # municipios tem 5.570 linhas
    assert r.tabelas_nao_perfiladas == 1


async def test_concorrencia_e_limitada(banco):
    """Promessa sobre o banco DO CLIENTE: não abrimos conexões sem teto nele."""
    await perfilar(banco, concorrencia=2)
    assert banco.concorrencia_maxima <= 2


async def test_progresso_e_reportado(banco):
    eventos = []
    await perfilar(banco, ao_progredir=lambda feitas, total: eventos.append((feitas, total)))
    assert len(eventos) == 3
    assert eventos[-1] == (3, 3)


async def test_uma_chamada_de_coluna_por_tabela(banco):
    """Sem N+1 escondido: o perfilamento é previsível no banco do cliente."""
    await perfilar(banco)
    assert banco.chamadas_de_coluna == 3


# ─── Compactação para o prompt ────────────────────────────────────────────────


async def test_prompt_pode_ser_restrito_a_algumas_tabelas(banco):
    """A recuperação de contexto: não mandar o banco inteiro para responder uma pergunta."""
    r = await perfilar(banco)
    completo = r.para_prompt()
    parcial = r.para_prompt(tabelas=["public.municipios"])

    assert len(completo["tabelas"]) == 3
    assert len(parcial["tabelas"]) == 1
    assert parcial["tabelas"][0]["tabela"] == "public.municipios"


async def test_prompt_omite_campo_nulo(banco):
    """Em 15 mil colunas, `"minimo": null` repetido é custo puro."""
    r = await perfilar(banco)
    mun = r.tabela("public.municipios")
    nome = next(c for c in mun.colunas if c.nome == "nome")
    d = nome.para_prompt()
    assert "faixa" not in d
    assert None not in d.values()


async def test_prompt_traz_as_ligacoes_das_tabelas_escolhidas(banco):
    r = await perfilar(banco)
    parcial = r.para_prompt(tabelas=["public.movimentacoes"])
    origens = {lig["origem"] for lig in parcial["ligacoes"]}
    assert "declarada" in origens
    assert "inferida" in origens


async def test_banco_sem_nenhuma_fk_declarada_ainda_classifica():
    """A regressão que a ordem do pipeline causava.

    Muito banco de produção não tem uma única FOREIGN KEY — o ORM cuidava disso, ou a
    restrição saiu por desempenho. Inferindo papel antes das ligações, o grafo fica
    vazio e **tudo** vira "desconhecido", que é uma camada semântica inútil.
    """
    tabelas = [
        TabelaBruta("public", "vendas", False, 4_000_000, 9_000_000_000),
        TabelaBruta("public", "clientes", False, 20_000, 5_000_000),
        TabelaBruta("public", "produtos", False, 8_000, 2_000_000),
    ]
    colunas = {
        "public.vendas": [
            coluna("id", "bigint", pk=True, card=4_000_000),
            coluna("cliente_id", card=20_000),
            coluna("produto_id", card=8_000),
            coluna("valor", "numeric(12,2)", card=300_000, maxi="98000.00"),
            coluna("data_venda", "date"),
        ],
        "public.clientes": [
            coluna("id", pk=True, card=20_000),
            coluna("nome", "text", card=19_800),
        ],
        "public.produtos": [
            coluna("id", pk=True, card=8_000),
            coluna("descricao", "text", card=7_900),
        ],
    }
    banco = BancoFalso(tabelas, colunas, relacionamentos=[])  # nenhuma FK

    r = await perfilar(banco)
    papeis = {t.nome: t.papel for t in r.tabelas}

    assert r.ligacoes_declaradas == []
    assert len(r.ligacoes_inferidas) == 2
    assert papeis["vendas"] == "fato"
    assert papeis["clientes"] == "dimensao"
    assert papeis["produtos"] == "dimensao"
