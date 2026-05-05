import os
import time
import logging
import json
import urllib.request
import uuid
import yaml
from datetime import datetime
import yfinance as yf
import pandas as pd
import numpy as np
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

# ==========================================
# 1. SRE仕様：ロギング & Slack通知
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

def send_slack_notification(message, level="info"):
    webhook_url = os.getenv('SLACK_WEBHOOK_URL')
    logger.info(f"Slack webhook exists: {bool(webhook_url)}")
    if not webhook_url:
        logger.error("SLACK_WEBHOOK_URL is empty")
        return
    emoji = {"info": "ℹ️", "success": "✅", "warning": "⚠️", "danger": "🚨"}.get(level, "💬")
    payload = {"text": f"{emoji} *Quant System*\n{message}"}
    try:
        req = urllib.request.Request(webhook_url, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=2) 
    except Exception as e: logger.error(f"Slack通知失敗: {e}")

# ==========================================
# 2. Config & API 初期化
# ==========================================
try:
    with open("strategy_config.yaml", "r") as f:
        CONFIG = yaml.safe_load(f)
    GLOBAL, STRATEGIES = CONFIG['global_settings'], CONFIG['strategies']
except Exception as e:
    logger.critical(f"🚨 設定読み込みエラー: {e}"); exit(1)

SYMBOLS, DRY_RUN = ["QQQ", "AAPL", "NVDA"], False
load_dotenv()
try:
    trading_client = TradingClient(os.getenv('ALPACA_API_KEY'), os.getenv('ALPACA_SECRET_KEY'), paper=True)
    logger.info("✅ Alpaca API接続成功")
except Exception as e: send_slack_notification(f"🚨 接続エラー: {e}", "danger"); exit(1)

# ==========================================
# 3. データ取得エンジン
# ==========================================
def get_market_data(symbol, period="3mo"):
    for _ in range(3):
        try:
            df = yf.Ticker(symbol).history(period=period)
            if df.empty or len(df) < 51: return None
            df['Prev_Close'] = df['Close'].shift(1)
            df['Volume_MA'], df['MA50'] = df['Volume'].rolling(20).mean(), df['Close'].rolling(50).mean()
            delta = df['Close'].diff()
            gain, loss = (delta.where(delta > 0, 0)).rolling(14).mean(), (-delta.where(delta < 0, 0)).rolling(14).mean()
            df['RSI'] = 100 - (100 / (1 + (gain / loss)))
            df['Range'], df['Avg_Range_5d'] = (df['High'] - df['Low']), (df['High'] - df['Low']).rolling(5).mean()
            df['TR'] = df[['High', 'Low', 'Prev_Close']].apply(lambda x: max(x[0]-x[1], abs(x[0]-x[2]), abs(x[1]-x[2])), axis=1)
            df['ATR'] = df['TR'].rolling(14).mean()
            return df
        except Exception: time.sleep(2)
    return None

def calculate_true_highest(symbol, current_price, avg_entry):
    hist = get_market_data(symbol, period="1mo")
    if hist is None: return current_price
    idx = abs(hist['Close'] - avg_entry).idxmin()
    return max(current_price, hist.loc[idx:]['High'].max()) if abs(hist.loc[idx, 'Close'] - avg_entry)/avg_entry < 0.03 else current_price

