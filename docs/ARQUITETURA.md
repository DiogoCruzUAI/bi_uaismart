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

## O caminho de uma pergunta

```
pergunta em português
   │
   ├─ selecionar_tabelas   recorta o catálogo — não manda o banco inteiro
   ├─ LLM                  devolve uma ConsultaSpec, nunca SQL
   ├─ construir            NOSSO código monta o SQL: lista branca + escala + parâmetros
   ├─ guardrails           prova que é leitura, tabela conhecida, um comando só
   ├─ EXPLAIN              recusa o que varre demais, antes de executar
   └─ executar             cursor, transação somente-leitura, prazo
```

O modelo participa de **um** passo, e o que produz é estrutura validada, não texto
executável. Errar a spec vira mensagem de erro; errar o SQL viraria um número.

### Por que a spec, e não SQL

Três propriedades que não dependem de o prompt ter dado certo naquela vez:

| Propriedade | Como |
|---|---|
| Nome nunca é interpolado | Tabela e coluna resolvidas contra o catálogo; o que não existe vira erro. O identificador sai citado a partir do nome **do catálogo**, não do que o modelo escreveu |
| Valor nunca entra no texto | Todo valor vira parâmetro vinculado (`$1`, `$2`) |
| A escala é aplicada por código | `media(salario)` vira `avg("salario") / 100.0`, e "acima de 5.000" vira `> $1` com `$1 = 500000` |

A terceira é a que justifica o desenho. A conversão nos **filtros** é a menos óbvia e a
mais perigosa: sem ela, "salário acima de 5.000" compararia 5.000 contra centavos e
traria praticamente a base inteira — uma resposta errada que ninguém questiona, porque
veio muita linha e não nenhuma.

O modelo recebe instrução explícita de escrever valores na unidade natural **mesmo
sabendo** que a coluna está em centavos. Pedir que ele converta seria devolver a ele a
responsabilidade que a camada semântica existe para tirar.

### "Não dá" é resposta

`RespostaDoModelo.pode_responder` existe para o modelo ter como recusar. Sem essa
saída, uma spec obrigatória o forçaria a inventar uma consulta para toda pergunta —
inclusive as que o catálogo não responde, que é onde a invenção causa mais dano.

Toda pergunta grava uma linha em `consultas`, inclusive as recusadas: são o mapa de
onde o dicionário está incompleto.

## O dicionário: três garantias

A proposta da IA passa por três filtros antes de virar catálogo.

**1. O modelo não inventa.** As instruções mandam responder `desconhecida` com
confiança baixa quando a evidência não basta — "não sei" é resposta correta e útil, um
palpite confiante não é.

**2. Alucinação não entra.** Saída estruturada garante o **formato**, não o
**conteúdo**: o modelo pode devolver uma coluna inexistente com schema perfeitamente
válido. `validar_proposta` descarta tabela e coluna que não foram enviadas, e zera
escala fora do plausível (`escala: 7` não descreve banco nenhum) — preferimos não
converter a converter errado.

**3. Proposta é proposta.** Tudo entra com `confianca_ia` e **sem** `revisada_em`. A
plataforma usa a descrição e diz, na resposta, que ninguém revisou ainda. O que já foi
revisado por uma pessoa nunca é sobrescrito — nem para "melhorar".

O campo que justifica o módulo é `descricao_negativa`: a leitura errada mais provável.
"Porte vem do cadastro fiscal e NÃO é faturamento." É o que nenhum schema tem e o que
separa um número certo de um número com cara de certo.

## Reperfilar preserva o julgamento humano

A regra mais importante da persistência (`semantic/persistencia.py`), e a mais fácil de
quebrar sem perceber:

| O que | No reperfilamento |
|---|---|
| Estatística (linhas, cardinalidade, fração nula, amostra) | **sobrescrita** — é o retrato de agora |
| Julgamento (descrição, o que **não** é, unidade, escala, papel revisado) | **preservado** — o que uma pessoa revisou não é tocado |
| Tabela ou coluna que sumiu da origem | **marcada**, não apagada — a procedência de uma consulta antiga precisa continuar apontando para algo |
| Ligação revisada | **preservada** — a inferência roda de novo e chegaria com outra confiança |

Sem isso, alguém revisa o dicionário, escreve que `salario` está em centavos, e o
próximo perfilamento apaga a frase. Nada falha, nada aparece no log — só as respostas
voltam a ficar plausíveis e erradas. O resumo devolve `revisoes_preservadas` para a
tela poder dizer que nada se perdeu.

