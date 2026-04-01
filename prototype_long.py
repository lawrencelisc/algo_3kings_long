import ccxt
import pandas as pd
import time
import numpy as np
import os
import logging
from datetime import datetime

# ==========================================
# 0. 日誌與系統設定
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('AlgoTrade')

# ⚠️ 請確保 API 金鑰安全
API_KEY = "1VjRtJ4cjuJiFk2wFs"
API_SECRET = "s5N38enwd75l0CxvIFLPFWWWmAbj2YxK941j"

exchange = ccxt.bybit({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})
exchange.load_markets()

LOG_DIR = "core/result_long_live"
LOG_FILE = f"{LOG_DIR}/09_live_long_log.csv"
if not os.path.exists(LOG_DIR): os.makedirs(LOG_DIR)

positions, cooldown_tracker = {}, {}

# 👑 策略核心參數 (1000U / 10x 槓桿)
WORKING_CAPITAL, MAX_LEVERAGE, RISK_PER_TRADE = 1000.0, 10.0, 0.01
NET_FLOW_SIGMA, TP_ATR_MULT, SL_ATR_MULT, TRAIL_ATR_MULT = 1.5, 1.5, 1.0, 1.0

# 🚫 終極黑名單 (21 種穩定幣與包裝資產)
BLACKLIST = [
    'USDC/USDT:USDT', 'DAI/USDT:USDT', 'FDUSD/USDT:USDT', 'BUSD/USDT:USDT',
    'RLUSD/USDT:USDT', 'USDE/USDT:USDT', 'USAT/USDT:USDT', 'USD0/USDT:USDT',
    'TUSD/USDT:USDT', 'PYUSD/USDT:USDT', 'USTC/USDT:USDT', 'EURS/USDT:USDT',
    'USDP/USDT:USDT', 'LUSD/USDT:USDT', 'FRAX/USDT:USDT', 'MIM/USDT:USDT',
    'WBTC/USDT:USDT', 'WETH/USDT:USDT', 'WBNB/USDT:USDT', 'WHT/USDT:USDT', 'WAVAX/USDT:USDT'
]

CSV_COLUMNS = [
    'timestamp', 'symbol', 'action', 'price', 'amount',
    'trade_value', 'atr', 'net_flow', 'tp_price', 'sl_price',
    'reason', 'realized_pnl', 'actual_balance', 'effective_balance'
]


# ==========================================
# 1. 核心輔助功能
# ==========================================
def log_to_csv(data_dict):
    row = {col: '' for col in CSV_COLUMNS}
    row.update(data_dict)
    row['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pd.DataFrame([row], columns=CSV_COLUMNS).to_csv(LOG_FILE, mode='a', index=False,
                                                    header=not os.path.exists(LOG_FILE))


def get_live_usdt_balance():
    try:
        return float(exchange.fetch_balance()['USDT']['free'])
    except:
        return 0.0


def cancel_all_conditional_orders(symbol):
    try:
        exchange.cancel_all_orders(symbol, params={'unfilledOrderType': 'ConditionalOrder'})
        exchange.cancel_all_orders(symbol)
        print(f"🧹 {symbol} 物理清場完成 (SL/TP + Limit)")
    except:
        pass


def get_market_metrics(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=50)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df['tr'] = np.maximum(df['h'] - df['l'],
                              np.maximum(abs(df['h'] - df['c'].shift(1)), abs(df['l'] - df['c'].shift(1))))
        atr = df['tr'].rolling(14).mean().iloc[-1]
        is_volatile = (atr / df['c'].iloc[-1]) > 0.0005
        return atr, is_volatile
    except:
        return None, False


def get_3_layer_avg_price(symbol, side='asks'):
    try:
        ob = exchange.fetch_order_book(symbol, limit=5)
        levels = ob[side][:3]
        return sum([level[0] for level in levels]) / len(levels)
    except:
        return None


# ==========================================
# 2. 導航與海選 (V5.8 鋼鐵邏輯)
# ==========================================
def get_btc_regime():
    try:
        ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=50)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        current_price = df['c'].iloc[-1]
        sma20 = df['c'].rolling(20).mean().iloc[-1]
        deviation = (current_price - sma20) / sma20
        # 門檻建議改為 0.0025 (即 0.25%) 來放寬
        if deviation > 0.0025:
            status, signal = "🟢 綠燈 (多頭)", 1
        elif deviation < -0.0025:
            status, signal = "🔴 紅燈 (空頭)", -1
        else:
            status, signal = "🟡 黃燈 (震盪)", 0
        print(f"📊 BTC 導航 | 現價: {current_price:.1f} | SMA20: {sma20:.1f} | 偏離: {deviation:.2%} | {status}")
        return signal
    except Exception as e:
        if "10006" in str(e): time.sleep(10)
        return 0


