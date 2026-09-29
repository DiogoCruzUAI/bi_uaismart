# NextGen BI — como se trabalha neste repositório

## O mantra

> **Somos inteligência, não força bruta no tratamento e disponibilização dos dados.**

Não é slogan, é critério de aceite. Em revisão de código, estas perguntas reprovam um PR:

| Situação | O que reprova | O que passa |
|---|---|---|
| Perfilar tabela do cliente | `SELECT ... FROM tabela` inteira, `COUNT(*)` sem recorte | `pg_stats`, `reltuples`, `TABLESAMPLE` |
| Contexto para o LLM | Despejar o dicionário inteiro no prompt | Recuperar só as tabelas relevantes à pergunta |
| Executar SQL gerado | Rodar e ver no que dá | `EXPLAIN` com teto de custo **antes** de executar |
| Pergunta repetida | Chamar o LLM de novo | Cache por (tenant, pergunta normalizada, versão do dicionário) |
| Prompt grande e estável | Reenviar sem `cache_control` | Prefixo estável cacheado; volátil depois do breakpoint |

## As duas seguranças

São requisitos de produto, não detalhes de implementação.

**1. Vazamento de informação.** Todo dado é de um tenant.
- Toda tabela tem `tenant_id`; todo acesso passa por `TenantRepository`, que injeta o filtro.
  Repositório sem tenant não existe — se precisar de um, o desenho está errado.
- Credencial de banco de cliente é cifrada em repouso (Fernet, `CREDENTIALS_KEY`). Nunca em log,
  nunca em resposta de API, nunca em mensagem de erro.
- Conexão ao banco do cliente usa usuário **somente-leitura**. A garantia é do banco, não nossa.

**2. Confiabilidade do número na tela.** Um número errado com cara de certo é pior que um erro.
- Nenhum número chega à tela sem **procedência**: qual medida, quais filtros, quantas linhas o
  sustentam, qual SQL rodou, qual versão do dicionário.
- Se a base é pequena demais para sustentar a estatística, o valor é **suprimido**, não estimado.
- Se o dicionário não sabe o que uma coluna significa, a plataforma diz que não sabe. Ela não
  chuta unidade, moeda, escala nem semântica.

## Arquitetura em uma frase

O LLM **não escreve SQL livre por padrão**. Ele escreve uma *spec de consulta* (dimensões,
medidas, filtros) validada contra um registro em lista branca; **nosso código monta o SQL**.
SQL livre existe como modo exploratório rotulado, com sandbox. Detalhes em
[docs/ARQUITETURA.md](docs/ARQUITETURA.md).

## Convenções

- **Idioma**: código, tabelas e colunas em português (segue Leads e SessionFlow). Comentário
  explica *por quê*, não *o quê*.
- **Datas/horas**: pt-BR, timezone `America/Sao_Paulo`.
- **Segredos**: só no `.env` do servidor. Nada de credencial em código, teste ou fixture.
- **SQL dinâmico**: nome de tabela/coluna sempre por lista branca. Valor sempre por parâmetro
  vinculado. Interpolação de string em SQL é sempre bug.
- **Migrations**: Alembic. Nada de `create_all()` fora de teste.
- **Números medidos**: quando um comentário afirmar um número (linhas, porcentagem, custo),
  dizer **quando** foi medido. Número sem data apodrece em silêncio.

## Os projetos de referência

`LEADS_UAI`, `PLATAFORMA_CLIENTE/alumipremium` e `PLATAFORMA_CLIENTE/sessionflow` são **fonte de
dados e de aprendizado, não especificação**. O NextGen é uma plataforma independente que precisa
funcionar em banco de cliente que ninguém nunca viu. Use-os para:

- **SessionFlow** — padrão de auth JWT, isolamento por tenant, repositório assíncrono.
- **AlumiPremium** — o YAML do Cube.dev como formato-alvo da camada semântica.
- **Leads** — o **caso de teste difícil**. Se a plataforma descobre sozinha as armadilhas daquela
  base (salário em centavos, unidades misturadas, transferências que não são contratação), ela
  funciona em qualquer lugar. Ver `docs/CASOS-DIFICEIS.md`.

## Documentação

- [docs/ARQUITETURA.md](docs/ARQUITETURA.md) — os dois caminhos de consulta e as camadas de defesa
- [docs/INFRA.md](docs/INFRA.md) — provisionamento, tuning, backup e migração
- [docs/CASOS-DIFICEIS.md](docs/CASOS-DIFICEIS.md) — conjunto de avaliação

## Comandos

```bash
docker compose up -d --build       # sobe tudo
docker compose logs -f api         # logs da API
docker compose exec api pytest     # testes
docker compose exec api alembic revision --autogenerate -m "descricao"
```
