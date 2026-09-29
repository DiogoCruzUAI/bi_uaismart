# Arquitetura do NextGen BI

Estado em 29/09/2026. Este documento descreve o que existe e o que está decidido;
onde algo ainda não foi construído, está dito.

## O problema que a plataforma resolve

Um usuário conecta o banco da empresa dele e pergunta em português. A plataforma
responde com texto e gráfico. Nenhuma linha de SQL, nenhum modelo de dados escrito
à mão, nenhum consultor.

O difícil não é gerar SQL — um LLM faz isso desde 2023. O difícil é gerar SQL que
esteja **certo** num banco que ninguém explicou, e saber quando não dá para responder.

## A tese

Um banco de dados não contém as regras do negócio dele. Contém tabelas.

`information_schema` diz que existe uma coluna `salario` do tipo `integer`. Não diz
que ela está em centavos, que mistura pagamento por hora e por mês, que metade dos
registros são transferências internas que não são contratação, nem que a média sobre
dez pessoas identifica essas dez pessoas.

Um Text-to-SQL que lê só o schema produz, para "qual o salário médio", um número
plausível e errado — cem vezes maior, contaminado por unidades incompatíveis e
violando a privacidade de quem está na amostra. E o entrega com confiança.

**A plataforma existe para produzir e manter o que falta ali.** O SQL é consequência.

## Os dois caminhos de consulta

```
pergunta em português
        │
        ├─ [padrão] caminho semântico
        │     LLM → spec estruturada → validada contra o registro → NOSSO código monta o SQL
        │     ✓ regras de negócio garantidas por construção
        │     ✓ supressão e recorte obrigatório aplicados sempre
        │     ✓ determinístico: mesma spec, mesmo SQL
        │
        └─ [rotulado] caminho exploratório
              LLM → SQL → guardrails → EXPLAIN → execução
              ✓ funciona em tabela sem registro
              ⚠ mostrado ao usuário COMO exploratório, com o SQL à vista
```

O caminho semântico é o padrão porque é o único em que a correção do número não
depende do prompt ter dado certo naquela vez. O exploratório existe porque um catálogo
nunca cobre tudo, e um BI que só responde o previsto não serve.

**Nenhum dos dois grava.** A plataforma nunca escreve no banco do cliente.

## Camadas de defesa da execução

Nenhuma é suficiente sozinha. A ordem é do mais barato para o mais caro:

| # | Camada | Onde | Custo |
|---|---|---|---|
| 1 | Parse em AST — se não parseia, não roda | `text2sql/guardrails.py` | zero |
| 2 | Prova de somente-leitura (árvore inteira, inclusive CTE) | `text2sql/guardrails.py` | zero |
| 3 | Comando único — `;` encadeado é recusado | `text2sql/guardrails.py` | zero |
| 4 | Lista branca de tabelas — só o que o dicionário conhece | `text2sql/guardrails.py` | zero |
| 5 | Lista negra de funções — arquivo, rede, DoS | `text2sql/guardrails.py` | zero |
| 6 | LIMIT forçado quando não há agregação | `text2sql/guardrails.py` | zero |
| 7 | **EXPLAIN com teto de linhas e custo** | `text2sql/executor.py` | ~zero |
| 8 | Usuário de banco somente-leitura | conexão do cliente | — |
| 9 | `READ ONLY` + `statement_timeout` na sessão | `connectors/postgres.py` | — |

As camadas 1 a 7 tornam o SQL *provavelmente* seguro. As 8 e 9 o tornam seguro mesmo
quando as anteriores tiverem um bug — e elas terão, algum dia.

Duas propriedades que valem registrar:

- **O que executa é o SQL regerado da AST**, não a string que o LLM escreveu. O que o
  parser não entendeu não sobrevive. Comentários são apagados (ver o comentário em
  `guardrails.py` sobre o vetor `-- */ ; DROP TABLE`).
- **`EXPLAIN` não é `EXPLAIN ANALYZE`.** O primeiro estima sem ler uma página; o
  segundo executa. Confundir os dois anula a camada 7 inteira.

