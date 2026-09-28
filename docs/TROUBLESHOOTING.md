# Resolución de problemas

Primero, siempre:

```bash
copytrader check-config                 # ¿la configuración y los secretos cargan?
copytrader preflight                    # ¿qué falla para operar con dinero real?
docker compose logs --tail 200 app      # o la salida de `copytrader run`
curl -s http://127.0.0.1:8080/healthz
```

Los logs son JSON, una línea por evento, y llevan `trace_id` cuando se
refieren a una señal. Para seguir una operación concreta:

```bash
docker compose logs app | grep '"trace_id": "<TRAZA>"'
```

La misma traza aparece en el dashboard: *Señales → (señal) → Línea temporal*.

---

## Arranque

**`configuración inválida: - risk.max_trade_usd: ...`**
La validación explica la clave y el motivo. Casos típicos: `max_trade_usd`
por encima del 25 % de `capital_usd`; pérdidas no ordenadas (diaria ≤ semanal
≤ mensual); `execution.slippage_bps` por encima de `risk.max_slippage_pct`;
nivel 4-5 sin `providers.mode: live`, sin firmador o sin
`execution.wallet_public_key`.

**`keystore ... permissions ... are too open (use chmod 600)`**
Fuera de Docker el keystore debe ser 600 (`chmod 600 secrets/bot.keystore.json`).
Dentro de Docker (`/run/secrets/...`) se acepta 444, que es lo necesario para
que el usuario del contenedor lo lea.

**El firmador no arranca: `missing required secret KEYSTORE_PASSPHRASE`**
`secrets/keystore_passphrase` está vacío. Escribe la passphrase del keystore.

**`could not translate host name "postgres"` / conexión rechazada**
PostgreSQL aún no está listo o `database_url` no corresponde a
`postgres_password`. `docker compose ps` debe mostrar `postgres` como
*healthy*. Si cambiaste la contraseña a mano, actualiza ambos secretos.

**`otra instancia de copytrader ya está usando esta base de datos`**
Solo puede haber un proceso de trading por base de datos: los límites de
riesgo dependen de ello. Busca el otro proceso (`docker compose ps`,
`ps aux | grep copytrader`) y detenlo. Si el proceso anterior murió, el
bloqueo se libera solo (PostgreSQL al cerrarse la conexión; SQLite al morir
el proceso). Los comandos de administración (`wallets`, `evaluate`,
`kill-switch`…) no toman el bloqueo y pueden usarse con la app en marcha.

**Un secreto "no se aplica"**
Los ficheros de `./secrets` vacíos cuentan como "no configurado". Tras editar
uno, reinicia el servicio (`docker compose up -d app`).

## Dashboard

### El login no se mantiene
Con `api.secure_cookies: true` el navegador solo envía la cookie por HTTPS o
`http://localhost`. Si entras por `http://127.0.0.1` o por la IP del servidor,
usa un túnel SSH y abre `http://localhost:8080`. Solo en desarrollo local:
`COPYTRADER__API__SECURE_COOKIES=false`.

**"Demasiados intentos"**: bloqueo por IP tras `api.login_max_attempts` fallos
durante `api.login_lockout_minutes`. Espera o reinicia la app.

**He olvidado la contraseña**: `copytrader set-password` en el servidor.

**"esta acción requiere confirmar la contraseña"**: cambiar el nivel, armar,
desactivar un kill switch y aplicar/revertir configuración piden contraseña
(y TOTP si está activado).

## Datos y wallets

**El backfill tarda horas / errores 429**
El RPC gratuito limita las peticiones. Baja `providers.solana.rate_limit_per_second`
y `backfill_concurrency`, o usa un RPC de pago. Verás `circuit_state` en los
logs cuando un proveedor falla repetidamente: el circuito se abre, las
llamadas fallan rápido durante `reset_timeout_seconds` y luego se reintenta.

**Una wallet aparece con 0 operaciones**
Solo se cuentan swaps que el parser entiende: compras/ventas contra SOL, USDC
o USDT, o token↔token con precio conocido. Transferencias, NFTs, bots
custodiales (el swap lo ejecuta otra cuenta) o transacciones ambiguas se
ignoran. Revisa también `analysis.history_days` y `analysis.min_trade_usd`.

**Todas las wallets en OBSERVAR**
Normalmente muestra insuficiente (`status_rules.min_trades_active`) o
inactividad (`status_rules.max_inactive_days`). El detalle de cada wallet
indica el motivo.

## Señales y copias

**No aparece ninguna señal**
- Nivel 1 solo analiza: sube a 2 (alertas) o 3 (paper).
- No hay wallets seleccionadas (*Wallets*: columna "Seleccionada"); revisa
  `selection.min_score` y `selection.top_n`.
- Compras de origen por debajo de `signals.min_source_value_usd`.
- El stream no está conectado: *Resumen → Proveedores*. Logs `ws_disconnected`,
  `ws_error`, `ws_subscribe_failed`.

