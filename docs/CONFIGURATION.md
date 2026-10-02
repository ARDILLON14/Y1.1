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
| `wallets`, `analysis`, `scoring`, `status_rules`, `detection`, `selection`, `signals`, `risk`, `sizing`, `latency`, `exits`, `execution`, `paper`, `levels`, `notifications`, `backtest`, `measurement`, `learning`, `filters` | `app` (incluido el **nivel máximo**), `providers`, `api`, `observability`, `security` |

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
- `solana.backup_stream`: un segundo stream en paralelo (de otro proveedor si
  pones su URL en el secreto `SOLANA_WS_URL_BACKUP`). Cada transacción se
  procesa en cuanto la entrega el **primero**; si el más lento trae la
  transacción completa mientras el otro aún la descarga, se usa la suya. Si
  el respaldo cae, el sistema queda *degradado*, no caído. La página
  *Sistema* muestra qué stream llega antes y con cuánta ventaja.
- `solana.stream_commitment`: `processed` detecta ~0,5-1 s antes con
  `helius_transaction_subscribe`, pero una pequeña parte de esas
  transacciones nunca se confirma (copiarías algo que no llegó a pasar). Por
  defecto se usa `confirmed`.
- `get_transaction_retries` / `get_transaction_retry_delay_ms`: con
  `logs_subscribe` la transacción se descarga aparte; el primer reintento
  llega a los 100 ms y la espera crece ×1,6 hasta 1 s.
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

**Replicación** (`replication_*`): para cada operación cerrada de la wallet se
estima qué habrías ganado **copiándola**, porque su PnL está medido a sus
propios precios y el tuyo no:

- llegas `latencia` segundos tarde: pierdes esa fracción del movimiento de la
  operación (todo, si la wallet mantiene menos tiempo que tu latencia);
- su compra ya movió el precio del pool (impacto ≈ su tamaño / la mitad de la
  liquidez) y tú pagas además tu propio impacto y el slippage de `paper`;
