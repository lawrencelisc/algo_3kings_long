import time
import os
import pandas as pd
import logging
from datetime import datetime
from core.connect import exchange, get_live_usdt_balance, cancel_all_v5, get_3_layer_avg_price, config

logger = logging.getLogger('AlgoTrade_Engine')

LOG_DIR = "v2_long_live"
LOG_FILE = f"{LOG_DIR}/09_live_long_log.csv"
if not os.path.exists(LOG_DIR): os.makedirs(LOG_DIR)

CSV_COLUMNS = ['timestamp', 'symbol', 'action', 'price', 'amount', 'trade_value', 'atr', 'net_flow', 'tp_price',
               'sl_price', 'reason', 'realized_pnl', 'actual_balance', 'effective_balance']

positions = {}
cooldown_tracker = {}


# 🚨 新增：獨立處理交易所自動平倉的 PnL 結算與 CSV 紀錄 (Long 版)
def process_native_exit_log(symbol, pos):
    from core.connect import exchange  # 確保取得到 exchange

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
            # 📈 Long PnL 公式: (現價 - 入場價) * 數量
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


def log_to_csv(data_dict):
    row = {col: '' for col in CSV_COLUMNS}
    row.update(data_dict)
    row['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pd.DataFrame([row], columns=CSV_COLUMNS).to_csv(LOG_FILE, mode='a', index=False,
                                                    header=not os.path.exists(LOG_FILE))


def execute_live_long(symbol, net_flow, current_price, is_strong, atr, is_volatile, z_score):
    t_cfg = config['TRADING']
    s_cfg = config['STRATEGY']
    rm_cfg = config['RISK_MANAGEMENT']

    # 冷卻時間檢查與過期條目清理
    if symbol in cooldown_tracker:
        if time.time() < cooldown_tracker[symbol]:
            return
        else:
            del cooldown_tracker[symbol]

    if not (is_strong and is_volatile and symbol not in positions):
        return

    cancel_all_v5(symbol)

    actual_bal = get_live_usdt_balance()
    eff_bal = min(t_cfg['working_capital'], actual_bal)

    base_risk = t_cfg['risk_per_trade']
    if z_score >= 2.5:
        dynamic_risk = base_risk * 1.5
        print(f"🔥 Alpha Signal! Z-Score {z_score:.2f} >= 2.5. Risk increased to {dynamic_risk * 100:.2f}%")
    elif z_score >= 2.0:
        dynamic_risk = base_risk * 1.2
        print(f"⭐ Strong Signal! Z-Score {z_score:.2f} >= 2.0. Risk increased to {dynamic_risk * 100:.2f}%")
    else:
        dynamic_risk = base_risk

    # 嚴謹的基於 ATR 之風險倉位計算公式
    risk_amount = eff_bal * dynamic_risk  # 願意承受的風險金額
    stop_loss_distance = atr * s_cfg['sl_atr_mult']  # 止損距離

    if stop_loss_distance <= 0: return  # 防呆

    position_size = risk_amount / stop_loss_distance  # 應買入幣數
    trade_val_raw = position_size * current_price  # 原始交易價值

    max_position_val = eff_bal * t_cfg['max_leverage'] * 0.95  # 槓桿上限保護
    trade_val = min(trade_val_raw, max_position_val)

    amount = float(exchange.amount_to_precision(symbol, trade_val / current_price))
    if amount < exchange.markets[symbol]['limits']['amount']['min']: return

    # 獲取精準的最佳賣一價 (Best Ask) 避免滑價
    try:
        ob = exchange.fetch_order_book(symbol, limit=1)
        ioc_p = ob['asks'][0][0]
    except:
        ioc_p = current_price

    if amount * ioc_p < t_cfg['min_notional']: return

    try:
        exchange.set_leverage(int(t_cfg['max_leverage']), symbol)
    except Exception as e:
        if "110043" not in str(e): return

    try:
        order = exchange.create_order(symbol, 'limit', 'buy', amount, ioc_p, {'timeInForce': 'IOC', 'positionIdx': 0})
        time.sleep(1)

        # 🚨 核心修復區：安全地獲取實際成交價 (防止 Bybit IOC 報錯導致無限開倉)
        actual_price = ioc_p
        actual_amount = 0

        try:
            # 嘗試正規途徑拿訂單
            order_detail = exchange.fetch_order(order['id'], symbol, params={"acknowledged": True})
            actual_price = float(order_detail.get('average') or order_detail.get('price') or ioc_p)
            actual_amount = float(order_detail.get('filled', 0))
        except Exception as e:
            # 全部改為英文輸出
            logger.warning(f"⚠️ {symbol} Failed to fetch order, initiating fallback position sync: {e}")
            time.sleep(0.5)
            # 備用途徑：直接查倉位
            live_pos = exchange.fetch_positions()
            for p in live_pos:
                if p['symbol'] == symbol and float(p.get('contracts', 0) or p.get('size', 0)) > 0:
                    actual_amount = float(p.get('contracts', 0) or p.get('size', 0))
                    actual_price = float(p.get('entryPrice') or ioc_p)
                    break

        if actual_amount == 0:
            # 🚀 修正：強化 IOC 未成交防禦，執行核彈撤單
            print(f"⏩ {symbol} IOC 未成交或數量為 0，執行核彈撤單並退出。")
            cancel_all_v5(symbol)
            return

        tp_p = float(exchange.price_to_precision(symbol, actual_price + (s_cfg['tp_atr_mult'] * atr)))
        sl_p = float(exchange.price_to_precision(symbol, actual_price - (s_cfg['sl_atr_mult'] * atr)))

        # 🛠️ 舊代碼保留：if expected_profit_margin < 0.003:
        # 🚀 妖幣特化 3：防禦嚴重滑價 (Slippage)，預期利潤空間必須大於 0.6%！
        expected_profit_margin = (tp_p - actual_price) / actual_price
        if expected_profit_margin < 0.006:
            print(f"🟡 放棄妖幣 [{symbol}]: 預期利潤空間 ({expected_profit_margin * 100:.2f}%) 太細，連滑價都賠唔起！")
            cancel_all_v5(symbol)
            return  # 直接中斷，唔做接盤俠

        try:
            exchange.private_post_v5_position_trading_stop({
                'category': 'linear', 'symbol': exchange.market_id(symbol),
                'stopLoss': str(sl_p), 'takeProfit': str(tp_p), 'tpslMode': 'Full', 'positionIdx': 0
            })
            print(f"✅ {symbol} TP/SL Set | TP: {tp_p} | SL: {sl_p}")
        except Exception as e:
            logger.warning(f"⚠️ {symbol} TP/SL setup exception (local tracking unaffected): {e}")

        # ❌ 舊代碼保留
        # positions[symbol] = {
        #     'amount': actual_amount, 'entry_price': actual_price, 'tp_price': tp_p,
        #     'sl_price': sl_p, 'is_breakeven': False, 'atr': atr
        # }

        # 🚀 修正：加入 'max_pnl_pct' 初始化，用於紀錄利潤最高點
        positions[symbol] = {
            'amount': actual_amount, 'entry_price': actual_price, 'tp_price': tp_p,
            'sl_price': sl_p, 'is_breakeven': False, 'atr': atr, 'max_pnl_pct': 0.0
        }
        cooldown_tracker[symbol] = time.time() + rm_cfg['cooldown_period']

        log_to_csv({
            'symbol': symbol, 'action': 'ENTRY', 'price': actual_price, 'amount': actual_amount,
            'trade_value': round(actual_amount * actual_price, 2), 'atr': round(atr, 4), 'net_flow': round(net_flow, 2),
            'tp_price': tp_p, 'sl_price': sl_p, 'actual_balance': round(actual_bal, 2), 'effective_balance': eff_bal
        })
        print(f"🚀 [ENTRY] {symbol} @ {actual_price:.4f} | Size: {actual_amount}")

    except Exception as e:
        logger.error(f"❌ {symbol} Entry Failed: {e}")


