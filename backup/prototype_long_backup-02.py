import ccxt
import pandas as pd
import time
import numpy as np
import os
import logging
import sys
from datetime import datetime

# ==========================================
# 0. 系統與日誌設定
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('AlgoTrade_V6.0_Final')

API_KEY = ""
API_SECRET = ""

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

# 👑 策略參數
WORKING_CAPITAL, MAX_LEVERAGE, RISK_PER_TRADE = 1000.0, 10.0, 0.01
NET_FLOW_SIGMA, TP_ATR_MULT, SL_ATR_MULT, TRAIL_ATR_MULT = 1.5, 1.5, 1.0, 1.0
SCOUTING_INTERVAL = 120
POSITION_CHECK_INTERVAL = 10
MIN_NOTIONAL = 5.0
# ✅ 新增代碼
MIN_IMBALANCE_RATIO = 0.2  # 要求的失衡比例 (可微調)

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


# ✅ 新增代碼：獨立處理交易所自動平倉的 PnL 結算與 CSV 紀錄
def process_native_exit_log(symbol, pos, position_type='long'):
    """🚨 新增：獨立處理交易所自動平倉的 PnL 結算與 CSV 紀錄"""
    real_exit_price = pos['entry_price']
    real_pnl = 0.0

    try:
        # 🎯 嘗試獲取 Bybit 官方最精準結算單 (含手續費)
        pnl_res = exchange.private_get_v5_position_closed_pnl({
            'category': 'linear',
            'symbol': exchange.market_id(symbol),
            'limit': 1
        })

        if pnl_res and pnl_res.get('result') and pnl_res['result'].get('list'):
            last_trade = pnl_res['result']['list'][0]
            real_exit_price = float(last_trade['avgExitPrice'])
            real_pnl = float(last_trade['closedPnl'])
        else:
            raise ValueError("Bybit PnL list is empty")

    except Exception as e:
        logger.debug(f"⚠️ {symbol} 獲取真實 PnL 失敗，使用備用估算: {e}")
        try:
            curr_p = exchange.fetch_ticker(symbol)['last']
            real_exit_price = curr_p
            # ✅ Long PnL: (現價 - 入場價) * 數量
            real_pnl = round((curr_p - pos['entry_price']) * pos['amount'], 4)
        except:
            pass

    log_to_csv({
        'symbol': symbol,
        'action': 'NATIVE_EXIT',
        'price': real_exit_price,
        'amount': pos['amount'],
        'reason': 'Bybit Native TP/SL',
        'realized_pnl': real_pnl
    })

    return real_pnl


def get_live_usdt_balance():
    try:
        return float(exchange.fetch_balance()['USDT']['free'])
    except:
        return 0.0


def cancel_all_v5(symbol):
    """核彈級撤單：清理所有掛單與倉位綁定的 TP/SL"""
    try:
        exchange.cancel_all_orders(symbol, params={'category': 'linear'})
        exchange.cancel_all_orders(symbol, params={'category': 'linear', 'orderFilter': 'StopOrder'})
        exchange.cancel_all_orders(symbol, params={'category': 'linear', 'orderFilter': 'tpslOrder'})
    except:
        pass
    try:
        exchange.private_post_v5_position_trading_stop({
            'category': 'linear',
            'symbol': exchange.market_id(symbol),
            'takeProfit': "0",
            'stopLoss': "0",
            'positionIdx': 0
        })
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
        curr_p = df['c'].iloc[-1]
        sma20 = df['c'].rolling(20).mean().iloc[-1]
        sma50 = df['c'].rolling(50).mean().iloc[-1]

        target_long = sma20 * (1 + 0.0025)

        cond_price = curr_p > target_long
        cond_trend = sma20 > sma50

        tick_p = "✅" if cond_price else "❌"
        tick_t = "✅" if cond_trend else "❌"

        if cond_price and cond_trend:
            status, signal = "🟢 GREEN (Bullish - All in)", 1
        elif cond_price or cond_trend:
            status, signal = "🟡 YELLOW (Conditions unmet - Standby)", 0
        else:
            status, signal = "🔴 RED (Bearish - Do not enter)", -1

        print("-" * 60)
        print(f"📈 BTC Live Status (Long) | Price: {curr_p:.0f}")
        print(f"1️⃣ Price Threshold: {curr_p:.0f} > {target_long:.0f} {tick_p}")
        print(f"2️⃣ Trend Confirmation: SMA20({sma20:.0f}) > SMA50({sma50:.0f}) {tick_t}")
        print(f"🚦 Final Decision: {status}")
        print("-" * 60)

        return signal
    except Exception as e:
        print(f"⚠️ Navigation Fault: {e}")
        return 0


