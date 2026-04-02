import ccxt
import pandas as pd
import time
import numpy as np
import os
import logging
from datetime import datetime

# ==========================================
# 0. 系統與日誌設定
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('AlgoTrade_V5.9')

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

# 👑 策略參數 (九大修正點：頻率分離)
WORKING_CAPITAL, MAX_LEVERAGE, RISK_PER_TRADE = 1000.0, 10.0, 0.01
NET_FLOW_SIGMA, TP_ATR_MULT, SL_ATR_MULT, TRAIL_ATR_MULT = 1.5, 1.5, 1.0, 1.0
SCOUTING_INTERVAL = 120  # 海選掃描：120秒一次
POSITION_CHECK_INTERVAL = 10  # 持倉管理：10秒一次 (修正 9)

# 🚀 恢復至 21+ 完整保護模式，防止 Bot 誤入「死水區」
BLACKLIST = [
    # 1. 傳統穩定幣 (1.00 附近死火，完全無趨勢可言)
    'USDC/USDT:USDT', 'DAI/USDT:USDT', 'FDUSD/USDT:USDT', 'BUSD/USDT:USDT',
    'TUSD/USDT:USDT', 'PYUSD/USDT:USDT', 'USDP/USDT:USDT', 'EURS/USDT:USDT',

    # 2. 算法/合成穩定幣 (波動極小，不適合 Lee-Ready 邏輯)
    'USDE/USDT:USDT', 'USAT/USDT:USDT', 'USD0/USDT:USDT', 'USTC/USDT:USDT',
    'LUSD/USDT:USDT', 'FRAX/USDT:USDT', 'MIM/USDT:USDT', 'RLUSD/USDT:USDT',

    # 3. Wrapped/LST 資產 (價格與原生幣掛鉤，會造成重複持倉風險)
    'WBTC/USDT:USDT', 'WETH/USDT:USDT', 'WBNB/USDT:USDT', 'WAVAX/USDT:USDT',
    'stETH/USDT:USDT', 'cbETH/USDT:USDT', 'WHT/USDT:USDT'
]

CSV_COLUMNS = ['timestamp', 'symbol', 'action', 'price', 'amount', 'trade_value', 'atr', 'net_flow', 'tp_price',
               'sl_price', 'reason', 'realized_pnl', 'actual_balance', 'effective_balance']


# ==========================================
# 1. 核心輔助模組
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


def cancel_all_v5(symbol):
    try:
        exchange.cancel_all_orders(symbol, params={'orderFilter': 'Order'})
        exchange.cancel_all_orders(symbol, params={'orderFilter': 'StopOrder'})  # 修正 1
    except:
        pass


def get_market_metrics(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=50)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df['tr'] = np.maximum(df['h'] - df['l'],
                              np.maximum(abs(df['h'] - df['c'].shift(1)), abs(df['l'] - df['c'].shift(1))))
        # 🚀 修正 4：防止 ATR 返回 NaN
        atr = df['tr'].rolling(14, min_periods=1).mean().iloc[-1]
        if pd.isna(atr) or atr == 0: return None, False
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
# 2. 導航與海選 (修正 6 & 8)
# ==========================================
def get_btc_regime():
    try:
        ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=60)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        current_price = df['c'].iloc[-1]
        sma20 = df['c'].rolling(20).mean().iloc[-1]
        sma50 = df['c'].rolling(50).mean().iloc[-1]  # 🚀 修正 6：加入長線確認

        deviation = (current_price - sma20) / sma20
        # 🟢 進場條件：偏離度 > 0.25% 且 短線在長線之上
        if deviation > 0.0025 and sma20 > sma50:
            status, signal = "🟢 綠燈 (趨勢確認)", 1
        elif deviation < -0.0025:
            status, signal = "🔴 紅燈", -1
        else:
            status, signal = "🟡 黃燈", 0
        print(
            f"📊 BTC | Price: {current_price:.0f} | Dev: {deviation:.2%} | Trend: {'UP' if sma20 > sma50 else 'DW'} | {status}")
        return signal
    except:
        return 0


