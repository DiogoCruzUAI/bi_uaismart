"""Testes da inferência de estrutura: papel das tabelas e ligações não declaradas.

Duas propriedades importam mais que acertar muito:

1. `'desconhecido'` é resposta legítima. Chutar fato ou dimensão para não dizer
   "não sei" produz uma camada semântica confiante e errada.
2. Ligação inferida não inventa relação entre tudo e todos. `pedidos.id` e
   `clientes.id` se chamam igual e não têm nada a ver um com o outro.
"""

from app.semantic.estrutura import (
    ColunaParaEstrutura,
    inferir_ligacoes,
    inferir_papel,
)
from app.semantic.sinais import FamiliaTipo, classificar_coluna


def c(tabela, nome, familia=FamiliaTipo.inteiro, pk=False, esquema="public", sinais=None):
    return ColunaParaEstrutura(
        esquema=esquema,
        tabela=tabela,
        nome=nome,
        familia=familia,
        eh_chave_primaria=pk,
        sinais=sinais,
    )


# ─── Papel da tabela ──────────────────────────────────────────────────────────


def test_tabela_apontada_por_outras_e_dimensao():
    colunas = [c("clientes", "id", pk=True), c("clientes", "nome", FamiliaTipo.texto)]
    papel = inferir_papel(
        linhas_estimadas=5_000, colunas=colunas, chaves_saindo=0, chaves_entrando=3
    )
    assert papel == "dimensao"


def test_tabela_que_aponta_para_varias_e_tem_medida_e_fato():
    colunas = [
        c("pedidos", "id", pk=True),
        c("pedidos", "cliente_id"),
        c("pedidos", "produto_id"),
        c("pedidos", "valor", FamiliaTipo.decimal),
        c("pedidos", "data_emissao", FamiliaTipo.data),
    ]
    papel = inferir_papel(
        linhas_estimadas=4_000_000, colunas=colunas, chaves_saindo=2, chaves_entrando=0
    )
    assert papel == "fato"


def test_tabela_so_de_chaves_e_ponte():
    """N-para-N: agrupar por ela não responde pergunta de negócio."""
    colunas = [c("pedido_produto", "pedido_id"), c("pedido_produto", "produto_id")]
    papel = inferir_papel(
        linhas_estimadas=900_000, colunas=colunas, chaves_saindo=2, chaves_entrando=0
    )
    assert papel == "ponte"


def test_tabela_isolada_sem_medida_e_desconhecida():
    """Sem grafo e sem número para somar, dizer qualquer coisa seria chute."""
    colunas = [c("avulsa", "texto_livre", FamiliaTipo.texto)]
    papel = inferir_papel(
        linhas_estimadas=50, colunas=colunas, chaves_saindo=0, chaves_entrando=0
    )
    assert papel == "desconhecido"


def test_tabela_grande_isolada_com_medida_e_fato():
    """Banco sem FK declarada: o tamanho é o único indício que sobra."""
    colunas = [c("caged", "competencia"), c("caged", "salario")]
    papel = inferir_papel(
        linhas_estimadas=293_000_000, colunas=colunas, chaves_saindo=0, chaves_entrando=0
    )
    assert papel == "fato"


def test_view_fica_desconhecida():
    colunas = [c("vw_resumo", "total", FamiliaTipo.decimal)]
    papel = inferir_papel(
        linhas_estimadas=100, colunas=colunas, chaves_saindo=0, chaves_entrando=2, eh_view=True
    )
    assert papel == "desconhecido"


def test_tabela_sem_colunas_e_desconhecida():
    assert inferir_papel(
        linhas_estimadas=None, colunas=[], chaves_saindo=0, chaves_entrando=0
    ) == "desconhecido"


# ─── Ligações inferidas ───────────────────────────────────────────────────────


def test_liga_coluna_com_sufixo_id_a_chave_da_tabela():
    colunas = [
        c("clientes", "id", pk=True),
        c("pedidos", "id", pk=True),
        c("pedidos", "cliente_id"),
    ]
    ligacoes = inferir_ligacoes(colunas)
    assert len(ligacoes) == 1
    lig = ligacoes[0]
    assert (lig.tabela_origem, lig.coluna_origem) == ("pedidos", "cliente_id")
    assert (lig.tabela_destino, lig.coluna_destino) == ("clientes", "id")
    assert lig.confianca >= 0.8


