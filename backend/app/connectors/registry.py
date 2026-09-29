"""Fábrica de conectores a partir de uma conexão cadastrada.

É o único lugar onde a senha de um banco de cliente é decifrada. Concentrar isso num
ponto é o que permite auditar, numa olhada, tudo que toca a credencial em claro — e
garantir que ela vai direto para o driver, sem passar por log, resposta de API ou
mensagem de erro no caminho.
"""

from app.connectors.base import Conector
from app.connectors.postgres import ConectorPostgres
from app.core.crypto import Segredo, decifrar
from app.models.conexao import Conexao, TipoBanco


class TipoNaoSuportado(Exception):
    """Tipo de banco cadastrado que ainda não tem conector."""


def criar_conector(conexao: Conexao) -> Conector:
    """Instancia o conector de uma conexão cadastrada.

    A senha é embrulhada em `Segredo`, cuja representação é mascarada: se este objeto
    cair num log estruturado ou num traceback, sai `Segredo(***)` e não a credencial
    do banco do cliente.
    """
    senha = Segredo(decifrar(conexao.senha_cifrada))

    if conexao.tipo is TipoBanco.postgres:
        return ConectorPostgres(
            host=conexao.host,
            porta=conexao.porta,
            banco=conexao.banco,
            usuario=conexao.usuario,
            senha=senha,
            usar_ssl=conexao.usar_ssl,
        )

    if conexao.tipo is TipoBanco.mysql:
        raise TipoNaoSuportado(
            "Conector MySQL ainda não implementado. O cadastro aceita o tipo para o "
            "catálogo já refletir o que o cliente tem, mas o perfilamento só roda "
            "em Postgres nesta versão."
        )

    raise TipoNaoSuportado(f"Tipo de banco não suportado: {conexao.tipo}")