def scouting_top_coins(n=5):
    try:
        tickers = exchange.fetch_tickers()
        data = [{'symbol': s, 'volume': t['quoteVolume'], 'change': t['percentage']}
                for s, t in tickers.items() if
                s.endswith(':USDT') and s not in BLACKLIST and t['percentage'] is not None]
        df = pd.DataFrame(data)
        return df.sort_values('volume', ascending=False).head(20).sort_values('change', ascending=False).head(n)[
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
        # 🚀 修正 8：改用金額加權流向 (Price * Amount)
        df['weighted_flow'] = df['final'] * df['amount'] * df['price']
        net_flow = df['weighted_flow'].sum()
        is_strong = net_flow > (df['weighted_flow'].std() * NET_FLOW_SIGMA)
        return net_flow, df['price'].iloc[-1], is_strong
    except:
        return 0, 0, False


# ==========================================
# 3. 持倉管理 (修正 2 & 3)
# ==========================================
def manage_long_positions():
    try:
        # 🚀 修正 2：重新對齊交易所實際倉位，清理幽靈倉位
        live_positions_raw = exchange.fetch_positions()
        live_symbols = {p['symbol']: p for p in live_positions_raw if
                        float(p.get('contracts', 0) or p.get('size', 0)) > 0}

        for s in list(positions.keys()):
            if s not in live_symbols:
                print(f"🧹 清理幽靈倉位或已手動平倉之標的: {s}")
                del positions[s]
                continue

        for s in list(positions.keys()):
            curr_p, pos = exchange.fetch_ticker(s)['last'], positions[s]
            pnl_pct = (curr_p - pos['entry_price']) / pos['entry_price']
            sl_updated = False

            if not pos['is_breakeven'] and pnl_pct > 0.003:
                pos['sl_price'], pos['is_breakeven'], sl_updated = pos['entry_price'] * 1.0002, True, True
            if pos['is_breakeven']:
                trail_sl = curr_p - (TRAIL_ATR_MULT * pos['atr'])
                if trail_sl > pos['sl_price']: pos['sl_price'], sl_updated = trail_sl, True

            if sl_updated:
                f_sl = exchange.price_to_precision(s, pos['sl_price'])
                try:
                    exchange.private_post_v5_position_trading_stop(
                        {'category': 'linear', 'symbol': exchange.market_id(s), 'stopLoss': str(f_sl),
                         'tpslMode': 'Full', 'positionIdx': 0})
                except:
                    pass

            # 🚀 修正 3：簡化平倉邏輯，高波動下直接使用 IOC
            exit_reason = None
            if curr_p >= pos['tp_price']:
                exit_reason = "TP (IOC)"
            elif curr_p <= pos['sl_price'] and not pos['is_breakeven']:
                exit_reason = "SL (IOC)"

            if exit_reason:
                print(f"⚔️ 觸發 {exit_reason}，執行 IOC 平倉: {s}")
                try:
                    ioc_price = get_3_layer_avg_price(s, 'bids') or curr_p
                    exchange.create_order(s, 'limit', 'sell', pos['amount'], ioc_price,
                                          {'timeInForce': 'IOC', 'reduceOnly': True})
                except:
                    exchange.create_market_sell_order(s, pos['amount'], {'reduceOnly': True})

                log_to_csv(
                    {'symbol': s, 'action': 'EXIT', 'price': curr_p, 'amount': pos['amount'], 'reason': exit_reason,
                     'realized_pnl': round((curr_p - pos['entry_price']) * pos['amount'], 4)})
                cancel_all_v5(s);
                del positions[s]
    except Exception as e:
        if "10006" in str(e): time.sleep(5)


# ==========================================
# 4. 執行與主程序 (修正 5 & 7 & 9)
# ==========================================
def execute_live_long(symbol, net_flow, current_price, is_strong, atr, is_volatile):
    if symbol in cooldown_tracker and time.time() < cooldown_tracker[symbol]: return
    if is_strong and is_volatile and symbol not in positions:
        cancel_all_v5(symbol)
        actual_bal = get_live_usdt_balance()
        eff_bal = min(WORKING_CAPITAL, actual_bal)
        trade_val = min((eff_bal * RISK_PER_TRADE) / (atr / current_price), eff_bal * MAX_LEVERAGE * 0.95)
        amount = float(exchange.amount_to_precision(symbol, trade_val / current_price))
        if amount < exchange.markets[symbol]['limits']['amount']['min']: return

        # 🚀 修正 5：記錄槓桿設置錯誤
        try:
            exchange.set_leverage(int(MAX_LEVERAGE), symbol)
        except Exception as e:
            if "110043" not in str(e):
                logger.warning(f"⚠️ {symbol} 槓桿失敗: {e}")

        ioc_p = get_3_layer_avg_price(symbol, 'asks') or current_price
        tp_p, sl_p = ioc_p + (TP_ATR_MULT * atr), ioc_p - (SL_ATR_MULT * atr)

        try:
            params = {'timeInForce': 'IOC', 'stopLoss': str(exchange.price_to_precision(symbol, sl_p)),
                      'takeProfit': str(exchange.price_to_precision(symbol, tp_p)), 'positionIdx': 0}
            exchange.create_order(symbol, 'limit', 'buy', amount, ioc_p, params)
            positions[symbol] = {'amount': amount, 'entry_price': ioc_p, 'tp_price': tp_p, 'sl_price': sl_p,
                                 'is_breakeven': False, 'atr': atr}
            # 🚀 修正 7：設置冷卻時間
            cooldown_tracker[symbol] = time.time() + 3600
            log_to_csv({'symbol': symbol, 'action': 'ENTRY', 'price': ioc_p, 'amount': amount,
                        'trade_value': round(amount * ioc_p, 2), 'atr': round(atr, 4), 'net_flow': round(net_flow, 2),
                        'tp_price': tp_p, 'sl_price': sl_p, 'actual_balance': round(actual_bal, 2),
                        'effective_balance': eff_bal})
            print(f"🚀 [已入貨] {symbol}")
        except Exception as e:
            print(f"❌ {symbol} 失敗: {e}")


# ==========================================
# 5. 主程序 (🚀 嚴格鎖死縮進版)
# ==========================================
def main():
    print(f"🚀 AI 實戰 V5.9.1 (靜音+嚴格鎖死版) 啟動...")
    last_scout_time = 0
    while True:
        try:
            manage_long_positions()
            curr_t = time.time()
            if curr_t - last_scout_time > SCOUTING_INTERVAL:
                regime = get_btc_regime()

                # 🚀 修正：確保海選邏輯「完全被包裹」在綠燈判斷入面
                if regime == 1:
                    print("🟢 綠燈：執行海選掃描...")
                    tickers = exchange.fetch_tickers()
                    data = [{'symbol': s, 'volume': t['quoteVolume'], 'change': t['percentage']} for s, t in
                            tickers.items() if s.endswith(':USDT') and s not in BLACKLIST]
                    target_coins = \
                    pd.DataFrame(data).sort_values('volume', ascending=False).head(20).sort_values('change',
                                                                                                   ascending=False).head(
                        5)['symbol'].tolist()
                    for s in target_coins:
                        flow, last_p, is_strong = apply_lee_ready_logic(s)
                        atr, is_v = get_market_metrics(s)
                        if last_p > 0: execute_live_long(s, flow, last_p, is_strong, atr, is_v)
                        time.sleep(0.5)
                else:
                    # 紅燈或黃燈時，只巡邏持倉，唔會做海選
                    print(f"🚦 目前導航狀態為 {regime}，海選暫停。")

                last_scout_time = curr_t
                print(f"⏳ 巡邏完畢 | 持倉: {list(positions.keys())}")

            time.sleep(POSITION_CHECK_INTERVAL)
        except Exception as e:
            if "10006" in str(e):
                time.sleep(30)
            else:
                time.sleep(10)


if __name__ == "__main__":
    main()