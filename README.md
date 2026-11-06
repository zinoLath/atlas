# ATLAS

CLI Python 3.12+ **só com stdlib** para capturar demandas, triar, classificar
e operar uma base de conhecimento em árvore com digest determinístico.
Sem LLM no runtime; a única rede opcional é um reader IMAP read-only
(`ingest email`) e o envio por signal-cli.

## Começando

Requisito: Python 3.12+ (somente biblioteca padrão).

```bash
git clone https://github.com/zinoLath/atlas.git
cd atlas
python3 -m unittest discover -s tests    # suíte completa, sem instalar nada
python3 examples/seed_demo.py            # seed de demonstração idempotente
atlas --help                             # CLI (ou: python3 -m atlas_cli.cli)
```

Licença MIT — veja `LICENSE`.

## Princípio

- **Canônico:** `state/*.jsonl` (um objeto por linha, chaves ordenadas,
  escrita atômica tmp+rename).
- **Derivado:** `state/hub.db` (SQLite/FTS5) e os `.md` (frontmatter gerado +
  corpo preservado).
- `state/transactions.jsonl` é o log autoritativo: toda escrita de estado é
  uma transação sob `flock` (ver "Transações"). `atlas rebuild` = replay de
  todas as transações a partir de zero, depois SQLite + vistas markdown. O
  corpo markdown existente é preservado (split no segundo `---`).
- Todo comando aceita `--now ISO8601` para execução determinística.

## Layout

```
bin/atlas                      executável (sem venv)
bin/atlasd                     daemon residente (escritor único)
atlas_cli/                     cli, daemon, store, states, policy, kb, digest, outbox, notifications, signal, bulletin, render, context, email_parse, ingest, control, health, backup, util
policy/v1.json                 lanes, digest, orçamento, forbidden_auto
areas/<area>/CHARTER.md        contrato de cada área
kb/<id>.md                     id = caminho relativo sem .md
demands/<id>.md                vista da demanda
state/events.jsonl             eventos de transição (append-only)
state/demands.jsonl            snapshot canônico
state/kb.jsonl                 metadados dos nós
state/edges.jsonl              arestas
state/digests.jsonl            histórico de digests
state/notifications.jsonl      outbox: notificações (canônico, aditivo)
state/triggers.jsonl           eventos de entrada (idempotente por source+external_id)
state/threads.jsonl            threads (chave source+external_id do thread_key)
state/subscriptions.jsonl      ligação thread <-> demanda (muitos-para-muitos)
state/deliveries.jsonl         entrega por (trigger, demanda): claim/lease/ack
router/rules.v1.json           fast path determinístico (ignore/route)
raw/blobs/XX/<sha256>.txt      corpo bruto do trigger (content-addressed)
state/transactions.jsonl       log transacional (append-only, autoritativo)
state/applied.json             última transação aplicada aos snapshots
state/schema_version           versão do layout (2)
state/.lock                    arquivo de flock
state/atlasd.sock              socket unix do daemon (modo 0600)
state/atlasd.socket            caminho absoluto do socket em uso (ponteiro)
state/atlasd.pid               pid do daemon (efêmero)
state/atlasd.log               log do daemon (rotativo simples)
state/cursor.json              último digest (cursor do rollover)
state/control.json             pausa operacional (paused/reason/updated_at)
backups/atlas-backup-*.tar.gz  backup do estado (modo 0700; manifest .json ao lado)
state/hub.db                   SQLite materializado (descartável)
INDEX.md                       índice navegável da KB (gerado por `kb index`)
tests/test_m0.py               suíte unittest
tests/test_m1b0a.py            triggers, threads, subscriptions e fast path
tests/test_m1b0b.py            context pack (Inline & Spill / Chain of Recall)
examples/seed_demo.py          demo idempotente
```

`ATLAS_HOME` muda a raiz (usado pelos testes e pelo seed).

## Comandos

