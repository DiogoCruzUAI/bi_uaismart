"""A base declarativa, sozinha e sem dependência de driver.

Mora fora de `database.py` de propósito. Lá, o `create_async_engine` roda no import
do módulo — então, com a `Base` junto, importar um *modelo* criaria um engine e
exigiria o driver do Postgres instalado. Teste de modelo e de repositório passaria a
precisar de infraestrutura para nada.

Separando, `app.models` importa só isto, e o engine só nasce para quem realmente vai
falar com o banco.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
