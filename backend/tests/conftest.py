"""Configuração comum dos testes.

As variáveis de ambiente são definidas **antes** de qualquer import de `app`, porque
`app.core.config` valida no import e derrubaria a coleta do pytest. Os valores são
gerados na hora: nenhum segredo, nem de teste, fica escrito em arquivo.
"""

import os
import secrets

from cryptography.fernet import Fernet

os.environ.setdefault("DB_PASSWORD", "senha-de-teste")
os.environ.setdefault("SECRET_KEY", secrets.token_hex(32))
os.environ.setdefault("CREDENTIALS_KEY", Fernet.generate_key().decode())
os.environ.setdefault("APP_ENV", "development")
