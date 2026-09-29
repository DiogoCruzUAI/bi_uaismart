# NextGen BI

Plataforma de BI em que o usuário conecta o banco da empresa e pergunta em português.
Resposta em texto e gráfico, sem escrever SQL e sem modelar nada à mão.

> **Somos inteligência, não força bruta no tratamento e disponibilização dos dados.**

## O que torna isto diferente de "LLM que escreve SQL"

Um banco não contém as regras de negócio dele — contém tabelas. O schema diz que
existe uma coluna `salario` inteira; não diz que ela está em centavos, que mistura
pagamento por hora e por mês, nem que a média sobre dez pessoas identifica essas dez
pessoas. Um Text-to-SQL ingênuo responde "salário médio" com um número cem vezes
maior, e responde com confiança.

A plataforma existe para descobrir e manter o que falta ali. O SQL é consequência.

Ver [docs/ARQUITETURA.md](docs/ARQUITETURA.md).

## Estado

**Fase 0 — fundação.** Existe e está testado:

- autenticação JWT com rotação de refresh e bloqueio por tentativas;
- multi-tenancy estrutural: `tenant_id` obrigatório no repositório, não por disciplina;
- credenciais de banco de clientes cifradas em repouso (Fernet);
- guardrails de SQL em 6 camadas, provados em 29 testes (escrita, DDL, encadeamento,
  CTE com `DELETE`, funções de arquivo e rede, esquemas de sistema, lista branca);
- execução vigiada com porta de `EXPLAIN` antes de qualquer consulta;
- conector Postgres que perfila por estatística de catálogo, nunca por varredura;
- registro de procedência de toda consulta, inclusive das recusadas;
- perfilamento semântico e geração de dicionário de dados por IA.

A construir: perfilador semântico, gerador de dicionário, pipeline de pergunta,
frontend. Roteiro no fim de [docs/ARQUITETURA.md](docs/ARQUITETURA.md).

## Subir o ambiente

```bash
cp .env.example .env
```

Gere os dois segredos e coloque no `.env`:

```bash
python -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))"
```

```bash
python -c "from cryptography.fernet import Fernet; print('CREDENTIALS_KEY=' + Fernet.generate_key().decode())"
```

> A `CREDENTIALS_KEY` cifra as credenciais de banco dos clientes. Perdê-la torna
> ilegível toda conexão já cadastrada — guarde uma cópia fora do servidor.

```bash
docker compose up -d --build
```

Para provisionar o servidor — especificação da máquina, tuning do Postgres, usuário
somente-leitura no banco do cliente, retenção e backup — ver
[docs/INFRA.md](docs/INFRA.md).

A API responde em `http://localhost:8000` e a documentação interativa em
`http://localhost:8000/docs` (desabilitada em produção, de propósito).

### Criar o primeiro tenant

Não há auto-cadastro: quem abre uma conta é a UAISmart. Sem este passo, ninguém
consegue fazer login.

```bash
docker compose exec api python scripts/criar_tenant.py --nome "Empresa Cliente" --slug empresa --admin-nome "Fulano" --admin-email fulano@empresa.com
```

A senha é gerada e mostrada **uma única vez** — passá-la por argumento a deixaria no
histórico do shell e visível em `ps` para qualquer usuário do servidor.

O login pede o slug junto com e-mail e senha, porque o mesmo e-mail pode existir em
mais de um tenant:

```bash
curl -X POST http://localhost:8000/api/v1/auth/login -H 'Content-Type: application/json' -d '{"tenant":"empresa","email":"fulano@empresa.com","senha":"..."}'
```

## Testes

```bash
docker compose exec api pytest -q
```

Ou localmente, a partir de `backend/`:

```bash
pip install -r requirements.txt && pytest -q
```

## Estrutura

```
backend/app/
├── core/          configuração, banco, JWT, cifra de credenciais, dependências
├── models/        tenant, conexões, catálogo semântico, chat e procedência
├── repositories/  CRUD com isolamento de tenant obrigatório
├── connectors/    acesso somente-leitura a bancos de clientes
├── text2sql/      guardrails e execução vigiada
├── semantic/      perfilamento e dicionário de dados        (a construir)
├── chat/          histórico, contexto e streaming            (a construir)
└── routers/       endpoints
docs/              arquitetura e conjunto de avaliação
frontend/          Next.js                                    (a construir)
```

## Convenções

Em [CLAUDE.md](CLAUDE.md) — idioma, tratamento de segredos, SQL dinâmico, migrations,
e o que reprova um PR.
