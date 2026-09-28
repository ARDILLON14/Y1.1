# Configuración

Toda la configuración ajustable vive en **un fichero YAML**
(`config/settings.yaml`, copia de `config/settings.example.yaml`, que documenta
cada clave con su valor por defecto). Los **secretos nunca van en el YAML**.

## 1. Capas y prioridad

De menor a mayor prioridad:

1. Valores por defecto del programa (`src/copytrader/config/models.py`).
2. `config/settings.yaml` (o la ruta de `--config` / `COPYTRADER_CONFIG`).
3. Variables de entorno `COPYTRADER__SECCION__CLAVE`, por ejemplo:
   ```bash
   COPYTRADER__SELECTION__TOP_N=20
   COPYTRADER__RISK__MAX_TRADE_USD=50
   ```
4. **Cambios en caliente** hechos desde el dashboard (página *Configuración*).
   Se validan completos, se guardan como una **versión nueva** (autor, fecha,
   comentario) y se pueden **revertir** a cualquier versión anterior. Un cambio
   inválido nunca sustituye a una configuración que funciona.

Todo se valida al arrancar y en cada cambio: tipos, rangos, coherencia entre
secciones y límites absolutos. Si algo no cuadra, el sistema no arranca o el
cambio se rechaza, explicando por qué. Compruébalo sin arrancar nada:

```bash
copytrader check-config
```

## 2. Qué se puede cambiar en caliente

| En caliente (dashboard) | Solo editando el YAML y reiniciando |
|---|---|
| `wallets`, `analysis`, `scoring`, `status_rules`, `detection`, `selection`, `signals`, `risk`, `sizing`, `latency`, `exits`, `execution`, `paper`, `levels`, `notifications`, `backtest` | `app` (incluido el **nivel máximo**), `providers`, `api`, `observability`, `security` |

Claves **bloqueadas** aunque su sección sea editable (requieren YAML + reinicio,
a propósito, porque controlan el dinero real):

- `levels.live_trading_enabled` — segunda compuerta del dinero real.
- `levels.require_arm` — tercera compuerta (armar desde el dashboard).
- `execution.wallet_public_key` — la wallet del bot.
- `execution.quote_mint` — el activo con el que se paga (SOL).

## 3. Límites absolutos (no configurables)

`src/copytrader/config/hard_limits.py` fija techos que ningún YAML puede
superar. La capa de ejecución los vuelve a comprobar justo antes de enviar
cada orden (defensa en profundidad), por si un error aguas arriba los saltara.

| Límite | Valor |
|---|---|
| Una operación nunca supera | 25 % del capital y 50.000 USD |
| Exposición total máxima | 100 % del capital (sin apalancamiento) |
| Slippage máximo de entrada / salida | 15 % / 50 % |
| Pérdida diaria configurable máxima | 50 % |
| Posiciones abiertas | 100 |
| Reserva mínima de SOL para comisiones | 0,01 SOL |
| Nivel 4: operación / posiciones | 100 USD / 5 |

Cambiarlos exige modificar el código y redesplegar. Es intencionado.

Reglas de coherencia que se validan siempre: `min_trade_usd < max_trade_usd`;
pérdida diaria ≤ semanal ≤ mensual; `execution.slippage_bps` ≤
`risk.max_slippage_pct`; `selection.top_n` ≤ `wallets.max_wallets`; stop de
emergencia ≥ stop loss; niveles de take profit crecientes; los niveles 4-5
exigen `providers.mode: live`, un firmador y `execution.wallet_public_key`.

## 4. Las variables centrales del enunciado

| Variable | Clave |
|---|---|
| `MAX_WALLETS` | `wallets.max_wallets` (100) |
| `MAX_OPEN_POSITIONS` | `risk.max_open_positions` |
| `MAX_RISK_PER_TRADE` | `risk.max_risk_per_trade_pct` (pérdida si salta el stop) |
| `MAX_DAILY_LOSS` | `risk.max_daily_loss_pct` → kill switch diario |
| `MIN_WALLET_SCORE` | `selection.min_score` |
| `COPY_DELAY_LIMIT` | `latency.max_signal_age_seconds` |
| `MAX_SLIPPAGE` | `risk.max_slippage_pct` |
| `MIN_LIQUIDITY` | `risk.min_liquidity_usd` |
| Top N | `selection.top_n` |
| Capital del bot | `risk.capital_usd` |

## 5. Secciones

Unidades: `*_pct` en porcentaje (5 = 5 %), `*_usd` en dólares, `*_bps` en
puntos básicos (100 bps = 1 %), `*_seconds`/`*_minutes` tiempo.

### `app`
`operating_level` es el **nivel máximo** (1 análisis · 2 alertas · 3 paper ·
4 real pequeño · 5 real). El dashboard puede moverse por debajo de ese techo.

### `providers`
- `mode: simulated | live`. `simulated` usa un mercado sintético (demo, tests).
- `solana.stream`: `logs_subscribe` (cualquier RPC; una suscripción por wallet
  + `getTransaction`) o `helius_transaction_subscribe` (la transacción llega
  completa en el evento: menos latencia y menos llamadas).
