# Revisión técnica final

Revisión completa hecha al terminar el desarrollo, con foco en lo que puede
causar una **pérdida inesperada de fondos**: errores, vulnerabilidades,
arquitectura, condiciones de carrera, operaciones duplicadas y latencia.

## 1. Método

- Lectura dirigida de cada camino por el que se mueve dinero: señal →
  pipeline → Risk Engine → ejecución → fill → posición → salida →
  recuperación tras reinicio.
- Pruebas: 140+ tests (unitarios e integración) que pasan en **SQLite y en
  PostgreSQL 16**, incluidos tests de concurrencia que lanzan señales en
  paralelo, entregan la misma señal dos veces a la vez y hacen competir un stop
  loss con una venta de origen.
- Ejecución real de la aplicación (mercado simulado) desde un clon limpio y
  del dashboard en navegador (tema claro, oscuro y móvil).
- `ruff` y `mypy` (con `check_untyped_defs`) sin errores.

## 2. Hallazgos corregidos

| # | Gravedad | Problema | Consecuencia posible | Corrección | Prueba |
|---|---|---|---|---|---|
| 1 | Crítica | **Condición de carrera en el Risk Engine**: señales simultáneas de tokens distintos leían la exposición antes de que las otras la reservaran; además, con lecturas por sentencia (SQLite autocommit, PostgreSQL READ COMMITTED) una orden podía pasar de "en vuelo" a "posición" entre dos lecturas y no contarse en ninguna | Superar `max_open_positions` / exposición máxima (se reprodujo: 3 posiciones con límite 2) | Reservas en memoria bajo lock; la exposición se lee en el orden reservas → órdenes en vuelo → posiciones, de modo que una transición nunca desaparece del cálculo | `test_parallel_signals_respect_max_open_positions` (10/10 en SQLite, 6/6 en PostgreSQL) |
| 2 | Alta | Dos procesos contra la misma base de datos | Las reservas son por proceso: juntos podían superar los límites | **Bloqueo de instancia única** (advisory lock en PostgreSQL, `flock` en SQLite): la segunda instancia no arranca | `test_instance_lock.py` y prueba manual |
| 3 | Alta | `levels.require_arm` se podía desactivar en caliente desde la API | Saltarse la 3ª compuerta del dinero real con una sesión robada | Clave bloqueada (solo YAML + reinicio) | `test_service_blocks_non_runtime_sections`, `test_invalid_or_locked_config_is_rejected` |
| 4 | Alta | Aplicar/revertir configuración y desactivar el kill switch diario no pedían contraseña | Con una sesión robada se podían relajar los límites de riesgo o reabrir entradas tras la pérdida diaria | Reautenticación (contraseña + TOTP) en toda acción que aumenta el riesgo | `test_invalid_or_locked_config_is_rejected` |
| 5 | Alta | Dejar de seguir una wallet con posiciones copiadas abiertas cortaba su seguimiento | Posiciones en modo espejo "huérfanas" (solo las protegía el stop de emergencia) | La wallet se sigue vigilando **solo para espejar salidas** hasta cerrar sus posiciones; nunca genera entradas | `test_untracked_wallet_keeps_mirroring_exits_of_open_positions` |
| 6 | Alta | Venta de la wallet origen mientras nuestra entrada estaba pendiente (la posición aún no existía), o recuperada por backfill/catch-up sin señal | Mantener una posición de la que el origen ya salió | Red de seguridad en el gestor de posiciones: si la última operación del origen en ese token es una venta total posterior a la compra copiada, se cierra (tras 20 s de gracia para el flujo normal) | `test_missed_source_exit_is_closed_by_safety_net` |
| 7 | Media | Whitelist + OBSERVAR: la wallet ocupaba plaza en la selección pero el motor de señales no la copiaba (definiciones distintas de "copiable") | Selección incoherente y difícil de razonar | Definición única `copyable()` usada por selector, señales y pipeline; OBSERVAR nunca se copia, tampoco en whitelist | `test_selection_whitelist_in_observe_is_not_selected` |
| 8 | Media | Posiciones sin precio de mercado (indexador retrasado, par retirado) | SL/TP sin evaluar | Precio de respaldo a partir de la cotización de venta ejecutable; si tampoco hay, alerta "posición sin precio" | revisión + tests de salida |
| 9 | Media | Señal de una entrada pendiente se quedaba en APROBADA aunque luego se confirmara o fallara | Historial engañoso | La resolución tardía actualiza la señal (EJECUTADA/FALLIDA) sin degradar nunca un estado ya resuelto | `test_timeout_keeps_order_pending_then_recovery_applies_fill_once` |
| 10 | Media | Sin comprobación del reloj local | Edad de señal, TTL y edad de cotización se miden con el reloj local: un reloj desviado invalida esas protecciones | Preflight compara con la hora de bloque de la red (≤ 10 s) | preflight |
| 11 | Baja | El catch-up periódico volvía a descargar las transacciones que no son swaps | Coste y rate limits del RPC | Cursor en memoria de la última firma totalmente procesada | revisión |
| 12 | Baja | `event_log` no se llenaba | Sin trazabilidad de extremo a extremo | Línea temporal por traza: señal, decisión, cada transición de orden, fill, salida y cierre; visible en el detalle de señal | tests de flujo |
| 13 | Baja | Motivos de rechazo ambiguos ("Market cap en rango: $71,863") y "limitado por model" | Explicaciones confusas | "No cumple — …" con el rango esperado; el sizing nombra el tope que lo limitó | revisión visual |
| 14 | Baja | Detalle de wallet filtraba en Python las 50 últimas posiciones globales | Posiciones de la wallet ausentes | Filtro en SQL por wallet origen | revisión |

