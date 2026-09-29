# Infraestrutura: provisionar e operar

Estado em 29/09/2026. Serve para dois momentos: subir a VPS de desenvolvimento no
servidor da UAISmart e, depois, migrar para a VPS dedicada.

## A pergunta que define o tamanho da máquina

**A plataforma é um *control plane*, não um *data plane*.** Ela guarda metadados —
tenants, conexões, catálogo, chat, procedência — e as consultas analíticas executam
**no banco do cliente**. O dado pesado nunca mora aqui.

Isso é o que mantém a máquina pequena e o custo baixo. Se em algum momento decidirmos
hospedar dado analítico de cliente, esta conta inteira muda: a máquina do Leads, com
73 e 293 milhões de linhas, roda com 24 GB de RAM, `shm_size` de 4 GB e SSD dedicado.
São ordens de grandeza diferentes.

## Especificação

| | Para começar | **Recomendado** |
|---|---|---|
| vCPU | 4 | **8** |
| RAM | 8 GB | **16 GB** |
| Disco | 80 GB NVMe | **160 GB NVMe** |
| Swap | 2 GB | **4 GB** |
| SO | Ubuntu Server LTS | Ubuntu Server LTS |

Vá direto para a coluna recomendada. A diferença de mensalidade é pequena; o caro é
migrar no meio do caminho, com conexão de cliente já cadastrada e credencial cifrada
para transportar. Isso atende ~30–50 tenants com uso real.

**NVMe, não SATA.** O gargalo do Postgres de metadados é escrita de procedência — uma
linha por consulta, sempre —, que é I/O aleatório.

## Onde a RAM vai

| Componente | Consumo | Observação |
|---|---|---|
| Resultados em voo | ~30 MB × consultas simultâneas | 10 mil linhas × ~3 KB como objeto Python |
| API (uvicorn, 4 workers) | ~1 GB | |
| Postgres de metadados | ~4 GB | `shared_buffers` 1 GB + cache do SO |
| Redis | 2 GB | com `maxmemory` definido |
| Next.js (SSR) | ~500 MB | |
| Pools para bancos de clientes | ~2 MB × 4 × conexões ativas | ver *Limite de pools* abaixo |
| SO + Docker | ~1 GB | |

O teto de 30 MB por consulta só vale porque a execução usa cursor
(`connectors/postgres.py`): o consumo é fixo em `max_result_rows + 1` linhas,
qualquer que seja o tamanho do resultado. Sem isso, uma agregação de alta
cardinalidade traz milhões de linhas para a memória e a conta deixa de existir.

**Limite de pools.** Cada conexão de cliente ativa mantém um pool de até 4 conexões.
Com muitos tenants ativos ao mesmo tempo isso soma. Quando passarmos de ~50 conexões
simultâneas, os pools precisam de cache LRU com fechamento das conexões ociosas —
ainda não implementado.

## CPU

Carga ligada a I/O: a maior parte do tempo é espera pelo banco do cliente e pela
Claude API. O que consome CPU de verdade: `bcrypt` no login (caro de propósito),
serialização JSON de resultados grandes e o SSR do Next.js. 8 vCPU sobra.

---

## Configuração do sistema

### Swap

```bash
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
sudo sysctl -w vm.swappiness=10 && echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf
```

Não é para rodar em swap. É para o kernel ter margem antes de invocar o OOM killer,
que escolhe o processo de maior memória — e esse é sempre o Postgres.

### Rotação de log do Docker

Sem isto, `/var/lib/docker` enche e derruba tudo de uma vez. Em
`/etc/docker/daemon.json`:

```json
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "3" }
}
```

```bash
sudo systemctl restart docker
```

### Firewall

```bash
sudo ufw default deny incoming && sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow 443/tcp
sudo ufw enable
```

Nada mais entra. O `docker-compose.yml` já publica Postgres, Redis e a API apenas em
`127.0.0.1` — quem fala com eles é o Nginx do host.

---

## Postgres de metadados — tuning para 16 GB

Numa máquina compartilhada com API, Redis e Next, o Postgres recebe ~4 GB. Em
`postgresql.conf` (ou como `command:` no compose):

```conf
shared_buffers = 1GB                  # 25% do que cabe ao Postgres; o resto é cache do SO
effective_cache_size = 4GB            # dica ao planejador, não alocação
work_mem = 16MB                       # ver o aviso abaixo
maintenance_work_mem = 256MB
max_connections = 100

# NVMe: o padrão 4.0 assume disco que gira e faz o planejador evitar índice sem motivo.
random_page_cost = 1.1
effective_io_concurrency = 200

wal_buffers = 16MB
max_wal_size = 2GB
checkpoint_completion_target = 0.9

log_min_duration_statement = 1000     # registra consulta acima de 1s
timezone = 'America/Sao_Paulo'
```

> **`work_mem` é por operação de ordenação, por conexão** — não por servidor. Uma
> consulta com três `sort` em 50 conexões pode reservar `50 × 3 × 16MB = 2,4 GB`. É a
> forma mais comum de estourar a memória de um Postgres "bem configurado". 16 MB é
> conservador de propósito; só aumente medindo.

## Redis — a decisão que não é óbvia

O Redis guarda **duas coisas de naturezas diferentes**: cache de resultado
(descartável) e blacklist de JWT (controle de segurança). Isso proíbe a configuração
que qualquer tutorial recomendaria:

