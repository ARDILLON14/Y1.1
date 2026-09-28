# Guía de seguridad

El objetivo es que **ningún fallo aislado** (un bug, una sesión robada, una
dependencia comprometida, un error de configuración) pueda vaciar la wallet
del bot ni exponer su clave privada.

## 1. Principios

1. **Wallet dedicada con el capital mínimo.** El bot usa su propia wallet con
   el capital configurado más la reserva de comisiones. Es el control más
   importante: acota la pérdida máxima ante cualquier fallo, incluidos los que
   no hemos previsto.
2. **La clave privada vive en un solo proceso**: el firmador, en una red
   interna sin Internet. La app de trading nunca la ve.
3. **Tres compuertas para el dinero real** (YAML + YAML + armar con
   contraseña tras el preflight) y límites absolutos compilados en el código.
4. **Defensa en profundidad**: el Risk Engine decide; la guardia de ejecución
   vuelve a comprobar los límites absolutos; el firmador aplica su propia
   política y sus propios topes sin confiar en la app.
5. **Todo queda auditado** y cada decisión, explicada.

## 2. Dónde está (y dónde nunca está) la clave privada

| Lugar | ¿Clave privada? |
|---|---|
| Keystore `bot.keystore.json` | Sí, **cifrada** (scrypt + AES-256-GCM, passphrase ≥ 12 caracteres, permisos 600/444) |
| Memoria del firmador | Sí, descifrada solo allí |
| Frontend / dashboard / API | **Nunca** (solo la clave pública) |
| Base de datos | **Nunca** |
| Logs | **Nunca** (y un redactor enmascara cualquier valor secreto o con forma de clave) |
| Variables de entorno | **Nunca** la clave; la passphrase solo como fichero secreto del firmador |
| Telegram / Discord | **Nunca** (los mensajes pasan por el mismo redactor) |
| Git | **Nunca** (`secrets/`, `.env`, `*.keystore.json` están en `.gitignore`) |

El redactor actúa en dos capas: sustituye literalmente cada secreto
configurado (API keys, tokens, passphrase, la propia clave en el firmador) y
enmascara formas de clave aunque no estén registradas (arrays JSON de keypair,
claves hex, frases mnemónicas, tokens de bot, webhooks, `api-key=` en URLs,
tokens Bearer, campos con nombres sensibles).

## 3. El firmador

- Servicio aparte (`python -m copytrader.signer_service`), perfil `live` del
  compose, en la red `signer` **interna** (sin salida a Internet), sin acceso a
  la base de datos ni a las API keys.
- **Autenticación HMAC-SHA256** de cada petición con marca de tiempo y nonce
  de un solo uso: una petición capturada no se puede reproducir (anti-replay) y
  una petición fuera de la ventana de tiempo se rechaza.
- **Política independiente** antes de firmar: decodifica la transacción y
  rechaza si el pagador no es la wallet del bot o hay otros firmantes, si
  aparece un programa fuera de la lista permitida (Jupiter, compute budget,
  system, SPL Token/Token-2022, ATA), si hay transferencias de SOL a terceros
  (solo a su propia cuenta WSOL o a una cuenta de tip de Jito, acotadas), si
  hay `Transfer`/`Approve`/`SetAuthority` de SPL, si `CloseAccount` no devuelve
  a la wallet del bot, o si la priority fee o el tip superan su tope.
- **Topes propios** por transacción, por día (persistidos, sobreviven a
  reinicios) y por minuto.

**Límite conocido.** El firmador no decodifica la instrucción de ruta de
Jupiter; un proceso de trading comprometido podría pedir un swap a mal precio
*dentro* de los topes del firmador. Mitigaciones: wallet con el capital justo,
topes del firmador bajos (sobre todo el diario) y alertas. Mejora propuesta en
[REVIEW.md](REVIEW.md).

`signer_mode: local` descifra la clave dentro del proceso de trading. Solo
para pruebas o importes pequeños.

## 4. Secretos

- Van en `./secrets` (Docker secrets, directorio 0700) o en `.env` (600) en
  instalaciones locales. Nunca en el YAML ni en git.
- Cada servicio del compose recibe **solo** los suyos.
- Genera valores con `copytrader gen-secret` / `scripts/init-secrets.sh`.
- La app nunca devuelve secretos por la API ni los guarda en la base de datos.
  El secreto TOTP se guarda cifrado con `DATA_ENCRYPTION_KEY`.

## 5. Rotación