## Multi-tenancy

Fundação, não fase posterior. Retrofit de isolamento é como se vaza dado.

- `tenant_id` em toda tabela, via `TenantMixin`.
- `TenantRepository` **exige o tenant no construtor** — não existe caminho de código
  que consulte sem filtro, porque o objeto não instancia sem ele.
- O `tenant_id` vem do **token assinado**, nunca de rota, corpo ou cabeçalho.
- Registro de outro tenant responde 404, não 403: um 403 confirmaria que aquele id
  existe em algum lugar.
- Credencial de banco do cliente é cifrada em repouso (Fernet, `CREDENTIALS_KEY`).
  Um dump do nosso Postgres não entrega banco de cliente nenhum.

## Procedência: por que o número na tela é confiável

Toda consulta gera uma linha em `consultas` (`models/chat.py`) com: a pergunta, a
spec, o SQL montado, o que o planejador estimou, o que aconteceu de fato, se houve
supressão, e a versão do dicionário em vigor.

Isso muda a natureza da afirmação. Sem o registro, "o BI disse" é argumento de
autoridade. Com ele, é verificável — e quando alguém contesta um número, a discussão
tem onde começar.

As consultas **recusadas** ficam guardadas de propósito: são o conjunto de treino do
produto. A lista do que os guardrails barraram mostra onde o dicionário está
incompleto.

## Custo

"Lowcost" é requisito de negócio, então é decisão de arquitetura.

| Alavanca | Efeito | Onde |
|---|---|---|
| Cache de prompt no dicionário | leitura de cache custa ~5% da entrada | `text2sql/llm.py` *(a construir)* |
| Spec estruturada em vez de SQL | saída ~4× menor | idem |
| Cache de resposta por (tenant, pergunta, versão do dicionário) | pergunta repetida custa zero | Redis |
| Batch API na geração do dicionário | 50% de desconto, e não é sensível a latência | `semantic/` *(a construir)* |
| `EXPLAIN` antes de executar | evita a consulta cara, não só a paga | `text2sql/executor.py` ✓ |

Ordem de grandeza estimada: **~US$ 0,01 por pergunta** com cache e spec, contra
~US$ 0,14 reenviando o dicionário inteiro e pedindo SQL completo. Números assumem um
dicionário de ~30 mil tokens; medir com `count_tokens` quando houver um dicionário real.

Modelo: `claude-opus-5-5`. Nesta fase precisão vale mais que custo, e o cache é que
segura a conta — não a troca por um modelo menor.

## O que existe hoje

```
backend/app/
├── core/          config, base, database, security, crypto, redis, deps    ✓
├── models/        tenant, conexao, semantico, chat                         ✓
├── repositories/  base (tenant obrigatório)                                ✓
├── connectors/    base (contrato), postgres                                ✓
├── text2sql/      guardrails ✓ (29 testes), executor ✓
├── semantic/      perfilamento e geração de dicionário                     a construir
├── chat/          histórico, contexto, streaming                           a construir
└── routers/       health ✓ · auth, conexoes, chat                          a construir
```

## Próximos passos

1. **Perfilador** (`semantic/profiler.py`) — lê `pg_class` e `pg_stats`, monta o retrato
   do banco sem varrer nada.
2. **Gerador de dicionário** (`semantic/dicionario.py`) — Claude lê o retrato e propõe
   descrição, o que **não** é, unidade e escala. Batch API. Humano aprova.
3. **Registro de medidas** — o que vira o equivalente genérico dos cubos YAML.
4. **Pipeline de pergunta** (`text2sql/pipeline.py`) — recuperação do contexto relevante,
   spec estruturada via `messages.parse()`, execução vigiada.
5. **Conjunto de avaliação** — ~50 perguntas com resposta conferida. Sem isso a acurácia
   é sentida, não medida. Semente em [CASOS-DIFICEIS.md](CASOS-DIFICEIS.md).
