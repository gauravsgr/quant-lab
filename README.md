# quant-lab

A personal algorithmic trading research and execution framework. Combines price-bar strategies with an alternative-data trading agent that integrates congressional stock disclosures, social sentiment, and analyst consensus into daily paper-trading decisions via Alpaca.

## What it does

The alternative-data agent runs on a daily schedule:

- **10:15 AM EST** - Pulls buzzing tickers from Adanos Market Sentiment, scrapes Capitol Trades for recent congressional disclosures, fetches Alpaca News headlines, and pulls yfinance analyst consensus. Scores each ticker using a weighted confidence formula, then submits call/put options orders on the Alpaca paper account for high-conviction signals.
- **3:45 PM EST** - Marks all open positions to market, updates trailing stops, records ghost trades (signals that fired but were not executed), and sends a daily Slack summary.
- **Friday 4:00 PM EST** - Runs a precision audit across all signal sources (win rate by source type) and posts a weekly Slack report.

All trades are logged to a local SQLite database. Slack notifications include a 5-section breakdown: executive summary, signal breakdown with ASCII progress bars, contextual news, counter-considerations reviewed, and research links.

## Project structure

```
quant-lab/
├── main.py                       # CLI entry point (agent / trade / backtest subcommands)
├── .env.example                  # environment variable template (safe to commit)
├── requirements.txt
├── config/
│   ├── settings.py               # loads .env into a typed Settings dataclass
│   └── strategies/
│       └── confluence.yaml       # all tunable weights and thresholds
├── agent/
│   ├── orchestrator.py           # wires data loaders, strategy, and execution into cycles
│   └── scheduler.py              # APScheduler: three cron jobs
├── db/
│   ├── models.py                 # SQLAlchemy Core table definitions
│   └── repository.py             # all DB reads and writes
├── data/
│   └── loaders/
│       ├── adanos.py             # Adanos Market Sentiment REST client
│       ├── capitol_trades.py     # BeautifulSoup scraper for capitoltrades.com
│       ├── alpaca_news.py        # Alpaca News API client
│       └── yfinance_ratings.py   # analyst consensus via yfinance (no API key needed)
├── strategies/
│   ├── base.py                   # Signal dataclass + abstract Strategy
│   └── confluence.py             # ConfluenceStrategy: confidence-weighted signal generator
├── execution/
│   ├── risk.py                   # position sizing and trailing stop logic
│   ├── validator.py              # pre-trade checks (confidence, duplicate guard, approval mode)
│   ├── runner.py                 # sole order-submission chokepoint
│   └── portfolio.py              # mark-to-market and ghost trade recording
├── brokers/
│   ├── base.py                   # abstract Broker interface
│   ├── alpaca.py                 # Alpaca paper/live broker via alpaca-py
│   └── backtest_broker.py        # in-memory broker for backtesting
├── backtesting/
│   ├── engine.py                 # historical replay
│   ├── metrics.py                # Sharpe, drawdown, win rate
│   └── results/                  # gitignored; JSON output per run
├── utils/
│   ├── rate_limiter.py           # sliding-window rate limiter decorator
│   ├── logging.py                # loguru setup (colorized stderr + serialized file sink)
│   └── notifier.py               # Slack Block Kit notifications
├── notebooks/                    # analysis only; reads results/ and logs/
├── logs/                         # gitignored; structured JSON trade logs
└── tests/
    ├── test_confluence.py
    ├── test_risk.py
    ├── test_repository.py
    ├── test_adanos.py
    ├── test_capitol_trades.py
    └── test_notifier.py
```

## Quickstart

```bash
# 1. Clone the repo
git clone https://github.com/gauravsgr/quant-lab.git
cd quant-lab

# 2. Create virtual environment and install dependencies
#    Uses uv (fast pip replacement). Install uv if needed:
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt

# 3. Configure environment variables
cp .env.example .env
# Edit .env with your real API keys (this file is gitignored)

# 4. Dry run: validate config and broker connectivity
python main.py agent --dry-run

# 5. Start the scheduled agent (blocks; runs all three daily cycles)
python main.py agent
```

## Manual cycle triggers

```bash
# Run morning cycle immediately (useful for testing)
python main.py agent --run-now morning

# Run afternoon mark-to-market immediately
python main.py agent --run-now afternoon

# Run weekly audit immediately
python main.py agent --run-now weekly
```

## Backtesting

```bash
python main.py backtest --strategy rsi_mean_revert --symbol AAPL --start 2023-01-01 --end 2024-01-01
```

Results are written to `backtesting/results/` as JSON with timestamp filenames.

## Running tests

```bash
pytest tests/ -v
```

