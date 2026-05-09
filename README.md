# quant-lab

A personal algorithmic trading research framework for stocks and crypto. Supports multiple strategies, paper trading via Alpaca (and other brokers), backtesting with historical data, and Jupyter notebooks for analysis.

## Features

- Plug-and-play strategy system — add a new strategy in one file
- Broker abstraction layer — swap Alpaca for another broker via config, not code
- `paper` / `live` mode controlled by a single env var, with hard risk guards
- Backtesting engine with Sharpe, drawdown, and win-rate metrics
- Structured JSON trade logs and per-run backtest results
- Jupyter notebooks for interactive performance analysis

## Project structure

```
quant-lab/
├── main.py                     # CLI entry point
├── config/
│   ├── settings.py             # global risk limits, symbols, timeframes
│   └── strategies/             # per-strategy YAML hyperparameters
├── brokers/
│   ├── base.py                 # abstract Broker interface
│   ├── alpaca.py               # Alpaca paper + live
│   └── backtest_broker.py      # simulated broker for backtests
├── strategies/
│   ├── base.py                 # abstract Strategy interface
│   └── ...                     # one file per strategy
├── backtesting/
│   ├── engine.py
│   ├── metrics.py
│   └── results/                # gitignored — JSON/CSV per run
├── execution/
│   ├── portfolio.py            # position sizing
│   ├── risk.py                 # hard stop guards
│   └── runner.py               # live trading loop
├── data/
│   ├── loaders/                # Alpaca + yfinance data fetchers
│   └── cache/                  # gitignored — parquet cache
├── notebooks/                  # analysis only — reads results/, logs/
├── logs/                       # gitignored — structured trade logs
└── tests/
```

## Quickstart

```bash
# 1. Clone and install dependencies
git clone https://github.com/gauravsgr/quant-lab.git
cd quant-lab
pip install -r requirements.txt

# 2. Set up environment
cp .env.example .env
# Edit .env with your Alpaca API keys

# 3. Backtest a strategy
python main.py backtest --strategy rsi_mean_revert --symbol AAPL --start 2023-01-01 --end 2024-01-01

# 4. Paper trade
python main.py trade --strategy rsi_mean_revert --broker alpaca --mode paper

# 5. Analyze results
jupyter notebook notebooks/02_backtest_analysis.ipynb
```

## Adding a new strategy

1. Add `config/strategies/<name>.yaml` with hyperparameters
2. Create `strategies/<name>.py` subclassing `strategies/base.py:Strategy`
3. Implement `generate_signal(df: pd.DataFrame) -> Signal`
4. Add tests in `tests/test_strategies.py`
5. Backtest before connecting to live execution

## Adding a new broker

1. Create `brokers/<name>.py` subclassing `brokers/base.py:Broker`
2. Implement: `submit_order()`, `get_positions()`, `get_bars()`, `cancel_order()`
3. Register it in the broker factory in `main.py`

## Environment variables

| Variable | Description |
|---|---|
| `TRADING_MODE` | `paper` or `live` |
| `ALPACA_API_KEY` | Alpaca key ID |
| `ALPACA_SECRET_KEY` | Alpaca secret key |
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets` for paper |

## Dependencies

| Package | Purpose |
|---|---|
| `alpaca-py` | Alpaca broker API |
| `yfinance` | Free historical data for backtests |
| `pandas` / `numpy` | Data manipulation |
| `vectorbt` | Fast backtesting engine |
| `ta` | Technical indicators |
| `loguru` | Structured JSON logging |
| `pyyaml` | Strategy config files |
| `jupyter` + `plotly` | Notebook analysis |

## Disclaimer

This is a personal research project. Nothing here constitutes financial advice. Always paper trade before going live and never risk more than you can afford to lose.