Corregidos antes, durante el desarrollo: swaps simulados entregados con
edad 0; histéresis de selección que excluía una wallet mejor que otra
retenida; error 500 al serializar dataclasses con `slots`; drawdown mayor que
100 %; modal que persistía al cambiar de vista; ticks de ejes duplicados.

## 3. Propiedades verificadas

| Propiedad | Cómo se garantiza | Verificado por |
|---|---|---|
| Una señal nunca se ejecuta dos veces | `signal_key` y `client_order_id` deterministas con `UNIQUE`; `INSERT … ON CONFLICT DO NOTHING` | `test_same_signal_delivered_concurrently_executes_once`, test de duplicados |
| Un fill se aplica exactamente una vez | fila de ejecución `UNIQUE` por orden, en la misma transacción que la posición | `test_timeout_keeps_order_pending_then_recovery_applies_fill_once` |
| Una posición no se vende dos veces | lock por token + estado `closing` + `SELECT … FOR UPDATE` | `test_stop_loss_and_source_sell_race_close_once` |
| Nunca se reenvía a ciegas | firma y `lastValidBlockHeight` persistidos antes de enviar; caducado → EXPIRED | `test_confirmed_live_entry_persists_signature_before_send`, `test_expired_blockhash_marks_expired_and_never_resends` |
| Reinicio seguro | recuperación de órdenes en vuelo contra la cadena; entradas pendientes caducan; salidas pendientes se reprocesan; vuelve desarmado | `test_restart_resolves_signed_live_order`, `test_restart_recovery_expires_pending_entries` |
| La blacklist nunca opera | clasificación IGNORE antes de crear señal | `test_blacklisted_wallet_never_copies` |
| El tamaño nunca depende del importe del origen | sizing propio con topes | `test_size_never_depends_on_source_amount_and_is_capped` |
| Límites absolutos imposibles de saltar por configuración | validación + guardia de ejecución independiente | `test_guard_blocks_oversized_live_entry`, `test_guard_blocks_live_when_disarmed` |
| Las salidas funcionan con kill switch activo | los kill switches solo bloquean entradas | tests de riesgo |
| El firmador no firma transferencias a terceros | política independiente | `test_policy_rejects_*` |
| Anti-replay app ↔ firmador | HMAC + timestamp + nonce único | `test_hmac_accepts_valid_and_blocks_replay` |
| La clave privada no sale del firmador | ni API, ni BD, ni logs, ni notificaciones; redactor en dos capas | `tests/unit/test_redaction.py`, revisión |

## 4. Riesgos residuales y limitaciones conocidas

Estos riesgos **no están eliminados**; conviene conocerlos antes de usar dinero real.