def scouting_top_coins(n=5):
    try:
        tickers = exchange.fetch_tickers()
        # ❌ 舊代碼
        # data = [{'symbol': s, 'volume': t['quoteVolume'], 'change': t['percentage']}
        #         for s, t in tickers.items() if
        #         s.endswith(':USDT') and s not in BLACKLIST and t['percentage'] is not None]

        # ✅ 新增代碼：加入 Spread 過濾
        data = []
        for s, t in tickers.items():
            if s.endswith(':USDT') and s not in BLACKLIST and t['percentage'] is not None:
                ask = t.get('ask')
                bid = t.get('bid')
                if ask and bid and bid > 0:
                    spread = (ask - bid) / bid
                    if spread < 0.0015:
                        data.append({'symbol': s, 'volume': t['quoteVolume'], 'change': t['percentage']})

        df = pd.DataFrame(data)
        return df.sort_values('volume', ascending=False).head(20).sort_values('change', ascending=False).head(n)[
            'symbol'].tolist()
    except:
        return []


def apply_lee_ready_logic(symbol):
    # ❌ 舊代碼
    # try:
    #     ob = exchange.fetch_order_book(symbol)
    #     midpoint = (ob['bids'][0][0] + ob['asks'][0][0]) / 2
    #     trades = exchange.fetch_trades(symbol, limit=200)
    #     df = pd.DataFrame(trades, columns=['price', 'amount', 'timestamp'])
    #     df['dir'] = np.where(df['price'] > midpoint, 1, np.where(df['price'] < midpoint, -1, 0))
    #     df['tick'] = df['price'].diff().apply(np.sign).replace(0, np.nan).ffill().fillna(0)
    #     df['final'] = np.where(df['dir'] != 0, df['dir'], df['tick'])
    #     df['weighted_flow'] = df['final'] * df['amount'] * df['price']
    #     net_flow = df['weighted_flow'].sum()
    #     is_strong = net_flow > (df['weighted_flow'].std() * NET_FLOW_SIGMA)
    #     return net_flow, df['price'].iloc[-1], is_strong
    # except:
    #     return 0, 0, False

    # ✅ 新增代碼：Lee-Ready 資金流邏輯 + 訂單簿失衡度 (Imbalance) + P95濾網 [終極做多版]
    try:
        ob = exchange.fetch_order_book(symbol, limit=20)
        midpoint = (ob['bids'][0][0] + ob['asks'][0][0]) / 2

        bid_vol = sum([b[1] for b in ob['bids']])
        ask_vol = sum([a[1] for a in ob['asks']])
        imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol) if (bid_vol + ask_vol) > 0 else 0
        imbalance = max(-1, min(1, imbalance))

        trades = exchange.fetch_trades(symbol, limit=200)
        df = pd.DataFrame(trades, columns=['price', 'amount', 'timestamp'])
        df['dir'] = np.where(df['price'] > midpoint, 1, np.where(df['price'] < midpoint, -1, 0))
        df['tick'] = df['price'].diff().apply(np.sign).replace(0, np.nan).ffill().fillna(0)
        df['final'] = np.where(df['dir'] != 0, df['dir'], df['tick'])

        df['usd_val'] = df['amount'] * df['price']
        p95 = df['usd_val'].quantile(0.95)
        df['usd_val_clipped'] = df['usd_val'].clip(upper=p95)

        df['weighted_flow'] = df['final'] * df['usd_val_clipped']
        net_flow = df['weighted_flow'].sum()
        flow_std = df['weighted_flow'].std()

        if pd.isna(flow_std) or flow_std <= 0:
            z_score = 0
        else:
            z_score = net_flow / flow_std
            z_score = np.clip(z_score, -10, 10)

        is_strong = (z_score > NET_FLOW_SIGMA) and (imbalance > MIN_IMBALANCE_RATIO)

        if is_strong:
            print(
                f"📈 {symbol} Long Validated | Z-Score: {z_score:.2f} | Imbalance: {imbalance:.2f} | P95 Cap: {p95:.0f}")
        elif z_score > NET_FLOW_SIGMA:
            print(
                f"⚠️ {symbol} Fake-Pump Prevented | Z-Score: {z_score:.2f} but Imbalance is {imbalance:.2f} (Sell Wall in the way)")

        return net_flow, df['price'].iloc[-1], is_strong
    except Exception as e:
        return 0, 0, False