```
bin/atlas [--now ISO] init
bin/atlas add --area projetos --title T [--due ISO] [--importance 1-5] ...
bin/atlas list [--state ready] [--area projetos]
bin/atlas show <id>
bin/atlas transition <id> <to> [--reason R] [--result R] [--owner O --lease ISO]
    [--artifact A] [--waiting-on W --follow-up-at ISO] [--wake-at ISO]
    [--expected-epoch N]
bin/atlas classify <id> [flags de classificação]
bin/atlas expire                       # leases vencidos + snoozed com wake_at
bin/atlas events [<id>]                # inclui o tx_id de origem
bin/atlas rebuild                      # replay das transações + vistas
bin/atlas migrate                      # estado v1 -> schema 2
bin/atlas doctor                       # saúde mínima do estado
bin/atlas pause [--reason TEXTO]       # bloqueia readers e outbox --retry
bin/atlas resume
bin/atlas health [--json]              # exit 1 se algum check for error
bin/atlas backup [--out DIR] [--keep 7]
bin/atlas watchdog [--repair] [--json] # liveness/progresso (determinístico)
bin/atlas q "termo" [--area projetos] [--limit 5]
bin/atlas bulletin
bin/atlas digest [--force] [--dry-run]
bin/atlas outbox [--retry] [--force] [--limit N] [--sender stdout|signal]
bin/atlas kb add --parent <id|""> --kind K --title T --summary S [--body-file F]
bin/atlas kb link <src> <rel> <dst> [--weight 0.8] [--note "..."]
bin/atlas kb tree <id>
bin/atlas kb path <id>
bin/atlas kb related <id>
bin/atlas kb check
bin/atlas kb index
bin/atlas kb dup
bin/atlas trigger add --source email --external-id ID --title T [--thread-key K]
    [--actor A] [--kind K] [--body T | --body-file F] [--received-at ISO]
    [--label L]... [--trusted]         # --trusted só com --source manual
bin/atlas trigger list [--state S] [--source S] [--thread thr] [--limit 20] [--json]
bin/atlas trigger route [--limit 100] [--dry-run] [--explain]
bin/atlas trigger transition <trg> <estado>
bin/atlas subscribe <dmd> <thr> [--role owner|watcher]
bin/atlas unsubscribe <dmd> <thr>
bin/atlas delivery list [--state S] [--demand dmd] [--trigger trg] [--limit 20] [--json]
bin/atlas delivery claim <dlv> --owner O [--lease-minutes 30]
bin/atlas delivery ack <dlv> --owner O --epoch N --report R [--summary "..."]
bin/atlas delivery fail <dlv> --error "..." [--owner O --epoch N]
bin/atlas delivery retry <dlv>
bin/atlas delivery expire
bin/atlas thread list [--open] [--limit 20]
bin/atlas context <dmd> [--budget 8000] [--expand ID] [--json]
bin/atlas daemon status                # socket, pid e resposta a um ping
bin/atlas daemon stop                  # pede shutdown e espera o socket sumir
bin/atlas daemon start                 # não spawna: use bin/atlasd (systemd)
bin/atlasd [--home DIR] [--socket PATH] # daemon em foreground
```

## Triggers, threads e fast path

O reader grava um `Trigger` (`new`), nunca uma demanda. `atlas trigger add`
cria a thread pelo `thread_key` (default `<source>:<external_id>`) e é
idempotente por `(source, external_id)`. O corpo é gravado antes do commit
em `raw/blobs/XX/<sha256>.txt` (content-addressed, `body_sha256` no trigger);
um crash entre blobs e trigger deixa apenas um blob órfão, nunca um trigger
sem corpo. `atlas doctor` acusa corpo ausente/adulterado e repara o ponteiro
quando o mesmo `external_id` chega de novo.

