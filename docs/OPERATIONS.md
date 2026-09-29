# Operación

Recorrido completo, en el orden recomendado: añadir wallets → analizar →
alertas → paper trading → backtesting → wallet de ejecución → dinero real
pequeño → dinero real. **No te saltes pasos**: cada nivel existe para detectar
problemas con menos dinero en juego.

## 1. Niveles operativos

| Nivel | Qué hace | Dinero real |
|---|---|---|
| 1 · Análisis | backfill, métricas, scoring, detección, selección | no |
| 2 · Alertas | además detecta operaciones en tiempo real y avisa | no |
| 3 · Paper | además simula automáticamente cada copia (con cotizaciones reales) | no |
| 4 · Real pequeño | ejecuta con los topes de `levels.level4` | sí, acotado |
| 5 · Real | ejecuta con los límites de `risk` | sí |

- `app.operating_level` (YAML) es el **techo**. En *Sistema* del dashboard
  puedes bajar o subir el nivel hasta ese techo; subir a 4-5 pide tu contraseña.
- Si el techo es 4-5 y nunca has elegido nivel, el sistema arranca en 3.
- En niveles 4-5, si alguna compuerta está cerrada, las entradas se simulan
  (paper) y el dashboard muestra el motivo exacto.

## 2. Añadir las 100 wallets

### Por CSV (recomendado)

`config/wallets.example.csv`:

```csv
address,label,list,notes
7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU,Trader A,,encontrado en X
9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM,Trader B,whitelist,confianza alta
```

- `list`: vacío, `whitelist`, `watchlist` o `blacklist`. Cabecera y líneas `#` opcionales.
- Las direcciones se validan; las repetidas se actualizan
  en lugar de duplicarse. Máximo `wallets.max_wallets` (100).

```bash
copytrader wallets import mis_wallets.csv
# Docker:
docker compose exec -T app copytrader wallets import /dev/stdin < mis_wallets.csv
```

O en el dashboard: *Wallets → Importar CSV* (pegas el contenido).

### Una a una

```bash
copytrader wallets add <DIRECCIÓN> --label "Trader X" [--list whitelist|watchlist|blacklist]
copytrader wallets list
```

O *Wallets → Añadir*.

### Qué significa cada lista

- **Whitelist**: prioridad en la selección y score mínimo propio
  (`selection.whitelist_min_score`). Nunca salta un estado BLOQUEADA u OBSERVAR.
- **Watchlist**: solo alertas; nunca se copia.
- **Blacklist**: nunca genera operaciones automáticas, ni siquiera alertas.

Dejar de seguir una wallet (*Eliminar*) conserva su histórico. Si tenía
posiciones copiadas abiertas, se sigue vigilando **solo para espejar sus
salidas** hasta que se cierren; nunca abre posiciones nuevas.

### Backfill y evaluación

Al arrancar (y en cada ciclo de `analysis.recompute_interval_seconds`) se
descarga el histórico de las wallets nuevas y se re-evalúan todas. Para
forzarlo:

```bash
copytrader backfill            # solo las que no tienen histórico (--force: todas)
copytrader evaluate            # métricas → detección → score → estado → selección
```

Coste: cada wallet necesita 1 llamada `getSignaturesForAddress` por cada
1.000 transacciones y 1 `getTransaction` por transacción (hasta
`backfill_max_signatures_per_wallet`, 1.000 por defecto). Con 100 wallets son
~100.000 llamadas la primera vez: con un RPC gratuito puede tardar horas; con
un plan de pago, minutos. Las siguientes evaluaciones solo descargan lo nuevo.

Resultado en *Wallets*: score, estado (ACTIVA/OBSERVAR/BLOQUEADA) con motivos,
métricas, si está seleccionada y **"Copia est./op"**: el retorno medio por
operación que estimamos que obtendrías copiándola con tu latencia, tamaño y
costes (su propio retorno aparece al pasar el ratón). El detalle de cada
wallet explica cada componente del score, cada aviso y, en *Si la copias*,
cuánto cuesta copiarla y el retraso máximo que se acepta para sus señales.

Una wallet muy rentable puede quedar en OBSERVAR con el motivo "Ventaja no
replicable": suele ser un scalper o alguien que compra mucho en pools pequeños.
Su ventaja es real, pero no la puedes capturar llegando segundos tarde.

## 3. Alertas (nivel 2) y notificaciones

Telegram:
1. Crea un bot con @BotFather y guarda el token en el secreto `TELEGRAM_BOT_TOKEN`.
2. Escribe al bot, abre `https://api.telegram.org/bot<TOKEN>/getUpdates` y
   copia `chat.id`.