def scouting_top_coins(n=5):
    try:
        tickers = exchange.fetch_tickers()
        data = [{'symbol': s, 'volume': t['quoteVolume'], 'change': t['percentage']}
                for s, t in tickers.items() if
                s.endswith(':USDT') and s not in BLACKLIST and t['percentage'] is not None]
        df = pd.DataFrame(data)
        if df.empty: return []
        return df.sort_values('volume', ascending=False).head(50).sort_values('change', ascending=False).head(n)[
            'symbol'].tolist()
    except:
        return []


def apply_lee_ready_logic(symbol):
    try:
        ob = exchange.fetch_order_book(symbol)
        midpoint = (ob['bids'][0][0] + ob['asks'][0][0]) / 2
        trades = exchange.fetch_trades(symbol, limit=200)
        df = pd.DataFrame(trades, columns=['price', 'amount', 'timestamp'])
        df['dir'] = np.where(df['price'] > midpoint, 1, np.where(df['price'] < midpoint, -1, 0))
        df['tick'] = df['price'].diff().apply(np.sign).replace(0, np.nan).ffill().fillna(0)
        df['final'] = np.where(df['dir'] != 0, df['dir'], df['tick'])
        flow = df['final'] * df['amount']
        net_flow = flow.sum()
        is_strong = net_flow > (flow.std() * NET_FLOW_SIGMA)
        return net_flow, df['price'].iloc[-1], is_strong
    except:
        return 0, 0, False


# ==========================================
# 3. 交易管理 (三段式平倉 + ATR 追蹤)
# ==========================================
def manage_long_positions():
    for s in list(positions.keys()):
        try:
            pos_info = exchange.fetch_position(s)
            actual_size = float(pos_info.get('contracts', 0) or pos_info.get('size', 0))

            if actual_size == 0:  # 偵測原生平倉
                pos = positions[s]
                curr_p = exchange.fetch_ticker(s)['last']
                print(f"✅ {s} 偵測到物理平倉 (Bybit SL/TP)")
                log_to_csv({'symbol': s, 'action': 'LIVE_LONG_EXIT_NATIVE', 'price': curr_p, 'amount': pos['amount'],
                            'reason': 'Bybit Native TP/SL',
                            'realized_pnl': round((curr_p - pos['entry_price']) * pos['amount'], 4),
                            'actual_balance': round(get_live_usdt_balance(), 2)})
                cancel_all_conditional_orders(s)
                if s in positions: del positions[s]; continue

            curr_p, pos = exchange.fetch_ticker(s)['last'], positions[s]
            pnl_pct = (curr_p - pos['entry_price']) / pos['entry_price']
            sl_updated = False

            if not pos['is_breakeven'] and pnl_pct > 0.003:  # 保本
                pos['sl_price'], pos['is_breakeven'], sl_updated = pos['entry_price'] * 1.0002, True, True
            if pos['is_breakeven']:  # 追蹤止損
                trail_sl = curr_p - (TRAIL_ATR_MULT * pos['atr'])
                if trail_sl > pos['sl_price']: pos['sl_price'], sl_updated = trail_sl, True

            if sl_updated:
                f_sl = exchange.price_to_precision(s, pos['sl_price'])
                try:
                    exchange.private_post_v5_position_trading_stop(
                        {'category': 'linear', 'symbol': exchange.market_id(s), 'stopLoss': str(f_sl),
                         'tpslMode': 'Full', 'positionIdx': 0})
                    log_to_csv({'symbol': s, 'action': 'SYNC_SL', 'price': f_sl, 'reason': 'Trailing/BE'})
                except:
                    pass

            # 智能平倉觸發
            exit_reason = None
            if curr_p >= pos['tp_price']:
                exit_reason = "Take Profit (Smart Exit)"
            elif curr_p <= pos['sl_price'] and not pos['is_breakeven']:
                exit_reason = "Stop Loss (Smart Exit)"

            if exit_reason:
                print(f"⚔️ 執行三段式平倉: {s}")
                try:
                    m_p = get_3_layer_avg_price(s, 'bids') or curr_p
                    m_order = exchange.create_order(s, 'limit', 'sell', pos['amount'], m_p,
                                                    {'postOnly': True, 'reduceOnly': True})
                    time.sleep(10)
                    if exchange.fetch_order(m_order['id'], s)['status'] != 'closed':
                        exchange.cancel_order(m_order['id'], s)
                        ioc_p = get_3_layer_avg_price(s, 'bids') or curr_p
                        exchange.create_order(s, 'limit', 'sell', pos['amount'], ioc_p,
                                              {'timeInForce': 'IOC', 'reduceOnly': True})
                except:
                    exchange.create_market_sell_order(s, pos['amount'], {'reduceOnly': True})

                log_to_csv({'symbol': s, 'action': 'LIVE_LONG_EXIT_SMART', 'price': curr_p, 'amount': pos['amount'],
                            'reason': exit_reason,
                            'realized_pnl': round((curr_p - pos['entry_price']) * pos['amount'], 4),
                            'actual_balance': round(get_live_usdt_balance(), 2)})
                cancel_all_conditional_orders(s);
                del positions[s]
        except Exception as e:
            if "10006" in str(e): time.sleep(5)
            if "order not exists" in str(e).lower(): del positions[s]


