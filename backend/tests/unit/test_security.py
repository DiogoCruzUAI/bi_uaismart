"""Hash de senha: o que a troca de `passlib` por `bcrypt` puro não pode quebrar.

Três coisas justificam este arquivo:

1. **Hash já gravado continua valendo.** A migração não pode invalidar a senha de
   ninguém, então há um vetor fixo gerado pelo passlib 1.7.4 (o que estava aqui
   antes) que precisa continuar conferindo.
2. **Hash ausente recusa sem atalho.** O caminho "usuário não existe" tem de gastar
   o mesmo bcrypt do caminho normal, senão o tempo de resposta enumera usuários.
3. **Hash corrompido recusa, não derruba.** `bcrypt.checkpw` entra em pânico no
   Rust com hash curto demais, e `pyo3_runtime.PanicException` herda de
   `BaseException` — escapa de `except Exception` e do middleware do FastAPI.
"""

from unittest.mock import patch

import bcrypt
import pytest

from app.core import security
from app.core.security import hash_senha, verificar_senha

# Gerado pelo passlib 1.7.4 + bcrypt 4.2.1 em 29/09/2026, antes da troca, para a
# senha abaixo. Não é segredo: é um vetor de compatibilidade de formato, o mesmo
# papel de um hash de exemplo em RFC.
_SENHA_LEGADA = "senha-legada-de-teste"
_HASH_LEGADO_PASSLIB = "$2b$12$EnXP9P1GsP6omz/YkMM3lOEOnYswaykdgB2GiCPQ2F9N5fmboL9.S"


def test_hash_e_verificacao_ida_e_volta():
    h = hash_senha("uma-senha-qualquer")
    assert verificar_senha("uma-senha-qualquer", h) is True


def test_senha_errada_nao_confere():
    h = hash_senha("uma-senha-qualquer")
    assert verificar_senha("outra-senha", h) is False


def test_hash_gerado_pelo_passlib_continua_valido():
    """O ponto da migração: ninguém perde a senha por causa dela."""
    assert verificar_senha(_SENHA_LEGADA, _HASH_LEGADO_PASSLIB) is True
    assert verificar_senha("senha-errada", _HASH_LEGADO_PASSLIB) is False


def test_hash_ausente_recusa():
    assert verificar_senha("qualquer-senha", None) is False


def test_hash_vazio_recusa():
    assert verificar_senha("qualquer-senha", "") is False


def test_hash_ausente_ainda_executa_bcrypt():
    """Defesa contra timing attack: sem hash real, o custo tem de ser pago igual.

    Espiona `checkpw` em vez de cronometrar — asserção sobre relógio é frouxa em
    CI compartilhada, e o que interessa é que o bcrypt rodou.
    """
    with patch.object(security.bcrypt, "checkpw", wraps=bcrypt.checkpw) as espiao:
        assert verificar_senha("qualquer-senha", None) is False
    assert espiao.call_count == 1, "bcrypt não rodou no caminho sem hash"


@pytest.mark.parametrize(
    "hash_ruim",
    [
        "$2b$12$curto",  # fatia curta: era o pânico no Rust
        "$2b$12$",
        "nao-e-um-hash",
        "x" * 60,
        "$2b$99$" + "a" * 53,  # custo fora da faixa
        "$2b$12$" + "a" * 53,  # forma válida, salt inválido -> ValueError interno
        "$2b$12$EnXP9P1GsP6omz/YkMM3lOEOnYswaykdgB2GiCPQ2F9N5fmboL9",  # truncado
    ],
)
def test_hash_malformado_recusa_sem_derrubar_o_processo(hash_ruim):
    assert verificar_senha("qualquer-senha", hash_ruim) is False


def test_hash_malformado_tambem_executa_bcrypt():
    """Hash corrompido não pode ser mais rápido que hash certo."""
    with patch.object(security.bcrypt, "checkpw", wraps=bcrypt.checkpw) as espiao:
        assert verificar_senha("qualquer-senha", "$2b$12$curto") is False
    assert espiao.call_count >= 1


def test_formato_e_custo_do_hash():
    h = hash_senha("uma-senha-qualquer")
    assert h.startswith("$2b$12$"), "custo tem de seguir o que o passlib aplicava"
    assert len(h) == 60


def test_cada_hash_tem_salt_proprio():
    assert hash_senha("mesma-senha") != hash_senha("mesma-senha")


def test_senha_unicode_e_longa():
    for senha in ["senhâ-çÃo-🔒", "a" * 100, ""]:
        h = hash_senha(senha)
        assert verificar_senha(senha, h) is True


def test_hash_novo_confere_com_bcrypt_direto():
    """Sem lock-in: o hash é bcrypt padrão, legível por qualquer implementação."""
    h = hash_senha("uma-senha-qualquer")
    assert bcrypt.checkpw(b"uma-senha-qualquer", h.encode("ascii")) is True
