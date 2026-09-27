"""Dynamic Top-N selection with hysteresis and manual lists.

Hard rules (never overridden):
* blacklist → never selected;
* BLOCKED status → never selected;
* watchlist → never copied (alerts only).

Hysteresis avoids churning the copied set because of score noise: an
incumbent stays while its rank is within ``top_n + rank_buffer`` and its score
within ``min_score − hysteresis_points``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from copytrader.config.models import SelectionSection
from copytrader.core.types import ListType, WalletStatus


@dataclass(frozen=True, slots=True)
class Candidate:
    wallet_id: int
    address: str
    score: float
    status: WalletStatus
    list_type: ListType
    label: str | None = None


@dataclass(slots=True)
class SelectionResult:
    selected: list[Candidate] = field(default_factory=list)
    ranks: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def addresses(self) -> set[str]:
        return {c.address for c in self.selected}


def select_wallets(candidates: list[Candidate], previous: set[str], cfg: SelectionSection) -> SelectionResult:
    result = SelectionResult()
    eligible: list[Candidate] = []
    for c in candidates:
        if c.list_type is ListType.BLACKLIST:
            result.reasons[c.address] = "Blacklist"
        elif c.list_type is ListType.WATCHLIST:
            result.reasons[c.address] = "Watchlist: solo alertas"
        elif c.status is WalletStatus.BLOCKED:
            result.reasons[c.address] = "Bloqueada"
        elif c.list_type is ListType.WHITELIST:
            if c.score >= cfg.whitelist_min_score:
                eligible.append(c)
            else:
                result.reasons[c.address] = (f"Whitelist pero score {c.score:.1f} < "
                                             f"{cfg.whitelist_min_score:.0f}")
        elif c.status is WalletStatus.OBSERVE:
            # Incumbents in OBSERVE are dropped immediately: observation means "do not copy".
            result.reasons[c.address] = "En observación"
        else:
            eligible.append(c)

    def order_key(c: Candidate) -> tuple[int, float]:
        wl_first = cfg.whitelist_policy == "priority" and c.list_type is ListType.WHITELIST
        return (0 if wl_first else 1, -c.score)

    ranked = sorted(eligible, key=order_key)
    for i, c in enumerate(ranked, start=1):
        result.ranks[c.address] = i

    def meets(c: Candidate, incumbent: bool) -> bool:
        if c.list_type is ListType.WHITELIST:
            return True
        floor = cfg.min_score - (cfg.hysteresis_points if incumbent else 0.0)
        return c.score >= floor

    whitelisted_extra: list[Candidate] = []
    pool = ranked
    if not cfg.whitelist_counts_toward_top_n:
        whitelisted_extra = [c for c in ranked if c.list_type is ListType.WHITELIST]
        pool = [c for c in ranked if c.list_type is not ListType.WHITELIST]

    keep = [c for c in pool if c.address in previous and result.ranks[c.address] <= cfg.top_n + cfg.rank_buffer
            and meets(c, incumbent=True)][: cfg.top_n]
    keep_set = {c.address for c in keep}
    newcomers = [c for c in pool if c.address not in keep_set and meets(c, incumbent=False)]
    chosen = sorted(keep + newcomers[: max(0, cfg.top_n - len(keep))], key=lambda c: result.ranks[c.address])
    result.selected = whitelisted_extra + chosen

    selected_set = result.addresses
    for c in ranked:
        rank = result.ranks[c.address]
        if c.address in selected_set:
            tag = " (whitelist)" if c.list_type is ListType.WHITELIST else ""
            tag += " (se mantiene por histéresis)" if c.address in keep_set and rank > cfg.top_n else ""
            result.reasons[c.address] = f"Seleccionada: rank #{rank}, score {c.score:.1f}{tag}"
        elif not meets(c, incumbent=c.address in previous):
            result.reasons[c.address] = f"Score {c.score:.1f} < mínimo {cfg.min_score:.0f}"
        else:
            result.reasons[c.address] = f"Fuera del Top {cfg.top_n} (rank #{rank})"
    return result