# ==========================================
# 4. 入貨執行 (物理清場版)
# ==========================================
def execute_live_long(symbol, net_flow, current_price, is_strong, atr, is_volatile):
    if symbol in cooldown_tracker and time.time() < cooldown_tracker[symbol].get('cooldown_until', 0): return
    if is_strong and is_volatile and symbol not in positions:
        cancel_all_conditional_orders(symbol)  # 🚀 入貨前必清場
        actual_bal = get_live_usdt_balance()
        eff_bal = min(WORKING_CAPITAL, actual_bal)
        trade_val = min((eff_bal * RISK_PER_TRADE) / (atr / current_price), eff_bal * MAX_LEVERAGE * 0.95)
        amount = float(exchange.amount_to_precision(symbol, trade_val / current_price))
        if amount < exchange.markets[symbol]['limits']['amount']['min']: return
        ioc_p = get_3_layer_avg_price(symbol, 'asks') or current_price
        tp_p = float(exchange.price_to_precision(symbol, ioc_p + (TP_ATR_MULT * atr)))
        sl_p = float(exchange.price_to_precision(symbol, ioc_p - (SL_ATR_MULT * atr)))
        try:
            try:
                exchange.set_leverage(int(MAX_LEVERAGE), symbol)
            except:
                pass
            # 🚀 Bybit V5 參數字串化
            params = {'timeInForce': 'IOC', 'stopLoss': str(sl_p), 'takeProfit': str(tp_p), 'positionIdx': 0}
            order = exchange.create_order(symbol, 'limit', 'buy', amount, ioc_p, params)
            fill_p = order['average'] if order.get('average') else ioc_p
            positions[symbol] = {'amount': amount, 'entry_price': fill_p, 'tp_price': tp_p, 'sl_price': sl_p,
                                 'is_breakeven': False, 'atr': atr}
            log_to_csv({'symbol': symbol, 'action': 'LIVE_LONG_ENTRY', 'price': fill_p, 'amount': amount,
                        'trade_value': round(amount * fill_p, 2), 'atr': round(atr, 4), 'net_flow': round(net_flow, 2),
                        'tp_price': tp_p, 'sl_price': sl_p, 'actual_balance': round(actual_bal, 2),
                        'effective_balance': eff_bal})
            print(f"🚀 [已入貨] {symbol} | SL: {sl_p}")
        except Exception as e:
            print(f"❌ {symbol} 入貨失敗: {e}")


# ==========================================
# 5. 主程式 (呼吸延時 + 全局保護)
# ==========================================
def main():
    print(f"🚀 AI 做多實戰 V5.8 FINAL 啟動...")
    try:
        while True:
            try:
                manage_long_positions()
                time.sleep(0.5)
                regime = get_btc_regime()
                if regime == 1:
                    print("🟢 BTC 綠燈：執行海選...");
                    time.sleep(0.5)
                    target_coins = scouting_top_coins(5)
                    for s in target_coins:
                        print(f"🔍 分析 {s}...");
                        time.sleep(0.5)  # 😴 呼吸延時
                        flow, last_p, is_strong = apply_lee_ready_logic(s)
                        atr, is_volatile = get_market_metrics(s)
                        if last_p > 0: execute_live_long(s, flow, last_p, is_strong, atr, is_volatile)
                print(f"⏳ 巡邏完畢 | 持倉: {list(positions.keys())} | 餘額: {get_live_usdt_balance():.2f}")
            except Exception as e:
                if "10006" in str(e):
                    print("🚨 流量過載！全局休眠 30 秒...");
                    time.sleep(30)
                else:
                    logger.error(f"⚠️ 主迴圈錯誤: {e}")
            time.sleep(120)
    except KeyboardInterrupt:
        logger.warning('接收到鍵盤中斷信號；程式已終止')


if __name__ == "__main__":
    main()