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
│   ├── notifier.py               # Slack Block Kit notifications via Web API
│   └── slack_actions.py          # Socket Mode listener for Approve/Reject button callbacks
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

## How scheduling works

No cron setup needed. APScheduler's `BlockingScheduler` is embedded in the process itself. When you run `python main.py agent`, it registers three internal jobs and then sleeps between firings:

| Job | When |
|---|---|
| Morning cycle | 10:15 AM EST, weekdays |
| Afternoon cycle | 3:45 PM EST, weekdays |
| Weekly audit | 4:00 PM EST, Fridays |

The process never exits between cycles. It wakes itself at the scheduled time, runs the cycle, and goes back to sleep. The only external thing you need is something to keep the process alive across crashes and reboots - Docker's `restart: unless-stopped` handles that.

## Running without Docker

The Quickstart section above is the no-Docker path. Clone, create a venv, install requirements.txt, copy `.env`, and run `python main.py agent`. That is all that is needed for local development.

## Running with Docker

Make sure `.env` exists in the project root (Docker reads it via `env_file:` in docker-compose.yml).

```bash
# Build the image and start the agent in the background
docker compose up --build -d

# Tail live logs
docker compose logs -f agent

# Trigger a cycle manually without restarting the container
docker compose exec agent python main.py agent --run-now morning

# Stop the agent
docker compose down

# Rebuild after a code change and restart
docker compose up --build -d
```

The SQLite database (`trading_system.db`) and logs (`logs/`) are mounted from the host, so trade history and logs persist across container restarts.

## Publishing to Docker Hub

Use `buildx` to create a single multi-platform image that works on amd64 (GCP), arm64 (Raspberry Pi 4), and armv7 (Raspberry Pi 3):

```bash
docker buildx create --use
docker buildx build --platform linux/amd64,linux/arm64,linux/arm/v7 \
  -t your-dockerhub-username/quant-lab-agent:latest \
  --push .
```

On any target machine, Docker automatically pulls the correct layer for that machine's architecture:

```bash
docker pull your-dockerhub-username/quant-lab-agent:latest
docker compose up -d
```

## Deploying to Raspberry Pi

Raspberry Pi 3 (1 GB RAM, ARMv7) is sufficient. The agent is I/O bound - it makes HTTP calls and writes to SQLite for a few seconds per cycle, then sleeps. CPU and RAM are not the constraint.

**Option A - pull from Docker Hub (faster):**

```bash
# On the Pi
docker pull your-dockerhub-username/quant-lab-agent:latest
# Copy docker-compose.yml and your .env to the Pi, then:
docker compose up -d
```

Docker automatically selects `linux/arm/v7` for Pi 3 and `linux/arm64` for Pi 4.

**Option B - build locally on the Pi:**

```bash
# On the Pi
git clone https://github.com/gauravsgr/quant-lab.git
cd quant-lab
cp .env.example .env   # fill in real keys
docker compose up --build -d
```

Building numpy and pandas locally on Pi 3 takes roughly 10-15 minutes the first time. Subsequent starts reuse the cached layer.

## Deploying to GCP free tier

GCP's always-free tier includes one `e2-micro` instance (0.25 vCPU, 1 GB RAM) in `us-central1`, `us-west1`, or `us-east1`. The agent runs comfortably within those limits.

```bash
# 1. Create the VM (or use the GCP Console)
gcloud compute instances create quant-lab \
  --machine-type=e2-micro \
  --zone=us-central1-a \
  --image-family=debian-12 \
  --image-project=debian-cloud

# 2. SSH in
gcloud compute ssh quant-lab --zone=us-central1-a

# 3. Install Docker (on the VM)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# 4. Clone the repo (or pull from Docker Hub)
git clone https://github.com/gauravsgr/quant-lab.git
cd quant-lab

# 5. Copy your .env from local machine (run this locally, not on the VM)
gcloud compute scp .env quant-lab:~/quant-lab/.env --zone=us-central1-a

# 6. Start the agent
docker compose up --build -d

# 7. Tail logs
docker compose logs -f agent
```

Keep the VM in one of the three free regions. Do not run other workloads on the same instance - 1 GB RAM is enough for the agent alone.

## Day-to-day operations

```bash
# Check if the agent container is running
docker compose ps

# Tail live logs
docker compose logs -f agent

# Trigger a cycle immediately without restarting the container
docker compose exec agent python main.py agent --run-now morning
docker compose exec agent python main.py agent --run-now afternoon
docker compose exec agent python main.py agent --run-now weekly

# Rebuild and restart after a code change
docker compose up --build -d

# Stop the agent
docker compose down

# Inspect recent orders in the SQLite database (from the host)
sqlite3 trading_system.db "SELECT ticker, order_type, status, pnl FROM orders ORDER BY submitted_at DESC LIMIT 10;"
```

## Backtesting

```bash
python main.py backtest --strategy rsi_mean_revert --symbol AAPL --start 2023-01-01 --end 2024-01-01
```

Results are written to `backtesting/results/` as JSON with timestamp filenames.

## Notebooks

Two Jupyter notebook templates are in `notebooks/`. Run them locally on your laptop - do not run Jupyter on the GCP machine (1 GB RAM is fully used by the agent).

| Notebook | What it shows |
|---|---|
| `01_backtest_analysis.ipynb` | Equity curve, trade list, P&L distribution for any backtest result file |
| `02_trading_performance.ipynb` | Live/paper trading P&L, signal quality by confidence, source attribution, ghost trade regret, approval history, Adanos budget |

**Running locally against data on GCP:**

```bash
# 1. Copy the database from GCP to your laptop
gcloud compute scp quant-lab:~/quant-lab/trading_system.db ./trading_system.db --zone=us-central1-a

# 2. Open the notebook (the agent on GCP is unaffected)
jupyter notebook notebooks/02_trading_performance.ipynb
```

**Running locally against a local agent:**

```bash
jupyter notebook notebooks/02_trading_performance.ipynb
# DB_PATH in the notebook defaults to ../trading_system.db (repo root)
```

Notebooks contain no strategy logic - they only read from `backtesting/results/` and `trading_system.db`.

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
| `SLACK_BOT_TOKEN` | Yes | Slack bot token (`xoxb-...`) for posting messages |
| `SLACK_APP_TOKEN` | No | Slack app token (`xapp-...`) for Socket Mode; required for interactive Approve/Reject buttons |
| `SLACK_CHANNEL_ID` | Yes | Slack channel ID where trade alerts are posted |
| `DB_PATH` | No | SQLite file path (default: `trading_system.db`) |
| `AGENT_TIMEZONE` | No | Scheduler timezone (default: `America/New_York`) |
| `REQUIRE_APPROVAL` | No | `false` (default): auto-execute and notify Slack as FYI. `true`: send signal to Slack with Approve/Reject buttons; agent waits for your click before trading. |

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

Five tables are maintained in `trading_system.db`:

- `signals` - every scored signal with all raw inputs
- `orders` - every submitted order plus trailing stop state
- `performance` - daily mark-to-market records; includes ghost trades (signals not executed)
- `adanos_usage` - monthly call counter for budget tracking
- `pending_approvals` - signals awaiting Slack approval; resolved when Approve or Reject is clicked

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
