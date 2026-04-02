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
logger = logging.getLogger('AlgoTrade_V6.0_Final')

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

# 👑 策略參數
WORKING_CAPITAL, MAX_LEVERAGE, RISK_PER_TRADE = 1000.0, 10.0, 0.01
NET_FLOW_SIGMA, TP_ATR_MULT, SL_ATR_MULT, TRAIL_ATR_MULT = 1.5, 1.5, 1.0, 1.0
SCOUTING_INTERVAL = 120
POSITION_CHECK_INTERVAL = 10
MIN_NOTIONAL = 5.0  # 🔥 新增: Bybit 最小訂單金額

BLACKLIST = [
    'USDC/USDT:USDT', 'DAI/USDT:USDT', 'FDUSD/USDT:USDT', 'BUSD/USDT:USDT',
    'TUSD/USDT:USDT', 'PYUSD/USDT:USDT', 'USDP/USDT:USDT', 'EURS/USDT:USDT',
    'USDE/USDT:USDT', 'USAT/USDT:USDT', 'USD0/USDT:USDT', 'USTC/USDT:USDT',
    'LUSD/USDT:USDT', 'FRAX/USDT:USDT', 'MIM/USDT:USDT', 'RLUSD/USDT:USDT',
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
        exchange.cancel_all_orders(symbol, params={'orderFilter': 'StopOrder'})
    except:
        pass


def get_market_metrics(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=50)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df['tr'] = np.maximum(df['h'] - df['l'],
                              np.maximum(abs(df['h'] - df['c'].shift(1)), abs(df['l'] - df['c'].shift(1))))
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
# 2. 導航與海選
# ==========================================
def get_btc_regime():
    try:
        ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=60)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        current_price = df['c'].iloc[-1]
        sma20 = df['c'].rolling(20).mean().iloc[-1]
        sma50 = df['c'].rolling(50).mean().iloc[-1]

        deviation = (current_price - sma20) / sma20
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
        df['weighted_flow'] = df['final'] * df['amount'] * df['price']
        net_flow = df['weighted_flow'].sum()
        is_strong = net_flow > (df['weighted_flow'].std() * NET_FLOW_SIGMA)
        return net_flow, df['price'].iloc[-1], is_strong
    except:
        return 0, 0, False


# ==========================================
# 3. 持倉管理
# ==========================================
def manage_long_positions():
    try:
        live_positions_raw = exchange.fetch_positions()
        live_symbols = {p['symbol']: p for p in live_positions_raw if
                        float(p.get('contracts', 0) or p.get('size', 0)) > 0}

        for s in list(positions.keys()):
            if s not in live_symbols:
                print(f"🧹 清理幽靈倉位或已手動平倉之標的: {s}")
                # 🔥 修改點 2: 平倉時清除冷卻時間
                if s in cooldown_tracker:
                    del cooldown_tracker[s]
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
                cancel_all_v5(s)
                # 🔥 修改點 3: 平倉時清除冷卻時間
                if s in cooldown_tracker:
                    del cooldown_tracker[s]
                del positions[s]
    except Exception as e:
        if "10006" in str(e):
            time.sleep(5)


