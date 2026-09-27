#!/bin/sh
# Apply database migrations before starting the trading process.
set -eu
if [ "${1:-}" = "run" ]; then
  copytrader init-db --alembic-ini /app/alembic.ini
fi
exec copytrader "$@"
