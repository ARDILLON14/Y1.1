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
El RPC gratuito limita las peticiones (Helius Free: 10 por segundo). Pon
`providers.solana.rate_limit_per_second` un poco por debajo de tu plan (8 con
Helius Free) para dejar margen al stream en tiempo real, o usa un RPC de pago.
Cuenta unos 2 minutos por wallet con 8 peticiones/s (1.000 transacciones).

Un 429 no abre el circuit breaker (prueba que el RPC responde: incluso lo cierra si estaba entreabierto); el limitador frena y respeta `retry-after`.
Si el RPC falla de verdad (timeouts, 5xx) el circuito se abre durante
`reset_timeout_seconds` (`circuit_state` en los logs); la descarga del
historial espera a que se cierre y reintenta cada transacción (~100 s de
paciencia) en lugar de descartarla.

El precio histórico de SOL (para valorar cada swap en USD) se descarga una vez
por wallet **antes** de pedir sus transacciones. Si `sol_price_history`
(Binance) no responde, esa wallet se aplaza al ciclo siguiente sin gastar
créditos del RPC (`backfill_failed` con `sol_price_history` en el error). Una
transacción concreta que no se pueda valorar cuenta como `missing`: nunca
descarta las demás.

Progreso: la pantalla **Wallets** muestra «Descargando historial: N de M». En
los logs, `backfill_done` es una wallet terminada y `backfill_incomplete` una
que quedó con transacciones sin descargar (`missing`): el ciclo siguiente
descarga solo lo que falta. Tras 3 intentos incompletos se acepta tal cual
para no gastar créditos del RPC indefinidamente.

```bash
docker compose logs app | grep -cE '"event": "backfill_done"'       # wallets terminadas
docker compose logs app | grep -E 'backfill_incomplete|backfill_failed' | tail -5
```

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

**"No cumple — Valor esperado de la copia tras costes"**
La ventaja copiable de la wallet no cubre las comisiones de una copia de este
tamaño. Suele pasar con tamaños pequeños (sizing reducido por volatilidad,
exposición o régimen). No lo relajes: sube el tamaño típico o deja que la
wallet demuestre más ventaja.

**"No cumple — Se puede vender (ruta de salida)"**
Jupiter no encuentra ruta para vender el token o venderlo al instante perdería
más de `filters.max_round_trip_quote_loss_pct`. Es exactamente el tipo de
token del que podrías no salir.

**"No cumple — Sin riesgos bloqueados (RugCheck)"**
El informe de RugCheck incluye un riesgo de `filters.blocked_risk_flags`. El
detalle indica cuál.

**Estoy en nivel 4-5 pero las entradas salen en PAPER**
Además de las compuertas (abajo), la wallet puede estar en su **periodo de
prueba** (`learning.probation_*`): la decisión muestra "en prueba: n/5".


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

**`tip ... exceeds cap` en el firmador**
Activaste Jito con `execution.jito_tip_lamports` por encima de
`SIGNER_MAX_TIP_LAMPORTS` del firmador. Sube ese tope del firmador hasta el
máximo que quieras pagar (o baja `jito_tip_lamports`) y reinícialo.

**`send_route_failed` en los logs**
Una ruta de envío (Jito, un RPC extra) rechazó o no respondió. No es un error
mientras otra ruta acepte: la orden solo falla si **todas** rechazan. Con Jito,
un `429` suele ser su límite por IP (usa `JITO_AUTH_UUID` o más regiones en
`jito_block_engine_urls`). Mira *Sistema → Velocidad y comisiones → Rutas de
envío*.

**Entradas que caducan sin aterrizar con la red congestionada**
Con `priority_fee_max_trade_pct` las operaciones pequeñas pagan poca prioridad.
Es a propósito: pagar un 3 % de comisión para entrar no compensa. Si pasa a
menudo, sube el tamaño mínimo (`risk.min_trade_usd`) antes que el porcentaje.

**Detección lenta**
*Sistema → Velocidad y comisiones*: si la detección mediana supera 1-2 s, usa
`helius_transaction_subscribe` o un proveedor más rápido, y añade un
`backup_stream` de otro proveedor. Con `logs_subscribe` cada transacción se
descarga aparte; `transaction_not_available` en los logs indica un RPC lento
indexando.

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

**Posición cerrada por "Liquidez -X% desde la entrada"**
La liquidez del pool cayó más de `exits.liquidity_drop_exit_pct` en dos
comprobaciones seguidas: suele ser un rug o la retirada del LP. Si ves que salta
por cambios de par en DexScreener (el pool más profundo cambió), sube
`liquidity_exit_confirmations` o el porcentaje.

**El backtest se queda en "Descargando precios históricos"**
La primera vez descarga velas de muchos tokens a ~30 peticiones por minuto
(límite de la API pública de GeckoTerminal): con 150 tokens puede tardar unos
10-15 minutos. Se guardan en la base de datos y el siguiente backtest es rápido.
Para ir más rápido, baja `backtest.max_price_tokens` o sube `candle_minutes` a 60.
Si en los logs aparece `price_history_fetch_failed` con 429, es el límite de
peticiones: esos tokens se reintentan en el siguiente backtest.

**Pocos tokens con velas en el backtest**
Tokens muy nuevos, ya retirados o cuyo pool principal no está en GeckoTerminal
no tienen historial. Se usan solo sus precios de operación y no se vuelven a
pedir hasta `backtest.refetch_failed_after_hours`.

**El stop de una posición no coincide con `exits.stop_loss_pct`**
Con `exits.volatility_stop: true` cada posición guarda el stop con el que se
dimensionó (*Posiciones*, columna *Salida*; el detalle aparece al pasar el
ratón). Cambiar la configuración después no lo modifica: la posición se abrió
con un tamaño calculado para ese stop.

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
