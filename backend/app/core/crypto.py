"""Cifra das credenciais de banco dos clientes.

O cliente confia o acesso ao banco dele à plataforma. Isso é o ativo mais sensível
que guardamos — mais que qualquer dado analítico, porque dá acesso a todos eles.

Regras que este módulo existe para sustentar:

1. A senha do banco do cliente é cifrada antes de tocar o disco. Um dump do nosso
   Postgres não entrega banco de cliente nenhum.
2. A chave (`CREDENTIALS_KEY`) mora no .env do servidor, nunca no banco. Quem tem o
   dump precisa também ter o servidor.
3. Nada aqui tem `__repr__` legível: credencial não vaza em log, traceback ou
   resposta de erro por descuido de formatação.

Rotação de chave: cifrar com chave nova e decifrar com a antiga exige guardar as duas
durante a transição. `MultiFernet` resolve; fica para quando houver cliente em
produção, e não antes — chave rotacionada sem cliente é complexidade sem benefício.
"""

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_fernet = Fernet(settings.credentials_key.encode())


class ErroDeCredencial(Exception):
    """Falha ao decifrar. A mensagem é deliberadamente vaga: detalhe de erro
    criptográfico é informação útil para quem ataca."""


def cifrar(valor: str) -> str:
    return _fernet.encrypt(valor.encode()).decode()


def decifrar(valor_cifrado: str) -> str:
    try:
        return _fernet.decrypt(valor_cifrado.encode()).decode()
    except InvalidToken as e:
        raise ErroDeCredencial(
            "Não foi possível decifrar a credencial. A CREDENTIALS_KEY mudou ou o "
            "registro está corrompido."
        ) from e


class Segredo(str):
    """String que não se revela em log nem em traceback.

    `print(senha)` e f-strings continuam funcionando (é preciso, para montar a DSN),
    mas `repr()` — que é o que aparece em log estruturado, traceback do Python e
    `pytest` — devolve uma máscara. Fecha o vazamento acidental, que é o comum;
    não pretende impedir quem tem o processo na mão.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "Segredo(***)"