## Environment variables

Copy `.env.example` to `.env` and fill in real values. Never commit `.env`.

| Variable | Required | Description |
|---|---|---|
| `TRADING_MODE` | Yes | `paper` or `live` |
| `ALPACA_API_KEY` | Yes | Alpaca API key ID |
| `ALPACA_SECRET_KEY` | Yes | Alpaca API secret |
| `ALPACA_BASE_URL` | No | Defaults to paper endpoint |
| `ADANOS_API_KEY` | Yes | Adanos Market Sentiment API key |
| `SLACK_WEBHOOK_URL` | Yes | Incoming webhook URL for trade alerts |
| `DB_PATH` | No | SQLite file path (default: `trading_system.db`) |
| `AGENT_TIMEZONE` | No | Scheduler timezone (default: `America/New_York`) |
| `REQUIRE_APPROVAL` | No | `true` to notify only without executing; `false` to auto-execute |

## Data sources

| Source | Purpose | Cost |
|---|---|---|
| Adanos Market Sentiment | Social buzz scores and sentiment across Reddit, StockTwits, Twitter | Free tier: 250 calls/month; budget guard at 225 |
| Capitol Trades (scraped) | Congressional stock disclosure filings (STOCK Act) | Free; polite scraping with rate limiting |
| Alpaca News API | Recent news headlines for buzzing tickers | Included with Alpaca account |
| yfinance | Analyst consensus ratings and price targets | Free; no API key needed |

## Signal logic

The `ConfluenceStrategy` computes a weighted confidence score per ticker:

```
confidence = 0.40 * sentiment + 0.35 * politician + 0.25 * analyst
```

Signal rules:
- **STRONG_BUY** (call option): sentiment > 0.70 AND politician == BUY AND analyst == Strong Buy
- **STRONG_PUT** (put option): sentiment < -0.70 AND (politician == SELL OR analyst in Sell/Strong Sell)
- **NEUTRAL**: everything else; no trade if confidence < 0.65

All weights and thresholds are in `config/strategies/confluence.yaml` and can be tuned without touching code.

## Risk management

- **Max position size**: 5% of account equity per trade (configurable via `MAX_POSITION_PCT` env var)
- **Trailing stop**: 10% from the position's highest close; updated at 3:45 PM EST each day
- **Duplicate guard**: one trade per ticker per day maximum
- **Budget guard**: Adanos API calls are tracked in SQLite; cycle is skipped if the monthly limit is reached

## SQLite schema

Four tables are maintained in `trading_system.db`:

- `signals` - every scored signal with all raw inputs
- `orders` - every submitted order plus trailing stop state
- `performance` - daily mark-to-market records; includes ghost trades (signals not executed)
- `adanos_usage` - monthly call counter for budget tracking

## Paper vs live trading

The `TRADING_MODE` env var controls which Alpaca endpoint is used. Set to `paper` for the paper trading account (default) or `live` for real money. No code changes required.

## Adding a new strategy

1. Add a YAML to `config/strategies/<name>.yaml` with all hyperparameters
2. Create `strategies/<name>.py` subclassing `strategies/base.py:Strategy`
3. Implement `generate_signal(df: pd.DataFrame) -> Signal`
4. Add tests in `tests/test_strategies.py`
5. Backtest before wiring to live execution

## Adding a new broker

1. Create `brokers/<name>.py` subclassing `brokers/base.py:Broker`
2. Implement all abstract methods: `submit_order`, `submit_options_order`, `get_positions`, `get_bars`, `cancel_order`, `get_account_equity`, `get_latest_price`, `get_options_chain`
3. Register it in `main.py`'s broker factory

## Dependencies

| Package | Purpose |
|---|---|
| `alpaca-py` | Alpaca broker and data API |
| `yfinance` | Analyst consensus and historical price data |
| `pandas` / `numpy` | Data manipulation |
| `sqlalchemy` | SQLite database access layer |
| `requests` + `beautifulsoup4` | Capitol Trades scraper |
| `fake-useragent` | User-Agent rotation for scraping |
| `tenacity` | Retry and exponential backoff |
| `APScheduler` | Daily cron jobs for the three cycles |
| `slack-sdk` | Slack Block Kit notifications |
| `loguru` | Structured JSON logging with rotation |
| `pyyaml` | Strategy config files |
| `ta` | Technical indicators for price-bar strategies |
| `vectorbt` | Fast backtesting engine |
| `jupyter` + `plotly` | Notebook analysis |
| `pytest` + `pytest-mock` | Test suite |

## Disclaimer

This is a personal research project. Nothing here constitutes financial advice. Always paper trade before going live and never risk more than you can afford to lose.