- vendes después de cada venta suya, cuando su venta ya bajó el precio;
- pagas las comisiones de red de la ida y vuelta ([§6](#6-costes-de-red)).

La latencia es la **mediana medida en tus copias recientes** en cuanto hay
`replication_min_latency_samples`; antes se usa `backtest.latency_seconds`. El
tamaño es el que daría el sizing por riesgo (`replication_size_usd` lo fija).
La liquidez se toma de la operación o del token y nunca se supone por debajo de
`risk.min_liquidity_usd`. Es una estimación sin histórico de precios tick a
tick: sirve para ordenar wallets por lo que tú puedes replicar, no como promesa.
El detalle de cada wallet la muestra en la tarjeta *Si la copias*.

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
- `copy_edge` (peso 0,18 por defecto): ventaja estimada **al copiarla**
  (retorno medio replicado y su límite inferior, escalados entre
  `bounds.copy_expectancy_lo_pct` y `bounds.copy_expectancy_hi_pct`).

### `status_rules`
Cuándo una wallet es **ACTIVA** (score y muestra mínimos, actividad reciente),
**OBSERVAR** (deterioro, avisos, inactividad) o **BLOQUEADA** (flag crítico).
Solo las ACTIVAS pueden copiarse; una wallet en OBSERVAR no se copia aunque
esté en whitelist. `min_copy_expectancy_pct` (0 % por defecto) pasa a
OBSERVAR las wallets cuyo retorno **copiado** estimado por operación es menor,
con muestra suficiente: su ventaja existe, pero tú no puedes capturarla.

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

Con `per_wallet_max_age` el retraso máximo se adapta a cada wallet:
`min(max_signal_age_seconds, max(min_signal_age_seconds, max_age_fraction_of_hold × holding mediano))`.
Una wallet que mantiene 1 minuto exige señales de menos de 6 s; una que
mantiene horas usa el límite global. La decisión indica el límite aplicado.

### `exits`
- `default_mode`: `mirror` (sigue las salidas de la wallet; solo aplica el stop
  de emergencia), `protected` (sigue sus salidas **y** aplica SL/TP/trailing/
  tiempo), `smart` (la wallet solo da la entrada; salen tus reglas).
- `close_on_source_sell`: cerrar todo en cuanto la wallet venda (cualquier modo).
- `emergency_stop_loss_pct`: se aplica **siempre**, también en modo espejo.
- `exit_slippage_pct`: tolerancia para salir de un token que se desploma.

Cada wallet puede tener su propio modo de salida (detalle de wallet).

**Salidas de protección** (también en modo espejo la de liquidez):
- `liquidity_drop_exit_pct` (50 % por defecto): si la liquidez del pool cae
  ese porcentaje desde la entrada en `liquidity_exit_confirmations`
  comprobaciones seguidas (cada `liquidity_check_seconds`), se vende todo.
  Detecta un rug o una retirada de liquidez antes de que el precio lo refleje
  del todo. Es una salida urgente: paga la prioridad máxima.
- `wallet_sells_exit_min` (desactivada): si N wallets fiables que sigues
  (activas o con ventaja copiable, nunca bloqueadas; el origen cuenta) venden
  al menos `wallet_sells_min_fraction` de lo que tenían desde tu entrada y en
  la última `wallet_sells_window_minutes`, se vende `wallet_sells_exit_fraction`
  (una sola vez por posición). No se aplica en modo espejo.

**Perfil adaptativo** (desactivado; se fija al abrir cada posición y se muestra
en *Posiciones* y en la decisión de la señal):
- `volatility_stop`: stop = `volatility_stop_sigmas` × el movimiento esperado
  del token durante el tiempo que se espera mantenerlo (holding mediano de la
  wallet, 1 h si no se conoce), entre `volatility_stop_min_pct` y
  `volatility_stop_max_pct`. El trailing se escala igual. El **tamaño** se
  calcula con ese stop, así que el capital en riesgo por operación no cambia:
  un token más volátil lleva un stop más ancho y una posición más pequeña.
- `wallet_exit_profile`: tiempo máximo = `profile_hold_multiple` × el holding
  mediano de la wallet (entre `profile_min_hold_minutes` y
  `profile_max_hold_minutes`) y, con `profile_take_profit`, los niveles de take
  profit escalados hacia su ganancia mediana.

Por qué vienen desactivados: en el backtest walk-forward del mercado simulado
(tres universos distintos) el stop por volatilidad bajó siempre el resultado
(p. ej. +26 % → +9 % con un drawdown mayor), el perfil de tiempo fue
inconsistente y la salida por ventas de varias wallets no mejoró el modo
protegido y empeoró el inteligente. Pruébalos con una variante del backtest y
en paper (*Análisis → por motivo de salida*) antes de activarlos.

### `execution`
Slippage de la transacción, prioridad y tope de priority fee, tiempo de
confirmación, re-difusión, Jito tip opcional, reintentos de entrada/salida y
reconciliación de saldos. Además:
- `expected_priority_fee_lamports`: lo que pagas de media por transacción (para
  los costes en paper, el backtest y el filtro de coste). `null` asume el tope,
  que es conservador; ajústalo con las comisiones reales de *Operaciones*.
- `close_empty_token_accounts`: cierra las cuentas de token vacías tras vender
  y recupera su alquiler (ver [§6](#6-costes-de-red)).

**Comisiones dinámicas.** Cada transacción decide cuánta prioridad paga:

| Orden | Nivel | Tope |
|---|---|---|
| Entrada | `priority_level` | `priority_fee_max_trade_pct` del tamaño (0,5 %), mín. `min_priority_fee_lamports`, máx. `priority_fee_max_lamports` |
| Salida rutinaria (take profit, tiempo máximo) | `exit_priority_level` | igual que la entrada |
| Salida de protección (stop, trailing, emergencia, kill switch, venta del origen, manual) o cualquier salida que ya falló una vez | `veryHigh` | el tope completo: que aterrice importa más que la comisión |

Jupiter paga su estimación del mercado para ese nivel, como mucho el tope.
Con `jito_tip_lamports > 0` se paga una **propina de Jito** en lugar de
priority fee: el percentil `jito_tip_percentile` de las propinas que están
aterrizando (API pública de Jito, cada 30 s), entre `min_jito_tip_lamports`
y `jito_tip_lamports`, y con el mismo tope por tamaño.

Las comisiones que pagan tus operaciones reales se guardan en cada orden.
Con `fee_min_samples` operaciones, su mediana sustituye a
`expected_priority_fee_lamports` en el modelo de costes (filtro de coste,
valor esperado, paper y replicación).

**Rutas de envío.** La transacción firmada sale a la vez por todas las rutas
activas y la primera que la acepta desbloquea la confirmación (la firma es la
misma: solo puede ejecutarse una vez):
- el RPC principal;
- `extra_send_urls` y el secreto `SOLANA_SEND_RPC_URLS` (p. ej. un endpoint
  de envío con conexión *staked*);
- `send_via_jito`: el block engine de Jito (`jito_block_engine_urls`), solo
  para transacciones con propina;
- `jito_only`: **solo** Jito y en modo *bundle-only*. Protege de sándwiches
  (nadie ve la transacción antes de que entre en un bloque), pero solo
  aterriza cuando el líder es un validador de Jito, así que puede tardar
  algo más. Las salidas de protección (stop, trailing, emergencia…) no lo
  aplican: salen por todas las rutas, porque que aterricen importa más.

Las URLs de envío y de propinas solo se cambian en el YAML (claves
bloqueadas): deciden a dónde viajan tus transacciones firmadas.

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

**Precios históricos reales** (`historical_prices`, solo con `providers.mode:
live`): sin ellos, con datos reales el backtest solo conoce el precio de las
operaciones de las wallets, así que un stop loss o un take profit no pueden
ocurrir entre dos operaciones. Con ellos:
- Se descargan velas de `candle_minutes` del pool principal de cada token
  comprado en el periodo (GeckoTerminal, API pública sin clave,
  `providers.geckoterminal`), empezando por los más comprados y hasta
  `max_price_tokens`. Se guardan en la base de datos: el siguiente backtest
  solo descarga lo que falte. La primera vez puede tardar (unas 30 peticiones
  por minuto); el progreso se ve en *Backtest → Ejecuciones*.
- Un token sin historial no se vuelve a pedir hasta pasadas
  `refetch_failed_after_hours`; los fallos transitorios (límite de peticiones,
  red) se reintentan en el siguiente backtest.
- Stops, take profits, trailing y tiempo máximo se evalúan vela a vela. Como
  una vela no dice si llegó antes el mínimo o el máximo, se supone el **peor
  orden**: primero el stop (al precio del stop, o a la apertura si la vela
  abrió por debajo), después el take profit (a su nivel), y el resto al cierre.
- Sin mirar al futuro: una posición solo usa velas que empiezan después de
  abrirla y ya han cerrado; las entradas y las salidas por venta de la wallet
  usan el precio de su operación.
- La liquidez histórica sale de lo que el propio bot observó mientras
  funcionaba (`token_snapshots`); antes de eso, la salida por caída de
  liquidez no se puede simular.

El resultado indica para cuántos tokens hubo velas. Con poca cobertura, el
backtest se parece al de antes (solo precios de operación).

### `filters`
Filtros por señal, aplicados en cada decisión además de los límites de riesgo.
Cada uno aparece en la explicación de la decisión:

- **Valor esperado de la copia** (`min_expected_value_pct`, 0 % por defecto):
  la ventaja copiable efectiva de la wallet ya descuenta las comisiones de una
  copia de tamaño típico; si esta copia es más pequeña, paga proporcionalmente
  más en comisiones fijas y se resta. Si el resultado queda por debajo del
  mínimo, no se copia.
- **Confluencia**: otras wallets seguidas que compraron el mismo token en
  `confluence_window_minutes`. Solo cuentan las creíbles e independientes: no
  bloqueadas (las de wash trading o grupos coordinados no cuentan) y ACTIVAS o
  con ventaja copiable positiva (una wallet aleatoria comprando no confirma nada). Cada una sube el tamaño un
  `confluence_size_bonus` hasta `confluence_max_mult`; los topes de riesgo
  siguen mandando. `min_confluence_wallets` permite exigirla.
- **Riesgos de RugCheck bloqueantes** (`blocked_risk_flags`): si el informe del
  token incluye un riesgo cuyo nombre contiene alguno de estos textos (holders
  muy concentrados, un único holder dominante, liquidez sin bloquear, creador
  con rugs previos…), no se compra. Edita la lista según lo que veas en los
  avisos de tus señales.
- **Ruta de venta** (`check_sell_route`): antes de comprar se cotiza la venta
  de lo que recibirías. Sin ruta de venta, o si comprar y vender al instante
  perdería más de `max_round_trip_quote_loss_pct`, no se compra. Detecta
  tokens sin salida o con liquidez de venta muy pobre (no sustituye a los
  filtros de autoridades y extensiones de Token-2022, que ya se aplican).
  La venta se cotiza **a la vez** que la compra (con la cantidad esperada) y
  un token que pasó hace menos de `sell_route_cache_seconds` con un tamaño
  similar no se vuelve a cotizar.
- **Régimen de mercado** (movimiento de SOL en 24 h, calculado en cada
  evaluación): `regime_size_multipliers` reduce el tamaño en regímenes
  violentos (×0,5 en caída extrema por defecto) y `block_regimes` detiene las
  entradas en los que indiques.

### `learning`
Aprender de tus propias copias:

- **Ventaja copiable efectiva**: la estimación del modelo de copia se corrige
  con lo que realmente devuelven tus copias cerradas (paper y real, netas de
  comisiones): `(k · estimación + n · real) / (k + n)`, con
  `k = feedback_prior_positions`. Con pocas copias manda la estimación; con
  muchas, la realidad. El score (`copy_edge`) y la regla
  `status_rules.min_copy_expectancy_pct` usan este valor.
- **Copiarla pierde en la práctica**: con al menos `losing_min_positions`
  copias cerradas en `feedback_window_days` y un escenario optimista (límite
  superior de su media) negativo, la wallet pasa a OBSERVAR.
- **Periodo de prueba** (`probation_*`): con el trading real activo, una wallet
  se copia en **paper** hasta tener `probation_min_positions` copias paper
  cerradas con un retorno medio ≥ `probation_min_return_pct`. Solo entonces
  sus señales usan dinero real. También aplica a la whitelist. La decisión
  muestra el estado ("en prueba: 2/5…" o "superado").

### `measurement`
Seguimiento de lo que hizo el precio después de cada decisión de copia,
ejecutada o rechazada, a los horizontes de `outcome_horizons_minutes` (5 min,
1 h y 24 h por defecto). Si una medida llega demasiado tarde (por ejemplo, la
app estuvo parada) se registra como "no medida" en lugar de con un precio que
no corresponde. Alimenta la página *Análisis*.

## 6. Costes de red

Cada operación paga costes fijos que no dependen de su tamaño, así que pesan
mucho en operaciones pequeñas. El mismo modelo se usa en paper trading, en el
backtest y en el filtro `risk.max_round_trip_cost_pct`:

| Coste | Cuándo | Valor |
|---|---|---|
| Comisión base | cada transacción | 5.000 lamports (`paper.network_fee_sol`) |
| Priority fee **o** tip de Jito | cada transacción (nunca ambos) | la mediana real de tus últimas operaciones; si aún no hay suficientes, `execution.expected_priority_fee_lamports`, la propina de mercado de Jito o el tope. Siempre ≤ `priority_fee_max_trade_pct` del tamaño |
| Alquiler de la cuenta del token | al comprar un token nuevo | ~0,00204 SOL; se **recupera** al cerrar la cuenta vacía tras vender (`close_empty_token_accounts: true`) y solo es coste si desactivas el cierre |

Sin tope por tamaño, el tope de priority fee de 0,001 SOL haría que una
compra y su venta costasen ~0,002 SOL: con SOL a 200 USD, ~0,40 USD, un 4 %
de una operación de 10 USD. Con `priority_fee_max_trade_pct: 0.5` cada
transacción paga como mucho el 0,5 % del tamaño (~1 % ida y vuelta), a cambio
de menos prioridad en operaciones pequeñas cuando la red está congestionada.
El filtro rechaza por defecto las que superen el 3 %.

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
| `SOLANA_WS_URL_BACKUP` | stream de respaldo (`providers.solana.backup_stream`), mejor de otro proveedor |
| `SOLANA_SEND_RPC_URLS` | RPC extra que también reciben cada transacción, separados por comas |
| `JITO_AUTH_UUID` | opcional: cabecera `x-jito-auth` para límites de envío más altos |
| `JUPITER_API_KEY` | opcional (API de pago de Jupiter) |
| `HELIUS_API_KEY` | no se usa: con Helius, la clave ya va dentro de `SOLANA_RPC_URL` y `SOLANA_WS_URL` |
| `TELEGRAM_BOT_TOKEN`, `DISCORD_WEBHOOK_URL` | notificaciones |
| `SIGNER_HMAC_KEY` (+ `SIGNER_HMAC_KEY_PREVIOUS` al rotar) | autenticación app ↔ firmador |
| `KEYSTORE_PASSPHRASE` | solo con `signer_mode: local` (en `remote` la tiene únicamente el firmador) |
| `DATA_ENCRYPTION_KEY` | cifrado de campos (secreto TOTP) |

Genera valores fuertes con `copytrader gen-secret`.