1. **El firmador no decodifica la ruta de Jupiter.** Un proceso de trading
   comprometido podría pedir un swap a mal precio dentro de los topes del
   firmador. Mitigado por la wallet dedicada con capital mínimo y los topes
   diarios del firmador (ver mejora P1).
2. **Latencia estructural del copy trading.** Siempre llegas después de la
   wallet origen; en tokens de poca liquidez el precio ya se ha movido. Las
   protecciones (edad, desviación, slippage, TTL) evitan malas entradas, pero
   a cambio muchas señales caducan. Es un coste, no un fallo.
3. **Sesgos en la evaluación.** Si eliges las 100 wallets de listas de
   "ganadores", hay sesgo de supervivencia: su pasado parece mejor que su
   futuro esperado. El shrinkage por muestra, el peso de lo reciente, la
   detección de deterioro y el backtest fuera de muestra lo reducen, **no lo
   eliminan**. El backtest simula latencia y slippage; la realidad suele ser peor.
4. **Cobertura del parser.** Se reconocen swaps por variación de saldos
   (independiente del DEX) contra SOL/USDC/USDT y token↔token con precio.
   Operaciones ejecutadas por cuentas intermedias (algunos bots custodiales),
   transacciones ambiguas o multi-salto sin precio se ignoran a propósito:
   mejor no copiar que copiar mal.
5. **Datos de mercado.** Precios de DexScreener/Jupiter con retraso o
   manipulables en pools pequeños pueden disparar o retrasar un stop.
   `max_data_age_seconds` y el stop de emergencia acotan el daño.
6. **Tokens trampa.** Autoridad de freeze/mint, extensiones peligrosas de
   Token-2022 e informe de RugCheck se comprueban, pero un honeypot nuevo
   puede pasar. Si la salida falla, se reintenta y alerta.
7. **Un solo proveedor RPC.** Hay circuit breaker y reconexión, pero no
   conmutación automática a un segundo RPC.
8. **Alertas dentro de la propia app.** Si la app cae, no avisa ella misma.
   El compose trae Prometheus/Grafana, pero no reglas de Alertmanager.
9. **Día UTC.** El kill switch diario se rearma al cambiar el día UTC, no en tu
   zona horaria.
10. **Coste del backfill inicial** con RPC gratuito (horas para 100 wallets).
11. **SQLite** vale para la demo y para empezar; para 24/7 usa PostgreSQL.
12. **Armado en memoria.** Tras un reinicio el sistema vuelve a paper hasta que
    lo armes: es seguro, pero implica que un reinicio de madrugada detiene el
    trading real hasta tu intervención.

## 5. Mejoras propuestas (por prioridad)

**P1 — antes de subir capital de forma significativa**

- **Validación de la ruta en el firmador**: decodificar la instrucción de
  Jupiter (mints de entrada/salida, importe, `minOut`) o simular la
  transacción y exigir que los cambios de saldo de la wallet del bot
  correspondan al swap declarado dentro del slippage. Cierra el riesgo 1.
- **Alertas externas** (Alertmanager o un healthcheck externo): stream caído,
  kill switch activo, salidas fallidas, descuadre de saldos, app caída.
- **RPC secundario** con conmutación automática y confirmación contra una
  segunda fuente.

**P2 — calidad de ejecución y datos**

- Stream de menor latencia (Yellowstone gRPC / LaserStream de Helius).
  *(Hecho: stream de respaldo en paralelo; pendiente gRPC.)*
- Estimación dinámica de priority fee y envío vía bundles de Jito para
  reducir el riesgo de sandwich. *(Hecho: ver §6.)*
- Comprobación de honeypot antes de comprar: cotizar y simular la venta.
  *(Hecho en parte: se cotiza la venta; la simulación en cadena está pendiente.)*
- Backfill con la API de transacciones enriquecidas de Helius (menos llamadas).
- **"Copiabilidad" por wallet**: comparar el resultado de *nuestras* copias con
  el de la wallet origen y degradar automáticamente las wallets cuyo
  resultado no se puede replicar.

**P3 — operación**

- Zona horaria configurable para los límites diarios.
- Copias de seguridad automáticas cifradas y prueba periódica de restauración.
- Rotación programada de secretos con recordatorio.

