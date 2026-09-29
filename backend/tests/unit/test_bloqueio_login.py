"""Testes do bloqueio por tentativas de login.

A suíte roda com `REDIS_ENABLED=false`, então este arquivo injeta um Redis falso. É
onde a lógica de bloqueio é de fato exercitada — sem isto ela ficaria escrita e nunca
executada, que é o pior estado para um controle de segurança.
"""

import pytest

from app.core import redis_client
from app.core.redis_client import (
    JANELA_SEGUNDOS,
    MAX_TENTATIVAS,
    _chave_tentativas,
    limpar_falhas_de_login,
    login_bloqueado,
    registrar_falha_de_login,
)


class RedisFalso:
    """O mínimo de Redis que o bloqueio usa, com registro das chamadas."""

    def __init__(self, quebrado=False):
        self.dados: dict[str, int] = {}
        self.ttl: dict[str, int] = {}
        self.quebrado = quebrado

    def _checar(self):
        if self.quebrado:
            raise ConnectionError("redis fora do ar")

    async def get(self, chave):
        self._checar()
        valor = self.dados.get(chave)
        return None if valor is None else str(valor)

    async def incr(self, chave):
        self._checar()
        self.dados[chave] = self.dados.get(chave, 0) + 1
        return self.dados[chave]

    async def expire(self, chave, segundos):
        self._checar()
        self.ttl[chave] = segundos

    async def delete(self, chave):
        self._checar()
        self.dados.pop(chave, None)
        self.ttl.pop(chave, None)


@pytest.fixture
def redis(monkeypatch):
    falso = RedisFalso()
    monkeypatch.setattr(redis_client, "get_redis", lambda: falso)
    return falso


# ─── Contagem e bloqueio ──────────────────────────────────────────────────────


async def test_conta_limpa_nao_esta_bloqueada(redis):
    assert not await login_bloqueado("teste", "a@b.com")


async def test_poucas_falhas_nao_bloqueiam(redis):
    """Três, quatro erros são normais para quem esqueceu a senha."""
    for _ in range(MAX_TENTATIVAS - 1):
        await registrar_falha_de_login("teste", "a@b.com")
    assert not await login_bloqueado("teste", "a@b.com")


async def test_bloqueia_ao_atingir_o_limite(redis):
    for _ in range(MAX_TENTATIVAS):
        await registrar_falha_de_login("teste", "a@b.com")
    assert await login_bloqueado("teste", "a@b.com")


async def test_login_bem_sucedido_limpa_a_contagem(redis):
    for _ in range(MAX_TENTATIVAS):
        await registrar_falha_de_login("teste", "a@b.com")
    await limpar_falhas_de_login("teste", "a@b.com")
    assert not await login_bloqueado("teste", "a@b.com")


async def test_contas_diferentes_nao_se_afetam(redis):
    for _ in range(MAX_TENTATIVAS):
        await registrar_falha_de_login("teste", "vitima@b.com")
    assert await login_bloqueado("teste", "vitima@b.com")
    assert not await login_bloqueado("teste", "outra@b.com")


async def test_mesmo_email_em_tenants_diferentes_conta_separado(redis):
    """São contas distintas; bloquear uma não pode trancar a outra."""
    for _ in range(MAX_TENTATIVAS):
        await registrar_falha_de_login("cliente-a", "a@b.com")
    assert await login_bloqueado("cliente-a", "a@b.com")
    assert not await login_bloqueado("cliente-b", "a@b.com")


# ─── Janela ───────────────────────────────────────────────────────────────────


async def test_janela_comeca_na_primeira_falha(redis):
    """A janela não é estendida a cada tentativa.

    Se fosse, um ataque contínuo manteria a conta trancada para sempre — e o alvo
    seria justamente o dono da conta, não quem ataca.
    """
    chave = _chave_tentativas("teste", "a@b.com")
    await registrar_falha_de_login("teste", "a@b.com")
    assert redis.ttl[chave] == JANELA_SEGUNDOS

    redis.ttl[chave] = 42  # simula tempo passando
    await registrar_falha_de_login("teste", "a@b.com")
    assert redis.ttl[chave] == 42, "o TTL foi reiniciado por uma tentativa posterior"


# ─── Privacidade e resiliência ────────────────────────────────────────────────


async def test_email_nao_e_guardado_em_claro(redis):
    """O Redis não é o lugar de um cadastro de e-mails de clientes."""
    await registrar_falha_de_login("teste", "pessoa@empresa.com")
    tudo = " ".join(redis.dados.keys())
    assert "pessoa@empresa.com" not in tudo
    assert "empresa" not in tudo


async def test_redis_fora_do_ar_nao_tranca_ninguem(monkeypatch):
    """Escolha consciente: aqui a falha abre.

    O login é a porta de entrada da plataforma inteira. Com o Redis fora do ar, a
    alternativa a liberar seria uma indisponibilidade total por causa do cache — e o
    bloqueio é defesa em profundidade, não a única. A senha continua sendo exigida.
    """
    monkeypatch.setattr(redis_client, "get_redis", lambda: RedisFalso(quebrado=True))
    assert not await login_bloqueado("teste", "a@b.com")
    # E registrar falha não estoura exceção para dentro do login.
    await registrar_falha_de_login("teste", "a@b.com")


async def test_sem_redis_configurado_nada_quebra(monkeypatch):
    monkeypatch.setattr(redis_client, "get_redis", lambda: None)
    assert not await login_bloqueado("teste", "a@b.com")
    await registrar_falha_de_login("teste", "a@b.com")
    await limpar_falhas_de_login("teste", "a@b.com")
