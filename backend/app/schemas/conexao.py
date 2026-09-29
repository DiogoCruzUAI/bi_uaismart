"""Schemas das conexões a bancos de clientes.

A regra que governa este arquivo: **a senha entra e nunca sai**.

`ConexaoCriar` tem senha; `ConexaoResposta` não tem — e não é por esquecimento, é a
única razão de serem dois schemas em vez de um. O campo é `SecretStr`, então nem um
`print` do objeto de entrada, nem um traceback do FastAPI, nem um log estruturado
revelam a credencial por descuido de formatação.

`tests/unit/test_schemas_conexao.py` guarda essa propriedade contra alguém que um dia
resolva "simplificar" reutilizando um schema só.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.models.conexao import StatusConexao, TipoBanco

# Portas padrão, para o cadastro não exigir do usuário algo que dá para inferir.
_PORTA_PADRAO = {TipoBanco.postgres: 5432, TipoBanco.mysql: 3306}


class ConexaoCriar(BaseModel):
    """Entrada do cadastro. Único schema que carrega senha."""

    nome: str = Field(min_length=1, max_length=120)
    tipo: TipoBanco
    host: str = Field(min_length=1, max_length=255)
    porta: int | None = Field(default=None, ge=1, le=65535)
    banco: str = Field(min_length=1, max_length=120)
    usuario: str = Field(min_length=1, max_length=120)
    senha: SecretStr
    usar_ssl: bool = True

    @field_validator("host")
    @classmethod
    def host_sem_esquema(cls, v: str) -> str:
        """Recusa `postgres://...` colado inteiro no campo de host.

        É o erro de cadastro mais comum, e o diagnóstico sem esta checagem é uma falha
        de DNS incompreensível — com a senha visível na tela, de quebra.
        """
        v = v.strip()
        if "://" in v:
            raise ValueError(
                "Informe apenas o endereço do servidor, sem 'postgres://' nem "
                "'mysql://'. Os demais dados têm campos próprios."
            )
        return v

    def porta_efetiva(self) -> int:
        return self.porta or _PORTA_PADRAO[self.tipo]


class ConexaoAtualizar(BaseModel):
    """Edição. Senha ausente significa "manter a que já está guardada"."""

    nome: str | None = Field(default=None, min_length=1, max_length=120)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    porta: int | None = Field(default=None, ge=1, le=65535)
    banco: str | None = Field(default=None, min_length=1, max_length=120)
    usuario: str | None = Field(default=None, min_length=1, max_length=120)
    senha: SecretStr | None = None
    usar_ssl: bool | None = None


class ConexaoResposta(BaseModel):
    """Saída. Sem senha, e sem campo que a derive.

    `senha_cifrada` também fica de fora: o texto cifrado não revela a senha, mas
    entregá-lo dá a quem ataca material para trabalhar offline, sem limite de
    tentativas e sem deixar rastro nos nossos logs.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    nome: str
    tipo: TipoBanco
    host: str
    porta: int
    banco: str
    usuario: str
    usar_ssl: bool
    status: StatusConexao
    ultimo_erro: str | None
    ultimo_teste_em: datetime | None
    ultimo_perfilamento_em: datetime | None
    versao_dicionario: int
    criado_em: datetime


class ResultadoTeste(BaseModel):
    """Resultado de `POST /conexoes/{id}/testar`.

    `alertas` traz privilégio amplo demais que ainda assim só lê — não impede o uso,
    mas o dono do dado precisa saber o que concedeu.
    """

    conectou: bool
    alertas: list[str] = Field(default_factory=list)


class ResultadoPerfilamento(BaseModel):
    tabelas_criadas: int
    tabelas_atualizadas: int
    tabelas_removidas: int
    colunas_criadas: int
    colunas_atualizadas: int
    colunas_removidas: int
    ligacoes_gravadas: int
    ligacoes_preservadas: int
    revisoes_preservadas: int
    versao_dicionario: int
    avisos: list[str] = Field(default_factory=list)


class ResultadoDicionario(BaseModel):
    """Resultado de `POST /conexoes/{id}/dicionario`.

    Os contadores de token vão para a tela de propósito: gerar dicionário é a operação
    mais cara da plataforma, e o cliente precisa ver o custo do que acabou de pedir em
    vez de descobri-lo na fatura.
    """

    tabelas_descritas: int
    colunas_descritas: int
    revisoes_respeitadas: int
    lotes: int
    lotes_com_falha: int
    tokens_entrada: int
    tokens_cache_leitura: int
    tokens_saida: int
    modelo: str
    problemas: list[str] = Field(default_factory=list)