| Secreto | Procedimiento |
|---|---|
| Passphrase del keystore | `copytrader keystore rotate --path secrets/bot.keystore.json`, actualiza `secrets/keystore_passphrase`, reinicia el firmador |
| Clave HMAC app ↔ firmador | 1) copia la clave actual a `secrets/signer_hmac_key_previous`; 2) pon una nueva (`gen-secret`) en `secrets/signer_hmac_key`; 3) reinicia el firmador (acepta ambas) y luego la app (firma con la nueva); 4) vacía `signer_hmac_key_previous` y reinicia el firmador |
| Wallet del bot (si sospechas de la clave) | crea un keystore nuevo, cambia `execution.wallet_public_key`, reinicia, y mueve los fondos de la wallet antigua con tu propia herramienta |
| API keys (RPC, Helius, Jupiter), token de Telegram, webhook de Discord | revoca en el proveedor, escribe la nueva en `./secrets`, reinicia la app |
| Contraseña de PostgreSQL | `ALTER USER copytrader PASSWORD '...'`, actualiza `postgres_password` y `database_url`, reinicia |
| `DATA_ENCRYPTION_KEY` | cambia el secreto y vuelve a ejecutar `copytrader totp-setup` |
| Contraseña del dashboard | `copytrader set-password` (invalida las sesiones al reiniciar) |

Recomendación: rota la clave HMAC y los tokens cada 90 días, y todo de
inmediato ante cualquier sospecha.

## 6. Dashboard y API

- Escucha solo en `127.0.0.1`; accede por túnel SSH o VPN. No lo expongas.
- Contraseñas con **scrypt**; **TOTP** opcional (`copytrader totp-setup`,
  `api.require_totp: true` para exigirlo).
- Sesión en cookie `HttpOnly`, `SameSite=Strict` y `Secure`; token **CSRF** en
  cada petición que modifica algo.
- **Reautenticación** (contraseña + TOTP) para toda acción que aumente el
  riesgo: cambiar el nivel operativo, armar el trading real, desactivar un
  kill switch, aplicar o revertir configuración. Activar un kill switch o
  cerrar posiciones no la pide: reducir riesgo nunca debe tener fricción.
- Bloqueo tras `login_max_attempts` intentos fallidos por IP y rate limit global.
- Cabeceras: CSP estricta (`script-src 'self'`, sin inline), `X-Frame-Options:
  DENY`, `nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`
  en la API. El frontend construye el DOM sin `innerHTML`.
- Validación de todas las entradas (Pydantic, tamaños máximos, direcciones).
- **Auditoría**: login, nivel, armado, kill switches, configuración, wallets y
  cierres manuales quedan en *Sistema → Auditoría* con usuario, IP y fecha.

## 7. Integridad de las operaciones

- **Idempotencia**: cada señal y cada orden tienen un identificador
  determinista con restricción `UNIQUE`; una notificación repetida, un catch-up
  solapado o un reintento nunca generan una segunda orden.
- La **firma se persiste antes de enviar** la transacción; tras un reinicio se
  consulta la cadena en lugar de reenviar. Un blockhash caducado marca la orden
  como caducada y **nunca** se reenvía a ciegas.
- Un fill se aplica **exactamente una vez** (fila de ejecución única por orden,
  en la misma transacción de base de datos que la posición).
- Reconciliación periódica de saldos on-chain frente a posiciones; un descuadre
  activa el kill switch global (`risk.kill_on_reconciliation_mismatch`).

## 8. Red y contenedores

- Contenedores de solo lectura, sin capabilities, `no-new-privileges`, usuario
  sin privilegios; puertos solo en `127.0.0.1`.
- `backend` y `signer` son redes internas; solo la app sale a Internet.
- Mantén el host actualizado, SSH solo con clave, cortafuegos que cierre todo
  salvo SSH, y NTP activo.

## 9. Checklist antes del dinero real

- [ ] Wallet del bot **nueva y dedicada**, con solo el capital necesario.
- [ ] Keystore cifrado; el keypair original sin cifrar, borrado; copia de
      seguridad del keystore y de la passphrase por separado y fuera de línea.
- [ ] `signer_mode: remote` y topes del firmador ajustados a tu capital.
- [ ] TOTP activado y `api.require_totp: true`.
- [ ] Acceso al dashboard solo por túnel/VPN.
- [ ] Notificaciones funcionando (recibirás los kill switches y errores).
- [ ] `copytrader preflight` sin fallos críticos.
- [ ] Semanas de paper trading revisadas y backtest fuera de muestra razonable.
- [ ] Empezar en nivel 4.

## 10. Si sospechas de un compromiso

1. Dashboard: kill switch **global** con *cerrar todas las posiciones*
   (o `copytrader kill-switch on --scope global`).
2. `docker compose stop signer` — sin firmador no sale ninguna transacción.
3. Mueve los fondos de la wallet del bot a una wallet segura con tu propia
   herramienta (no con este sistema).
4. Rota **todo** (sección 5), revisa *Auditoría* y los logs, y crea una wallet
   de bot nueva antes de volver a operar.
