"""Command line interface.

copytrader run                       start the whole system
copytrader init-db                   create/upgrade the database schema
copytrader set-password              create/update the dashboard user
copytrader totp-setup                enable TOTP 2FA for the dashboard user
copytrader gen-secret                print strong random secrets
copytrader keystore create|rotate    manage the encrypted bot keypair
copytrader wallets add|import|list   manage tracked wallets
copytrader backfill [--force]        download wallet history
copytrader evaluate                  run one analysis/scoring/selection cycle
copytrader backtest                  walk-forward backtest on stored history
copytrader preflight                 checks required before real trading
copytrader check-config              validate and summarise the configuration
copytrader kill-switch on|off        manual kill switch
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import secrets
import sys
from pathlib import Path
from typing import Any

from copytrader.config.loader import load_base_config_dict
from copytrader.config.secrets import Secrets
from copytrader.core.errors import ConfigError, CopyTraderError


def _load(args: argparse.Namespace) -> tuple[dict[str, Any], Secrets]:
    raw = load_base_config_dict(args.config)
    return raw, Secrets()


async def _container(args: argparse.Namespace, *, create_schema: bool = True) -> Any:
    from copytrader.container import Container, build_config_service
    from copytrader.db.base import Database
    from copytrader.observability.logging import configure_logging

    raw, sec = _load(args)
    db = Database(sec.database_url.get_secret_value())
    if create_schema and db.is_sqlite:
        await db.create_all()
    service = build_config_service(db, raw)
    cfg = service.current
    configure_logging(cfg.observability.log_level, cfg.observability.json_logs if args.command == "run" else False)
    await service.load()
    c = Container(service, sec, db=db)
    await c.mode.load()
    await c.kill.load()
    return c


# ------------------------------------------------------------------ commands
async def cmd_run(args: argparse.Namespace) -> int:
    from copytrader.app import Application

    try:
        import uvloop

        uvloop.install()
    except ImportError:
        pass
    c = await _container(args)
    await Application(c).run_forever()
    return 0


async def cmd_init_db(args: argparse.Namespace) -> int:
    from copytrader.db.base import Database

    _, sec = _load(args)
    url = sec.database_url.get_secret_value()
    if url.startswith("sqlite"):
        db = Database(url)
        await db.create_all()
        await db.dispose()
        print("Esquema SQLite creado/actualizado.")
        return 0
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(args.alembic_ini)))
    await asyncio.to_thread(command.upgrade, cfg, "head")
    print("Migraciones aplicadas (alembic upgrade head).")
    return 0


def _ask_new_password() -> str:
    from copytrader.security.passwords import MIN_PASSWORD_LEN

    pw = getpass.getpass(f"Nueva contraseña (mín. {MIN_PASSWORD_LEN} caracteres): ")
    if pw != getpass.getpass("Repite la contraseña: "):
        raise SystemExit("Las contraseñas no coinciden.")
    return pw


async def cmd_set_password(args: argparse.Namespace) -> int:
    from copytrader.db.repositories import UserRepo
    from copytrader.security.passwords import hash_password

    c = await _container(args)
    hashed = hash_password(_ask_new_password())
    async with c.db.session() as s:
        await UserRepo(s).set_password(args.username, hashed)
    await c.aclose()
    print(f"Contraseña guardada para '{args.username}'. Las sesiones abiertas caducarán al reiniciar.")
    return 0


async def cmd_totp_setup(args: argparse.Namespace) -> int:
    from copytrader.db.repositories import UserRepo
    from copytrader.security.passwords import FieldCipher, new_totp_secret, totp_uri

    c = await _container(args)
    key = c.secrets.data_encryption_key
    if not key:
        raise SystemExit("Configura DATA_ENCRYPTION_KEY (copytrader gen-secret) antes de activar TOTP.")
    secret = new_totp_secret()
    async with c.db.session() as s:
        if await UserRepo(s).get(args.username) is None:
            raise SystemExit("Primero crea el usuario con 'copytrader set-password'.")
        await UserRepo(s).set_totp(args.username, FieldCipher(key.get_secret_value()).encrypt(secret))
    await c.aclose()
    print("Añade esta clave a tu app de autenticación (Google Authenticator, Aegis, 1Password...):")
    print(f"  Secreto: {secret}")
    print(f"  URI:     {totp_uri(secret, args.username)}")
    print("Este secreto no se volverá a mostrar.")
    return 0


def cmd_gen_secret(_: argparse.Namespace) -> int:
    from copytrader.security.passwords import FieldCipher

    print(f"SIGNER_HMAC_KEY={secrets.token_urlsafe(48)}")
    print(f"DATA_ENCRYPTION_KEY={FieldCipher.generate_key()}")
    print(f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}")
    return 0


def cmd_keystore(args: argparse.Namespace) -> int:
    from copytrader.security.keystore import (
        MIN_PASSPHRASE_LEN,
        create_keystore_from_keypair_bytes,
        rotate_passphrase,
        write_keystore,
    )

    if args.keystore_action == "rotate":
        old = getpass.getpass("Passphrase actual: ")
        new = getpass.getpass(f"Nueva passphrase (mín. {MIN_PASSPHRASE_LEN}): ")
        if new != getpass.getpass("Repite la nueva passphrase: "):
            raise SystemExit("No coinciden.")
        rotate_passphrase(args.path, old, new)
        print("Passphrase rotada.")
        return 0
    if args.generate:
        from solders.keypair import Keypair

        raw = bytes(Keypair())
    else:
        if not args.from_json:
            raise SystemExit("Indica --from-json <keypair.json> (formato solana-keygen) o --generate.")
        data = json.loads(Path(args.from_json).read_text())
        if not isinstance(data, list) or len(data) != 64:
            raise SystemExit("El fichero debe ser un array JSON de 64 bytes (formato solana-keygen).")
        raw = bytes(data)
    passphrase = getpass.getpass(f"Passphrase para cifrar (mín. {MIN_PASSPHRASE_LEN}): ")
    if passphrase != getpass.getpass("Repite la passphrase: "):
        raise SystemExit("No coinciden.")
    ks = create_keystore_from_keypair_bytes(raw, passphrase)
    write_keystore(args.out, ks)
    print(f"Keystore cifrado escrito en {args.out} (permisos 600).")
    print(f"Clave pública de la wallet del bot: {ks['public_key']}")
    if args.from_json:
        print(f"IMPORTANTE: borra de forma segura el fichero original sin cifrar: {args.from_json}")
    return 0


async def cmd_wallets(args: argparse.Namespace) -> int:
    from copytrader.collector.wallet_collector import parse_list_type
    from copytrader.db.repositories import WalletRepo

    c = await _container(args)
    try:
        if args.wallets_action == "add":
            created = await c.collector.add_wallet(
                args.address, label=args.label, list_type=parse_list_type(args.list) if args.list else None
            )
            print("Añadida." if created else "Actualizada.")
        elif args.wallets_action == "import":
            text = await asyncio.to_thread(Path(args.file).read_text, encoding="utf-8")
            report = await c.collector.import_csv(text)
            print(
                f"Añadidas: {len(report.added)} · Actualizadas: {len(report.updated)} · Errores: {len(report.errors)}"
            )
            for err in report.errors:
                print("  -", err)
        else:
            async with c.db.session() as s:
                for w in await WalletRepo(s).list():
                    print(
                        f"{w.address}  {w.status:8} score={w.score if w.score is not None else '-':>6}  "
                        f"sel={'Y' if w.selected else '-'}  list={w.list_type:9} {w.label or ''}"
                    )
    finally:
        await c.aclose()
    return 0


async def cmd_backfill(args: argparse.Namespace) -> int:
    c = await _container(args)
    try:
        result = await c.collector.backfill_all(force=args.force)
        print(f"Backfill completado: {sum(v for v in result.values() if v > 0)} swaps nuevos en {len(result)} wallets")
    finally:
        await c.aclose()
    return 0


async def cmd_evaluate(args: argparse.Namespace) -> int:
    c = await _container(args)
    try:
        report = await c.cycle.run()
        print(
            f"Evaluadas {report.evaluated} wallets · estados {report.status_counts} · "
            f"seleccionadas {len(report.selected)}"
        )
    finally:
        await c.aclose()
    return 0


async def cmd_backtest(args: argparse.Namespace) -> int:
    from copytrader.backtest.service import run_backtest
    from copytrader.db.repositories import BacktestRepo

    c = await _container(args)
    try:
        run_id = await run_backtest(
            c,
            {
                "train_days": args.train_days,
                "test_days": args.test_days,
                "top_n": args.top_n,
                "exit_mode": args.exit_mode,
            },
        )
        async with c.db.session() as s:
            run = await BacktestRepo(s).get(run_id)
        if run is None or run.status != "done":
            print(f"Backtest fallido: {run.error if run else '?'}")
            return 1
        print(f"Periodo: {run.results['period']['start']} → {run.results['period']['end']}")
        for name, r in run.results["results"].items():
            print(
                f"  {name:9} ROI {r['roi_pct']:8.2f}%  DD {r['max_drawdown_pct']:6.2f}%  trades {r['n_trades']:5}"
                f"  win {r['win_rate'] or 0:.2%}  PF {r['profit_factor'] or 0:.2f}"
            )
        for note in run.results["notes"]:
            print("  *", note)
    finally:
        await c.aclose()
    return 0


async def cmd_preflight(args: argparse.Namespace) -> int:
    from copytrader.preflight import preflight_passed, run_preflight

    c = await _container(args)
    try:
        checks = await run_preflight(c)
        for ch in checks:
            mark = "✓" if ch.passed else ("✗" if ch.critical else "!")
            print(f" {mark} {ch.label}" + (f" — {ch.message}" if ch.message else ""))
        ok = preflight_passed(checks)
        print("\nPREFLIGHT: " + ("SUPERADO" if ok else "NO SUPERADO"))
        return 0 if ok else 2
    finally:
        await c.aclose()


def cmd_check_config(args: argparse.Namespace) -> int:
    from copytrader.config.loader import build_config

    raw, sec = _load(args)
    cfg = build_config(raw)
    print("Configuración válida.")
    print(
        f"  Nivel máximo: {int(cfg.app.operating_level)} · proveedores: {cfg.providers.mode} · "
        f"firmador: {cfg.security.signer_mode}"
    )
    print(
        f"  Capital: ${cfg.risk.capital_usd:,.0f} · máx. operación ${cfg.risk.max_trade_usd:,.0f} · "
        f"pérdida diaria {cfg.risk.max_daily_loss_pct}% · posiciones {cfg.risk.max_open_positions}"
    )
    print(f"  Selección: Top {cfg.selection.top_n} · score mínimo {cfg.selection.min_score}")
    configured = [n for n in type(sec).model_fields if getattr(sec, n) is not None]
    print(f"  Secretos configurados: {', '.join(configured)}")
    return 0


async def cmd_kill(args: argparse.Namespace) -> int:
    from copytrader.core.types import KillSwitchScope

    c = await _container(args)
    try:
        scope = KillSwitchScope(args.scope)
        if args.state == "on":
            await c.kill.activate(scope, args.reason, actor="cli")
        else:
            await c.kill.deactivate(scope, actor="cli")
        await c.bus.drain(2)
        print(json.dumps(c.kill.snapshot(), indent=2, ensure_ascii=False))
    finally:
        await c.aclose()
    return 0


# -------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="copytrader", description="Wallet analytics & risk-managed copy trading")
    p.add_argument("--config", default=None, help="ruta del YAML (por defecto config/settings.yaml)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    init = sub.add_parser("init-db")
    init.add_argument("--alembic-ini", default="alembic.ini")
    sp = sub.add_parser("set-password")
    sp.add_argument("--username", default="admin")
    tp = sub.add_parser("totp-setup")
    tp.add_argument("--username", default="admin")
    sub.add_parser("gen-secret")
    ks = sub.add_parser("keystore")
    ks_sub = ks.add_subparsers(dest="keystore_action", required=True)
    ksc = ks_sub.add_parser("create")
    ksc.add_argument("--from-json")
    ksc.add_argument("--generate", action="store_true")
    ksc.add_argument("--out", default="secrets/bot.keystore.json")
    ksr = ks_sub.add_parser("rotate")
    ksr.add_argument("--path", default="secrets/bot.keystore.json")
    w = sub.add_parser("wallets")
    w_sub = w.add_subparsers(dest="wallets_action", required=True)
    wa = w_sub.add_parser("add")
    wa.add_argument("address")
    wa.add_argument("--label")
    wa.add_argument("--list", choices=["whitelist", "watchlist", "blacklist", "none"])
    wi = w_sub.add_parser("import")
    wi.add_argument("file")
    w_sub.add_parser("list")
    bf = sub.add_parser("backfill")
    bf.add_argument("--force", action="store_true")
    sub.add_parser("evaluate")
    bt = sub.add_parser("backtest")
    bt.add_argument("--train-days", type=int)
    bt.add_argument("--test-days", type=int)
    bt.add_argument("--top-n", type=int)
    bt.add_argument("--exit-mode", choices=["mirror", "protected", "smart"])
    sub.add_parser("preflight")
    sub.add_parser("check-config")
    k = sub.add_parser("kill-switch")
    k.add_argument("state", choices=["on", "off"])
    k.add_argument("--scope", choices=["global", "daily"], default="global")
    k.add_argument("--reason", default="manual (CLI)")
    return p


ASYNC = {
    "run": cmd_run,
    "init-db": cmd_init_db,
    "set-password": cmd_set_password,
    "totp-setup": cmd_totp_setup,
    "wallets": cmd_wallets,
    "backfill": cmd_backfill,
    "evaluate": cmd_evaluate,
    "backtest": cmd_backtest,
    "preflight": cmd_preflight,
    "kill-switch": cmd_kill,
}
SYNC = {"gen-secret": cmd_gen_secret, "keystore": cmd_keystore, "check-config": cmd_check_config}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command in SYNC:
            return SYNC[args.command](args)
        return asyncio.run(ASYNC[args.command](args))
    except (ConfigError, CopyTraderError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
