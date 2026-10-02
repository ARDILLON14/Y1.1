# Instalación

Dos formas de instalarlo:

- **Local** (desarrollo, demo, o un servidor sin Docker): Python 3.12 y SQLite o PostgreSQL.
- **Docker Compose** (recomendado para operar): PostgreSQL, la app y, para
  dinero real, el firmador aislado en su propia red sin Internet.

Requisitos mínimos: 2 vCPU, 2-4 GB de RAM, 20 GB de disco (el histórico de
100 wallets ocupa poco; lo que más crece es `event_log` y `wallet_transactions`).
Linux x86-64 o ARM64. Reloj sincronizado por NTP (el preflight lo comprueba).

---

## 1. Instalación local

```bash
git clone <tu-repo> copytrader && cd copytrader
make install                    # crea .venv con Python 3.12 e instala el paquete + dev
cp config/settings.example.yaml config/settings.yaml
cp .env.example .env            # secretos locales (está en .gitignore)
chmod 600 .env
```

Crea el esquema y el usuario del dashboard:

```bash
.venv/bin/copytrader init-db
.venv/bin/copytrader set-password              # usuario "admin" por defecto
.venv/bin/copytrader totp-setup                # opcional: segundo factor (necesita DATA_ENCRYPTION_KEY)
.venv/bin/copytrader check-config              # valida YAML + secretos sin arrancar nada
```

Arranca:

```bash
.venv/bin/copytrader run
```

El dashboard queda en <http://127.0.0.1:8080> (solo en localhost). Si entras por
`http://` y el login no se mantiene, mira
[TROUBLESHOOTING.md → login](TROUBLESHOOTING.md#el-login-no-se-mantiene).

### Demo sin claves

```bash
make demo
```

Nivel 3 (paper) contra un mercado simulado con wallets de perfiles conocidos.
No necesita RPC, API keys ni wallet. Sirve para aprender el dashboard y para
validar la configuración de riesgo antes de tocar datos reales.

### Base de datos

- **SQLite** (por defecto, `./data/copytrader.db`): suficiente para la demo y
  para empezar. `init-db` crea el esquema.
- **PostgreSQL 16** (recomendado para operar 24/7):
  ```bash
  DATABASE_URL=postgresql+asyncpg://copytrader:PASSWORD@localhost:5432/copytrader
  ```
  `init-db` aplica las migraciones de Alembic (`alembic upgrade head`).

---

## 2. Instalación con Docker Compose

```bash
git clone <tu-repo> copytrader && cd copytrader
./scripts/init-secrets.sh                       # genera ./secrets (0700) con valores aleatorios
cp config/settings.example.yaml config/settings.yaml
```

Edita los secretos que quieras usar (cada secreto es un fichero):

```bash
chmod 600 secrets/solana_rpc_url && nano secrets/solana_rpc_url && chmod 444 secrets/solana_rpc_url
```

| Fichero en `./secrets` | Para qué |
|---|---|
| `database_url`, `postgres_password` | generados automáticamente |
| `solana_rpc_url`, `solana_ws_url` | tu RPC con API key (Helius, QuickNode, Triton…) — necesario con `providers.mode: live` |
| `helius_api_key` | no hace falta: la clave de Helius ya va dentro de `solana_rpc_url` y `solana_ws_url` (déjalo vacío) |
| `jupiter_api_key` | opcional (API de pago de Jupiter) |
| `telegram_bot_token`, `discord_webhook_url` | notificaciones (opcionales) |
| `signer_hmac_key` | generado: autentica app ↔ firmador |
| `signer_hmac_key_previous` | vacío salvo durante una rotación |
| `data_encryption_key` | generado: cifra campos sensibles (secreto TOTP) |
| `keystore_passphrase`, `bot.keystore.json` | solo para dinero real (ver [OPERATIONS.md §5](OPERATIONS.md#5-conectar-la-wallet-de-ejecución)) |
| `grafana_admin_password` | generado (perfil `monitoring`) |

Un fichero vacío significa "no configurado".

Arranca (niveles 1-3, sin firmador):

```bash
make up                          # docker compose up -d --build
docker compose exec app copytrader set-password
docker compose logs -f app
```

La app aplica las migraciones al arrancar (`docker/entrypoint.sh`).

Perfiles opcionales:

```bash
make up-live                     # añade el firmador (niveles 4-5)
make up-monitoring               # Prometheus (127.0.0.1:9090) + Grafana (127.0.0.1:3000)
```

### Acceso remoto

Todos los puertos se publican **solo en 127.0.0.1**. No abras el dashboard a
Internet. Desde tu ordenador:

```bash
ssh -N -L 8080:127.0.0.1:8080 -L 3000:127.0.0.1:3000 usuario@servidor
```

y abre <http://localhost:8080>. Alternativa: VPN (WireGuard/Tailscale).

### Endurecimiento que ya trae el compose

- Contenedores `read_only`, `cap_drop: ALL`, `no-new-privileges`, usuario sin
  privilegios (uid 10001), `/tmp` en tmpfs, rotación de logs.
- Redes: `backend` (app ↔ postgres, interna), `signer` (app ↔ firmador,
  interna, **sin salida a Internet**), `egress` (solo la app sale a Internet).
- Cada servicio recibe solo sus secretos: la app nunca ve el keystore ni su
  passphrase; el firmador no ve la base de datos ni las API keys.

---

## 3. Actualizar

```bash
git pull
make up                    # reconstruye; las migraciones se aplican al arrancar
```

Local: `git pull && .venv/bin/pip install -e . && .venv/bin/copytrader init-db`.

Tras un reinicio el sistema vuelve **desarmado** (sin dinero real) hasta que lo
armes otra vez desde el dashboard. Las órdenes que estaban en vuelo se
resuelven contra la cadena antes de aceptar señales nuevas.

## 4. Copias de seguridad

```bash
make backup                # pg_dump comprimido en ./backups
```

Restaurar:

```bash
gunzip -c backups/copytrader-AAAAMMDD-HHMMSS.sql.gz | docker compose exec -T postgres psql -U copytrader copytrader
```

Guarda aparte (y cifrado) `secrets/bot.keystore.json` **y** su passphrase: sin
ambos no se puede recuperar la wallet del bot. Mejor aún: guarda también la
frase semilla / keypair original fuera de línea.

## 5. Verificar la instalación

```bash
make test                  # 140+ tests (SQLite)
.venv/bin/copytrader check-config
.venv/bin/copytrader preflight           # qué falta para poder operar con dinero real
curl -s http://127.0.0.1:8080/healthz
```