# ==========================================
# 3. 持倉管理
# ==========================================
def manage_long_positions():
    try:
        live_positions_raw = exchange.fetch_positions()
        live_symbols = {p['symbol']: p for p in live_positions_raw if
                        float(p.get('contracts', 0) or p.get('size', 0)) > 0}

        # ❌ 舊代碼
        # for s in list(positions.keys()):
        #     if s not in live_symbols:
        #         print(f"🧹 交易所已自動平倉 (TP/SL)，清理本地紀錄: {s}")
        #
        #         # 📝 補漏：為「交易所原生平倉」補寫 CSV 紀錄
        #         try:
        #             curr_p = exchange.fetch_ticker(s)['last']
        #             pos = positions[s]
        #             # 做空 PnL 估算
        #             est_pnl = round((curr_p - pos['entry_price']) * pos['amount'], 4)
        #             log_to_csv({
        #                 'symbol': s,
        #                 'action': 'NATIVE_EXIT',
        #                 'price': curr_p,
        #                 'amount': pos['amount'],
        #                 'reason': 'Bybit Native TP/SL',
        #                 'realized_pnl': est_pnl
        #             })
        #         except Exception as e:
        #             pass  # 即使攞價錢失敗，都照樣繼續清理
        #
        #         cancel_all_v5(s)
        #         # 🚨 修復：絕對不刪除冷卻時間，必須硬性坐滿 1 小時！
        #         del positions[s]
        #         continue

        # ✅ 新增代碼：處理真實 PnL 結算單及非對稱冷卻
        for s in list(positions.keys()):
            if s not in live_symbols:
                print(f"🧹 交易所已自動平倉，處理真實 PnL 結算單: {s}")

                # 攞返賺蝕結果
                real_pnl = process_native_exit_log(s, positions[s], position_type='long')

                cancel_all_v5(s)

                # 🚀 新增非對稱冷卻邏輯：如果贏錢 (PnL > 0)，即刻剷走冷卻時間！
                if real_pnl > 0:
                    print(f"🏆 {s} 贏錢平倉！解除冷卻，允許乘勝追擊！")
                    if s in cooldown_tracker:
                        del cooldown_tracker[s]

                del positions[s]
                continue

        for s in list(positions.keys()):
            curr_p, pos = exchange.fetch_ticker(s)['last'], positions[s]
            pnl_pct = (curr_p - pos['entry_price']) / pos['entry_price']
            sl_updated = False

            # ❌ 舊代碼
            # if not pos['is_breakeven'] and pnl_pct > 0.003:
            #     pos['sl_price'], pos['is_breakeven'], sl_updated = pos['entry_price'] * 1.0002, True, True

            # ✅ 新增代碼：0.15% (1.0015) 確保能完全覆蓋 Bybit 雙向 Taker 手續費 (0.11%) 及潛在滑價
            if not pos['is_breakeven'] and pnl_pct > 0.003:
                pos['sl_price'], pos['is_breakeven'], sl_updated = pos['entry_price'] * 1.0015, True, True

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
                print(f"⚔️ Triggered {exit_reason}, Executing IOC Exit: {s}")
                try:
                    ioc_price = get_3_layer_avg_price(s, 'bids') or curr_p
                    exchange.create_order(s, 'limit', 'sell', pos['amount'], ioc_price,
                                          {'timeInForce': 'IOC', 'reduceOnly': True})
                except:
                    exchange.create_market_sell_order(s, pos['amount'], {'reduceOnly': True})

                # ✅ 新增代碼：先計好利潤 (Long)
                ioc_pnl = round((curr_p - pos['entry_price']) * pos['amount'], 4)

                # ❌ 舊代碼
                # log_to_csv(
                #     {'symbol': s, 'action': 'EXIT', 'price': curr_p, 'amount': pos['amount'], 'reason': exit_reason,
                #      'realized_pnl': round((curr_p - pos['entry_price']) * pos['amount'], 4)})

                # ✅ 新增代碼
                log_to_csv(
                    {'symbol': s, 'action': 'EXIT', 'price': curr_p, 'amount': pos['amount'], 'reason': exit_reason,
                     'realized_pnl': ioc_pnl})

                cancel_all_v5(s)

                # ✅ 新增代碼：同步非對稱冷卻邏輯：如果主動平倉係贏錢，一樣解除冷卻！
                if ioc_pnl > 0:
                    print(f"🏆 {s} Bot 主動止盈平倉！解除冷卻，允許乘勝追擊！")
                    if s in cooldown_tracker:
                        del cooldown_tracker[s]

                # 🚨 修復：絕對不刪除冷卻時間
                del positions[s]
    except Exception as e:
        if "10006" in str(e):
            time.sleep(5)


