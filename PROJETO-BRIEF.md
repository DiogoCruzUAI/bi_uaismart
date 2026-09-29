# Prompt de Inicialização — NextGen BI-AI

## Contexto: quem sou e o que já existe

Sou Diogo Cruz, da UAISmart. Tenho três projetos em produção/desenvolvimento que servem de base para construir uma plataforma SaaS de BI inteligente. Este chat é para iniciar o projeto do zero, bem direcionado.

---

## OS TRÊS PROJETOS EXISTENTES (leia antes de propor qualquer coisa)

### 1. Leads_UAI (`C:\Users\diogo\Desktop\LEADS_UAI`)

Portal de dados públicos brasileiros em produção em `leads.uaismart.com`. Stack: FastAPI + Postgres 16 + React/Vite/TypeScript, Docker, Nginx com SSL.

**O que tem de dados (números reais, medidos):**
- ~60 milhões de empresas (Receita Federal), modelo medalhão Bronze → Silver → Gold
- 4,7 milhões de contratos públicos (PNCP), 420 mil empresas fornecedoras
- 293 milhões de movimentações de emprego (Novo CAGED), 79 competências desde 2020
- Dívida ativa da União (PGFN): 28,3 milhões de inscrições
- Sanções (CEIS/CNEP): 10.262 empresas
- ETL rodando em produção com coleta diária/mensal automatizada

**O que tem de código reaproveitável como referência:**
- `backend/app/trabalho_medidas.py` — registro de medidas onde cada dimensão e indicador está definido uma vez só e o SQL é montado a partir de lista branca. É o modelo do que uma "camada semântica automática" deveria produzir.
- SQL dinâmico com proteção contra injeção (nomes de coluna em whitelist, nunca interpolação). Mesmo princípio que o Text-to-SQL precisa.
- Exportação em Excel/CSV/PDF com metadados (período, filtros, caveats).
- `docs/FONTES-DADOS.md` — documentação coluna a coluna do que cada fonte significa E O QUE NÃO SIGNIFICA. Modelo para o dicionário de dados que a IA deve gerar.

**Regras do projeto Leads que devem ser respeitadas:**
- Datas/horas em pt-BR, timezone Brasília.
- Nada de UPDATE em massa na Gold.
- Entender cada coluna antes de publicar.
- Segredos só no `.env` do servidor.

### 2. AlumiPremium (`C:\Users\diogo\Desktop\PLATAFORMA_CLIENTE\alumipremium`)

Migração de Power BI para stack própria. Um cliente real (distribuidora de alumínio).

**O que tem:**
- Pipeline ETL Python: Bronze → Silver → Gold, 28 fontes Postgres + 5 SharePoint
- Star schema completo: 11 dimensões + 15 fatos (26 tabelas Gold)
- **Cube.dev como camada semântica** — 26 cubos modelados em YAML (`cube/model/cubes/*.yml`) com joins, measures, dimensions e filters. Exposto em `bi-alumipremium.uaismart.com` com JWT.
- Dashboard Next.js + Recharts consumindo o Cube.dev
- Ingestão incremental com watermarks
- Triagem de 421 medidas DAX do Power BI original (grafo de dependências, validação contra DAX)

**O que é reaproveitável:**
- O formato YAML do Cube.dev é exatamente o que a IA de "auto-configuração" deveria gerar como output da análise de schema.
- O padrão ETL config-driven (`gold/registry.py`, `ingestion/registry.py`) — declarativo, sem ORM.
- A experiência de migrar DAX para SQL/Cube mostra a complexidade real de traduzir lógica de negócio.

### 3. SessionFlow (`C:\Users\diogo\Desktop\PLATAFORMA_CLIENTE\sessionflow`)

SaaS multi-tenant de gestão de sessões (clínicas). Em produção em `limacruz.ddns.net`.

**O que tem:**
- **Multi-tenancy real**: `tenant_id` em todas as queries, isolamento por row-level filtering
- FastAPI async com SQLAlchemy async, Alembic migrations
- Auth completa: OAuth2 + JWT (access + refresh), blacklist de tokens via Redis, perfis (owner/professional/patient)
- Analytics views: DRE, inadimplência, ocupação, faturamento, churn — tudo filtrado por `tenant_id`
- Rate limiting (slowapi), audit log, Celery workers
- Deploy Docker com nginx, Redis

**O que é reaproveitável diretamente:**
- `backend/app/core/database.py` — engine async, connection pool, session factory
- `backend/app/core/deps.py` — injeção de dependências FastAPI (auth + db + tenant)
- `backend/app/core/security.py` — JWT encode/decode, hash de senha
- `backend/app/repositories/base.py` — CRUD genérico com tenant isolation
- `backend/app/repositories/analytics.py` — padrão de queries analíticas com SQL dinâmico seguro
- O padrão inteiro de multi-tenancy com `tenant_id`

---

## O PRODUTO: NextGen BI-AI

### Visão
Plataforma SaaS de BI "Zero-Code" onde o usuário conecta seu banco de dados e faz perguntas em linguagem natural (português), recebendo insights de texto e gráficos interativos via Text-to-SQL com LLM.

### Diferencial
- A IA faz engenharia reversa do banco do cliente (gera dicionário de dados e relacionamentos automaticamente)
- Chat + dashboards persistentes em uma única interface
- Começa com Claude API para Text-to-SQL (precisão > custo nesta fase)