## 6. Rentabilidad: hallazgos y hoja de ruta

Una copia solo gana si la ventaja de la wallet supera lo que se pierde por
llegar tarde y por los costes de ejecución. Revisión específica de esos puntos:

**Implementado**

| Hallazgo | Corrección |
|---|---|
| El paper trading cobraba 0,000105 SOL por transacción, mientras la real paga hasta 0,001 SOL de priority fee; el backtest usaba 0,05 USD fijos. Con operaciones de 10-20 USD, el paper sobrestimaba el resultado en ~2-4 % por operación | Modelo de costes único (`execution/costs.py`) para paper, backtest y decisión: comisión base + priority fee esperada o tip de Jito + alquiler de cuenta si no se cierra |
| El alquiler de la cuenta de cada token (~0,002 SOL) nunca se recuperaba | Cierre automático de cuentas vacías tras vender, verificado por la política del firmador |
| Operaciones cuyo coste fijo se come la ventaja | Filtro `risk.max_round_trip_cost_pct` (3 % por defecto) con explicación en la decisión |
| Las cotizaciones y swaps de órdenes compartían el límite de 1 petición/s de Jupiter con la consulta de precios y podían esperar detrás de ella | Rate limit con prioridades: ejecución antes que precios en segundo plano |
| Las wallets se puntuaban por su PnL a sus propios precios, no por lo que puede capturar quien las copia | **PnL replicado** por wallet (latencia medida, impacto de su compra y la tuya, deriva durante el retraso, slippage, comisiones): componente `copy_edge` del score y regla que pasa a OBSERVAR las wallets no replicables |
| Un único retraso máximo de señal (20 s) para scalpers y wallets de horas | Retraso máximo por wallet según su holding mediano |
| Cada señal se juzgaba solo con límites de riesgo, sin considerar su valor esperado, la confirmación de otras wallets, la posibilidad de vender ni el estado del mercado | **Filtros por señal**: valor esperado tras los costes de esa copia, confluencia de wallets independientes (sube el tamaño), riesgos de RugCheck bloqueantes (holders concentrados, liquidez sin bloquear…), cotización de la venta antes de comprar, y régimen de mercado (reduce el tamaño o bloquea) |
| La selección no aprendía de lo que realmente devolvía copiar cada wallet, y una wallet recién seleccionada operaba con dinero real desde su primera señal | **Aprendizaje propio**: la ventaja copiable se corrige con las copias reales (mezcla bayesiana con la estimación) y las wallets que pierden al copiarlas pasan a OBSERVAR; **periodo de prueba** en paper por wallet antes del dinero real |
| No se medía qué pasaba con lo rechazado ni de dónde venía el resultado | **Medición**: seguimiento del precio tras cada decisión (también las rechazadas) y página *Análisis* con la lectura de cada filtro, resultado real vs estimado por wallet, por salida, por retraso y costes; **comparación de configuraciones** en el backtest |
| Un único stream (si iba lento o caía, se llegaba tarde) y descarga de cada transacción con esperas de 250 ms, 500 ms, 750 ms… | **Stream de respaldo** en paralelo (gana el primero; la transacción completa de un stream lento ahorra la descarga del otro) y reintentos cortos que crecen (100 ms ×1,6) |
| La misma priority fee máxima para una entrada de 10 USD que para una salida por stop loss; el modelo de costes asumía siempre el tope | **Comisiones dinámicas**: nivel por urgencia (las salidas de protección y los reintentos pagan el máximo), tope en % del tamaño para lo demás, propina de Jito según el mercado, y el modelo de costes usa la mediana de lo que pagan de verdad tus operaciones |
| Cada transacción salía solo por el RPC principal | **Envío por varias rutas** a la vez (RPC, RPC extra, block engine de Jito) y modo solo-Jito *bundle-only* contra sándwiches |
| La cotización de la venta (ruta de salida) esperaba a la de la compra y se repetía para cada señal del mismo token; los datos de mercado del token esperaban a los de RugCheck/mint | Cotización de venta en paralelo con la compra, caché de 5 min por token y datos del token pedidos a la vez a sus tres fuentes |
| Una posición cuyo pool perdía la liquidez (rug, LP retirado) solo se cerraba cuando el precio caía hasta el stop | **Salida por caída de liquidez** (−50 % desde la entrada, confirmada dos veces), urgente y activa en todos los modos |
| Mismos stop, tiempo máximo y take profit para cualquier token y wallet | **Perfil de salida adaptativo** (stop según volatilidad con el tamaño ajustado para arriesgar lo mismo; tiempo y TP según la wallet) y **salida cuando venden varias wallets fiables**: implementados y medidos, pero desactivados por defecto (ver abajo) |

