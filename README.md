# Copy Trading Engine

Aplicación privada, de uso personal, para **analizar wallets de Solana** y
**copiar sus operaciones de forma automatizada con control de riesgo**.

> **Aviso.** Este software no promete rentabilidad. Copiar a otras wallets tiene
> desventajas estructurales (llegas tarde, pagas más slippage, heredas sus
> errores) y todas las decisiones del sistema son probabilísticas. Está diseñado
> para **preservar capital primero**: límites absolutos, kill switches y una
> activación gradual en 5 niveles. Úsalo solo con dinero que puedas perder y
> empieza siempre en *paper trading*.

## Qué hace

- **Analiza hasta 100 wallets** (configurable): PnL realizado y no realizado,
  win rate, profit factor, ROI, drawdown máximo, tiempos de holding, frecuencia,
  rendimiento por token, categoría, periodo y régimen de mercado, rachas de pérdidas…
- **Puntúa** cada wallet con un score configurable que no se basa solo en el PnL
  y protege frente a muestras pequeñas (límites inferiores de Wilson, shrinkage
  bayesiano, peso del comportamiento reciente).
- **Detecta comportamiento sospechoso** (wash trading, coordinación, snipers,
  tokens ilíquidos, operaciones no replicables, dependencia de una sola operación,
  rugs, deterioro reciente…) y asigna estado **ACTIVA / OBSERVAR / BLOQUEADA**
  con explicación.
- **Selecciona dinámicamente** el Top N (5, 10, 20… sin tocar código), con
  histéresis, whitelist, watchlist y blacklist (la blacklist nunca opera).
- **Detecta operaciones en tiempo real** (WebSocket con reconexión, catch-up y
  reconciliación) y las pasa por un **pipeline de 12 validaciones** y un **Risk
  Engine independiente** antes de ejecutar.
- **Ejecuta** en paper trading o con dinero real mediante Jupiter, con un
  **firmador aislado** que es el único proceso que ve la clave privada.
- **Gestiona salidas** en modo espejo, protegido o inteligente (stop loss, take
  profit escalonado, trailing stop, tiempo máximo y stop de emergencia).
- **Explica cada decisión** (qué comprobaciones pasaron, cuál la rechazó y por qué)
  y guarda una línea temporal por operación.
- **Dashboard web** privado, notificaciones por **Telegram/Discord**, métricas
  Prometheus/Grafana y **backtesting** walk-forward con separación train/test.

## Prueba rápida (sin claves, mercado simulado)

```bash
make install                       # Python 3.12 + dependencias en .venv
.venv/bin/copytrader set-password  # crea el usuario del dashboard
make demo                          # nivel 3 (paper) con un mercado sintético
```

Abre <http://127.0.0.1:8080>. El modo simulado genera wallets con perfiles
conocidos (hábiles, aleatorias, con suerte, wash traders, snipers, coordinadas,
que se deterioran…) para comprobar que el sistema las clasifica bien.

## Documentación

| Documento | Contenido |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Diseño: arquitectura, diagrama, stack, proveedores, modelo de datos, flujo, scoring, riesgo, ejecución, testing, riesgos y costes |
| [docs/INSTALL.md](docs/INSTALL.md) | Instalación local y con Docker, secretos, base de datos, actualización y copias de seguridad |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Cómo se configura (YAML, variables, cambios en caliente, límites absolutos) y qué significa cada sección |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Añadir las 100 wallets, niveles 1-5, activar paper trading, conectar la wallet de ejecución, activar trading real, operación diaria |
| [docs/SECURITY.md](docs/SECURITY.md) | Modelo de amenazas, gestión de claves y secretos, rotación, firmador, dashboard, checklist |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Problemas frecuentes y cómo resolverlos |
| [docs/REVIEW.md](docs/REVIEW.md) | Revisión técnica final: hallazgos, correcciones, riesgos residuales y mejoras propuestas |

## Estructura

```
src/copytrader/
  core/           tipos, modelos, reloj, IDs deterministas, bus de eventos
  config/         modelos de configuración, límites absolutos, secretos, cambios en caliente
  resilience/     reintentos, circuit breakers, rate limiting, HTTP resiliente
  observability/  logs JSON con redacción, métricas Prometheus, salud
  security/       keystore cifrado, HMAC, contraseñas/TOTP, política del firmador
  signer_service/ servicio firmador aislado (único proceso con la clave)
  db/             modelos SQLAlchemy y repositorios idempotentes
  providers/      Solana RPC/WS/parser, Jupiter, DexScreener, RugCheck, mercado simulado
  collector/      alta de wallets, importación CSV, backfill
  analysis/       reconstrucción de operaciones, estadísticas, regímenes, métricas
  detection/      reglas de comportamiento sospechoso
  scoring/        score, estados y ciclo de evaluación
  selection/      Top N dinámico con histéresis
  signals/        detección y clasificación de señales, colas por token
  pipeline/       pipeline de copia (validaciones + decisión explicada)
  risk/           Risk Engine, sizing, kill switches, monitor
  execution/      paper, live, guardia de ejecución, recuperación tras reinicio
  positions/      gestión de posiciones y reglas de salida
  alerts/ notifications/  alertas y canales Telegram/Discord
  backtest/       backtesting walk-forward con baselines
  api/ web/       API FastAPI y dashboard (JS sin dependencias, CSP estricta)
tests/            unitarios e integración (SQLite y PostgreSQL)
config/           configuración de ejemplo documentada, CSV de wallets
deploy/ docker/   Prometheus, Grafana, entrypoint
migrations/       Alembic
```

## Desarrollo

```bash
make test        # tests (SQLite)
make test-pg     # integración contra PostgreSQL (COPYTRADER_TEST_PG=postgresql+asyncpg://...)
make lint        # ruff
make typecheck   # mypy
```

Stack: Python 3.12 (asyncio), FastAPI, SQLAlchemy 2 async, PostgreSQL 16
(SQLite para desarrollo), Pydantic v2, httpx, websockets, solders, cryptography,
structlog, Prometheus. La justificación está en
[docs/ARCHITECTURE.md §3](docs/ARCHITECTURE.md#3-stack-tecnológico).