```conf
maxmemory 2gb
maxmemory-policy noeviction     # NÃO use allkeys-lru
appendonly yes
```

Com `allkeys-lru`, o Redis cheio começa a descartar as chaves menos usadas — e uma
delas pode ser a entrada que marca um token como revogado. O efeito é um token
revogado voltando a valer, em silêncio, por pressão de memória. Um logout que não
desloga.

Com `noeviction`, o Redis cheio recusa escrita e a falha aparece nos logs. **Falhar
alto é melhor que falhar em silêncio quando o que está em jogo é um controle de
segurança.** Todas as entradas de cache têm TTL, então o crescimento é limitado; se
ainda assim encher, é sinal para separar em duas instâncias.

---

## Usuário somente-leitura no banco do cliente

A plataforma nunca escreve no banco do cliente. A garantia tem que ser do Postgres
dele, não do nosso código. No banco do cliente:

```sql
CREATE ROLE nextgen_leitor LOGIN PASSWORD 'gere-uma-senha-forte';

GRANT CONNECT ON DATABASE o_banco TO nextgen_leitor;
GRANT USAGE ON SCHEMA public TO nextgen_leitor;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO nextgen_leitor;

-- Sem isto, tabela criada depois fica invisível para a plataforma.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO nextgen_leitor;

-- As duas linhas que fazem a garantia ser do banco:
ALTER ROLE nextgen_leitor SET default_transaction_read_only = on;
ALTER ROLE nextgen_leitor SET statement_timeout = '30s';
```

Repita os três `GRANT` para cada esquema que a plataforma deve enxergar.

> **Lacuna conhecida, a corrigir.** O `testar()` de `connectors/postgres.py` confere
> `SHOW transaction_read_only` — mas a própria conexão já envia
> `default_transaction_read_only=on` em `server_settings`, então a resposta é `on`
> mesmo para um superusuário. A checagem hoje valida a nossa configuração, não o
> privilégio do papel: é uma tautologia. Precisa passar a recusar `is_superuser` e a
> conferir ausência de `INSERT` via `has_table_privilege`. Até lá, o `GRANT` acima é
> a única garantia real — aplique-o.

## Rede até o Postgres do Leads

O Postgres do Leads publica em `127.0.0.1:5432` (`LEADS_UAI/docker-compose.yml`):
não é alcançável de outra máquina. Se a VPS do NextGen for separada:

- **WireGuard ou Tailscale** entre as duas, e a plataforma conecta pelo IP da rede
  privada. Preferível: não expõe porta de banco à internet em momento nenhum.
- **Túnel SSH** resolve para desenvolvimento, mas cai e não reconecta sozinho.
- **Não** abra a 5432 no firewall com IP na lista branca. Postgres exposto à internet
  é varrido em minutos.

---

## Retenção e backup

### `consultas` cresce e nunca encolhe

É o registro de procedência: uma linha por consulta, ~4 KB. A 10 mil consultas/dia
são ~15 GB/ano. Política sugerida:

| Idade | O que guardar |
|---|---|
| até 90 dias | linha completa (spec, SQL, estimativas) |
| depois | agregado diário por tenant e conexão; descarta `spec` e `sql_gerado` |

As recusadas seguem a mesma regra, mas vale guardar por mais tempo: são o mapa de onde
o dicionário está incompleto.

### Backup

```bash
docker compose exec -T postgres pg_dump -U nextgen nextgen_bi | gzip > bi_$(date +%F).sql.gz
```

Duas coisas precisam de backup, **em lugares separados**:

1. **O banco de metadados** — catálogo, conexões, histórico.
2. **A `CREDENTIALS_KEY`** do `.env` — cifra as credenciais dos bancos de clientes.
   Perdê-la torna ilegível toda conexão já cadastrada, e o dump do banco não ajuda.

Separados de propósito: juntos, um único vazamento entrega as credenciais de todos os
bancos de todos os clientes.

## O que monitorar

| Sinal | Limite | Por quê |
|---|---|---|
| Disco livre | < 20% | `consultas` e log de Docker crescem sozinhos |
| Memória do Redis | > 80% de `maxmemory` | com `noeviction`, cheio = escrita recusada |
| `consultas` com status `recusada` | tendência de alta | dicionário incompleto, não ataque |
| Conexões no Postgres | > 80 de 100 | vazamento de pool |
| Latência p95 do chat | — | separa lentidão de LLM de lentidão de banco |

---

## Migração para a VPS dedicada

Ordem que evita janela de indisponibilidade com dado de cliente em jogo:

1. Provisionar a nova com a mesma especificação e o mesmo `docker-compose.yml`.
2. Copiar a `CREDENTIALS_KEY` **antes** de restaurar o dump. Sem ela, as conexões
   restauradas são lixo cifrado.
3. Restaurar o dump do Postgres de metadados.
4. Refazer o túnel de rede até os bancos de clientes — a nova máquina tem outro IP, e
   é provável que haja lista branca do lado do cliente.
5. Testar cada conexão cadastrada (`POST /api/v1/conexoes/{id}/testar`) antes de virar
   o DNS.
6. Virar o DNS. Manter a máquina antiga de pé por uma semana.

O passo 4 é o que costuma ser esquecido e o que trava a virada.