Efecto medido en el mercado simulado (walk-forward, mismos datos): con los
costes reales, la selección anterior elegía también scalpers y una wallet
aleatoria y **perdía un 12,7 %** (profit factor 0,81); con la replicación
selecciona solo las wallets con ventaja copiable y **gana un 19,2 %** con un
drawdown máximo del 2,8 % (profit factor 1,83), por encima de las referencias
"copiar todo" (−36 %) y "elegir por PnL" (+15,6 %). Es un mercado sintético:
confirma que el mecanismo funciona, no que el mercado real vaya a ser rentable.

Salidas medidas en el mismo backtest walk-forward (tres universos simulados;
rentabilidad de la estrategia con y sin cada salida):

| Salida | Modo protegido (por defecto) | Modo inteligente |
|---|---|---|
| Stop según volatilidad | +23,0 → +14,4 % · +25,9 → +20,8 % · +26,2 → +8,7 % | +57,3 → +40,6 % · +40,3 → +34,4 % · +27,5 → +8,7 % |
| Perfil de tiempo de la wallet | sin cambio | +57,3 → +35,5 % · +40,3 → +41,3 % · +27,5 → +34,3 % |
| Ventas de varias wallets | +23,0 → +23,1 % · +25,9 → +25,9 % · +26,2 → +25,3 % | +57,3 → +53,4 % · +40,3 → +41,8 % · +27,5 → +19,5 % |
| Caída de liquidez | sin cambio (ningún rug entre los tokens copiados) | sin cambio (1 salida) |

Por eso solo la salida por liquidez viene activada: es un seguro que no costó
nada. Las otras quedan disponibles para probarlas con una variante del
backtest y en paper. El mercado simulado no reproduce, por ejemplo, que las
buenas wallets salgan juntas de un token por una razón real, así que no
demuestra que esas salidas no sirvan en el mercado real; solo que activarlas
sin medir no está justificado.

**Pendiente, por impacto esperado**

1. **Varias configuraciones en paper en paralelo** sobre las señales reales
   (hoy la comparación de configuraciones se hace en el backtest, y el efecto
   de los filtros se mide con el seguimiento de señales rechazadas). Requiere
   separar libros de paper por variante en posiciones, riesgo y salidas.
2. **Más filtros por señal** con datos que hoy no se obtienen de forma
   fiable: presión compradora reciente, concentración de holders calculada
   directamente (no solo el aviso de RugCheck) y simulación completa de la
   venta en cadena.
3. **Backtest con histórico de precios real** (hoy, con datos reales, no puede
   evaluar stop loss ni take profit entre operaciones, ni las salidas
   adaptativas: se calculan sobre la serie de precios).
4. **Medir en la sombra** las salidas desactivadas (qué habría pasado si
   hubieran saltado), como ya se hace con las señales rechazadas.
5. **Ejecución, siguiente paso**: stream gRPC (Yellowstone / LaserStream), que
   necesita un proveedor de pago y otra dependencia; confirmación por
   suscripción en lugar de consultar el estado cada 0,4 s.

## 7. Conclusión

- **Niveles 1-3** (análisis, alertas, paper): listos para usar.
- **Niveles 4-5** (dinero real): técnicamente preparados y protegidos por
  tres compuertas, límites absolutos, un Risk Engine independiente, un
  firmador aislado con su propia política y kill switches. Úsalos solo tras
  completar la checklist de [SECURITY.md](SECURITY.md#9-checklist-antes-del-dinero-real),
  semanas de paper trading y, idealmente, la mejora P1 del firmador.
- Nada en este sistema garantiza rentabilidad. Está construido para que,
  cuando algo salga mal, la pérdida quede acotada y explicada.