# ==========================================
# 4. メイン執行エンジン
# ==========================================
def run_execution_cycle():
    logger.info("🚀 執行サイクル開始...")

    send_slack_notification("🧪 TEST: Slack接続OK", "info")
    
    # [防御] 市場オープン確認
    try:
        if not trading_client.get_clock().is_open:
            logger.info("🛑 Market closed")
            return
    except Exception as e:
        logger.error(f"市場状態取得エラー: {e}")
        return

    # [防御] 資産・DD監視 (ハートビート)
    try:
        account = trading_client.get_account()
        portfolio_value, cash = float(account.portfolio_value), float(account.cash)
        logger.info(f"💼 PORTFOLIO: ${portfolio_value:.2f} | CASH: ${cash:.2f}")
        
        if (portfolio_value - 100000.0) / 100000.0 <= GLOBAL['system_stop_drawdown']:
            send_slack_notification("🚨 最大DD突破。システム停止。", "danger")
            return
    except Exception as e:
        logger.error(f"アカウント情報取得エラー: {e}")
        send_slack_notification(f"🚨 アカウント初期チェックエラー: {e}", "danger")
        return

    # VIX監視 (2連続判定)
    vix_df = get_market_data("^VIX", period="5d")
    vix_kill = False
    if vix_df is not None and len(vix_df) >= 2:
        if vix_df['Close'].iloc[-1] >= GLOBAL['vix_kill_switch'] and vix_df['Close'].iloc[-2] >= GLOBAL['vix_kill_switch']:
            vix_kill = True; send_slack_notification("🚨 VIX継続異常。全決済実行", "danger")

    # ポジション処理
    existing_symbols = set()
    try:
        positions = trading_client.get_all_positions()
        for p in positions:
            qty, sym = float(p.qty), p.symbol
            if qty == 0: continue
            existing_symbols.add(sym)
            curr_p, avg_e = float(p.current_price), float(p.avg_entry_price)

            if vix_kill:
                if not DRY_RUN: trading_client.submit_order(order_data=MarketOrderRequest(symbol=sym, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
                continue

            df = get_market_data(sym, period="1mo")
            if df is None: continue
            atr = df['ATR'].iloc[-1]
            if pd.isna(atr) or atr == 0: atr = curr_p * 0.05 # フェイルセーフ

            true_h = calculate_true_highest(sym, curr_p, avg_e)
            stop_p, trail_p = avg_e - (atr * GLOBAL['default_atr_stop_multiplier']), true_h - (atr * GLOBAL['default_atr_trail_multiplier'])

            if curr_p <= stop_p:
                loss_pct = (curr_p - avg_e) / avg_e
                msg_level = "danger" if loss_pct < -0.10 else "warning"
                send_slack_notification(f"{'🚨 GAP' if loss_pct < -0.10 else '🔴 STOP'} LOSS {sym}: {loss_pct:.2%}", msg_level)
                if not DRY_RUN: trading_client.submit_order(order_data=MarketOrderRequest(symbol=sym, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
            elif curr_p <= trail_p:
                 send_slack_notification(f"🟢 TRAILING STOP {sym}", "success")
                 if not DRY_RUN: trading_client.submit_order(order_data=MarketOrderRequest(symbol=sym, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
    except Exception as e: logger.error(f"ポジションエラー: {e}")

    # 新規エントリー制御
    open_orders = trading_client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    pending_syms = {o.symbol for o in open_orders} if open_orders else set()
    total_positions = len(existing_symbols.union(pending_syms))
    if vix_kill or total_positions >= GLOBAL.get('max_concurrent_positions', 2): return

    for symbol in SYMBOLS:
        if symbol in existing_symbols or symbol in pending_syms: continue
        df = get_market_data(symbol)
        if df is None: continue
        curr, prev = df.iloc[-1], df.iloc[-2]
        atr = curr['ATR'] if not pd.isna(curr['ATR']) else curr['Close'] * 0.05

        is_breakout, is_not_overbought = curr['Close'] > prev['High'], curr['RSI'] < 80
        matched = None
        if is_breakout and is_not_overbought:
            if curr['Volume'] > (curr['Volume_MA'] * STRATEGIES['vcp_breakout_v1']['volume_threshold']) and curr['Range'] < (curr['Avg_Range_5d'] * STRATEGIES['vcp_breakout_v1']['vcp_compression_ratio']):
                matched = 'vcp_breakout_v1'
            elif curr['Volume'] > (curr['Volume_MA'] * STRATEGIES['gap_up_v1']['volume_threshold']) and curr['Open'] > (prev['Close'] * STRATEGIES['gap_up_v1']['gap_threshold_pct']):
                matched = 'gap_up_v1'
        
        if matched:
            risk_p_s = atr * GLOBAL['default_atr_stop_multiplier']
            shares = int(np.floor((portfolio_value * STRATEGIES[matched]['risk_pct_per_trade']) / risk_p_s)) if risk_p_s > 0 else 0
            buy_qty = min(shares, int(portfolio_value * GLOBAL['max_portfolio_risk_pct'] / curr['Close']), GLOBAL['max_shares_per_trade'])
            
            if buy_qty > 0 and (buy_qty * curr['Close']) <= cash:
                if not DRY_RUN:
                    try:
                        order_id = f"{matched}-{symbol}-{uuid.uuid4().hex[:6]}"
                        trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=buy_qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY, client_order_id=order_id))
                        send_slack_notification(f"🔥 ENTRY: {matched} {symbol} ({buy_qty}株)", "info")
                    except Exception as e: logger.error(f"発注エラー: {e}")

if __name__ == "__main__":
    run_execution_cycle()