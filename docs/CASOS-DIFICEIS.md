# Casos difíceis: o conjunto de avaliação

Estado em 29/09/2026. Semente do conjunto de avaliação — a meta é ~50 perguntas com
resposta conferida antes do fim da Fase 0.

## Para que serve

Acurácia de Text-to-SQL não se sente, se mede. Sem este conjunto, "está funcionando
bem" quer dizer "as três perguntas que eu testei deram certo". Toda mudança de prompt,
de modelo ou de recuperação de contexto roda contra ele antes de entrar.

Cada caso tem: a pergunta, a armadilha, e **como saber que a resposta está errada** —
essa última coluna é o que faz o caso valer, porque o erro aqui é sempre plausível.

## Por que o banco do Leads é o melhor primeiro teste

Não porque seja nosso. Porque é **hostil da forma certa**: dados públicos reais, 73
milhões de estabelecimentos, 293 milhões de movimentações de emprego, e um conjunto de
armadilhas semânticas que nenhum schema revela. Se a plataforma descobre essas
armadilhas sozinha, num banco que ela nunca viu, ela funciona em banco de cliente.

O que a plataforma **não** pode fazer é receber essas regras de nós. Elas são o gabarito,
não a entrada.

## Armadilhas conhecidas — `trabalho.caged`

Medidas contra `LEADS_UAI/backend/app/trabalho_medidas.py` em 29/09/2026.

Pergunta-gabarito: **"Qual o salário mediano de desenvolvedores em Uberlândia?"**

| # | Armadilha | O erro que produz | Como detectar |
|---|---|---|---|
| 1 | `salario` em **centavos** | valor 100× maior | mediana na casa de centenas de milhares de reais |
| 2 | `unidade_salario` mistura hora/dia/semana/mês | soma grandezas diferentes | distribuição bimodal; só 91,8% é mensal (código 5) |
| 3 | `tipo_movimentacao` 70 e 80 são **transferências** | conta transferência como contratação | total de admissões acima do saldo declarado |
| 4 | `peso = -1` é evento **cancelado** | inclui o que não aconteceu | contagem maior que a soma dos pesos |
| 5 | outliers até R$ 100 mi/mês | média destruída por uma linha | média muito acima da mediana |
| 6 | célula mínima de 5 movimentações | **expõe pessoas** | resposta com base de 1 a 4 registros |
| 7 | sem recorte de município/UF | varre 353 milhões de linhas | `EXPLAIN` acima do teto |

As armadilhas 1, 2 e 5 são detectáveis por `pg_stats` — é exatamente para isso que
`connectors/postgres.py` coleta `most_common_vals` e `histogram_bounds`. As 3, 4 e 6
exigem entender a semântica do domínio; são o teste real do gerador de dicionário.

A 7 é a única que os guardrails pegam sozinhos, sem dicionário nenhum.

## Armadilhas conhecidas — outras bases

| Base | Armadilha | O que não dá para afirmar |
|---|---|---|
| `gold.estabelecimentos` | `porte` vem da Receita | **não** é faturamento nem número de funcionários |
| `gold.estabelecimentos` | `capital_social` é o declarado no contrato | **não** é patrimônio nem receita |
| `gold.estabelecimentos` | `data_inicio_atividade` é do estabelecimento | **não** é a fundação da empresa |
| `risco.sancoes` | sanção é de um CNPJ específico | **não** implica culpa além do registrado, nem impede todo contrato |
| `governo.contratos` | valor é o contratado | **não** é o pago |
| `silver.socios` | CPF vem mascarado pela Receita | seis dígitos **não** identificam ninguém sozinhos |

Fonte: `LEADS_UAI/docs/FONTES-DADOS.md`, que documenta coluna a coluna o que cada
fonte significa **e o que não significa**. É o modelo do que o campo
`descricao_negativa` do nosso catálogo deve conter.

## Formato de um caso

```yaml
- id: caged-salario-mediano-uberlandia
  pergunta: "Qual o salário mediano de desenvolvedores em Uberlândia?"
  conexao: leads
  armadilhas: [centavos, unidade_salario, celula_minima]
  resposta_esperada:
    tipo: valor_unico
    faixa: [2800, 4200]          # reais mensais; faixa, não valor exato
    unidade: BRL/mes
  exige:
    - supressao_declarada        # a resposta precisa dizer se houve supressão
    - recorte_municipio
  reprova_se:
    - valor > 100000             # sinal claro de centavos não convertidos
    - base_de_calculo < 5        # violou a célula mínima
```

## Estado

| Domínio | Casos escritos | Meta |
|---|---|---|
| `trabalho.caged` | 0 | 15 |
| `gold.estabelecimentos` | 0 | 15 |
| `governo.contratos` | 0 | 10 |
| `risco.*` | 0 | 5 |
| Multi-tenant (isolamento) | 0 | 5 |

Os casos de isolamento são os mais importantes e os mais fáceis de esquecer: perguntas
que **tentam** alcançar dado de outro tenant e precisam falhar.
