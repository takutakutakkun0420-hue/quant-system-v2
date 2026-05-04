import os
import logging
from datetime import datetime
import yfinance as yf
import pandas as pd
import numpy as np
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

# --- 1. SRE仕様：ロギング設定 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler() # GitHub Actionsのログに残すため標準出力のみ（ファイル出力は削除）
    ]
)
logger = logging.getLogger(__name__)

# --- 2. 設定値 ---
SYMBOLS = [
    "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "TSLA", "META", "NFLX", "AMD",
    "JPM", "XOM", "KO", "WMT"
]
RISK_PCT = 0.01             
STOP_LOSS_PCT = 0.05        
TRAILING_STOP_PCT = 0.05    
ENTRY_CHANGE_PCT = 0.015    
VOLUME_MULTIPLIER = 1.1     
RSI_LIMIT = 70              
VIX_KILL_SWITCH = 30.0      

DRY_RUN = True  # 🛡️ 観測フェーズ（1週間ログ収集）のためON

# --- 3. Alpaca API接続 ---
load_dotenv()
API_KEY = os.getenv('ALPACA_API_KEY')
SECRET_KEY = os.getenv('ALPACA_SECRET_KEY')

try:
    trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
    account = trading_client.get_account()
    logger.info(f"✅ Alpaca API接続成功: 口座残高 [${account.portfolio_value}]")
    if DRY_RUN:
        logger.warning("🛡️ DRY_RUNモード有効 (発注・決済スキップ)")
except Exception as e:
    logger.critical(f"🚨 Alpaca API接続エラー: {e}")
    exit(1)

# --- 4. 市場データ取得 ---
def get_market_data(symbol):
    try:
        df = yf.Ticker(symbol).history(period="3mo")
        if len(df) < 51: return None
        df['Prev_Close'] = df['Close'].shift(1)
        df['Change_Rate'] = (df['Close'] - df['Prev_Close']) / df['Prev_Close']
        df['Volume_MA'] = df['Volume'].rolling(20).mean()
        df['MA50'] = df['Close'].rolling(50).mean()
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        return df.iloc[-1], df.iloc[-2]
    except Exception as e:
        logger.error(f"⚠️ {symbol} データ取得失敗: {e}")
        return None, None

# 【NEW】ステートレスな最高値の動的計算 (v7のコア)
def calculate_dynamic_highest_price(symbol, current_price, position_pl_pcnt):
    """
    ローカルDBを使わず、現在価格と含み益の割合から「簡易的な最高値」を算出する。
    ※日中のヒゲまでは追えないが、日足ベースのトレーリングとしては十分に機能する。
    """
    try:
        # 含み益がマイナスの場合は、現在価格を最高値とする（まだ利益が出ていないため）
        if position_pl_pcnt <= 0:
            return current_price
            
        # yfinanceから直近の最高値を取得して補正する（より正確に）
        hist = yf.Ticker(symbol).history(period="1mo") # 過去1ヶ月のデータを取得
        if not hist.empty:
            recent_high = hist['High'].max()
            # 現在価格と、直近1ヶ月の高値の、高い方を「最高値」として採用する
            return max(current_price, recent_high)
        return current_price
    except Exception as e:
        logger.error(f"⚠️ {symbol} 動的最高値計算エラー: {e}")
        return current_price