- `catchup_on_reconnect` y `reconcile_poll_interval_seconds`: tras una
  desconexión se recuperan las operaciones perdidas; la reconciliación
  periódica es una red de seguridad, no el mecanismo principal.
- `rate_limit_per_second` de cada proveedor: ajústalo a tu plan. Dentro de
  ese límite, las cotizaciones y swaps de órdenes (entradas y salidas) se
  atienden siempre antes que la consulta periódica de precios, que puede esperar.
- `token_categories_file`: categorías de tokens (meme, defi, IA…) para las
  métricas por categoría (ejemplo en `config/token_categories.example.yaml`).

### `analysis`
Ventana histórica (`history_days`), tamaño de la ventana reciente
(`recent_trades`), vida media del decaimiento (`decay_half_life_days`), qué se
considera operación rápida (`fast_trade_max_minutes`) y umbrales de régimen de
mercado (tendencia y volatilidad de SOL). `recompute_interval_seconds` marca
cada cuánto se re-evalúan todas las wallets.

### `scoring`
- `weights`: peso relativo de cada componente (se normalizan). Ninguno es "PnL
  a secas": rentabilidad ajustada, consistencia, drawdown, win rate (límite
  inferior de Wilson), profit factor contraído, riesgo, volatilidad, tamaño de
  muestra, actividad, concentración, movimientos extremos y replicabilidad.
- `sample.prior_trades` (k) y `sample.prior_score`: con n operaciones, el score
  es `prior + (bruto − prior)·n/(n+k)`. Con pocas operaciones el score se
  acerca a `prior_score` (escepticismo). Sube `prior_trades` para ser más
  exigente con muestras pequeñas.
- `recent_weight`: cuánto pesa el comportamiento reciente frente al histórico.
- `degradation`: detección de deterioro (caída de win rate estadísticamente
  significativa, profit factor reciente bajo el suelo…) y su penalización.
- Penalización por avisos de detección: `warning_penalty_points` por aviso,
  hasta `max_warning_penalty_points`. Un aviso crítico limita el score a 20.

### `status_rules`
Cuándo una wallet es **ACTIVA** (score y muestra mínimos, actividad reciente),
**OBSERVAR** (deterioro, avisos, inactividad) o **BLOQUEADA** (flag crítico).
Solo las ACTIVAS pueden copiarse; una wallet en OBSERVAR no se copia aunque
esté en whitelist.

### `detection`
Umbrales de cada regla de comportamiento sospechoso (wash trading,
coordinación, snipers, liquidez baja, operaciones no replicables, dependencia
de una operación, ganancias extremas, rugs, tokens de riesgo, cambio de
comportamiento, HFT, inactividad). `severity_overrides` permite cambiar la
severidad de una regla (p. ej. `{COORDINATED: warning}`).

### `selection`
`top_n`, `min_score`, histéresis (`hysteresis_points`: una wallet nueva debe
superar en esos puntos a una ya seleccionada para desplazarla; evita rotación
constante), política de whitelist (`priority` la coloca primero; necesita
`whitelist_min_score`) y alertas para watchlist / observación.

### `signals`
`min_source_value_usd` (ignora compras de origen pequeñas), `follow_sells`
(espejar ventas de origen) y `late_sell_max_age_minutes`.