# ==========================================
# 4. 執行入場
# ==========================================
def execute_live_long(symbol, net_flow, current_price, is_strong, atr, is_volatile):
    if symbol in cooldown_tracker:
        if time.time() < cooldown_tracker[symbol]:
            return
        else:
            del cooldown_tracker[symbol]  # 只有在這裡（時間到期）才可以刪除冷卻！

    if not (is_strong and is_volatile and symbol not in positions):
        return

    cancel_all_v5(symbol)

    actual_bal = get_live_usdt_balance()
    eff_bal = min(WORKING_CAPITAL, actual_bal)
    trade_val = min((eff_bal * RISK_PER_TRADE) / (atr / current_price), eff_bal * MAX_LEVERAGE * 0.95)
    amount = float(exchange.amount_to_precision(symbol, trade_val / current_price))

    if amount < exchange.markets[symbol]['limits']['amount']['min']:
        return

    ioc_p = get_3_layer_avg_price(symbol, 'asks') or current_price

    if amount * ioc_p < MIN_NOTIONAL:
        logger.warning(f"⚠️ {symbol} Order notional {amount * ioc_p:.2f} below minimum {MIN_NOTIONAL}")
        return

    try:
        exchange.set_leverage(int(MAX_LEVERAGE), symbol)
    except Exception as e:
        if "110043" in str(e):
            pass
        elif "110026" in str(e):
            logger.error(f"❌ {symbol} 10x leverage not supported, skipping.")
            return
        else:
            logger.warning(f"⚠️ {symbol} Leverage setup anomaly: {e}")

    try:
        order = exchange.create_order(symbol, 'limit', 'buy', amount, ioc_p,
                                      {'timeInForce': 'IOC', 'positionIdx': 0})
        time.sleep(1)

        actual_price = ioc_p
        actual_amount = 0

        try:
            order_detail = exchange.fetch_order(order['id'], symbol, params={"acknowledged": True})
            actual_price = float(order_detail.get('average') or order_detail.get('price') or ioc_p)
            actual_amount = float(order_detail.get('filled', 0))
        except Exception as e:
            logger.warning(f"⚠️ {symbol} Failed to fetch order, initiating fallback position sync: {e}")
            time.sleep(0.5)
            live_pos = exchange.fetch_positions()
            for p in live_pos:
                if p['symbol'] == symbol and float(p.get('contracts', 0) or p.get('size', 0)) > 0:
                    actual_amount = float(p.get('contracts', 0) or p.get('size', 0))
                    actual_price = float(p.get('entryPrice') or ioc_p)
                    break

        if actual_amount == 0:
            # ❌ 舊代碼
            # print(f"⏩ {symbol} IOC order not filled, aborting.")
            # return

            # ✅ 新增代碼：強化 IOC 未成交防禦，執行核彈撤單
            print(f"⏩ {symbol} IOC 未成交或數量為 0，執行核彈撤單並退出。")
            cancel_all_v5(symbol)
            return

        tp_p = float(exchange.price_to_precision(symbol, actual_price + (TP_ATR_MULT * atr)))
        sl_p = float(exchange.price_to_precision(symbol, actual_price - (SL_ATR_MULT * atr)))

        try:
            exchange.private_post_v5_position_trading_stop({
                'category': 'linear',
                'symbol': exchange.market_id(symbol),
                'stopLoss': str(sl_p),
                'takeProfit': str(tp_p),
                'tpslMode': 'Full',
                'positionIdx': 0
            })
            print(f"✅ {symbol} TP/SL Set | TP: {tp_p} | SL: {sl_p}")
        except Exception as e:
            logger.error(f"❌ {symbol} TP/SL setup failed (local tracking unaffected): {e}")

        positions[symbol] = {
            'amount': actual_amount,
            'entry_price': actual_price,
            'tp_price': tp_p,
            'sl_price': sl_p,
            'is_breakeven': False,
            'atr': atr
        }

        cooldown_tracker[symbol] = time.time() + 3600  # 嚴格賦予 1 小時冷卻

        log_to_csv({
            'symbol': symbol,
            'action': 'ENTRY',
            'price': actual_price,
            'amount': actual_amount,
            'trade_value': round(actual_amount * actual_price, 2),
            'atr': round(atr, 4),
            'net_flow': round(net_flow, 2),
            'tp_price': tp_p,
            'sl_price': sl_p,
            'actual_balance': round(actual_bal, 2),
            'effective_balance': eff_bal
        })

        print(f"🚀 [ENTRY] {symbol} @ {actual_price:.4f} | Size: {actual_amount}")

    except Exception as e:
        logger.error(f"❌ {symbol} Entry Failed: {e}")


