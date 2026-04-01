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

LOG_DIR = "result_long_live"
LOG_FILE = f"{LOG_DIR}/09_live_long_log.csv"
if not os.path.exists(LOG_DIR): os.makedirs(LOG_DIR)

positions, cooldown_tracker = {}, {}

# 👑 策略核心參數
WORKING_CAPITAL, MAX_LEVERAGE, RISK_PER_TRADE = 1000.0, 10.0, 0.01
NET_FLOW_SIGMA, TP_ATR_MULT, SL_ATR_MULT, TRAIL_ATR_MULT = 1.5, 1.5, 1.0, 1.0

# 🚫 終極黑名單
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
# 2. 鋼鐵導航與海選邏輯 (V5.8 重點強化)
# ==========================================
def get_btc_regime():
    """
    BTC 大盤導航 (1小時線 SMA 20 濾網)
    加強版：具備數據防錯與 API 流量保護
    """
    try:
        # 增加 limit 到 50，確保計算穩定
        ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=50)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])

        if len(df) < 20: return 0  # 數據不足回傳黃燈

        current_price = df['c'].iloc[-1]
        sma20 = df['c'].rolling(20).mean().iloc[-1]

        if np.isnan(sma20): return 0

        deviation = (current_price - sma20) / sma20

        if deviation > 0.005:
            status, signal = "🟢 綠燈 (多頭)", 1
        elif deviation < -0.005:
            status, signal = "🔴 紅燈 (空頭)", -1
        else:
            status, signal = "🟡 黃燈 (震盪)", 0

        # Dashboard 輸出
        print(f"📊 BTC 導航 | 現價: {current_price:.1f} | SMA20: {sma20:.1f} | 偏離: {deviation:.2%} | {status}")

        return signal

    except Exception as e:
        if "10006" in str(e):
            print("🛑 Bybit 流量管制中 (get_btc_regime)，強制休眠 10 秒...")
            time.sleep(10)
        elif "TLS" in str(e) or "certificate" in str(e):
            logger.error("🚨 SSL 證書錯誤！請檢查環境變數 SSL_CERT_FILE 設定。")
        else:
            logger.error(f"⚠️ BTC 導航出錯: {e}")
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
# 3. 交易執行與管理
# ==========================================
def execute_live_long(symbol, net_flow, current_price, is_strong, atr, is_volatile):
    if symbol in cooldown_tracker and time.time() < cooldown_tracker[symbol].get('cooldown_until', 0): return

    if is_strong and is_volatile and symbol not in positions:
        cancel_all_conditional_orders(symbol)
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
            order = exchange.create_order(symbol, 'limit', 'buy', amount, ioc_p,
                                          {'timeInForce': 'IOC', 'stopLoss': str(sl_p), 'takeProfit': str(tp_p),
                                           'positionIdx': 0})
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


def manage_long_positions():
    for s in list(positions.keys()):
        try:
            pos_info = exchange.fetch_position(s)
            actual_size = float(pos_info.get('contracts', 0) or pos_info.get('size', 0))
            if actual_size == 0:
                pos = positions[s]
                curr_p = exchange.fetch_ticker(s)['last']
                log_to_csv({'symbol': s, 'action': 'LIVE_LONG_EXIT_NATIVE', 'price': curr_p, 'amount': pos['amount'],
                            'reason': 'Bybit Native TP/SL',
                            'realized_pnl': round((curr_p - pos['entry_price']) * pos['amount'], 4),
                            'actual_balance': round(get_live_usdt_balance(), 2)})
                cancel_all_conditional_orders(s)
                del positions[s];
                continue

            curr_p, pos = exchange.fetch_ticker(s)['last'], positions[s]
            pnl_pct = (curr_p - pos['entry_price']) / pos['entry_price']
            sl_up = False
            if not pos['is_breakeven'] and pnl_pct > 0.003: pos['sl_price'], pos['is_breakeven'], sl_up = pos[
                                                                                                              'entry_price'] * 1.0002, True, True
            if pos['is_breakeven']:
                trail_sl = curr_p - (TRAIL_ATR_MULT * pos['atr'])
                if trail_sl > pos['sl_price']: pos['sl_price'], sl_up = trail_sl, True
            if sl_up:
                formatted_sl = exchange.price_to_precision(s, pos['sl_price'])
                exchange.private_post_v5_position_trading_stop(
                    {'category': 'linear', 'symbol': exchange.market_id(s), 'stopLoss': str(formatted_sl),
                     'tpslMode': 'Full', 'positionIdx': 0})
                log_to_csv({'symbol': s, 'action': 'SYNC_SL', 'price': formatted_sl, 'reason': 'Trailing/BE'})
        except Exception as e:
            if "10006" in str(e): time.sleep(5)
            if "order not exists" in str(e).lower(): del positions[s]


# ==========================================
# 4. 主程式 (含防擁塞延時)
# ==========================================
def main():
    print(f"🚀 AI 做多實戰 V5.8 啟動... (鋼鐵防禦版)")
    try:
        while True:
            try:
                manage_long_positions()
                time.sleep(0.5)

                regime = get_btc_regime()

                if regime == 1:
                    print("🟢 BTC 綠燈：大盤強勢，執行海選...")
                    target_coins = scouting_top_coins(5)
                    for s in target_coins:
                        print(f"🔍 分析 {s}...");
                        time.sleep(0.5)
                        flow, last_p, is_strong = apply_lee_ready_logic(s)
                        atr, is_volatile = get_market_metrics(s)
                        if last_p > 0: execute_live_long(s, flow, last_p, is_strong, atr, is_volatile)
                elif regime == -1:
                    print("🔴 BTC 紅燈：趨勢轉弱，不開新倉。")
                else:
                    print("🟡 BTC 黃燈：震盪行情，不加新單。")

                print(f"⏳ 巡邏完畢 | 持倉: {list(positions.keys())} | 餘額: {get_live_usdt_balance():.2f}")
            except Exception as e:
                if "10006" in str(e):
                    print("🛑 全局流量保護，休眠 30 秒..."); time.sleep(30)
                else:
                    logger.error(f"⚠️ 主迴圈錯誤: {e}")
            time.sleep(120)
    except KeyboardInterrupt:
        logger.warning('接收到鍵盤中斷信號；程式已終止')


if __name__ == "__main__":
    main()