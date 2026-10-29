from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .config import StageRule


@dataclass
class Decision:
    send: bool
    reason: str
    rule: StageRule | None = None
    event_name: str | None = None
    rank: int = 0


def decide(lead: Mapping[str, object] | None, rule: StageRule | None) -> Decision:
    if rule is None:
        return Decision(False, "etapa_sin_mapeo")
    if not rule.enabled:
        return Decision(False, "etapa_deshabilitada", rule)
    if not rule.event:
        return Decision(False, "evento_vacio", rule)
    if rule.rank <= 0:
        return Decision(False, "rank_invalido", rule)

    max_rank = int((lead or {}).get("max_rank") or 0)
    if max_rank and rule.rank <= max_rank:
        return Decision(False, "etapa_ya_enviada_o_inferior", rule, rule.event, rule.rank)

    return Decision(True, "nueva_etapa", rule, rule.event, rule.rank)