# ==========================================
# 4. 執行入場（核心修復區域）
# ==========================================
def execute_live_long(symbol, net_flow, current_price, is_strong, atr, is_volatile):
    # 冷卻檢查
    if symbol in cooldown_tracker and time.time() < cooldown_tracker[symbol]:
        return

    # 信號檢查
    if not (is_strong and is_volatile and symbol not in positions):
        return

    cancel_all_v5(symbol)

    # 資金計算
    actual_bal = get_live_usdt_balance()
    eff_bal = min(WORKING_CAPITAL, actual_bal)
    trade_val = min((eff_bal * RISK_PER_TRADE) / (atr / current_price), eff_bal * MAX_LEVERAGE * 0.95)
    amount = float(exchange.amount_to_precision(symbol, trade_val / current_price))

    # 🔥 修改點 4: 檢查數量和金額下限
    if amount < exchange.markets[symbol]['limits']['amount']['min']:
        return

    ioc_p = get_3_layer_avg_price(symbol, 'asks') or current_price

    # 🔥 修改點 5: 檢查最小名義價值
    if amount * ioc_p < MIN_NOTIONAL:
        logger.warning(f"⚠️ {symbol} 訂單金額 {amount * ioc_p:.2f} 低於最小值 {MIN_NOTIONAL}")
        return

    # 🔥 修改點 6: 嚴格的槓桿錯誤處理
    try:
        exchange.set_leverage(int(MAX_LEVERAGE), symbol)
    except Exception as e:
        if "110043" in str(e):
            pass  # 槓桿已設置，忽略
        elif "110026" in str(e):
            logger.error(f"❌ {symbol} 不支持 {MAX_LEVERAGE}x 槓桿，跳過入場")
            return
        else:
            logger.warning(f"⚠️ {symbol} 槓桿設置異常: {e}")

    try:
        # 🔥 修改點 7: 第一步 - 只開倉，不帶止盈止損
        order = exchange.create_order(symbol, 'limit', 'buy', amount, ioc_p,
                                      {'timeInForce': 'IOC', 'positionIdx': 0})

        time.sleep(1)

        # 🔥 修改點 8: 第二步 - 獲取實際成交價和數量
        order_detail = exchange.fetch_order(order['id'], symbol)
        actual_price = float(order_detail.get('average') or order_detail.get('price') or ioc_p)
        actual_amount = float(order_detail.get('filled', 0))

        # 🔥 修改點 9: 檢查是否成交
        if actual_amount == 0:
            logger.warning(f"❌ {symbol} IOC 訂單未成交，放棄入場")
            return

        # 🔥 修改點 10: 基於實際成交價計算止盈止損
        tp_p = float(exchange.price_to_precision(symbol, actual_price + (TP_ATR_MULT * atr)))
        sl_p = float(exchange.price_to_precision(symbol, actual_price - (SL_ATR_MULT * atr)))

        # 🔥 修改點 11: 第三步 - 設置止盈止損
        try:
            exchange.private_post_v5_position_trading_stop({
                'category': 'linear',
                'symbol': exchange.market_id(symbol),
                'stopLoss': str(sl_p),
                'takeProfit': str(tp_p),
                'tpslMode': 'Full',
                'positionIdx': 0
            })
            print(f"✅ {symbol} 止盈止損已設置 | TP: {tp_p} | SL: {sl_p}")
        except Exception as e:
            logger.error(f"❌ {symbol} 止盈止損設置失敗: {e}")
            # 即使止盈止損失敗，仍記錄倉位（後續可手動管理）

        # 🔥 修改點 12: 記錄倉位（使用實際成交價和數量）
        positions[symbol] = {
            'amount': actual_amount,
            'entry_price': actual_price,
            'tp_price': tp_p,
            'sl_price': sl_p,
            'is_breakeven': False,
            'atr': atr
        }

        cooldown_tracker[symbol] = time.time() + 3600

        log_to_csv({
            'symbol': symbol,
            'action': 'ENTRY',
            'price': actual_price,  # 🔥 使用實際價格
            'amount': actual_amount,  # 🔥 使用實際數量
            'trade_value': round(actual_amount * actual_price, 2),
            'atr': round(atr, 4),
            'net_flow': round(net_flow, 2),
            'tp_price': tp_p,
            'sl_price': sl_p,
            'actual_balance': round(actual_bal, 2),
            'effective_balance': eff_bal
        })

        print(f"🚀 [已入貨] {symbol} @ {actual_price:.4f} | 數量: {actual_amount}")

    except Exception as e:
        logger.error(f"❌ {symbol} 入場失敗: {e}")


# ==========================================
# 5. 主程序
# ==========================================
def main():
    print(f"🚀 AI 實戰 V6.0 FINAL (完全修復版) 啟動...")
    last_scout_time = 0

    while True:
        try:
            manage_long_positions()
            curr_t = time.time()

            if curr_t - last_scout_time > SCOUTING_INTERVAL:
                regime = get_btc_regime()

                if regime == 1:
                    print("🟢 綠燈：執行海選掃描...")
                    # 🔥 修改點 13: 使用現有函數避免重複代碼
                    target_coins = scouting_top_coins(5)

                    for s in target_coins:
                        # 🔥 修改點 14: 加入異常捕獲，避免單一幣種錯誤中斷整個循環
                        try:
                            flow, last_p, is_strong = apply_lee_ready_logic(s)
                            atr, is_v = get_market_metrics(s)
                            if last_p > 0:
                                execute_live_long(s, flow, last_p, is_strong, atr, is_v)
                        except Exception as e:
                            logger.warning(f"⚠️ {s} 分析失敗: {e}")
                            continue
                        time.sleep(0.5)
                else:
                    print(f"🚦 目前導航狀態為 {regime}，海選暫停。")

                last_scout_time = curr_t
                print(f"⏳ 巡邏完畢 | 持倉: {list(positions.keys())} | 餘額: {get_live_usdt_balance():.2f}")

            time.sleep(POSITION_CHECK_INTERVAL)

        except Exception as e:
            if "10006" in str(e):
                logger.warning("🚨 流量過載，休眠 30 秒...")
                time.sleep(30)
            else:
                logger.error(f"⚠️ 主循環錯誤: {e}")
                time.sleep(10)


if __name__ == "__main__":
    main()