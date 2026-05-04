import yfinance as yf
import pandas as pd
import numpy as np

# --- 1. 絶対ルール（設定値） ---
SYMBOLS = [
    "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "TSLA", "META", "NFLX", "AMD",
    "JPM", "XOM", "KO", "WMT"
]
INITIAL_CAPITAL = 10000.0
RISK_PCT = 0.01             # 1回の最大許容リスク（総資金の1%）
STOP_LOSS_PCT = 0.05        # 損切り -5%
TRAILING_STOP_PCT = 0.05    # トレーリングストップ 高値から-5%
ENTRY_CHANGE_PCT = 0.015    # エントリー条件: 前日比+1.5%
VOLUME_MULTIPLIER = 1.1     # 出来高1.1倍
RSI_LIMIT = 70              # RSI 70未満
VIX_KILL_SWITCH = 30.0      # VIX 30以上でエントリー停止
PERIOD = "10y"              # 過去10年間

def prepare_data():
    print("🌍 13銘柄 + VIX + SPY のデータを一括取得・構築中（数十秒かかります）...")
    tickers = SYMBOLS + ["^VIX", "SPY"]
    
    # yfinanceで一括ダウンロード (API制限回避と高速化)
    data = yf.download(tickers, period=PERIOD, progress=False)
    data.index = pd.to_datetime(data.index).tz_localize(None).floor('D')
    
    dict_df = {}
    vix_close = data['Close']['^VIX']
    spy_close = data['Close']['SPY']
    
    for sym in SYMBOLS:
        df = pd.DataFrame()
        df['Close'] = data['Close'][sym]
        df['Volume'] = data['Volume'][sym]
        df['VIX'] = vix_close
        
        # インジケーター計算
        df['Prev_Close'] = df['Close'].shift(1)
        df['Change_Rate'] = (df['Close'] - df['Prev_Close']) / df['Prev_Close']
        df['Volume_MA'] = df['Volume'].rolling(20).mean()
        df['MA50'] = df['Close'].rolling(50).mean()
        
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        
        dict_df[sym] = df
        
    return dict_df, spy_close