`versao_dicionario` só sobe quando a **estrutura** muda: ela entra na chave de cache de
toda pergunta, e reperfilar sem novidade não pode invalidar o cache de todo mundo.

## Autenticação

**O login não conta nada a quem não entrou.** Tenant inexistente, e-mail inexistente,
senha errada e usuário desativado devolvem a mesma resposta — e todos executam bcrypt,
inclusive quando não há hash real para comparar. Sem isso, a diferença de tempo entre
"tenant não existe" (microssegundos) e "senha errada" (centenas de milissegundos)
enumeraria clientes e usuários com um cronômetro.

**O login pede o tenant.** O e-mail é único por tenant, não globalmente: a mesma pessoa
pode ser usuária de duas empresas clientes. Procurar o e-mail em todos os tenants
transformaria o login num oráculo de onde cada pessoa trabalha. Na prática o frontend
preenche o campo a partir do subdomínio.

**Renovar rotaciona.** O refresh usado é revogado no ato. Sem isso, um refresh vazado
vale até expirar — inclusive depois do logout e da troca de senha, que é justamente
quando a pessoa acha que resolveu o problema.

**Bloqueio por tentativas**: dez falhas em quinze minutos travam a conta por quinze
minutos, contados da primeira falha e não da última — estender a janela a cada
tentativa deixaria a conta presa indefinidamente sob ataque contínuo, punindo o dono.
A chave no Redis é um hash: o cache não é lugar de cadastro de e-mails de clientes.

Com o Redis fora do ar, o bloqueio **abre**. É escolha consciente: o login é a porta de
entrada da plataforma, a alternativa seria indisponibilidade total por causa do cache,
e a senha continua sendo exigida.

### Dívida conhecida

Trocar a senha **não revoga as sessões existentes**. Fazê-lo exige rastrear todos os
`jti` do usuário, não só o atual. Precisa ser fechado antes do primeiro cliente real:
hoje, quem troca a senha porque desconfia de invasão não expulsa o invasor.

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
├── llm/           cliente da Claude API ✓ (preguiçoso, cacheado, com uso medido)
├── text2sql/      guardrails ✓, executor ✓, spec ✓, catalogo ✓, construtor ✓,
│                  pipeline ✓
├── semantic/      sinais ✓, estrutura ✓, retrato ✓, profiler ✓, persistencia ✓,
│                  dicionario ✓
├── schemas/       conexao ✓
├── chat/          histórico, contexto, streaming                           a construir
└── routers/       health ✓, auth ✓, conexoes ✓, chat ✓
alembic/           migration inicial ✓ (10 tabelas, 19 índices)
scripts/           criar_tenant.py ✓
```

295 testes. A divisão que se repete em todo módulo: **lógica pura e testável, I/O
separado**. Guardrails, avaliação de privilégio, sinais e inferência de estrutura são
funções puras com cobertura exaustiva; conectores e perfilador são coordenação.

## Próximos passos

1. ~~**Perfilador**~~ ✓ — completo: `semantic/profiler.py` monta o retrato,
   `semantic/persistencia.py` grava no catálogo e `routers/conexoes.py` expõe
   `POST /conexoes/{id}/perfilar`.
2. ~~**Gerador de dicionário**~~ ✓ — `semantic/dicionario.py`. Falta só a chave da
   Anthropic: tudo que decide qualidade (prompt, lotes, validação, aplicação) está
   escrito e testado; a chamada é uma linha. Batch API fica para quando houver volume.
3. **Registro de medidas** — o que vira o equivalente genérico dos cubos YAML.
4. **Pipeline de pergunta** (`text2sql/pipeline.py`) — recuperação do contexto relevante,
   spec estruturada via `messages.parse()`, execução vigiada.
5. **Conjunto de avaliação** — ~50 perguntas com resposta conferida. Sem isso a acurácia
   é sentida, não medida. Semente em [CASOS-DIFICEIS.md](CASOS-DIFICEIS.md).

### A divisão de trabalho com o LLM

O perfilador entrega **evidência**, não julgamento: `salario` é `bigint`, tem 4,2
milhões de valores distintos e magnitude 10. Que isso significa **centavos** é
conclusão do modelo — dez bilhões de reais de salário mensal não existe, cem milhões
de centavos é um salário alto porém plausível.

A linha está aí de propósito. Aritmética sobre estatística é nossa: determinística,
barata e testável. Conhecimento de mundo é do modelo. Escrever "salário costuma vir em
centavos" no perfilador seria embutir a regra do Leads num produto que precisa
funcionar em banco que ninguém nunca viu.
