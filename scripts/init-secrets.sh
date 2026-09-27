#!/usr/bin/env bash
# Creates ./secrets with strong random values for the Docker deployment.
# Optional secrets are created empty (= not configured); fill them in later.
# The directory is 0700 (only you can enter it); files are 0444 so the non-root
# container user can read the bind-mounted copies.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
mkdir -p secrets
chmod 700 secrets

gen() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }
put() {  # put <name> <value> (never overwrites an existing non-empty secret)
  local f="secrets/$1"
  if [ -s "$f" ]; then echo "  = $1 (ya existe, no se toca)"; return; fi
  printf '%s' "$2" > "$f"; chmod 444 "$f"; echo "  + $1"
}

echo "Generando secretos en ./secrets"
PGPASS=$(gen 24)
[ -s secrets/postgres_password ] && PGPASS=$(cat secrets/postgres_password)
put postgres_password "$PGPASS"
put database_url "postgresql+asyncpg://copytrader:${PGPASS}@postgres:5432/copytrader"
put signer_hmac_key "$(gen 48)"
put data_encryption_key "$(python3 -c 'import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
put grafana_admin_password "$(gen 18)"
for optional in solana_rpc_url solana_ws_url helius_api_key jupiter_api_key telegram_bot_token \
                discord_webhook_url signer_hmac_key_previous keystore_passphrase; do
  put "$optional" ""
done
if [ ! -e secrets/bot.keystore.json ]; then
  printf '{}' > secrets/bot.keystore.json; chmod 444 secrets/bot.keystore.json
  echo "  + bot.keystore.json (placeholder: crea el real con 'copytrader keystore create')"
fi
cat <<'MSG'

Hecho. Siguientes pasos:
  1. Edita secrets/solana_rpc_url y secrets/solana_ws_url con tu RPC (p. ej. Helius) para datos reales.
  2. (Opcional) secrets/telegram_bot_token y/o secrets/discord_webhook_url para notificaciones.
  3. Para dinero real: crea el keystore cifrado y escribe su passphrase en secrets/keystore_passphrase.
  Para editar un secreto: chmod 600 secrets/X && editor secrets/X && chmod 444 secrets/X
MSG
