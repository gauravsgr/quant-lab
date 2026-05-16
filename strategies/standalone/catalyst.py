"""Standalone catalyst discovery strategy (keyword + government program matching).

Entirely free — no external API calls. Uses pre-computed catalyst_hits from the
DataBundle (which are populated by data/bundle.py's _scan_catalyst_hits() using
CATALYST_PATTERNS and config/catalyst/government_programs.yaml).

Scoring:
    keyword_score   = min(1.0, len(keywords_matched) * 0.35)   keyword hits
    program_score   = 1.0 if program matched, else 0.0
    recency_score   = 1.0 (0-2 days), 0.5 (3-5 days), 0.2 (6-7 days)

    confidence = 0.35 * keyword_score + 0.45 * program_score + 0.20 * recency_score

Catalyst signals are always STRONG_BUY — supply deals, partnerships, and gov
program tailwinds are typically positive catalysts. Negative catalysts (recalls,
regulatory blocks) are detected by keywords in CATALYST_PATTERNS but not traded
short in this strategy (they emit NEUTRAL to avoid false shorts).

Threshold: 0.50 (higher than technical — catalyst must be meaningful)
"""
from loguru import logger

from strategies.base import Signal, StandaloneStrategy


ANALYST_SCORE_MAP = {
    "Strong Buy": 1.0,
    "Buy": 0.75,
    "Hold": 0.50,
    "Sell": 0.25,
    "Strong Sell": 0.0,
}

THRESHOLD = 0.50
POSITIVE_TYPES = {"supply_deal", "gov_contract", "partnership", "gov_program", "regulatory", "guidance_raise"}


class CatalystStrategy(StandaloneStrategy):
    """Keyword + government program catalyst discovery strategy."""

    @property
    def name(self) -> str:
        return "catalyst"

    def run(self, bundle) -> list[Signal]:
        if not bundle.catalyst_hits:
            logger.info("CatalystStrategy: no catalyst hits in bundle")
            return []

        signals: list[Signal] = []

        for ticker, hit in bundle.catalyst_hits.items():
            catalyst_type = hit.get("catalyst_type", "")
            if catalyst_type not in POSITIVE_TYPES:
                continue

            keywords = hit.get("keywords_matched", [])
            programs = hit.get("programs", [])
            days_ago = hit.get("days_ago")
            summary = hit.get("summary", "")
            headline = hit.get("headline", "")

            keyword_score = min(1.0, len(keywords) * 0.35) if keywords else 0.0
            program_score = 1.0 if programs else 0.0

            if days_ago is None:
                recency_score = 0.1
            elif days_ago <= 2:
                recency_score = 1.0
            elif days_ago <= 5:
                recency_score = 0.5
            else:
                recency_score = 0.2

            # Must have at least one layer of evidence
            if keyword_score == 0.0 and program_score == 0.0:
                continue

            confidence = round(
                0.35 * keyword_score + 0.45 * program_score + 0.20 * recency_score, 4
            )
            if confidence < THRESHOLD:
                continue

            rating_data = bundle.ratings.get(ticker, {})
            rating = rating_data.get("rating", "Hold")
            info = bundle.company_info.get(ticker, {})
            news_list = bundle.news.get(ticker, [])

            program_name = programs[0].split(":")[0].strip() if programs else None

            signals.append(Signal(
                ticker=ticker,
                signal_type="STRONG_BUY",
                confidence=confidence,
                order_type="call_option",
                analyst_rating=rating,
                analyst_buy_count=rating_data.get("buy_count", 0),
                analyst_hold_count=rating_data.get("hold_count", 0),
                analyst_sell_count=rating_data.get("sell_count", 0),
                analyst_price_target=rating_data.get("price_target"),
                news_headline=headline or (news_list[0].get("headline") if news_list else None),
                news_url=news_list[0].get("url") if news_list else None,
                strategy_name=self.name,
                catalyst_type=catalyst_type,
                catalyst_summary=summary,
                program_match=program_name,
                company_name=info.get("name", ticker),
                company_description=info.get("description", ""),
                components={
                    "keyword_score": keyword_score,
                    "program_score": program_score,
                    "recency_score": recency_score,
                    "keywords": keywords,
                    "programs": programs,
                    "days_ago": days_ago,
                },
            ))

        signals.sort(key=lambda s: s.confidence, reverse=True)
        logger.info(f"CatalystStrategy: {len(signals)} signals from {len(bundle.catalyst_hits)} hits")
        return signals
