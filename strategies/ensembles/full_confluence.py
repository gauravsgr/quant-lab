"""FullEnsembleStrategy: weighted vote across all standalone strategies.

Runs AFTER all standalone strategies have produced signals. Takes the collected
list of signals from every other strategy and produces a single ensemble signal
per ticker by:
    1. Grouping signals by ticker.
    2. Counting agreeing strategies (same direction).
    3. Computing a weighted average confidence (weights from registry.yaml).
    4. Adding a multi-strategy agreement boost (+0.08 per extra agreeing strategy).
    5. Emitting one ensemble Signal per ticker with agreement_count >= 1.

The ensemble's own signals always carry strategy_name="full_confluence" and a
separate Approve/Reject Slack button. Conflicted tickers (strategies disagree)
are emitted as NEUTRAL so they appear in the summary but are never traded.

Ensemble weights (configurable in config/strategies/registry.yaml):
    technical: 0.25, sentiment: 0.20, political_news: 0.25,
    catalyst: 0.20, technical_buzz_ensemble: 0.10
"""
import yaml
from loguru import logger

from strategies.base import Signal, StandaloneStrategy


DEFAULT_WEIGHTS = {
    "technical": 0.25,
    "sentiment": 0.20,
    "political_news": 0.25,
    "catalyst": 0.20,
    "technical_buzz_ensemble": 0.10,
}
AGREEMENT_BOOST = 0.08
THRESHOLD = 0.55


def _load_weights(path: str = "config/strategies/registry.yaml") -> dict:
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f)
        return cfg.get("ensemble_weights", DEFAULT_WEIGHTS)
    except Exception:
        return DEFAULT_WEIGHTS


class FullEnsembleStrategy(StandaloneStrategy):
    """Cross-strategy ensemble that runs on all preceding signals."""

    def __init__(self):
        self._weights = _load_weights()
        self._prior_signals: list[Signal] = []

    @property
    def name(self) -> str:
        return "full_confluence"

    def set_prior_signals(self, signals: list[Signal]) -> None:
        """Provide all signals from preceding strategies before calling run()."""
        self._prior_signals = signals

    def run(self, bundle) -> list[Signal]:
        if not self._prior_signals:
            logger.info("FullEnsembleStrategy: no prior signals to aggregate")
            return []

        # Group by ticker
        by_ticker: dict[str, list[Signal]] = {}
        for sig in self._prior_signals:
            if sig.strategy_name == self.name:
                continue  # skip our own prior output if ever called twice
            by_ticker.setdefault(sig.ticker, []).append(sig)

        ensemble_signals: list[Signal] = []

        for ticker, ticker_signals in by_ticker.items():
            buys = [s for s in ticker_signals if s.signal_type == "STRONG_BUY"]
            puts = [s for s in ticker_signals if s.signal_type == "STRONG_PUT"]

            # Determine majority direction
            if len(buys) >= len(puts) and buys:
                majority_type = "STRONG_BUY"
                majority_signals = buys
                minority_count = len(puts)
            elif puts:
                majority_type = "STRONG_PUT"
                majority_signals = puts
                minority_count = len(buys)
            else:
                continue

            conflicted = minority_count > 0

            # Weighted average confidence across majority signals
            total_weight = 0.0
            weighted_conf = 0.0
            for sig in majority_signals:
                w = self._weights.get(sig.strategy_name, 0.10)
                weighted_conf += w * sig.confidence
                total_weight += w

            base_confidence = weighted_conf / total_weight if total_weight > 0 else 0.0
            agreement_count = len(majority_signals)
            boost = AGREEMENT_BOOST * (agreement_count - 1)
            final_confidence = round(min(1.0, base_confidence + boost), 4)

            # Pick the richest signal (political > catalyst > technical > sentiment)
            priority_order = ["political_news", "catalyst", "technical_buzz_ensemble", "technical", "sentiment"]
            representative = _pick_representative(majority_signals, priority_order)

            strategy_names = ", ".join(
                sorted({s.strategy_name for s in majority_signals})
            )
            minority_names = ", ".join(
                sorted({s.strategy_name for s in (puts if majority_type == "STRONG_BUY" else buys)})
            )

            info = bundle.company_info.get(ticker, {})

            ensemble_signals.append(Signal(
                ticker=ticker,
                signal_type=majority_type if not conflicted or agreement_count > minority_count else "NEUTRAL",
                confidence=final_confidence,
                order_type=representative.order_type,
                sentiment_score=representative.sentiment_score,
                politician_action=representative.politician_action,
                politician_name=representative.politician_name,
                politician_party=representative.politician_party,
                politician_chamber=representative.politician_chamber,
                politician_amount=representative.politician_amount,
                disclosure_url=representative.disclosure_url,
                analyst_rating=representative.analyst_rating,
                analyst_buy_count=representative.analyst_buy_count,
                analyst_hold_count=representative.analyst_hold_count,
                analyst_sell_count=representative.analyst_sell_count,
                analyst_price_target=representative.analyst_price_target,
                news_headline=representative.news_headline,
                news_url=representative.news_url,
                technical_score=representative.technical_score,
                technical_direction=representative.technical_direction,
                technical_rsi=representative.technical_rsi,
                catalyst_type=representative.catalyst_type,
                catalyst_summary=representative.catalyst_summary,
                program_match=representative.program_match,
                strategy_name=self.name,
                company_name=info.get("name", ticker) or representative.company_name,
                company_description=info.get("description", "") or representative.company_description,
                components={
                    "agreement_count": agreement_count,
                    "conflicted": conflicted,
                    "minority_count": minority_count,
                    "strategies_agreeing": strategy_names,
                    "strategies_conflicting": minority_names,
                    "base_confidence": round(base_confidence, 4),
                    "boost": round(boost, 4),
                },
            ))

        # Sort by confidence descending; conflicted/NEUTRAL last
        ensemble_signals.sort(
            key=lambda s: (s.signal_type != "NEUTRAL", s.confidence),
            reverse=True,
        )
        logger.info(f"FullEnsembleStrategy: {len(ensemble_signals)} ensemble signals")
        return ensemble_signals


def _pick_representative(signals: list[Signal], priority: list[str]) -> Signal:
    """Select the most information-rich signal as the ensemble's representative."""
    for strategy in priority:
        for sig in signals:
            if sig.strategy_name == strategy:
                return sig
    return signals[0]