O caminho de biblioteca é `atlas_cli.ingest.add_trigger(store, spec, actor=...)`:
recebe o `trigger` do parser M1b.1a (`source`, `external_id`, `thread_key`,
`title`, `body`, `actor`, `kind`, `received_at`, `meta`, `labels`, `untrusted`,
`trusted`) e devolve o trigger gravado — com `repaired=True` quando o corpo
foi reconectado. `meta` carrega só metadado do reader (`shadow`, `account`,
`mailbox`, `uidvalidity`, `uid`, `message_key`, `occurrences`, `headers`,
`labels`) e nunca campos de identidade do trigger. O CLI é um adaptador fino
sobre essa função; não há subprocess por mensagem.

`atlas trigger route` processa os `new` em ordem de `received_at, id`:

1. thread com subscription ativa (e demanda não terminal) → `routed` com
   `route.by=subscription`, os `demand_ids` ativos e uma `Delivery` `pending`
   por demanda em `route.delivery_ids`; demanda `done`/`dropped` é ignorada
   e não impede a regra normal;
2. senão aplica `router/rules.v1.json` (primeira regra que casar; `priority`
   opcional): `ignore` → `ignored`, `route` → `routed` com área válida; nos
   dois casos a regra vencedora fica em `route.by=rule` e `route.rule`;
3. nada casou → `default` (`candidate` ou `ignore`; nunca `route`).

`ignore` é opt-in: o default é `candidate`, e o `DEFAULT_RULES` não descarta
remetente (nada de `no-reply` ⇒ `ignored`). Para descartar, escreva uma regra
explícita com `action: "ignore"` em `router/rules.v1.json`.

Estados: `new -> candidate|ignored|routed`; `candidate -> routed|ignored|
needs_human`; `routed -> processed|skipped|needs_human`; o resto é terminal.
Transição fora da tabela sai com exit 2. `atlas subscribe` só aceita demanda
ativa; `unsubscribe` grava `active=false`. Quando a demanda vira `done` ou
`dropped`, a mesma transação grava `active=false` (+`updated_at`) em todas as
subscriptions dela e `state=skipped`/`error=demand_terminal` nas deliveries
`pending|delivered|failed`, então doctor e watchdog não veem inscrição ativa
terminal. Não há transição de saída de `done`/`dropped`, e nada é reativado
automaticamente. Nenhuma chamada de modelo acontece nesse caminho.

## Entregas por subscriber (`delivery`)

`route.demand_ids` não é entrega: com dois subscribers um pode concluir e o
outro morrer, e um único `trigger.state` não diz quem recebeu o quê. Por isso
o roteamento cria uma `Delivery` em `state/deliveries.jsonl` por
`(trigger, demand)` — chave única `(trigger_id, demand_id)` — com
`role` (`owner`/`watcher`), `state`
(`pending|delivered|acked|failed|skipped`), `attempts`, `lease_owner`,
`lease_until`, `lease_epoch`, `report_id` e `error`. O trigger continua sendo
a entrada; só `by=subscription` cria delivery (`ignore`/`candidate` e regras
`route` deixam `delivery_ids=[]`).

Ciclo: `pending -> delivered` (`claim`) `-> acked` (`ack`) ou `failed`;
`failed -> pending` (`retry`). `skipped` é terminal e vem da demanda:
`transition <dmd> done|dropped` marca as deliveries abertas da demanda
(`pending|delivered|failed`) como `skipped` com `error=demand_terminal` na
mesma transação. Toda transição inválida sai com exit 2, não grava nada e a
delivery fica como estava.

- `claim <dlv> --owner O [--lease-minutes 30]`: só `pending`/`failed`; grava
  `delivered`, lease até `now + minutos`, `lease_epoch += 1`, `attempts += 1`
  e imprime o epoch.
- `ack <dlv> --owner O --epoch N --report R [--summary ...]`: exige
  `delivered`, o mesmo `lease_owner` e o mesmo `lease_epoch` (fencing); grava
  `acked`, `report_id`, limpa lease e erro. Repetir com o mesmo `--report` é
  idempotente; com outro report é erro. `--summary`, quando informado, é
  gravado em `summary` na delivery (campo aditivo até existir a entidade
  `Report`).
