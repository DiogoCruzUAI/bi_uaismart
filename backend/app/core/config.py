"""Configuração da plataforma. Lê o .env e falha cedo quando algo inseguro passa.

A regra de ouro: um erro de configuração deve derrubar o processo no startup, não
virar vazamento em produção três semanas depois.
"""

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Chaves que já circularam em exemplo, README ou tutorial. Nenhuma entra em produção.
_SEGREDOS_FRACOS = {
    "changeme",
    "secret",
    "troque-me",
    "GERE_UMA_CHAVE_FORTE_AQUI",
    "nextgen-secret-key-troque-em-producao",
}

# token_hex(32) = 64 caracteres hex = 32 bytes = 256 bits de entropia.
_MIN_CARACTERES_SEGREDO = 64


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── Aplicação ────────────────────────────────────────────────────────────
    app_name: str = "NextGen BI"
    app_env: str = "development"
    app_debug: bool = False
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    frontend_url: str = "http://localhost:3000"

    # ─── Banco de metadados da plataforma ─────────────────────────────────────
    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "nextgen_bi"
    db_user: str = "nextgen"
    db_password: str

    @property
    def database_url(self) -> str:
        pwd = quote_plus(self.db_password)
        return f"postgresql+asyncpg://{self.db_user}:{pwd}@{self.db_host}:{self.db_port}/{self.db_name}"

    # ─── Segredos ─────────────────────────────────────────────────────────────
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 7

    # Fernet, para cifrar credenciais de banco dos clientes em repouso.
    credentials_key: str

    # ─── Redis ────────────────────────────────────────────────────────────────
    redis_url: str = "redis://redis:6379/0"
    redis_enabled: bool = True

    # ─── Claude API ───────────────────────────────────────────────────────────
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "high"

    # ─── Limites de execução ──────────────────────────────────────────────────
    # O planejador do Postgres estima linhas e custo antes de ler a primeira
    # página. É de graça e é o que impede a varredura de 300 milhões de linhas.
    max_estimated_rows: int = 50_000_000
    max_estimated_cost: float = 5_000_000.0
    query_timeout_seconds: int = 30
    max_result_rows: int = 10_000

    # Teto de linhas de SAIDA estimadas — outra coisa que `max_estimated_rows`.
    # Aquele barra a varredura; este barra a agregação que devolve mais grupos do
    # que qualquer pessoa lê ou qualquer gráfico desenha. Uma pergunta que produz
    # dois milhões de grupos foi mal formulada, e calcular tudo para jogar 99,5%
    # fora é exatamente a força bruta que o projeto recusa.
    # Folga proposital sobre max_result_rows: estimativa erra, e recusar uma
    # consulta boa por erro de estimativa é pior que devolvê-la truncada.
    max_estimated_output_rows: int = 1_000_000

    # ─── Validadores ──────────────────────────────────────────────────────────

    @field_validator("secret_key")
    @classmethod
    def segredo_forte(cls, v: str) -> str:
        if v in _SEGREDOS_FRACOS or len(v) < _MIN_CARACTERES_SEGREDO:
            raise ValueError(
                f"SECRET_KEY fraca ou curta (mínimo {_MIN_CARACTERES_SEGREDO} caracteres "
                f'= 256 bits). Gere com: python -c "import secrets; print(secrets.token_hex(32))"'
            )
        return v

    @field_validator("credentials_key")
    @classmethod
    def chave_fernet_valida(cls, v: str) -> str:
        """Valida no startup, não na primeira conexão cadastrada.

        Uma CREDENTIALS_KEY inválida só apareceria quando o primeiro cliente
        tentasse conectar o banco dele — o pior momento possível para descobrir.
        """
        from cryptography.fernet import Fernet

        try:
            Fernet(v.encode())
        except Exception as e:
            raise ValueError(
                "CREDENTIALS_KEY inválida (esperado base64 de 32 bytes). Gere com: "
                'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            ) from e
        return v

    @field_validator("llm_effort")
    @classmethod
    def effort_valido(cls, v: str) -> str:
        niveis = {"low", "medium", "high", "xhigh", "max"}
        if v not in niveis:
            raise ValueError(f"LLM_EFFORT deve ser um de {sorted(niveis)}")
        return v

    @model_validator(mode="after")
    def checagens_de_producao(self) -> "Settings":
        if self.app_env == "production":
            if self.app_debug:
                raise ValueError("APP_DEBUG não pode ser true em produção (vaza SQL nos logs).")
            if self.db_user.lower() in {"postgres", "root", "admin"}:
                raise ValueError(
                    f"Em produção, DB_USER não pode ser '{self.db_user}'. "
                    "Crie um usuário dedicado com privilégios mínimos."
                )
            # A lista de segredos fracos existia só para a SECRET_KEY, então o
            # `troque-me` que vem no .env.example passava batido aqui. O banco só
            # escuta em 127.0.0.1, mas "está atrás do firewall" é a premissa que
            # todo vazamento lateral desmente.
            if self.db_password in _SEGREDOS_FRACOS or len(self.db_password) < 16:
                raise ValueError(
                    "DB_PASSWORD é fraca ou curta demais para produção (mínimo 16 "
                    'caracteres). Gere com: python -c "import secrets; '
                    'print(secrets.token_urlsafe(24))"'
                )
            if not self.anthropic_api_key:
                raise ValueError("ANTHROPIC_API_KEY é obrigatória em produção.")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