### `risk` — Risk Management Engine
Capital del bot, riesgo por operación / por wallet, exposición total, por
token y de alto riesgo, pérdidas diaria/semanal/mensual (kill switches),
posiciones máximas, tamaño mínimo/máximo, slippage, liquidez, market cap,
antigüedad y score de riesgo del token, autoridades de mint/freeze, extensiones
peligrosas de Token-2022, cooldown de reentrada, pérdidas consecutivas,
errores de ejecución por hora, antigüedad máxima de los datos, reserva de SOL,
listas de tokens y `max_round_trip_cost_pct`: rechaza entradas cuyo coste fijo
de red (compra + venta, ver [§6](#6-costes-de-red)) supere ese porcentaje del
tamaño.

### `sizing`
`method: risk_based | fixed`. En `risk_based` el tamaño parte del riesgo por
operación y el stop, y se multiplica por factores explicados en cada decisión:
confianza (score), volatilidad del token, fracción máxima de la liquidez del
pool, penalización por slippage, alto riesgo y correlación con posiciones
abiertas. Siempre acotado por `risk.max_trade_usd`, la exposición disponible y
los límites absolutos.

### `latency`
Edad máxima de la señal (`max_signal_age_seconds`), TTL de la señal,
desviación máxima de precio frente al de la wallet origen, re-cotizar antes de
ejecutar y edad máxima de la cotización. Si algo caduca, la operación se
cancela y se explica.

### `exits`
- `default_mode`: `mirror` (sigue las salidas de la wallet; solo aplica el stop
  de emergencia), `protected` (sigue sus salidas **y** aplica SL/TP/trailing/
  tiempo), `smart` (la wallet solo da la entrada; salen tus reglas).
- `close_on_source_sell`: cerrar todo en cuanto la wallet venda (cualquier modo).
- `emergency_stop_loss_pct`: se aplica **siempre**, también en modo espejo.
- `exit_slippage_pct`: tolerancia para salir de un token que se desploma.

Cada wallet puede tener su propio modo de salida (detalle de wallet).

### `execution`
Slippage de la transacción, prioridad y tope de priority fee, tiempo de
confirmación, re-difusión, Jito tip opcional, reintentos de entrada/salida y
reconciliación de saldos. Además:
- `expected_priority_fee_lamports`: lo que pagas de media por transacción (para
  los costes en paper, el backtest y el filtro de coste). `null` asume el tope,
  que es conservador; ajústalo con las comisiones reales de *Operaciones*.
- `close_empty_token_accounts`: cierra las cuentas de token vacías tras vender
  y recupera su alquiler (ver [§6](#6-costes-de-red)).

### `paper`
Latencia simulada, slippage extra, comisión base por transacción y si se usan
cotizaciones reales de Jupiter (recomendado: el paper trading solo es útil si
se parece a la realidad). El paper cobra los mismos costes de red que pagaría
la transacción real ([§6](#6-costes-de-red)).

### `levels`
Compuertas del dinero real (ver [OPERATIONS.md §6](OPERATIONS.md#6-activar-el-trading-real))
y **topes del nivel 4**, que se aplican como mínimo con los de `risk`.

### `notifications`
Telegram (`telegram_enabled`, `telegram_chat_id`; el token es un secreto) y
Discord (el webhook es un secreto), severidad mínima, deduplicación y límite
por minuto.

### `api`
Solo en `127.0.0.1` por defecto. Duración de sesión, bloqueo tras intentos
fallidos, rate limit, `secure_cookies` y `require_totp`.

### `observability`
Nivel de log, logs JSON, métricas Prometheus (`metrics_port: 9464`) y
retención de la línea temporal (`event_log_retention_days`).

### `security`
`signer_mode: none | remote | local` (`remote` recomendado: firmador aislado),
URL del firmador, ruta del keystore (solo `local`) y tolerancia de reloj HMAC.

### `backtest`
Ventanas de entrenamiento y test, latencia, slippage, comisiones y coeficiente
de impacto de mercado usados en la simulación. `fee_usd_per_trade: null` usa el
mismo modelo de costes de red que el paper trading.

## 6. Costes de red

Cada operación paga costes fijos que no dependen de su tamaño, así que pesan
mucho en operaciones pequeñas. El mismo modelo se usa en paper trading, en el
backtest y en el filtro `risk.max_round_trip_cost_pct`:

| Coste | Cuándo | Valor |
|---|---|---|
| Comisión base | cada transacción | 5.000 lamports (`paper.network_fee_sol`) |
| Priority fee **o** tip de Jito | cada transacción (nunca ambos) | `execution.expected_priority_fee_lamports` (o el tope `priority_fee_max_lamports`) / `jito_tip_lamports` |
| Alquiler de la cuenta del token | al comprar un token nuevo | ~0,00204 SOL; se **recupera** al cerrar la cuenta vacía tras vender (`close_empty_token_accounts: true`) y solo es coste si desactivas el cierre |

Con los valores por defecto (tope de priority fee de 0,001 SOL) una compra y
su venta cuestan ~0,002 SOL: con SOL a 200 USD son ~0,40 USD, un 4 % de una
operación de 10 USD y un 2 % de una de 20 USD. El filtro rechaza por defecto
las que superen el 3 %.

El cierre de cuentas vacías solo envía transacciones con el trading real
armado, nunca toca cuentas con saldo, con posición abierta u orden en vuelo, ni
las de SOL/USDC/USDT, y agrupa hasta 8 cuentas por transacción. El firmador
verifica que solo contenga cierres que devuelven el alquiler a la wallet del bot.

## 7. Secretos

Se cargan aparte, nunca se guardan en la base de datos ni se muestran por la
API, y se enmascaran en logs y notificaciones. Fuentes, por prioridad:

1. Ficheros en `/run/secrets` (Docker secrets) o `COPYTRADER_SECRETS_DIR`,
   con el nombre del campo en minúsculas (`telegram_bot_token`…).
2. Variables `NOMBRE_FILE=/ruta/al/fichero`.
3. Variables de entorno / `.env` (`TELEGRAM_BOT_TOKEN`…).

| Secreto | Uso |
|---|---|
| `DATABASE_URL` | conexión a la base de datos |
| `SOLANA_RPC_URL`, `SOLANA_WS_URL` | RPC con API key (tienen prioridad sobre las URLs públicas del YAML) |
| `HELIUS_API_KEY`, `JUPITER_API_KEY` | opcionales |
| `TELEGRAM_BOT_TOKEN`, `DISCORD_WEBHOOK_URL` | notificaciones |
| `SIGNER_HMAC_KEY` (+ `SIGNER_HMAC_KEY_PREVIOUS` al rotar) | autenticación app ↔ firmador |
| `KEYSTORE_PASSPHRASE` | solo con `signer_mode: local` (en `remote` la tiene únicamente el firmador) |
| `DATA_ENCRYPTION_KEY` | cifrado de campos (secreto TOTP) |

Genera valores fuertes con `copytrader gen-secret`.
