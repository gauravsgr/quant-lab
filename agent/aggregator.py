"""Cross-strategy signal aggregator.

Groups signals from all strategies by ticker and computes:
  - Agreement count (how many strategies agree on direction)
  - Conviction boost (+multi_strategy_boost per extra agreeing strategy)
  - Conflict detection (if both BUY and PUT signals exist for same ticker)

The aggregator does NOT generate new signals — it annotates and groups existing
ones. Execution can be triggered from any strategy's signal, not just the ensemble.

Typical usage (inside orchestrator):
    agg = SignalAggregator(multi_strategy_boost=0.08)
    aggregated = agg.aggregate(all_strategy_signals)
    for ag in aggregated:
        runner.submit_signal_order(ag.best_signal, ...)
"""
from dataclasses import dataclass, field

import yaml
from loguru import logger


REGISTRY_PATH = "config/strategies/registry.yaml"


def _load_registry_cfg() -> dict:
    try:
        with open(REGISTRY_PATH) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


@dataclass
class AggregatedSignal:
    """Cross-strategy view of all signals for one ticker.

    Attributes:
        ticker: Stock symbol.
        final_signal_type: STRONG_BUY, STRONG_PUT, NEUTRAL, or CONFLICTED.
        final_confidence: Boosted confidence from multi-strategy agreement.
        strategy_signals: All signals for this ticker (one per strategy).
        agreement_count: Number of strategies in the majority direction.
        conflict: True if at least one strategy disagrees with the majority.
        best_signal: The highest-confidence signal among majority signals.
    """
    ticker: str
    final_signal_type: str
    final_confidence: float
    strategy_signals: list = field(default_factory=list)
    agreement_count: int = 1
    conflict: bool = False
    best_signal: object = None  # Signal instance with highest confidence


class SignalAggregator:
    """Groups and boosts cross-strategy signals by ticker."""

    def __init__(self, multi_strategy_boost: float = 0.08, conflict_threshold: int = 2):
        self._boost = multi_strategy_boost
        self._conflict_threshold = conflict_threshold

    @classmethod
    def from_registry(cls) -> "SignalAggregator":
        cfg = _load_registry_cfg()
        return cls(
            multi_strategy_boost=cfg.get("multi_strategy_boost", 0.08),
            conflict_threshold=cfg.get("conflict_threshold", 2),
        )

    def aggregate(self, all_signals: list) -> list[AggregatedSignal]:
        """Group signals by ticker and compute cross-strategy conviction.

        Args:
            all_signals: Flat list of Signal instances from all strategies.

        Returns:
            List of AggregatedSignal sorted by conviction (highest first),
            with CONFLICTED signals sorted last.
        """
        # Group by ticker
        by_ticker: dict[str, list] = {}
        for sig in all_signals:
            by_ticker.setdefault(sig.ticker, []).append(sig)

        results: list[AggregatedSignal] = []

        for ticker, sigs in by_ticker.items():
            buys = [s for s in sigs if s.signal_type == "STRONG_BUY"]
            puts = [s for s in sigs if s.signal_type == "STRONG_PUT"]

            conflict = len(buys) > 0 and len(puts) > 0

            if len(buys) >= len(puts) and buys:
                majority_type = "STRONG_BUY"
                majority = buys
            elif puts:
                majority_type = "STRONG_PUT"
                majority = puts
            else:
                continue  # all NEUTRAL

            best = max(majority, key=lambda s: s.confidence)
            agreement_count = len(majority)
            boosted = round(
                min(1.0, best.confidence + self._boost * (agreement_count - 1)), 4
            )

            final_type = "CONFLICTED" if conflict else majority_type

            results.append(AggregatedSignal(
                ticker=ticker,
                final_signal_type=final_type,
                final_confidence=boosted,
                strategy_signals=sigs,
                agreement_count=agreement_count,
                conflict=conflict,
                best_signal=best,
            ))

        results.sort(
            key=lambda ag: (ag.final_signal_type != "CONFLICTED", ag.final_confidence),
            reverse=True,
        )
        logger.info(
            f"SignalAggregator: {len(results)} unique tickers | "
            f"{sum(1 for r in results if not r.conflict)} clean | "
            f"{sum(1 for r in results if r.conflict)} conflicted"
        )
        return results