# --- 5. メイン執行エンジン ---
def run_execution_cycle():
    logger.info("🚀 執行サイクル(v7: Stateless Cloud-Native Architecture)を開始...")
    portfolio_value = float(account.portfolio_value)
    cash = float(account.cash)

    # 🎯 [STEP 1] Pending注文の取得 (SSOT)
    alpaca_pending_symbols = []
    try:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        open_orders = trading_client.get_orders(filter=req)
        if open_orders:
            logger.info("📋 [ORDER] Alpaca側のPending注文 (新規発注ロック):")
            for o in open_orders:
                alpaca_pending_symbols.append(o.symbol)
                logger.info(f"  └ {o.symbol} | status={o.status.value}")
        else:
            logger.info("📋 [ORDER] Pending注文はありません。")
    except Exception as e:
        logger.error(f"⚠️ 注文取得エラー: {e}")

    # 🎯 [STEP 2] Position取得と Exit(ステートレス・トレーリング)監視
    try:
        positions = trading_client.get_all_positions()
        existing_symbols = []
        
        for p in positions:
            sym = p.symbol
            existing_symbols.append(sym)
            current_price = float(p.current_price)
            avg_entry = float(p.avg_entry_price)
            qty = p.qty
            unrealized_pl = float(p.unrealized_plpc) * 100

            # 🔥 ローカルDBを使わず、動的に最高値を算出
            dynamic_highest = calculate_dynamic_highest_price(sym, current_price, float(p.unrealized_plpc))
            
            logger.info(f"💼 [POSITION] {sym}: 取得 ${avg_entry:.2f} | 現在 ${current_price:.2f} | 動的最高値 ${dynamic_highest:.2f} | 損益 {unrealized_pl:.2f}%")

            # 決済シグナル (Exitロジックの完成)
            if current_price <= avg_entry * (1 - STOP_LOSS_PCT):
                logger.warning(f"🔴 [EXIT SIGNAL - STOP LOSS] {sym}: 損切りライン(-5%)到達！")
                if not DRY_RUN:
                    trading_client.submit_order(order_data=MarketOrderRequest(symbol=sym, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC))
                    logger.info(f"✅ {sym}: 損切り決済完了")
                else:
                    logger.info(f"  🛡️ DRY_RUN: {sym}の損切りをスキップ")
                    
            elif current_price <= dynamic_highest * (1 - TRAILING_STOP_PCT):
                 logger.warning(f"🟢 [EXIT SIGNAL - TRAILING STOP] {sym}: トレーリング利確ライン(最高値から-5%)到達！")
                 if not DRY_RUN:
                    trading_client.submit_order(order_data=MarketOrderRequest(symbol=sym, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC))
                    logger.info(f"✅ {sym}: トレーリング決済完了")
                 else:
                    logger.info(f"  🛡️ DRY_RUN: {sym}の利確決済をスキップ")

    except Exception as e:
         logger.error(f"ポジション取得エラー: {e}")
         existing_symbols = []

    # 🎯 [STEP 3] VIX監視 & 新規エントリー
    try:
        vix = yf.Ticker("^VIX").history(period="5d")['Close'].iloc[-1]
        if vix >= VIX_KILL_SWITCH:
            logger.warning(f"🚨 VIXキルスイッチ発動 ({vix:.2f} >= {VIX_KILL_SWITCH})。新規エントリー中止。")
            return
    except Exception:
        logger.warning("VIX取得失敗。安全を優先し新規エントリー中止。")
        return

    for symbol in SYMBOLS:
        if symbol in existing_symbols or symbol in alpaca_pending_symbols:
            continue
            
        current_data, prev_data = get_market_data(symbol)
        if current_data is None: continue
        current_price = current_data['Close']
        
        if (
            current_data['Change_Rate'] >= ENTRY_CHANGE_PCT
            and current_price > current_data['MA50']
            and current_data['MA50'] > prev_data['MA50']
            and current_data['Volume'] > current_data['Volume_MA'] * VOLUME_MULTIPLIER
            and current_data['RSI'] < RSI_LIMIT
        ):
            max_loss_amount = portfolio_value * RISK_PCT
            risk_per_share = current_price * STOP_LOSS_PCT
            shares_to_buy = int(np.floor(max_loss_amount / risk_per_share))
            
            if shares_to_buy > 0 and (shares_to_buy * current_price) <= cash:
                logger.info(f"🔥 [ENTRY SIGNAL] {symbol} | 期待価格: ${current_price:.2f} | 株数: {shares_to_buy}株")
                if not DRY_RUN:
                    trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=shares_to_buy, side=OrderSide.BUY, time_in_force=TimeInForce.GTC))
                    logger.info(f"✅ {symbol}: 買い注文送信完了")
                else:
                    logger.info(f"  🛡️ DRY_RUN: {symbol}の買い発注をスキップ")

    logger.info("🏁 執行サイクル(v7 Stateless)完了。")

if __name__ == "__main__":
    run_execution_cycle()