def run_portfolio_backtest():
    dict_df, spy_close = prepare_data()
    print("🚀 ファンド・シミュレーション（全銘柄の同時並列処理）を実行中...")
    
    valid_dates = spy_close.dropna().index
    cash = INITIAL_CAPITAL
    portfolio_history = []
    trade_log = []
    
    # 各銘柄のポジション状態を管理する辞書
    positions = {sym: None for sym in SYMBOLS} 

    for i, current_date in enumerate(valid_dates):
        if i == 0:
            continue
            
        prev_date = valid_dates[i-1]
        current_portfolio_value = cash
        
        # --- 1. 既存ポジションの決済処理と評価額更新 ---
        for sym in SYMBOLS:
            pos = positions[sym]
            df = dict_df[sym]
            
            if current_date not in df.index or pd.isna(df.loc[current_date, 'Close']):
                if pos is not None:
                    current_portfolio_value += pos['shares'] * pos['highest_price']
                continue
                
            current_price = df.loc[current_date, 'Close']
            
            if pos is not None:
                pos['highest_price'] = max(pos['highest_price'], current_price)
                
                # ① 損切り
                if current_price <= pos['entry_price'] * (1 - STOP_LOSS_PCT):
                    cash += pos['shares'] * current_price
                    trade_log.append({'Date': current_date, 'Symbol': sym, 'Type': 'LOSS'})
                    positions[sym] = None
                # ② トレーリングストップ
                elif current_price <= pos['highest_price'] * (1 - TRAILING_STOP_PCT) and current_price > pos['entry_price']:
                    cash += pos['shares'] * current_price
                    trade_log.append({'Date': current_date, 'Symbol': sym, 'Type': 'WIN'})
                    positions[sym] = None
                else:
                    # 保有継続
                    current_portfolio_value += pos['shares'] * current_price
        
        # --- 2. 新規エントリー処理 (1日1銘柄ずつチェック) ---
        for sym in SYMBOLS:
            df = dict_df[sym]
            if current_date not in df.index or prev_date not in df.index:
                continue
                
            row = df.loc[current_date]
            prev_row = df.loc[prev_date]
            
            if pd.isna(row['Close']) or pd.isna(prev_row['Close']) or pd.isna(row['MA50']):
                continue
            
            current_price = row['Close']
            
            if positions[sym] is None:
                if (
                    row['Change_Rate'] >= ENTRY_CHANGE_PCT
                    and row['VIX'] < VIX_KILL_SWITCH
                    and current_price > row['MA50']
                    and row['MA50'] > prev_row['MA50']
                    and row['Volume'] > row['Volume_MA'] * VOLUME_MULTIPLIER
                    and row['RSI'] < RSI_LIMIT
                ):
                    # 💥 ポートフォリオ全体の総資産からリスクを計算
                    max_loss_amount = current_portfolio_value * RISK_PCT
                    risk_per_share = current_price * STOP_LOSS_PCT
                    shares_to_buy = np.floor(max_loss_amount / risk_per_share)
                    investment = shares_to_buy * current_price
                    
                    # キャッシュの残高チェック
                    if investment > 0 and investment <= cash:
                        cash -= investment
                        positions[sym] = {
                            'shares': shares_to_buy,
                            'entry_price': current_price,
                            'highest_price': current_price
                        }
                        trade_log.append({'Date': current_date, 'Symbol': sym, 'Type': 'ENTRY'})

        # 毎日のポートフォリオ評価額を記録
        final_daily_value = cash
        for sym, pos in positions.items():
            if pos is not None:
                try:
                    final_daily_value += pos['shares'] * dict_df[sym].loc[current_date, 'Close']
                except:
                    final_daily_value += pos['shares'] * pos['highest_price']
                    
        portfolio_history.append({'Date': current_date, 'Capital': final_daily_value})

    # --- 結果解析 ---
    res_df = pd.DataFrame(portfolio_history).set_index('Date')
    res_df['Peak'] = res_df['Capital'].cummax()
    res_df['Drawdown'] = (res_df['Capital'] - res_df['Peak']) / res_df['Peak'] * 100
    
    max_drawdown = res_df['Drawdown'].min()
    final_capital = res_df['Capital'].iloc[-1]
    total_return = ((final_capital - INITIAL_CAPITAL) / INITIAL_CAPITAL) * 100
    
    # SPYの10年リターン算出
    spy_return = ((spy_close.iloc[-1] - spy_close.iloc[0]) / spy_close.iloc[0]) * 100
    
    trades_df = pd.DataFrame([t for t in trade_log if t['Type'] in ['WIN', 'LOSS']])
    total_trades = len(trades_df)
    win_rate = (len(trades_df[trades_df['Type'] == 'WIN']) / total_trades * 100) if total_trades > 0 else 0

    print("\n" + "⸻"*22)
    print("👑 【完成版】ファンド・バックテスト結果 (13銘柄統合 / 過去10年)")
    print("⸻"*22)
    print(f"💰 初期資金: ${INITIAL_CAPITAL:,.2f}")
    print(f"🏦 最終資金: ${final_capital:,.2f}")
    print("-" * 44)
    print(f"🎯 システム総リターン: +{total_return:,.2f}%")
    print(f"📈 比較 SPY(S&P500): +{spy_return:,.2f}%")
    print(f"🛡️ システム最大MDD:    {max_drawdown:.2f}% (※SPYは期間中-33%以上)")
    print("-" * 44)
    print(f"⚔️ 総取引回数: {total_trades} 回 (月平均: 約 {total_trades/120:.1f} 回)")
    print(f"🏆 全体勝率:   {win_rate:.2f}%")
    print("⸻"*22)

if __name__ == "__main__":
    run_portfolio_backtest()