- `fail <dlv> --error "..." [--owner O --epoch N]`: `pending`/`delivered` →
  `failed`, `attempts += 1`, limpa lease; owner/epoch, quando informados, também
  são exigidos iguais.
- `retry <dlv>`: `failed` → `pending`, limpando lease e erro.
- `expire`: `delivered` com `lease_until <= now` → `failed` com
  `error=lease_expired`; respeita `--now`.

Um trigger roteado por subscription cria as deliveries na mesma transação do
`route`; rodar de novo não duplica (`trigger` já saiu de `new` e a chave única
protege). `--dry-run` não cria delivery. `atlas doctor` acusa delivery órfã,
duplicata, `acked` sem `report_id`, `delivered` sem lease, `pending`/`failed`
com lease, subscription ativa para demanda `done`/`dropped` e trigger `routed`
por subscription sem `delivery_ids` (triggers do M1b.0a caem nesta última até
serem reprocessados).

## Context pack (Inline & Spill)

`atlas context <dmd>` monta o pacote que o Worker recebe: IDs, resumos e
ponteiros, nunca o estado inteiro. É somente leitura — não grava transação,
não chama modelo e não abre `secrets.env`.

Ordem de montagem: `demand` (sempre, título completo), `threads` (subscriptions
ativas; últimos 3 triggers por `received_at, id`, com `body_path`), `kb`
(primeiro `links.kb` na ordem do link, depois `kb.search` na área; dedupe,
máximo 5, resumo de 200 chars, `relation` = `link`/`fts`/`edge`), `siblings`
(ativas da mesma área, `due` nulo por último, `updated_at` desc, máximo 3) e
`spill` (corpo do trigger, `kb/<id>.md`, `demands/<dmd>.md`).

`--budget` é em caracteres do markdown renderizado (default 8000). Ao estourar,
corta `siblings`, depois reduz `kb` a 3, depois resume títulos/trechos; o
cabeçalho e a seção `spill` nunca são cortados, e o pacote marca
`budget.truncated=true` com o que foi omitido em `budget.omitted`.

`--expand ID` ignora o orçamento e imprime o objeto (limite de 20 000 bytes,
com aviso e ponteiro quando trunca): `trg-...` metadados + corpo bruto,
`thr-...` todos os triggers, `dmd-...` o JSON da demanda + o `.md`, e um nó de
KB (`projetos/...`) o `.md` ou o JSON do nó. ID desconhecido sai com exit 2.
Mesma entrada → mesmo output byte a byte.

## Modelo de demandas

ID `dmd-YYYYMMDD-NNNN`, sequencial por dia, derivado de `--now`.

```
new -> routed -> triaged -> planned -> ready -> running -> done
                     |          |         |     ^   |  |
                     v          v         v     |   v  v
                waiting_user  dropped  snoozed | waiting_review
                     |                          |      |
                     +---- done                 |      +-> ready (rejeição)
                                                |
                              waiting_external -+-> ready (follow-up)
```

`running` exige `--owner` e `--lease`; `done` exige `--result`; `dropped`
exige `--reason`; `waiting_review` exige `--artifact`; `waiting_external`
exige `--waiting-on` e `--follow-up-at`; `snoozed` exige `--wake-at`.
Transição fora da tabela sai com **exit 2** e não grava. `atlas expire`
devolve `running` com lease vencido e acorda `snoozed` com `wake_at <= now`
(actor `system`).

`lease_epoch` é monotônico por demanda: adquirir um lease e revogá-lo
(`lease_expired`) incrementam o epoch. `--expected-epoch N` (CAS) rejeita
com exit 2 qualquer transição feita com epoch antigo.

## Política e lanes