### Os 5 Goals do MVP
1. **Conexão multi-tenant de banco** — o cliente conecta seu Postgres/MySQL
2. **IA lê schema e gera dicionário** — análise automática de tabelas, colunas, tipos, relações
3. **Text-to-SQL seguro** — pergunta em português → SQL → resultado, com proteções contra comandos destrutivos
4. **API de Chat** — resposta em texto + payload JSON para gráficos, com histórico
5. **Frontend unificado** — chat + renderização dinâmica de gráficos

---

## ESTRATÉGIA DECIDIDA (não re-discutir, executar)

### Projeto novo, peças existentes

- Repositório novo para o NextGen BI. Não é extensão do Leads nem do SessionFlow.
- Stack: FastAPI async + Postgres + React (ou Next.js) + Docker.

### Ordem de execução

**Fase 0 — Prova de conceito (2-3 semanas):**
1. Copiar a base multi-tenant do SessionFlow (database.py, deps.py, auth JWT, tenant isolation).
2. Usar o banco do Leads como primeiro "conector" — apontar para o Postgres do Leads com as tabelas Gold/governo/trabalho. São dados reais, densos e variados.
3. Construir o módulo Text-to-SQL: pergunta em português → ler metadados do schema → gerar SQL via Claude API → validar (whitelist de operações) → executar → retornar JSON.
4. API de Chat com FastAPI async, histórico de conversa e streaming.

**Fase 1 — MVP funcional (mais 3-4 semanas):**
1. Frontend com chat + renderização dinâmica de gráficos.
2. Geração automática de dicionário de dados a partir do schema Postgres (Claude gera descrições, validando contra FONTES-DADOS.md do Leads).
3. Segundo conector: banco MySQL do AlumiPremium (star schema, 26 tabelas) — prova que funciona com mais de um banco.
4. O formato YAML do Cube.dev como modelo para o output da camada semântica.

**Fase 2 — Multi-tenant real (depois do MVP):**
1. Conexão dinâmica a bancos de clientes (connection string por tenant).
2. Inferência automática de schema com IA.
3. Dashboards persistentes salvos por usuário.

### O que NÃO fazer
- Não enfiar o BI dentro do Leads nem do SessionFlow — são produtos separados.
- Não começar pelo multi-tenant genérico — provar Text-to-SQL primeiro.
- Não migrar para LLMs open source cedo — Text-to-SQL com GROUPING SETS, CTEs e JOINs complexos precisa de modelo forte. LLM comercial para SQL, open source só para resumos se necessário.
- Não reinventar o que já existe: copiar padrões do SessionFlow (auth, multi-tenant) e do AlumiPremium (camada semântica).

---

## DADOS DO LEADS PARA TESTAR (o "conector zero")

Tabelas Gold do Leads que o Text-to-SQL deve conseguir consultar:

| Schema.Tabela | Linhas | O que responde |
|---|---|---|
| `gold.estabelecimentos` | ~73 mi | Empresas brasileiras com filtros (UF, CNAE, porte, situação, dívida, sanção) |
| `governo.contratos` | 4,7 mi | Quem vendeu ao governo, quanto e quando |
| `governo.fornecedores` | 420 mil | Resumo por empresa fornecedora |
| `governo.contratacoes` | ~1,5 mi | Licitações (o que o governo está comprando) |
| `trabalho.caged` | 293 mi | Admissões/desligamentos com carteira assinada |
| `risco.divida_ativa_resumo` | ~5 mi | Dívida ativa da empresa com a União |
| `risco.sancoes` | ~24 mil | Empresas sancionadas (CEIS/CNEP) |

Perguntas de teste que o Text-to-SQL deve acertar:
- "Quantas empresas de tecnologia abriram em MG nos últimos 12 meses?"
- "Quais os 10 maiores fornecedores do governo federal em valor?"
- "Qual o salário mediano de desenvolvedores em Uberlândia?"
- "Empresas de construção civil em SP com dívida ativa acima de R$ 1 milhão"

---

## ESTRUTURA SUGERIDA PARA O REPOSITÓRIO

```
nextgen-bi/
├── backend/
│   ├── app/
│   │   ├── core/           # database.py, config.py, security.py (base do SessionFlow)
│   │   ├── auth/           # JWT, tenant isolation (copiado do SessionFlow)
│   │   ├── connectors/     # Conexão a bancos externos (Postgres, MySQL)
│   │   ├── semantic/       # Análise de schema, geração de dicionário
│   │   ├── text2sql/       # Pipeline: pergunta → metadados → LLM → SQL → validação → execução
│   │   ├── chat/           # Histórico de conversa, contexto, streaming
│   │   └── routers/        # Endpoints FastAPI
│   ├── tests/
│   └── Dockerfile
├── frontend/               # Next.js ou React+Vite
│   ├── chat/               # Interface de chat
│   ├── viz/                # Renderização dinâmica de gráficos
│   └── dashboard/          # Dashboards persistentes (fase 2)
├── docker-compose.yml
└── .env.example
```

---

## INSTRUÇÃO

Comece pela **Fase 0**: estruture o projeto, copie os padrões do SessionFlow, crie o módulo Text-to-SQL apontando para o banco do Leads como primeiro conector. O objetivo é ter um chat funcionando que responda perguntas sobre empresas brasileiras com SQL gerado por IA.

Os três projetos de referência estão em:
- `C:\Users\diogo\Desktop\LEADS_UAI` (Leads)
- `C:\Users\diogo\Desktop\PLATAFORMA_CLIENTE\alumipremium` (AlumiPremium)
- `C:\Users\diogo\Desktop\PLATAFORMA_CLIENTE\sessionflow` (SessionFlow)