3. En la configuración: `notifications.telegram_enabled: true` y
   `notifications.telegram_chat_id: <id>`.

Discord: crea un webhook en el canal y guárdalo en `DISCORD_WEBHOOK_URL`;
`notifications.discord_enabled: true`.

Recibirás compras/ventas detectadas, decisiones (aprobada/rechazada con la
razón), ejecuciones, cierres con PnL, kill switches, errores críticos y
cambios de estado de wallets. Las notificaciones nunca contienen secretos
(se enmascaran).

## 4. Activar el paper trading (nivel 3)

1. `app.operating_level: 3` en el YAML (o mayor) y reinicia; o, si el techo ya
   lo permite, *Sistema → Nivel 3*.
2. Ajusta `risk.capital_usd` al capital que **de verdad** piensas usar, y los
   límites de `risk`/`sizing`/`exits`. El paper trading usa las mismas reglas
   que el real.
3. Deja `paper.use_real_quotes: true`: cada copia pide una cotización real a
   Jupiter y aplica latencia, slippage y los mismos costes de red que pagaría
   la transacción real (priority fee o tip y, si desactivas el cierre de
   cuentas, el alquiler de la cuenta del token). Ver
   [CONFIGURATION.md §6](CONFIGURATION.md#6-costes-de-red).
4. Sigue en *Resumen*, *Señales* (cada decisión con sus comprobaciones y su
   línea temporal), *Posiciones* y *Operaciones*.

Mínimo recomendado: **2-4 semanas** en paper antes del nivel 4 (el preflight
exige `levels.preflight_min_paper_days`, 7 por defecto). Mira sobre todo:
slippage real frente al esperado, cuántas señales caducan por latencia, qué
parte del resultado se va en comisiones, y si el resultado depende de una o
dos operaciones. Si muchas entradas se rechazan por "Coste de red de ida y
vuelta", tu tamaño por operación es demasiado pequeño para las comisiones:
sube el capital por operación o baja la priority fee, no relajes el filtro.

### Backtesting

*Backtest* en el dashboard, o:

```bash
copytrader backtest --train-days 30 --test-days 7 --top-n 10 --exit-mode protected
```

Walk-forward: selecciona wallets solo con datos del periodo de entrenamiento y
mide en el de test, que no ha visto. Se compara con dos referencias (copiar a
todas, y elegir por PnL bruto). Si la estrategia no bate a las referencias
fuera de muestra, no subas de nivel.

**Comparar configuraciones**: en *Backtest → Comparar configuraciones* añade
hasta 2 variantes como cambios en JSON (las mismas secciones y límites que la
página *Configuración*), por ejemplo `{"risk": {"max_slippage_pct": 5}}` o
`{"exits": {"default_mode": "smart"}}`. Se ejecutan sobre los mismos datos y
ventanas que la configuración actual y se muestran lado a lado. Aplica un
cambio solo si mejora de forma clara y con suficientes operaciones; una
diferencia pequeña suele ser ruido.

**Precios reales**: con datos reales, el backtest descarga (una vez) velas
históricas de los tokens comprados en el periodo, para que los stops y take
profits ocurran también entre operaciones. La columna *Precios* de las
ejecuciones dice para cuántos tokens hubo velas; si la cobertura es baja, los
resultados de salidas por precio son poco fiables. Es la forma de probar con
datos reales las salidas desactivadas (stop por volatilidad, perfil de la
wallet): lanza el backtest con una variante que las active.

### Filtros por señal

Además de los límites de riesgo, cada decisión aplica los filtros de
`filters.*` ([CONFIGURATION.md](CONFIGURATION.md#filters)): valor esperado
tras costes, confluencia de wallets, riesgos de RugCheck, ruta de venta y
régimen de mercado. Usa *Análisis → ¿Protegen los filtros?* para comprobar con
tus datos si cada uno protege o solo te cuesta oportunidades antes de
cambiarlo.

### Análisis de resultados

La página *Análisis* responde, con tus propios datos, a:

- **¿Protegen los filtros?** Para cada motivo de rechazo, qué hizo el precio
  del token a 5 min, 1 h y 24 h, frente a las señales ejecutadas. Si lo que un
  filtro rechaza sube más que lo que se ejecuta ("Revisar"), ese filtro puede
  estar costándote oportunidades; si baja ("Protege"), está haciendo su
  trabajo. Con menos de 10 medidas se indica "Muestra insuficiente".
- **Resultado por wallet**: lo que has ganado realmente copiando cada wallet
  frente a lo que estimaba el modelo de copia y a lo que gana ella.
- **Por motivo de salida** (stop loss, take profit, venta del origen…) y **por
  retraso de entrada**.
- **Costes**: resultado bruto, comisiones y neto, y el coste de entrar
  desglosado en "llegar tarde" (precio de la señal → cotización) y "ejecución"
  (cotización → precio conseguido).

## 5. Conectar la wallet de ejecución

Usa una **wallet exclusiva para el bot** con **solo el capital necesario**
(capital configurado + `risk.reserve_sol` para comisiones). Nunca tu wallet
principal.

### Crear el keystore cifrado

Instalación local (o cualquier máquina con el paquete):

```bash
copytrader keystore create --generate --out secrets/bot.keystore.json
# o importar un keypair de solana-keygen:
copytrader keystore create --from-json ~/bot-keypair.json --out secrets/bot.keystore.json
shred -u ~/bot-keypair.json        # borra el original sin cifrar
```

Pide una passphrase (mínimo 12 caracteres) y muestra la **clave pública**. El
fichero queda cifrado con scrypt + AES-256-GCM y permisos 600.

Con Docker, sin instalar nada en el host:

```bash
docker run --rm -it --network none --user "$(id -u):$(id -g)" -v "$PWD/secrets:/out" \
  --entrypoint copytrader copytrader:latest keystore create --generate --out /out/bot.keystore.json
```

Después:

```bash
chmod 444 secrets/bot.keystore.json            # el firmador (uid 10001) debe poder leerlo
chmod 600 secrets/keystore_passphrase && nano secrets/keystore_passphrase && chmod 444 secrets/keystore_passphrase
```

Envía a la clave pública el SOL necesario.

### Configurar

```yaml
providers:
  mode: live                    # datos reales (y RPC con API key en los secretos)
security:
  signer_mode: remote           # firmador aislado (recomendado)
execution:
  wallet_public_key: <CLAVE PÚBLICA DEL BOT>
```

Topes propios del firmador (independientes de la app, en `docker-compose.yml`
o como variables al hacer `up`):
`SIGNER_MAX_NOTIONAL_USD_PER_TX` (25), `SIGNER_MAX_NOTIONAL_USD_PER_DAY` (200),
`SIGNER_MAX_TX_PER_MINUTE` (10), `SIGNER_MAX_PRIORITY_FEE_LAMPORTS`,
`SIGNER_MAX_TIP_LAMPORTS`. El firmador **rechaza** cualquier transacción que
los supere, o que no encaje con un swap de la propia wallet del bot (programas
fuera de la lista permitida, transferencias o aprobaciones a terceros, otro
pagador de comisiones…), aunque la app estuviera comprometida. Límite conocido:
no decodifica la ruta de Jupiter, así que un proceso comprometido aún podría
pedir un swap a mal precio *dentro* de esos topes; por eso la wallet del bot
solo debe tener el capital imprescindible (ver [SECURITY.md](SECURITY.md)).

```bash
make up-live
```

`signer_mode: local` (la app descifra el keystore en su propio proceso) existe
para instalaciones sin Docker, pero pierde el aislamiento; evítalo con dinero
significativo.

### Ir más rápido (opcional)

La velocidad importa porque cada segundo de retraso es precio que ya se
movió. Mide primero y cambia después: la tarjeta *Sistema → Velocidad y
comisiones* muestra el retraso de copia (operación de la wallet → tu
ejecución), la detección por stream, qué rutas aceptan tus transacciones y
lo que pagas en comisiones.

1. **Stream más rápido**: `providers.solana.stream: helius_transaction_subscribe`
   (con RPC de Helius) evita descargar cada transacción aparte.
2. **Stream de respaldo**: `backup_stream` con la URL de **otro** proveedor en
   `SOLANA_WS_URL_BACKUP`. Gana el que avisa antes; si uno cae, el otro sigue.
3. **Envío por varias rutas**: `extra_send_urls` / `SOLANA_SEND_RPC_URLS` y,
   con propina, `send_via_jito: true` y `jito_tip_lamports` (el máximo; la
   propina real sigue al mercado). Sube también `SIGNER_MAX_TIP_LAMPORTS` del
   firmador hasta ese máximo, o rechazará las transacciones con propina.
4. **Protección contra sándwiches**: `jito_only: true` (solo Jito, bundle-only).
   Útil si ves slippage real peor que el cotizado; a cambio, puede tardar algo
   más en entrar. Los stops y demás salidas de protección salen siempre por
   todas las rutas.

Pagar más prioridad no convierte en rentable una wallet que no lo es:
`priority_fee_max_trade_pct` evita que las comisiones se coman operaciones
pequeñas, y el filtro de coste y el de valor esperado usan lo que de verdad
estás pagando.

Tras actualizar una instalación con Docker, ejecuta otra vez
`scripts/init-secrets.sh`: crea vacíos los secretos opcionales nuevos y no
toca los que ya existen.

## 6. Activar el trading real

Tres compuertas independientes, todas necesarias:

1. **YAML**: `app.operating_level: 4` y `levels.live_trading_enabled: true`,
   y reinicia. (Ninguna de las dos se puede cambiar desde el dashboard.)
2. **Preflight** (*Sistema → Preflight* o `copytrader preflight`): proveedores
   reales, base de datos, kill switches inactivos, RPC sano, reloj sincronizado
   con la red, firmador accesible y con la clave esperada, saldo suficiente y
   no excesivo, stream conectado, notificaciones, días de paper trading.
3. **Armar** en *Sistema → Armar trading real* (pide contraseña y TOTP si lo
   tienes). El armado vive **solo en memoria**: tras cualquier reinicio el
   sistema vuelve desarmado y opera en paper hasta que lo armes otra vez.

Empieza en **nivel 4** (topes de `levels.level4`: 20 USD por operación, 3
posiciones, 2 % de pérdida diaria por defecto) al menos 1-2 semanas.

**Periodo de prueba por wallet**: aunque el trading real esté armado, cada
wallet se sigue copiando en paper hasta tener 5 copias paper cerradas con
resultado medio no negativo (`learning.probation_*`). Así una wallet recién
seleccionada nunca estrena con tu dinero. El detalle de la wallet y cada
decisión indican si está "en prueba" o "superado". Compara
las ejecuciones reales con lo que predijo el paper trading. Solo entonces
considera el nivel 5 (`app.operating_level: 5`, reinicio, *Sistema → Nivel 5*).

Con el trading real armado, el sistema cierra cada 2 minutos las cuentas de
token que quedan vacías tras vender y recupera su alquiler (~0,002 SOL por
token). Tras la primera semana, mira en *Operaciones* la comisión media real y
ponla en `execution.expected_priority_fee_lamports`: así el paper, el backtest
y el filtro de coste usan tu coste real en lugar del tope.

Para parar el dinero real en cualquier momento: *Desarmar*, bajar a nivel 3,
o activar un kill switch.

## 7. Operación diaria

- **Resumen**: capital, PnL, exposición, posiciones, estado de las compuertas,
  kill switches y salud de proveedores.
- **Kill switches** (*Riesgo* o CLI):
  ```bash
  copytrader kill-switch on --scope global --reason "revisión"
  copytrader kill-switch off --scope global
  ```
  - *Diario*: salta solo al llegar a `max_daily_loss_pct`; se rearma al cambiar
    el día UTC.
  - *Global*: salta por pérdida semanal/mensual, pérdidas consecutivas,
    ráfagas de errores de ejecución o descuadre de saldos; solo tú lo desactivas.
  - Ambos bloquean **entradas**; las salidas siguen funcionando. Desde el
    dashboard puedes activarlo con *cerrar todas las posiciones*.
- **Cerrar una posición a mano**: *Posiciones → Cerrar*.
- **Alertas**: *Alertas* (se pueden marcar como vistas).
- **Auditoría**: cada acción sensible (nivel, armado, kill switch, cambios de
  configuración, wallets) queda registrada con usuario, IP y fecha.
- **Métricas**: `make up-monitoring` y abre Grafana (dashboard *Copy Trading Engine*):
  latencia de detección y ejecución, decisiones por motivo, errores, cola,
  exposición, PnL.

### Qué vigilar cada semana

- Wallets que pasan a OBSERVAR por deterioro (el pasado no garantiza nada).
- Motivos de rechazo más frecuentes (*Señales* filtrando por rechazadas): si
  casi todo caduca por latencia, necesitas un RPC/stream más rápido (ver
  [Ir más rápido](#ir-más-rápido-opcional)), no relajar límites.
- *Análisis*: filtros marcados "Revisar" con muestra suficiente, wallets cuyo
  resultado real queda muy por debajo del estimado, y qué parte del resultado
  se va en comisiones. Las wallets que pierden al copiarlas pasan solas a
  OBSERVAR ("Copiarla pierde en la práctica"); revisa si fue mala suerte o un
  cambio real antes de volver a incluirlas.
- *Análisis → Por motivo de salida*: qué salidas ganan y cuáles pierden. Si
  activas una salida nueva (stop por volatilidad, perfil de la wallet, ventas
  de varias wallets), pruébala antes con una variante del backtest y vigílala
  aquí en paper.
- Slippage real frente al configurado.
- Que el resultado no dependa de 1-2 operaciones.