`policy/v1.json` mapeia `agent_level` → lane (`observe`, `auto_flash`,
`flash_review`, `approve_premium`, `human`). `compute_lane` sobe (nunca
desce) para `human` se `responsibility == "me"` ou `confidence < 0.6`, e para
no mínimo `approve_premium` se `importance == 5` e `difficulty >= 4`.

## KB

- ID é o caminho relativo sem `.md`; o pai é o diretório. Raiz de área só com
  `--parent ""` e slug igual a uma área.
- Arestas têm peso (default 0.5). `kb check` valida pai existente, id ==
  caminho, summary, órfãs, self-edge, duplicata `(src, dst, rel)` e arquivo
  `kb/<id>.md` ausente; sai 1.
- `kb index` reconstrói o FTS em `state/hub.db` **e** grava `INDEX.md` na
  raiz: uma seção por área na ordem de `policy.AREAS`, árvore indentada por
  id, contagem por área ("N nós"), e cada linha `- [title](kb/<id>.md) —
  summary` com resumo truncado em 120 chars. Mesmo `--now` e mesmo estado ⇒
  bytes idênticos.
- Busca: FTS5 top-20 por bm25, expande 1 salto por arestas `weight >= 0.3`
  (top-12) e inclui o pai dos acertos; score
  `-bm25 + 0.5*weight + 0.1*importance` (vizinho sem bm25 usa 0). Acertos
  diretos por FTS são listados antes das expansões; dentro de cada grupo,
  ordena por score e id.

## Digest, outbox e rollover

- Candidatos (ativos, exceto `done`/`dropped`/`failed`; `snoozed` só com
  `wake_at <= now`):
  - `changed`: `revision > last_notified_revision` (criação, transição,
    classificação e wake incrementam `revision`);
  - `reminder`: `next_notify_at <= now`;
  - `waiting`: `waiting_user` e `changed`;
  - `due_soon`: `due <= now + 24h` e `changed`.
- Score: `2*importance + urgency + 3*(waiting_user) + 2*(due <= 24h) + 1*(blocked)`.
  Ordena por score desc e id; os 12 primeiros são `displayed`, o resto
  `hidden` (aparecem como "não exibidos" e **não** são marcados reportados).
- Decisão: sem candidatos e sem briefing → não grava; briefing
  (`hour >= briefing_hour`, sem briefing hoje) ou `--force` → envia; score
  máximo abaixo do threshold **e** item mais antigo com menos de
  `rollover_max_hours` → `status=rollover` (incrementa
  `rollover.digest_count`, não avança o cursor); senão envia.
- Envio em duas transações: grava `notifications.jsonl` `pending` **antes**
  de chamar o sender (default: imprime no stdout e devolve
  `stdout:<digest_id>`) e depois grava `sent`/`failed`/`delivery_unknown`.
  Só `sent` avança `cursor.last_digest_at` e marca as demandas exibidas
  (`reported_at`, `last_digest_id`, `last_notified_revision`,
  `report_count += 1`, `next_notify_at = now + backoff`).
- Backoff: 24h, 72h, 7d, limitado a `due - 1h`. `next_notify_at` de uma
  demanda nova é `due - 24h` (ou null).
- `--dry-run` imprime a mensagem e não grava; `--force` ignora threshold.
- `atlas outbox` lista as notificações recentes; `--retry` reenvia
  `pending|failed` com o mesmo `digest_id`/payload e `--retry --force`
  também reenvia `delivery_unknown` (pode duplicar). Uma notificação
  `pending` com `attempts > 0` (kill entre o envio e o registro) vira
  `delivery_unknown` na próxima execução.
- `bulletin` lê `notifications.jsonl` em ÚLTIMOS DIGESTS, usa
  `reported_at IS NULL` em PRÓXIMOS e `status=rollover`/`digest_count > 0`
  em ROLLOVER; é byte-idêntico para o mesmo `--now`.

### Signal

`atlas outbox --retry --sender signal` envia pelo `signal-cli` (note-to-self
por padrão). Configuração em `~/.config/atlas/secrets.env` (ou ambiente, que
vence o arquivo):