def manage_long_positions():
    # 🚀 提取 TRADING 中的 config (因為利潤鎖定參數在 TRADING 內)
    t_cfg = config['TRADING']
    s_cfg = config['STRATEGY']
    is_critical_zone = False

    try:
        live_positions_raw = exchange.fetch_positions()
        live_symbols = {p['symbol']: p for p in live_positions_raw if
                        float(p.get('contracts', 0) or p.get('size', 0)) > 0}

        for s in list(positions.keys()):
            if s not in live_symbols:

                # 🚀 新代碼：處理原生平倉
                print(f"🧹 交易所已自動平倉，處理真實 PnL 結算單: {s}")
                real_pnl = process_native_exit_log(s, positions[s])  # 呼叫新函數

                cancel_all_v5(s)  # 核彈打鬼

                # 🏆 非對稱冷卻邏輯：贏錢即解除冷卻，輸錢硬罰企
                if real_pnl > 0:
                    print(f"🏆 {s} 贏錢平倉！解除冷卻，允許乘勝追擊！")
                    if s in cooldown_tracker:
                        del cooldown_tracker[s]

                del positions[s]
                continue

        if positions:
            all_tickers = exchange.fetch_tickers(list(positions.keys()))

        for s in list(positions.keys()):
            if s not in all_tickers: continue
            curr_p, pos = all_tickers[s]['last'], positions[s]
            pnl_pct = (curr_p - pos['entry_price']) / pos['entry_price']
            sl_updated = False

            # 🚀 新增：追蹤最高利潤點，用於鎖利機制
            if 'max_pnl_pct' not in pos:
                pos['max_pnl_pct'] = pnl_pct
            pos['max_pnl_pct'] = max(pos['max_pnl_pct'], pnl_pct)

            dist_to_tp = abs(pos['tp_price'] - curr_p) / curr_p
            dist_to_sl = abs(curr_p - pos['sl_price']) / curr_p
            if dist_to_tp < 0.0015 or dist_to_sl < 0.0015:
                is_critical_zone = True

            # 🚀 修正：0.15% (1.0015) 確保能完全覆蓋 Bybit 雙向 Taker 手續費 (0.11%) 及滑價
            if not pos['is_breakeven'] and pnl_pct > 0.003:
                pos['sl_price'], pos['is_breakeven'], sl_updated = pos['entry_price'] * 1.0015, True, True

            if pos['is_breakeven']:
                trail_sl = curr_p - (s_cfg['trail_atr_mult'] * pos['atr'])

                # 計算：只有當新的 SL 比舊的 SL 高出至少 0.2% 時，才發送 API 更新到交易所
                if trail_sl > pos['sl_price']:
                    # ❌ 舊代碼保留
                    # if (trail_sl - pos['sl_price']) / pos['sl_price'] > 0.002:
                    # 🚀 修正：降低更新門檻 (從 0.2% 降到 0.05%)，解決 Bybit 止損單更新不及時問題
                    if (trail_sl - pos['sl_price']) / pos['sl_price'] > 0.0005:
                        sl_updated = True
                    # 本地端始終保持最新，隨時準備 IOC 平倉
                    pos['sl_price'] = trail_sl

            if sl_updated:
                f_sl = exchange.price_to_precision(s, pos['sl_price'])
                try:
                    exchange.private_post_v5_position_trading_stop({
                        'category': 'linear', 'symbol': exchange.market_id(s), 'stopLoss': str(f_sl),
                        'tpslMode': 'Full', 'positionIdx': 0
                    })
                except:
                    pass

            # exit_reason = None
            # if curr_p >= pos['tp_price']:
            #     exit_reason = "TP (IOC)"
            # elif curr_p <= pos['sl_price'] and not pos['is_breakeven']:
            #     exit_reason = "SL (IOC)"

            # 🚀 升級版：喚醒本地 Trail SL 攔截機制
            exit_reason = None

            # 🚀 新增：利潤回撤保護 (防 7U 變 -3U 的殺手鐧)
            # 使用 config 中設定的 profit_lock_threshold 與 profit_retrace_limit
            profit_lock_threshold = t_cfg.get('profit_lock_threshold', 0.015)
            profit_retrace_limit = t_cfg.get('profit_retrace_limit', 0.4)

            if pos['max_pnl_pct'] > profit_lock_threshold:
                # 如果當前利潤回落幅度超過容忍比例 (例如 5% 利潤回落 40%，即跌破 3%)
                if pnl_pct < (pos['max_pnl_pct'] * (1 - profit_retrace_limit)):
                    exit_reason = "Profit Retrace Lock (IOC)"

            # 只有當沒有觸發回撤保護時，才檢查常規的 TP/SL
            if not exit_reason:
                if curr_p >= pos['tp_price']:
                    exit_reason = "TP (IOC)"
                elif curr_p <= pos['sl_price']:
                    # 如果已經解鎖保本，就叫佢 "Trail SL"，否則叫 "SL"
                    if pos['is_breakeven']:
                        exit_reason = "Trail SL (IOC)"
                    else:
                        exit_reason = "SL (IOC)"

            if exit_reason:
                print(f"⚔️ Triggered {exit_reason}, Executing IOC Exit: {s} | Max PnL: {pos['max_pnl_pct'] * 100:.2f}%")

                try:
                    try:
                        ob = exchange.fetch_order_book(s, limit=1)
                        ioc_price = ob['bids'][0][0] * 0.998
                    except:
                        ioc_price = curr_p * 0.995

                    exchange.create_order(s, 'limit', 'sell', pos['amount'], ioc_price,
                                          {'timeInForce': 'IOC', 'reduceOnly': True})
                except:
                    exchange.create_market_sell_order(s, pos['amount'], {'reduceOnly': True})

                # 🚀 新代碼：處理主動平倉
                # 📈 先獨立計算好 Long 的利潤
                ioc_pnl = round((curr_p - pos['entry_price']) * pos['amount'], 4)

                # 直接使用變數 ioc_pnl，保持代碼潔癖
                log_to_csv({
                    'symbol': s, 'action': 'EXIT', 'price': curr_p, 'amount': pos['amount'], 'reason': exit_reason,
                    'realized_pnl': ioc_pnl
                })

                cancel_all_v5(s)

                # 🏆 同步非對稱冷卻邏輯：Bot主動平倉如果贏錢，同樣解除冷卻！
                if ioc_pnl > 0:
                    print(f"🏆 {s} Bot 主動止盈平倉！解除冷卻，允許乘勝追擊！")
                    if s in cooldown_tracker:
                        del cooldown_tracker[s]

                del positions[s]

        return is_critical_zone

    except Exception as e:
        if "10006" in str(e): time.sleep(5)
        return False