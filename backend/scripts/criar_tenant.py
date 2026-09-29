#!/usr/bin/env python
"""Cria um tenant e o primeiro usuário administrador dele.

Existe porque não há auto-cadastro: quem abre uma conta na plataforma é a UAISmart,
não um visitante. Auto-cadastro exige verificação de e-mail, proteção contra abuso e
uma política de o que fazer com contas abandonadas — tudo isso é produto, não
infraestrutura, e não se resolve com um formulário.

Uso, com a aplicação de pé:

    docker compose exec api python scripts/criar_tenant.py \\
        --nome "Empresa Cliente" --slug empresa \\
        --admin-nome "Fulano" --admin-email fulano@empresa.com

Sem `--senha`, uma senha forte é gerada e mostrada **uma única vez**. É de propósito:
senha passada em argumento fica no histórico do shell e na lista de processos da
máquina, onde qualquer usuário do servidor a lê com `ps`.
"""

import argparse
import asyncio
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.core.security import hash_senha  # noqa: E402
from app.models.tenant import PerfilUsuario, Tenant, Usuario  # noqa: E402
from app.schemas.auth import SENHA_MINIMA  # noqa: E402

_SLUG_VALIDO = re.compile(r"^[a-z0-9][a-z0-9-]{1,58}[a-z0-9]$")


async def criar(
    *, nome: str, slug: str, admin_nome: str, admin_email: str, senha: str
) -> None:
    async with AsyncSessionLocal() as db:
        existente = (
            await db.execute(select(Tenant).where(Tenant.slug == slug))
        ).scalar_one_or_none()
        if existente is not None:
            print(f"ERRO: já existe um tenant com o slug '{slug}'.", file=sys.stderr)
            raise SystemExit(1)

        tenant = Tenant(nome=nome, slug=slug)
        db.add(tenant)
        await db.flush()

        db.add(
            Usuario(
                tenant_id=tenant.id,
                nome=admin_nome,
                email=admin_email.lower(),
                senha_hash=hash_senha(senha),
                perfil=PerfilUsuario.admin,
            )
        )
        await db.commit()

    print(f"Tenant criado: {nome} (slug: {slug}, id: {tenant.id})")
    print(f"Administrador: {admin_email}")


def main() -> None:
    p = argparse.ArgumentParser(description="Cria um tenant e seu administrador.")
    p.add_argument("--nome", required=True, help="Nome da empresa cliente.")
    p.add_argument("--slug", required=True, help="Identificador curto, usado no login.")
    p.add_argument("--admin-nome", required=True)
    p.add_argument("--admin-email", required=True)
    p.add_argument(
        "--senha",
        default=None,
        help="Opcional. Sem ela, uma senha forte é gerada e mostrada uma vez.",
    )
    args = p.parse_args()

    slug = args.slug.strip().lower()
    if not _SLUG_VALIDO.match(slug):
        print(
            "ERRO: o slug deve ter de 3 a 60 caracteres, só minúsculas, números e "
            "hífen, sem começar nem terminar com hífen.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    senha = args.senha
    gerada = senha is None
    if gerada:
        senha = secrets.token_urlsafe(18)
    elif len(senha) < SENHA_MINIMA:
        print(f"ERRO: a senha precisa de pelo menos {SENHA_MINIMA} caracteres.", file=sys.stderr)
        raise SystemExit(1)

    asyncio.run(
        criar(
            nome=args.nome.strip(),
            slug=slug,
            admin_nome=args.admin_nome.strip(),
            admin_email=args.admin_email.strip(),
            senha=senha,
        )
    )

    if gerada:
        print()
        print("  Senha gerada (não será mostrada de novo):")
        print(f"      {senha}")
        print()
        print("  Guarde-a agora, num gerenciador de senhas. Depois do primeiro acesso,")
        print("  troque-a em POST /api/v1/auth/senha.")


if __name__ == "__main__":
    main()