```
ATLAS_SIGNAL_NUMBER=+55...          # obrigatório (número registrado)
ATLAS_SIGNAL_TO=+55...              # opcional; ausente = note-to-self
ATLAS_SIGNAL_CLI=signal-cli         # default
ATLAS_SIGNAL_CONFIG=~/.local/share/signal-cli   # default
```

O adaptador chama `signal-cli -a NUMBER --config DIR send --note-to-self -m
MSG` (ou `... send -m MSG TO`) sem shell, com timeout de 60 s. Falha do
binário, registro ausente ou `rc != 0` vira `failed` na notificação, com
stderr redigido e truncado — a mensagem e o número nunca aparecem no erro.
Timeout é entrega **ambígua** (o processo pode ter enviado antes de estourar):
vira `delivery_unknown` e só é reenviado com `outbox --retry --force`. Sem
`ATLAS_SIGNAL_NUMBER` o comando não explode: cada notificação fica `failed`
com o motivo e continua disponível para retry.

Instalação/registro do `signal-cli` no sera é **pendência do usuário**
(`signal-cli -a NUMBER register` / `link`); depois de registrar, teste com:

```
ATLAS_SIGNAL_NUMBER=+55... bin/atlas outbox --retry --sender signal
```

O argv e o mapeamento de erros são cobertos por `tests/test_m1c3.py` com
`subprocess.run` mockado; nenhum teste envia mensagem real.

## Watchdog

`atlas watchdog` observa o estado canônico, sem LLM e sem rede, e lista
problemas em ordem fixa com `code`, `severity`, `subject` e `message`:

| code | severidade | critério |
|---|---|---|
| `doctor` | critical | um item por problema do `doctor.check` |
| `lease_expired` | warn | `running` com `lease_until <= now` |
| `stale_running` | warn | `running` sem evento há mais de 20 min (ou sem evento) |
| `notification_stuck` | warn | `pending|failed|delivery_unknown` há mais de 2 h |
| `candidate_backlog` | warn/critical | trigger `candidate` há mais de 24 h; critical acima de 100 |
| `demand_no_lane` | warn | demanda ativa com `lane` vazio ou nulo |
| `kb_stub` | warn | nó de KB sem `summary` (no máximo 10 linhas + resumo com o total) |
| `digest_stale` | warn | `last_digest_at` nulo/mais de 2 h com demanda ativa |
| `disk_low` | critical | menos de 1 GiB livre em `$ATLAS_HOME` |
| `paused` | warn | `state/control.json` com `paused=true` |

Sem `--repair` só relata: exit 1 se houver `critical`, 0 caso contrário.
`--repair` aplica apenas `expire` (leases vencidos + `snoozed` com
`wake_at`) via transação `watchdog.repair` com `actor=system`, re-renderiza
as demandas afetadas e depois relata o estado resultante; nada mais é
reparado. `--json` imprime a lista completa (determinística). O BULLETIN
usa as primeiras 8 linhas em `## SAÚDE` (ou `- ok`).

## Controle operacional, saúde e backup

`atlas pause [--reason T]` grava `state/control.json`
(`{"paused": true, "reason": T, "updated_at": ISO}`) e `atlas resume`
desfaz. A pausa é operacional, não criptográfica: `health`, `backup`,
`resume` e os demais comandos manuais continuam funcionando; param apenas
`ingest email` (antes do `SELECT`) e `outbox --retry`, ambos com
`atlas pausado: <ação> bloqueado (<motivo>)` e exit 2.

`atlas health` lista seis checks em ordem fixa — `control`, `cursors`
(`last_success_at` — ou `last_seen_at` legado — há mais de 2 h, sem sucesso
desde o último `last_attempt_at`, tentativa mais nova que o último sucesso,
cursor sem poll ou `consecutive_errors > 0`),
`outbox`
(`failed`/`delivery_unknown` = error; `pending` há mais de 24 h = warn),
`deliveries` (lease vencido, `failed`), `backup` (nenhum ou mais de 48 h) e
`disk` (menos de 2 GiB livres). `ok` global = nenhum check com
`severity=error`, e o exit é 1 nesse caso. `--json` usa `sort_keys`; o
texto é uma linha por check e começa com `PAUSADO` quando aplicável.

