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

# 🚫 終極黑名單 (穩定幣 & 包裝資產)
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
    df = pd.DataFrame([row], columns=CSV_COLUMNS)
    df.to_csv(LOG_FILE, mode='a', index=False, header=not os.path.exists(LOG_FILE))


def get_live_usdt_balance():
    try:
        balance = exchange.fetch_balance()
        return float(balance['USDT']['free'])
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
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=20)
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
# 2. 導航與海選邏輯 (核心更新)
# ==========================================
def get_btc_regime():
    """
    BTC 大盤導航 (1小時線 SMA 20 濾網)
    加強版：具備 API 流量保護與數據輸出
    """
    try:
        # 1. 獲取 BTC 1小時線數據
        ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=30)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])

        current_price = df['c'].iloc[-1]
        sma20 = df['c'].rolling(20).mean().iloc[-1]

        # 2. 計算偏離度 (Deviation)
        deviation = (current_price - sma20) / sma20

        # 🟢 根據偏離度決定燈號文字
        if deviation > 0.005:
            status_text = "🟢 綠燈 (多頭強勢)"
            signal = 1
        elif deviation < -0.005:
            status_text = "🔴 紅燈 (空頭趨勢)"
            signal = -1
        else:
            status_text = "🟡 黃燈 (震盪觀望)"
            signal = 0

        print(f"📊 BTC 導航 | Close: {current_price:.2f} | SMA20: {sma20:.2f} | 偏離: {deviation:.2%} | {status_text}")
        time.sleep(0.5)

        return signal

    except Exception as e:
        # 🚨 針對 10006 Rate Limit 進行特殊處理
        error_msg = str(e)
        if "10006" in error_msg or "Rate Limit" in error_msg:
            print("🛑 Bybit API 流量過載！get_btc_regime 強制進入休眠 10 秒...")
            time.sleep(10)
            return 0

        logger.error(f"⚠️ BTC 導引獲取失敗: {e}")
        return 0


def scouting_top_coins(n=5):
    """【海選功能】過濾黑名單並按成交量與漲幅排序"""
    try:
        tickers = exchange.fetch_tickers()
        data = []
        for s, t in tickers.items():
            if s.endswith(':USDT') and s not in BLACKLIST and t['percentage'] is not None:
                data.append({'symbol': s, 'volume': t['quoteVolume'], 'change': t['percentage']})

        df = pd.DataFrame(data)
        if df.empty: return []

        # 排序邏輯：成交量 Top 50 中找漲幅 Top n
        top_coins = df.sort_values('volume', ascending=False).head(50) \
            .sort_values('change', ascending=False).head(n)['symbol'].tolist()
        return top_coins
    except Exception as e:
        logger.error(f"⚠️ 海選偵測錯誤: {e}")
        return []


def apply_lee_ready_logic(symbol):
    """Lee-Ready 資金流判定"""
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

            params = {'timeInForce': 'IOC', 'stopLoss': str(sl_p), 'takeProfit': str(tp_p), 'positionIdx': 0}
            order = exchange.create_order(symbol, 'limit', 'buy', amount, ioc_p, params)
            fill_p = order['average'] if order.get('average') else ioc_p

            positions[symbol] = {'amount': amount, 'entry_price': fill_p, 'tp_price': tp_p, 'sl_price': sl_p,
                                 'is_breakeven': False, 'atr': atr}

            log_to_csv({
                'symbol': symbol, 'action': 'LIVE_LONG_ENTRY', 'price': fill_p, 'amount': amount,
                'trade_value': round(amount * fill_p, 2), 'atr': round(atr, 4), 'net_flow': round(net_flow, 2),
                'tp_price': tp_p, 'sl_price': sl_p, 'actual_balance': round(actual_bal, 2), 'effective_balance': eff_bal
            })
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
                pnl = (curr_p - pos['entry_price']) * pos['amount']
                print(f"✅ {s} 偵測到原生平倉，清場紀錄中...")
                log_to_csv({
                    'symbol': s, 'action': 'LIVE_LONG_EXIT_NATIVE', 'price': curr_p, 'amount': pos['amount'],
                    'reason': 'Bybit Native TP/SL', 'realized_pnl': round(pnl, 4),
                    'actual_balance': round(get_live_usdt_balance(), 2)
                })
                cancel_all_conditional_orders(s)
                if s in positions: del positions[s]
                continue

            curr_p, pos = exchange.fetch_ticker(s)['last'], positions[s]
            pnl_pct = (curr_p - pos['entry_price']) / pos['entry_price']
            sl_up = False

            if not pos['is_breakeven'] and pnl_pct > 0.003:
                pos['sl_price'], pos['is_breakeven'], sl_up = pos['entry_price'] * 1.0002, True, True

            if pos['is_breakeven']:
                trail_sl = curr_p - (TRAIL_ATR_MULT * pos['atr'])
                if trail_sl > pos['sl_price']:
                    pos['sl_price'], sl_up = trail_sl, True

            if sl_up:
                formatted_sl = exchange.price_to_precision(s, pos['sl_price'])
                try:
                    exchange.private_post_v5_position_trading_stop({
                        'category': 'linear', 'symbol': exchange.market_id(s),
                        'stopLoss': str(formatted_sl), 'tpslMode': 'Full', 'positionIdx': 0
                    })
                    print(f"✅ {s} SL 同步成功: {formatted_sl}")
                    log_to_csv({'symbol': s, 'action': 'SYNC_SL', 'price': formatted_sl, 'reason': 'Trailing/BE'})
                except Exception as api_e:
                    print(f"❌ {s} SL 同步失敗: {api_e}")

        except Exception as e:
            if "order not exists" in str(e).lower():
                if s in positions: del positions[s]


# ==========================================
# 4. 主程式 (BTC 導航整合)
# ==========================================
def main():
    print(f"🚀 AI 做多實戰 V5.6 啟動... (BTC 導航 + 完整海選)")
    try:
        while True:
            try:
                # 1. 持倉管理 (隨時運行)
                manage_long_positions()

                # 2. 大盤檢查
                regime = get_btc_regime()

                if regime == 1:
                    target_coins = scouting_top_coins(5)
                    for s in target_coins:
                        flow, last_p, is_strong = apply_lee_ready_logic(s)
                        atr, is_volatile = get_market_metrics(s)
                        if last_p > 0:
                            execute_live_long(s, flow, last_p, is_strong, atr, is_volatile)

                print(f"⏳ 監控中... 持倉: {list(positions.keys())} | 餘額: {get_live_usdt_balance():.2f}")
            except Exception as e:
                logger.error(f"⚠️ 主迴圈錯誤: {e}")
            time.sleep(120)
    except KeyboardInterrupt:
        print("\n" + "=" * 45)
        logger.warning('接收到鍵盤中斷信號；程式已終止')
        print("=" * 45)


if __name__ == "__main__":
    main()