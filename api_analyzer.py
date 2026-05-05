import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetActivityListRequest
from alpaca.trading.enums import ActivityType

load_dotenv()
try:
    trading_client = TradingClient(os.getenv('ALPACA_API_KEY'), os.getenv('ALPACA_SECRET_KEY'), paper=True)
    print("✅ Alpaca API接続成功: FILL履歴を解析します...")
except Exception as e: print(f"🚨 接続エラー: {e}"); exit(1)

def analyze():
    activities, page_token = [], None
    try:
        while True:
            req = GetActivityListRequest(activity_types=[ActivityType.FILL], page_token=page_token)
            res = trading_client.get_account_activities(req)
            if not res: break
            activities.extend(res)
            if len(res) < 100: break
            page_token = res[-1].id
    except Exception as e: print(f"🚨 履歴取得エラー: {e}"); return

    if not activities: return print("📊 約定履歴なし")
    activities.reverse()
    
    trades, open_pos = [], {}
    for act in activities:
        sym, side, qty, price = act.symbol, act.side.value, float(act.qty), float(act.price)
        cid = getattr(act, 'client_order_id', None) or getattr(act, 'order_id', "unknown-unknown")
        sid = cid.rsplit('-', 2)[0] if cid and cid.count('-') >= 2 else "unknown"

        if side == 'buy':
            if sym not in open_pos: open_pos[sym] = []
            open_pos[sym].append({'price': price, 'qty': qty, 'time': act.transaction_time, 'strategy': sid})
        else:
            rem_qty = qty
            while rem_qty > 0 and sym in open_pos and open_pos[sym]:
                entry = open_pos[sym][0]; matched = min(rem_qty, entry['qty'])
                fee = float(getattr(act, 'fee', 0.0))
                pnl_val = ((price - entry['price']) * matched) - fee
                trades.append({'symbol': sym, 'strategy_id': entry['strategy'], 'exit_time': act.transaction_time, 'pnl_pct': (price - entry['price']) / entry['price'], 'pnl_value': pnl_val})
                entry['qty'] -= matched; rem_qty -= matched
                if entry['qty'] == 0: open_pos[sym].pop(0)

    if not trades: return print("📊 完了済みトレードなし")
    df = pd.DataFrame(trades).sort_values('exit_time').reset_index(drop=True)
    df['equity'] = 100000.0 + df['pnl_value'].cumsum()
    df['drawdown'] = (df['equity'] - df['equity'].cummax()) / df['equity'].cummax()
    
    ret = df['equity'].pct_change().dropna()
    sharpe = (ret.mean() / ret.std()) * np.sqrt(252) if len(ret)>1 and ret.std()!=0 else 0

    print("\n" + "="*55 + "\n💎 THE QUANT TRUTH: パフォーマンスレポート\n" + "="*55)
    print(f"💰 総損益: ${df['pnl_value'].sum():.2f} | 📉 最大DD: {(df['drawdown'].min()*100):.2f}% | ⚖️ Sharpe: {sharpe:.2f}")
    
    for label, col in [("戦略別", "strategy_id"), ("銘柄別", "symbol")]:
        print(f"\n🎯 【{label}成績】")
        g = df.groupby(col)['pnl_value'].agg(['count', 'sum', 'mean'])
        for idx, row in g.iterrows(): print(f"  [{idx}] {int(row['count'])}回 | P&L: ${row['sum']:.2f} | EV: ${row['mean']:.2f}")
    
    try:
        plt.figure(figsize=(10, 5)); plt.plot(df['exit_time'], df['equity'], color='blue', label='Portfolio Equity')
        plt.title('Equity Curve'); plt.grid(True); plt.show()
    except Exception: pass

if __name__ == "__main__":
    analyze()