"""Testes dos validadores de configuração.

Configuração errada tem que derrubar o processo no startup. O modo de falha que estes
testes impedem é o silencioso: subir com segredo de exemplo e descobrir três semanas
depois, por um vazamento.
"""

import secrets

import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings


def cfg(**mudancas) -> Settings:
    """Configuração válida de produção; cada teste quebra um campo por vez.

    `_env_file=None` é obrigatório: sem ele o pydantic-settings leria um `.env` real
    da máquina e o teste passaria a depender do ambiente de quem o roda.
    """
    base = {
        "app_env": "production",
        "app_debug": False,
        "db_user": "nextgen",
        "db_password": secrets.token_urlsafe(24),
        "secret_key": secrets.token_hex(32),
        "credentials_key": Fernet.generate_key().decode(),
        "anthropic_api_key": "sk-ant-exemplo",
    }
    return Settings(**{**base, **mudancas}, _env_file=None)


def test_configuracao_de_producao_valida_passa():
    assert cfg().app_env == "production"


# ─── Segredos ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("valor", ["curta", "changeme", "troque-me", "a" * 63])
def test_secret_key_fraca_ou_curta_e_recusada(valor):
    with pytest.raises(ValueError, match="SECRET_KEY"):
        cfg(secret_key=valor)


def test_credentials_key_invalida_e_recusada():
    """Validar no startup, não na primeira conexão de cliente cadastrada."""
    with pytest.raises(ValueError, match="CREDENTIALS_KEY"):
        cfg(credentials_key="nao-e-uma-chave-fernet")


@pytest.mark.parametrize("valor", ["troque-me", "changeme", "senha123", "curta"])
def test_db_password_fraca_e_recusada_em_producao(valor):
    """`troque-me` vem no .env.example e passava batido antes desta checagem."""
    with pytest.raises(ValueError, match="DB_PASSWORD"):
        cfg(db_password=valor)


def test_db_password_fraca_passa_em_desenvolvimento():
    """Exigir senha forte em dev só faria as pessoas contornarem o validador."""
    assert cfg(app_env="development", db_password="troque-me").db_password == "troque-me"


# ─── Outras checagens de produção ─────────────────────────────────────────────


def test_debug_em_producao_e_recusado():
    with pytest.raises(ValueError, match="APP_DEBUG"):
        cfg(app_debug=True)


@pytest.mark.parametrize("usuario", ["postgres", "root", "admin", "POSTGRES"])
def test_usuario_privilegiado_de_banco_e_recusado(usuario):
    with pytest.raises(ValueError, match="DB_USER"):
        cfg(db_user=usuario)


def test_chave_da_anthropic_e_obrigatoria_em_producao():
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        cfg(anthropic_api_key="")


def test_effort_invalido_e_recusado():
    with pytest.raises(ValueError, match="LLM_EFFORT"):
        cfg(llm_effort="altissimo")