`atlas backup` cria `backups/atlas-backup-YYYYMMDDTHHMMSS.tar.gz` com
`state/` (sem sockets nem arquivos efêmeros do daemon — `atlasd.pid`,
`atlasd.log*` — e sem `*.db-wal`/`*.db-shm`), `raw/blobs/`,
`kb/`, `policy/`, `BULLETIN.md` e `reports/`, mais um manifest `.json` com
`created_at`, `entries`, `bytes` e `sha256`. O tar é aberto e contado antes
do `rename`; `--keep N` remove os mais antigos (tar + manifest).

## Transações

Todo read-modify-write abre `state/.lock` com `flock(LOCK_EX)` (timeout de
30 s), carrega os snapshots, aplica as ops em memória e grava:

1. a linha da transação em `state/transactions.jsonl` (`fsync`);
2. `demands.jsonl`, `events.jsonl`, `kb.jsonl`, `edges.jsonl`,
   `digests.jsonl`, `cursor.json` com `tmp+rename`;
3. `state/applied.json` com o `last_tx_id`.

Um processo que morra entre 1 e 2 é recuperado no próximo comando: as
transações além de `applied.json` são replayadas (ops idempotentes). O hook
`ATLAS_TEST_CRASH_AFTER=tx|snapshot|applied` mata o processo em cada
fronteira e só é lido quando a variável existe. Da mesma forma,
`ATLAS_TEST_CRASH_AFTER_TRIGGER_BLOB` mata o processo depois de gravar o
corpo e antes de commitar o trigger. Uma cauda parcial de
`transactions.jsonl` (append interrompido) é reparada sob `flock` antes do
próximo append: registro íntegro sem `\n` ganha o terminador, registro
truncado é descartado; corrupção em linha completa continua sendo erro.
`atlas migrate` lê o estado v1 (sem transações) e cria a transação
`migrate.bootstrap` com os dados existentes + `schema_version`/`applied.json`
(idempotente).

## Daemon (`atlasd`)

`bin/atlasd` roda em foreground (o systemd supervisiona), escuta em
`$ATLAS_HOME/state/atlasd.sock` (modo 0600), grava `state/atlasd.pid` e é o
**escritor único** do estado. O log vai para `state/atlasd.log` (rotação
simples em 1 MiB); nada é escrito no stdout do protocolo.

Protocolo: JSON por linha, uma requisição e uma resposta por conexão.

```json
{"argv":["add","--area","projetos","--title","X"],"now":"2026-11-01T14:00:00-03:00"}
{"code":0,"stdout":"dmd-... new projetos: X\n","stderr":""}
```

- `argv` é a linha do CLI sem `atlas`; `--now` é repassado no campo `now`.
- Comandos são serializados por `threading.Lock`; cada um abre a transação
  normal (`Store.transaction`), então `flock` e o log transacional continuam
  valendo.
- Erro de uso retorna `code=2` com o stderr do argparse e não derruba o
  daemon. Estouro de 120 s por comando retorna `code=124`; o comando **não**
  é cancelável (thread stdlib), então ele termina segurando o lock e os
  comandos seguintes esperam na fila — a resposta 124 só libera o cliente.
- `SO_PEERCRED`: só o UID do próprio daemon é aceito; outro UID recebe
  `code=2` e a conexão é fechada.
- `{"cmd":"shutdown"}` (ou `argv=["daemon","stop"]`) responde e encerra;
  SIGTERM/SIGINT fecham limpo e removem socket e pid. O socket órfão é
  removido ao subir.

