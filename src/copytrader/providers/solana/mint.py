"""On-chain mint inspection: authorities and dangerous Token-2022 extensions."""

from __future__ import annotations

from typing import Any

from copytrader.providers.interfaces import MintData
from copytrader.providers.solana.rpc import SolanaRpc

# Extensions that let someone else move/freeze/tax our tokens or block selling.
_DANGEROUS = {"permanentDelegate", "nonTransferable", "pausableConfig"}


class RpcMintInfoSource:
    def __init__(self, rpc: SolanaRpc) -> None:
        self.rpc = rpc

    async def mint_info(self, mint: str) -> MintData | None:
        value = await self.rpc.get_account_info(mint)
        if not value:
            return None
        return parse_mint_account(mint, value)


def parse_mint_account(mint: str, value: dict[str, Any]) -> MintData:
    data = value.get("data") or {}
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = (parsed or {}).get("info") or {}
    dangerous: list[str] = []
    for ext in info.get("extensions") or []:
        name = ext.get("extension")
        state = ext.get("state") or {}
        if name in _DANGEROUS:
            dangerous.append(str(name))
        elif name == "transferHook" and state.get("programId"):
            dangerous.append("transferHook")
        elif name == "defaultAccountState" and str(state.get("accountState", "")).lower() == "frozen":
            dangerous.append("defaultAccountState:frozen")
        elif name == "transferFeeConfig":
            fees = [state.get("newerTransferFee") or {}, state.get("olderTransferFee") or {}]
            if any(int(f.get("transferFeeBasisPoints") or 0) > 0 for f in fees):
                dangerous.append("transferFee")
    return MintData(
        mint=mint,
        decimals=info.get("decimals"),
        token_program=value.get("owner"),
        mint_authority=info.get("mintAuthority"),
        freeze_authority=info.get("freezeAuthority"),
        dangerous_extensions=dangerous,
    )
