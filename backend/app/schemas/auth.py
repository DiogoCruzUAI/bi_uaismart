"""Schemas de autenticação.

O login pede o **tenant** junto com e-mail e senha, e isso não é burocracia: o modelo
define e-mail único por tenant (`UniqueConstraint("tenant_id", "email")`), porque a
mesma pessoa pode ser usuária de duas empresas clientes com o mesmo endereço. Sem o
tenant, "diogo@empresa.com" é ambíguo.

A alternativa seria procurar o e-mail em todos os tenants e entrar no único que
casasse. Foi descartada: além de quebrar quando alguém tem duas contas, ela transforma
o login num oráculo — dá para descobrir em quais clientes uma pessoa trabalha
tentando o e-mail dela.

Na prática o frontend preenche o campo a partir do subdomínio; o usuário não digita.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator

from app.models.tenant import PerfilUsuario


class LoginRequisicao(BaseModel):
    tenant: str = Field(min_length=1, max_length=60, description="Slug do tenant.")
    email: EmailStr
    senha: SecretStr


class RefreshRequisicao(BaseModel):
    refresh_token: str


class Tokens(BaseModel):
    """`token_type` fixo em bearer para o cliente HTTP montar o cabeçalho sozinho."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expira_em_segundos: int


class UsuarioResposta(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nome: str
    email: EmailStr
    perfil: PerfilUsuario
    tenant_id: int
    ativo: bool
    ultimo_acesso_em: datetime | None


# Mínimo de caracteres da senha de um usuário da plataforma.
#
# Dez, e não a receita de maiúscula-número-símbolo: composição forçada produz
# "Senha@123" em toda parte, que é pior que uma frase longa. O comprimento é o que
# realmente custa a quem tenta adivinhar, e o bloqueio por tentativas
# (`app.core.redis_client`) cobre o resto.
SENHA_MINIMA = 10


def _validar_senha(v: SecretStr) -> SecretStr:
    if len(v.get_secret_value()) < SENHA_MINIMA:
        raise ValueError(f"A senha precisa de pelo menos {SENHA_MINIMA} caracteres.")
    return v


class UsuarioCriar(BaseModel):
    """Criação de usuário por um admin, dentro do próprio tenant.

    Não há `tenant_id`: ele vem do token de quem está criando. Aceitá-lo no corpo
    permitiria a um admin criar usuários no tenant de outro cliente.
    """

    nome: str = Field(min_length=1, max_length=200)
    email: EmailStr
    senha: SecretStr
    perfil: PerfilUsuario = PerfilUsuario.analista

    _senha_forte = field_validator("senha")(_validar_senha)


class SenhaAlterar(BaseModel):
    senha_atual: SecretStr
    senha_nova: SecretStr

    _senha_forte = field_validator("senha_nova")(_validar_senha)