**Casi todas las señales CADUCAN** ("Retraso de la señal", "Señal todavía
vigente", "Precio de mercado cerca del pagado por la wallet")
La latencia de detección es alta. Usa un RPC con WebSocket de calidad o
`stream: helius_transaction_subscribe`. No subas `latency.max_signal_age_seconds`
a la ligera: llegar tarde es la principal desventaja del copy trading.

**Muchos rechazos por liquidez, market cap o riesgo del token**
Es el sistema funcionando. Si quieres copiar tokens más pequeños, ajusta
`risk.min_liquidity_usd` / `min_market_cap_usd` conscientemente y reduce el
tamaño (`sizing.high_risk_mult`).

**"No cumple — Tamaño … < mínimo útil"**
El sizing, tras aplicar todos los factores y topes, queda por debajo de
`risk.min_trade_usd`. El mensaje dice qué lo limitó (exposición disponible,
riesgo por wallet, liquidez…). Suele indicar que ya hay mucha exposición abierta.

**"No cumple — Coste de red de ida y vuelta asumible"**
Las comisiones fijas de comprar y vender (priority fee o tip, y el alquiler si
no cierras cuentas) superan `risk.max_round_trip_cost_pct` del tamaño. La
operación es demasiado pequeña para ser rentable después de comisiones. Sube el
tamaño por operación (capital, `risk.max_trade_usd`) o ajusta
`execution.expected_priority_fee_lamports` a lo que realmente pagas.

**Estoy en nivel 4-5 pero las entradas salen en PAPER**
Alguna compuerta está cerrada; el motivo exacto aparece en *Sistema* y en el
resumen: `levels.live_trading_enabled` en false, sin armar (tras cada
reinicio hay que volver a armar), o el nivel efectivo es 3.

**Armar falla con "preflight no superado"**
El mensaje lista las comprobaciones críticas fallidas. Las más habituales:
proveedores simulados, firmador inaccesible o con otra clave pública, saldo
insuficiente, reloj desincronizado (activa NTP: `timedatectl set-ntp true`).

## Ejecución (dinero real)

**Orden en ENVIADA mucho tiempo**
La transacción se envió pero no se confirmó a tiempo. No se reenvía: el
proceso de recuperación consulta su estado y la resuelve (confirmada →
se aplica el fill una sola vez; blockhash caducado → CADUCADA). Log
`order_pending_confirmation`.

**`signer_auth_failed` (401 del firmador)**
Clave HMAC distinta entre app y firmador, o relojes desincronizados más allá
de `security.hmac_max_skew_seconds`. Ambos contenedores leen
`secrets/signer_hmac_key`; tras cambiarla reinicia los dos.

**`signer_policy_violation`**
El firmador rechazó la transacción (programa no permitido, tope por
transacción/día/minuto superado, priority fee alta…). Es la última barrera:
revisa el motivo en los logs del firmador antes de relajar sus topes.

**Kill switch global por "descuadre de saldos"**
Los saldos on-chain no cuadran con las posiciones. Causa habitual: mover
tokens o SOL a mano en la wallet del bot. Revisa *Posiciones* frente a la
wallet en un explorador, corrige (cierra o ajusta) y desactiva el kill switch.

**"Posición sin precio"**
No hay precio de mercado para el token; el sistema intenta usar la
cotización de venta de Jupiter. Si tampoco hay cotización (sin liquidez), SL/TP
no pueden evaluarse: valora cerrarla manualmente.

**"No se puede cerrar una posición"**
La salida falla repetidamente (sin liquidez, slippage mayor que
`exits.exit_slippage_pct`). Se sigue reintentando con espera creciente. Puedes
subir temporalmente `exits.exit_slippage_pct` (máximo absoluto 50 %).

**Quedan cuentas de token vacías en la wallet del bot**
Solo se cierran con el trading real armado, cada
`execution.close_accounts_interval_seconds`, y nunca si tienen saldo (aunque
sea polvo), posición abierta u orden en vuelo. Una cuenta cuyo cierre falla
(p. ej. Token-2022 con comisiones retenidas) se reintenta más tarde con espera
creciente. Log `token_account_close_failed`; los cierres correctos aparecen como
`token_accounts_closed` con el SOL recuperado.

## Notificaciones

**Telegram no envía nada**: el bot debe haber recibido antes un mensaje tuyo;
comprueba `notifications.telegram_chat_id`, `telegram_enabled: true`, el
secreto `TELEGRAM_BOT_TOKEN` y `notifications.min_severity`. Log
`send_failed_will_retry`.

## Métricas

**Grafana vacío**: la app publica métricas en `observability.metrics_port`
(9464) solo si `metrics_enabled: true`; en Docker `metrics_host` debe ser
`0.0.0.0` (ya lo fija el compose). Prometheus: <http://localhost:9090/targets>.

## Disco

**`no space left on device`**: lo que más crece es `event_log`
(`observability.event_log_retention_days`) y los logs de Docker (rotan a
5 × 20 MB). Haz `make backup` y limpia copias antiguas en `./backups`.