Encaminhamento no CLI: `bin/atlas` usa o socket quando
`ATLAS_DAEMON_DISABLE` não está setado. O caminho é resolvido por
`ATLAS_SOCKET` → `state/atlasd.socket` (ponteiro gravado pelo daemon, útil
quando ele sobe com `--socket` fora do padrão) → default; assim um socket
custom não cai silenciosamente no fallback local. Se o socket não existir ou
a conexão recusar, remove o socket órfão e roda local (manutenção), a menos
que `ATLAS_REQUIRE_DAEMON=1` (aí sai 2, "daemon indisponível"). Queda no
meio do comando ou resposta inválida: exit 2 com stderr, **sem** reexecutar
local. O daemon seta `ATLAS_IN_DAEMON=1` ao chamar `main(argv)` para não
reencaminhar.

## Testes

```
python3 -m unittest discover -s tests -v
```

Cobrem fluxo de estados/eventos, transição ilegal, lease/expire, lanes,
rollover/briefing/dry-run, KB (árvore, arestas, check, busca), rebuild com
corpo preservado, determinismo do bulletin, idempotência do seed e o daemon
(8 adds concorrentes via socket, modo 0600, fallback local, socket órfão,
`ATLAS_REQUIRE_DAEMON`, status/stop, erro de uso sem derrubar, recusa por
UID, resposta cortada → exit 2 sem traceback, socket custom via ponteiro e
timeout → 124). M1a.2a cobre outbox: falha do sender mantém o cursor e o
retry reusa `digest_id`/payload, sucesso grava cursor e `reported_at`,
12 exibidos/8 ocultos de 20 candidatos, briefing às 9h mas não às 7h,
rollover até 12h, `delivery_unknown` só com `--retry --force`, rebuild de
notificações preservando o cursor e kill entre sender e update. M1b.0c
cobre o watchdog: lease vencido + `--repair` com evento `actor=system`,
`stale_running`, notificação presa, backlog de 101 triggers (critical),
digest parado, doctor crítico (exit 1), demanda ativa sem lane, nó de KB
stub com limite de 10, `## SAÚDE` no BULLETIN e `--json` determinístico.
M1c.3 cobre o sender Signal: env vence `secrets.env`, argv exato de
note-to-self e de destinatário, `rc != 0`/binário ausente sem vazar mensagem
ou número, timeout devolvendo `unknown` e gravando `delivery_unknown` (sem
retry automático), e `outbox --retry --sender signal` gravando
`sent`/`failed` sem perder a notificação.

## Pendências

- Digest automático desligado: nenhum `atlas-digest.timer`/`atlas-outbox.timer`
  no perfil de produção. O sender padrão é stdout, então "enviar" sem
  transporte real só escreveria no journal e marcaria `sent` sem entrega ao
  usuário. Habilite `atlas outbox --retry --sender signal` apenas depois do
  transporte real.
- Signal real pendente: `signal-cli` ainda não está instalado/registrado no
  sera; timeout agora vira `delivery_unknown` (não `failed`), e o retry só
  repete com `--force`.
- Worker/RPC idempotente, ocorrências e slow path ficam para M1d/M2; o
  contrato atual cobre só o fast path determinístico e o outbox local.

## Demo

```
python3 examples/seed_demo.py --now 2026-11-01T14:00:00-03:00
python3 examples/seed_demo.py --now 2026-11-01T14:00:00-03:00   # não duplica
bin/atlas --now 2026-11-01T14:00:00-03:00 q lidar
bin/atlas --now 2026-11-01T14:00:00-03:00 bulletin
bin/atlas --now 2026-11-01T14:00:00-03:00 digest
bin/atlas --now 2026-11-01T14:00:00-03:00 outbox
bin/atlas --now 2026-11-01T14:00:00-03:00 rebuild
```

O seed cria 6 nós (raiz `projetos`, FAPEMIG, 2 artigos, dataset, pessoa), 6
arestas, 5 demandas em estados diferentes e 2 itens de inbox em `new`.