def test_nao_liga_id_generico_com_id_generico():
    """A falsa ligação que inventaria relação entre todas as tabelas do banco."""
    colunas = [c("clientes", "id", pk=True), c("pedidos", "id", pk=True)]
    assert inferir_ligacoes(colunas) == []


def test_prefixo_id_tambem_e_reconhecido():
    colunas = [c("clientes", "id", pk=True), c("pedidos", "id_cliente")]
    ligacoes = inferir_ligacoes(colunas)
    assert len(ligacoes) == 1
    assert ligacoes[0].tabela_destino == "clientes"


def test_prefixo_de_modelagem_dimensional_e_removido():
    """`dim_cliente` é referenciada como `cliente_id`, não `dim_cliente_id`."""
    colunas = [c("dim_cliente", "sk_cliente", pk=True), c("f_vendas", "cliente_id")]
    ligacoes = inferir_ligacoes(colunas)
    assert len(ligacoes) == 1
    assert ligacoes[0].tabela_destino == "dim_cliente"


def test_tipo_incompativel_nao_liga():
    """Chave inteira não casa com coluna de texto, por mais que o nome combine."""
    colunas = [
        c("clientes", "id", pk=True, familia=FamiliaTipo.inteiro),
        c("pedidos", "cliente_id", familia=FamiliaTipo.texto),
    ]
    assert inferir_ligacoes(colunas) == []


def test_decimal_nunca_liga():
    """Chave em ponto flutuante não existe; aceitar produziria ligação entre valores."""
    colunas = [
        c("clientes", "id", pk=True, familia=FamiliaTipo.decimal),
        c("pedidos", "cliente_id", familia=FamiliaTipo.decimal),
    ]
    assert inferir_ligacoes(colunas) == []


def test_ligacao_ja_declarada_nao_e_reproposta():
    """Segundo caminho entre as mesmas tabelas só cria ambiguidade de JOIN."""
    colunas = [c("clientes", "id", pk=True), c("pedidos", "cliente_id")]
    ligacoes = inferir_ligacoes(
        colunas, declaradas={("public", "pedidos", "public", "clientes")}
    )
    assert ligacoes == []


def test_plural_em_oes_e_tratado():
    colunas = [c("situacoes", "id", pk=True), c("empresas", "situacao_id")]
    ligacoes = inferir_ligacoes(colunas)
    assert len(ligacoes) == 1
    assert ligacoes[0].tabela_destino == "situacoes"


def test_coluna_categorica_perde_confianca():
    """Poucos valores distintos apontando para tabela grande é mais código que chave."""
    categorica = classificar_coluna(
        tipo_sql="integer", cardinalidade=5, fracao_nula=0.0, linhas_tabela=1_000_000
    )
    colunas = [
        c("clientes", "id", pk=True),
        c("pedidos", "cliente_id", sinais=categorica),
    ]
    ligacoes = inferir_ligacoes(colunas)
    assert len(ligacoes) == 1
    assert ligacoes[0].confianca < 0.8


def test_resultado_vem_ordenado_por_confianca():
    colunas = [
        c("clientes", "id", pk=True),
        c("pedidos", "cliente_id"),           # casamento exato: 0.8
        c("pedidos", "ref_cliente_antigo"),   # contém o nome: 0.65
    ]
    ligacoes = inferir_ligacoes(colunas)
    assert len(ligacoes) == 2
    assert ligacoes[0].confianca >= ligacoes[1].confianca


def test_toda_ligacao_explica_o_motivo():
    """A tela de aprovação precisa dizer por que a ligação foi proposta."""
    colunas = [c("clientes", "id", pk=True), c("pedidos", "cliente_id")]
    for lig in inferir_ligacoes(colunas):
        assert lig.motivo
        assert "cliente" in lig.motivo


def test_confianca_inferida_nunca_chega_a_um():
    """1.0 é reservado para FK declarada. Palpite não empata com fato."""
    colunas = [c("clientes", "id", pk=True), c("pedidos", "cliente_id")]
    for lig in inferir_ligacoes(colunas):
        assert lig.confianca < 1.0
