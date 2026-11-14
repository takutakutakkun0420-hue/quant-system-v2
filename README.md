# Quant System v2

A Python-based automated market observation, order execution, and strategy backtesting engine driven by YAML configuration and scheduled GitHub Actions workflows.

## Overview

`quant-system-v2` is a lightweight, stateless-friendly execution and backtesting framework built for systematic market data analysis and automated order lifecycle management. It separates strategy parameters (`strategy_config.yaml`) and runtime position tracking (`positions_state.json`) from core execution and backtesting logic.

### Core Components

- **`execution_engine.py`**: Handles market state evaluation, order placement, concurrency control, and stale order cleanup.
- **`backtest_engine.py`**: Runs historical simulations against configured strategy parameters to evaluate performance metrics before live deployment.
- **`api_analyzer.py`**: Inspects and validates external market data and broker API responses.
- **`strategy_config.yaml`**: Centralized configuration for global execution settings, risk thresholds, and per-strategy parameters.
- **`positions_state.json`**: Tracks active position states across scheduled execution runs.
- **`.github/workflows/`**: Automated workflow definitions for scheduled observation and execution phases.

## Getting Started

### Prerequisites

- Python 3.10+
- Required packages: `pandas`, `numpy`, `yfinance`, `pyyaml`, `requests`, `alpaca-trade-api`

### Usage

1. Review and adjust parameters in `strategy_config.yaml`.
2. Run backtesting on historical data:
   ```bash
   python backtest_engine.py