# ==========================================
# 5. 主程序
# ==========================================
def main():
    print(f"🚀 AI AlgoTrade V6.0 FINAL LONG (防連夾修復版) Started...")
    last_scout_time = 0

    while True:
        try:
            manage_long_positions()
            curr_t = time.time()

            if curr_t - last_scout_time > SCOUTING_INTERVAL:
                regime = get_btc_regime()

                if regime == 1:
                    print("🟢 GREEN: Executing Top Coins Scouting...")
                    target_coins = scouting_top_coins(5)

                    for s in target_coins:
                        try:
                            flow, last_p, is_strong = apply_lee_ready_logic(s)
                            atr, is_v = get_market_metrics(s)
                            if last_p > 0:
                                execute_live_long(s, flow, last_p, is_strong, atr, is_v)
                        except Exception as e:
                            logger.warning(f"⚠️ {s} Analysis Failed: {e}")
                            continue
                        time.sleep(0.5)
                else:
                    print(f"🚦 Current Navigation Status: {regime}, Scouting Paused.")

                last_scout_time = curr_t
                print(
                    f"⏳ Patrol Complete | Positions: {list(positions.keys())} | Balance: {get_live_usdt_balance():.2f}")

            time.sleep(POSITION_CHECK_INTERVAL)

        except KeyboardInterrupt:
            print(
                f"\n👋 Commander manually terminated. Balance: {get_live_usdt_balance():.2f} USDT | Positions: {list(positions.keys())}")
            sys.exit(0)

        except Exception as e:
            if "10006" in str(e):
                logger.warning("🚨 Rate limit exceeded, sleeping for 30s...")
                time.sleep(30)
            else:
                logger.error(f"⚠️ Main Loop Error: {e}")
                time.sleep(10)


if __name__ == "__main__":
    main()