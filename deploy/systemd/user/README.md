# ATLAS no systemd (usuário)

Estes são os units de produção do ATLAS no `sera` (NixOS). O código fica em
`~/atlas` e o estado em `~/.local/share/atlas` (`ATLAS_HOME`), **separado do
checkout**: `rebuild`, `backup` e as vistas `.md` nunca escrevem no
repositório. O daemon `atlasd` é o escritor único; os demais serviços só
chamam o CLI.

Os units chamam `/run/current-system/sw/bin/python3` (o Python do perfil do
sistema NixOS; conferido no sera, hoje 3.12.12) e exportam
`PATH=/run/current-system/sw/bin:/usr/bin:/bin` para achar `signal-cli` e
utilitários. Os serviços que declaram dependência do daemon
(health/ingest/shadow/watchdog) setam `ATLAS_REQUIRE_DAEMON=1`: se o socket
não existir ou recusar conexão, o comando sai 2 (`daemon indisponível`) em
vez de rodar local — fail-closed, sem um segundo escritor. `backup` é a
exceção deliberada: não declara `After/Wants=atlasd` e não usa
`ATLAS_REQUIRE_DAEMON`, porque pega `flock` direto e precisa rodar mesmo com
o daemon caído. `atlasd.service` roda
`atlas init` em `ExecStartPre` (garante o layout antes do socket), usa
`RestartSec=10` e `StartLimitBurst=5`, e mantém `ATLAS_HOME` em
`~/.local/share/atlas`.

## Instalar

```sh
mkdir -p ~/.config/systemd/user
cp ~/atlas/deploy/systemd/user/*.service ~/atlas/deploy/systemd/user/*.timer \
  ~/.config/systemd/user/
systemctl --user daemon-reload
ATLAS_HOME=~/.local/share/atlas ~/atlas/bin/atlas init   # idempotente
ATLAS_HOME=~/.local/share/atlas ~/atlas/bin/atlas backup
systemctl --user enable --now atlasd.service
systemctl --user enable --now atlas-ingest.timer \
  atlas-watchdog.timer atlas-shadow.timer atlas-backup.timer atlas-health.timer
```

Digest e outbox ficam **fora deste perfil** até existir transporte real: os
units `atlas-digest.service`/`atlas-digest.timer` não estão no repositório
(confirmado: não existem) e não devem ser criados no host, porque o sender
padrão é stdout — um digest "enviado" só iria para o journal e o outbox
marcaria `sent` sem o usuário ter recebido. O mesmo vale para
`atlas-outbox.timer`, que também não existe.

`loginctl enable-linger zino` já está ativo no sera; sem ele, o user
manager para no logout.

## Verificar

```sh
systemctl --user list-timers 'atlas-*'
systemctl --user status atlasd.service
journalctl --user -u atlas-ingest.service -n 50 --no-pager
ATLAS_HOME=~/.local/share/atlas ~/atlas/bin/atlas health
```

## Pausar

```sh
ATLAS_HOME=~/.local/share/atlas ~/atlas/bin/atlas pause --reason "manutenção"
ATLAS_HOME=~/.local/share/atlas ~/atlas/bin/atlas resume
```

A pausa grava `state/control.json` (`paused=true`) e bloqueia
`ingest email` e `outbox --retry`; health, backup e comandos manuais
continuam. `atlas watchdog` reporta `paused` (warn) lendo o mesmo arquivo.

## Não incluído

- Digest automático: desabilitado até existir transporte real (ver acima).
- `atlas-outbox.timer`: o sender padrão é stdout; só deve ser habilitado
  depois do registro do `signal-cli`. O comando seria
  `atlas outbox --retry --sender signal`.
- Signal: `signal-cli` ainda não está instalado/vinculado no sera; até o
  registro (`signal-cli -a NUMBER register`/`link`), não crie o timer. Um
  timeout de envio termina `delivery_unknown` (entrega ambígua) e o retry
  só repete com `--force`.
- Worker/RPC idempotente, ocorrências e slow path: M1d/M2.
