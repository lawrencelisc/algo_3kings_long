import ccxt
import pandas as pd
import time
import numpy as np
import os
import logging
import sys
from datetime import datetime

# ==========================================
# ⚙️ [系統/參數] 妖幣模組初始化與 API 配置
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('AlgoTrade_Long_V6.0')

# Name: dreadnought
API_KEY = "xd8NcfedvibG9tP4iD"
API_SECRET = "ZzGICmYtkDHyTWgT1UiGpiesjz9b26Mactbw"

exchange = ccxt.bybit({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})
exchange.load_markets()

# 檔案與路徑設定
LOG_DIR = "result"
STATUS_DIR = "status"

LOG_FILE = f"{LOG_DIR}/live_long_log.csv"
STATUS_FILE = f"{STATUS_DIR}/btc_regime_long.csv"

if not os.path.exists(LOG_DIR): os.makedirs(LOG_DIR)
if not os.path.exists(STATUS_DIR): os.makedirs(STATUS_DIR)

# 系統狀態記憶體
positions = {}
cooldown_tracker = {}

# ==========================================
# ⚙️ [系統/參數] 妖幣策略與風控全局變數 (對齊 config.yaml)
# ==========================================
# --- 基礎資金管理 ---
WORKING_CAPITAL = 1000.0                ## 運作本金上限
MAX_LEVERAGE = 10.0                     ## 最大槓桿倍數
RISK_PER_TRADE = 0.01                   ## 每筆交易風險比例 (1%)
MIN_NOTIONAL = 5.0                      ## 交易所最小名義價值要求
SCOUTING_COINS = 12                     # 監察幣數量

# 🛡️ 防護網 1：單筆名義價值硬上限
MAX_NOTIONAL_PER_TRADE = 100.0

# --- 妖幣專用設定 ---
NET_FLOW_SIGMA = 2.0                    ## 資金流偏離度觸發門檻
TP_ATR_MULT = 5.0                       ## 止盈 ATR 倍數
SL_ATR_MULT = 0.8                       ## 止損 ATR 倍數
MIN_IMBALANCE_RATIO = 0.2               ## 訂單簿失衡度門檻

# --- 🛠️ V6.1 新增：妖幣專屬入場過濾參數（由 2026-04-10 數據校準）---
# altcoin net_flow 中位數=1870，遠低於大幣版(12734)，門檻絕不可共用
ALTCOIN_MIN_FLOW = 800          # L2 最低 net_flow；flow>800 OR atr_pct>2% 二選一
ALTCOIN_HIGH_VOL_ATR = 0.02     # 高波動豁免：atr%>2% 的妖幣豁免 flow 下限，保留 RAVE/MAGMA 類爆發機會

# --- 系統監控頻率 ---
SCOUTING_INTERVAL = 130                 ## 海選掃描頻率 (秒)
POSITION_CHECK_INTERVAL = 4             ## 持倉巡邏頻率 (秒) - 4秒極速貼盤

# 🗑️ (以下兩行舊版死板參數必須刪除或加上 # 註解，改用動態 ATR)
# PROFIT_LOCK_THRESHOLD = 0.015         ## 啟動門檻：當利潤達到 1.0% 時啟動回撤保護
# PROFIT_RETRACE_LIMIT = 0.4            ## 容忍回撤：利潤從最高點回落 30% 即觸發強制平倉

# --- 交易黑名單 (排除穩定幣與質押幣) ---
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

STATUS_COLUMNS = ['timestamp', 'btc_price', 'target_price', 'hma20', 'hma50', 'adx', 'signal_code', 'decision_text']

# ==========================================
# 🛠️ [輔助模組] 記錄、帳戶與訂單管理
# ==========================================
def log_to_csv(data_dict):
    """一般交易紀錄寫入 CSV"""
    row = {col: '' for col in CSV_COLUMNS}
    row.update(data_dict)
    row['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pd.DataFrame([row], columns=CSV_COLUMNS).to_csv(LOG_FILE, mode='a', index=False,
                                                    header=not os.path.exists(LOG_FILE))


def log_status_to_csv(data_dict):
    """BTC 大盤導航狀態寫入 CSV"""
    row = {col: '' for col in STATUS_COLUMNS}
    row.update(data_dict)
    row['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pd.DataFrame([row], columns=STATUS_COLUMNS).to_csv(STATUS_FILE, mode='a', index=False,
                                                       header=not os.path.exists(STATUS_FILE))


def process_native_exit_log(symbol, pos, position_type='long'):
    """處理交易所自動平倉 (Native Exit) 的 PnL 結算與紀錄"""
    real_exit_price = pos['entry_price']
    real_pnl = 0.0

    try:
        # 嘗試獲取 Bybit 官方最精準結算單 (含手續費)
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
            real_pnl = round((curr_p - pos['entry_price']) * pos['amount'], 4)
        except:
            pass

    log_to_csv({
        'symbol': symbol, 'action': 'NATIVE_EXIT', 'price': real_exit_price,
        'amount': pos['amount'], 'reason': 'Bybit Native TP/SL', 'realized_pnl': real_pnl
    })

    return real_pnl


def get_live_usdt_balance():
    """獲取帳戶可用 USDT 餘額"""
    try:
        return float(exchange.fetch_balance()['USDT']['free'])
    except:
        return 0.0


def cancel_all_v5(symbol):
    """核彈級撤單：清理該幣種所有掛單與倉位綁定的 TP/SL"""
    try:
        exchange.cancel_all_orders(symbol, params={'category': 'linear'})
        exchange.cancel_all_orders(symbol, params={'category': 'linear', 'orderFilter': 'StopOrder'})
        exchange.cancel_all_orders(symbol, params={'category': 'linear', 'orderFilter': 'tpslOrder'})
    except:
        pass
    try:
        exchange.private_post_v5_position_trading_stop({
            'category': 'linear', 'symbol': exchange.market_id(symbol),
            'takeProfit': "0", 'stopLoss': "0", 'positionIdx': 0
        })
    except:
        pass


def get_3_layer_avg_price(symbol, side='bids'):
    """獲取訂單簿前 3 檔平均價格 (用於減少 IOC 滑價)"""
    try:
        ob = exchange.fetch_order_book(symbol, limit=5)
        levels = ob[side][:3]
        if not levels: return None
        return sum([level[0] for level in levels]) / len(levels)
    except:
        return None


def get_market_metrics(symbol):
    """計算 ATR 並過濾低波動率幣種"""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=50)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df['tr'] = np.maximum(df['h'] - df['l'],
                              np.maximum(abs(df['h'] - df['c'].shift(1)), abs(df['l'] - df['c'].shift(1))))
        atr = df['tr'].rolling(14, min_periods=1).mean().iloc[-1]

        if pd.isna(atr) or atr == 0: return None, False

        # 🛡️ 防護網 3：死魚幣過濾 (波幅 < 0.15% 直接放棄，防止手續費磨損)
        return atr, (atr / df['c'].iloc[-1]) > 0.0015
    except:
        return None, False


# ==========================================
# 🧠 [核心邏輯] 市場導航與選幣系統
# ==========================================
def get_btc_regime():
    """終極導航：HMA 交叉 + ADX 趨勢過濾 + 均量過濾"""
    try:
        ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='15m', limit=150)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        curr_p = df['c'].iloc[-1]  # 🚀 刪除了會隨時間歸零的 curr_v

        # --- 1️⃣ 極速趨勢引擎：計算 HMA 20 與 HMA 50 ---
        def calc_hma(s, period):
            half_length = int(period / 2)
            sqrt_length = int(np.sqrt(period))
            weights_half = np.arange(1, half_length + 1)
            weights_full = np.arange(1, period + 1)
            weights_sqrt = np.arange(1, sqrt_length + 1)

            wma_half = s.rolling(half_length).apply(lambda x: np.dot(x, weights_half) / weights_half.sum(), raw=True)
            wma_full = s.rolling(period).apply(lambda x: np.dot(x, weights_full) / weights_full.sum(), raw=True)
            s_diff = (2 * wma_half) - wma_full
            return s_diff.rolling(sqrt_length).apply(lambda x: np.dot(x, weights_sqrt) / weights_sqrt.sum(), raw=True)

        df['hma20'], df['hma50'] = calc_hma(df['c'], 20), calc_hma(df['c'], 50)
        hma20_val, hma50_val = df['hma20'].iloc[-1], df['hma50'].iloc[-1]

        # 🟢 多軍 (Long) 邏輯：HMA20 大於 HMA50
        cond_trend = hma20_val > hma50_val

        # --- 2️⃣ 趨勢強度濾網：計算 ADX (14) ---
        df['up'] = df['h'] - df['h'].shift(1)
        df['down'] = df['l'].shift(1) - df['l']
        df['+dm'] = np.where((df['up'] > df['down']) & (df['up'] > 0), df['up'], 0)
        df['-dm'] = np.where((df['down'] > df['up']) & (df['down'] > 0), df['down'], 0)
        df['tr'] = np.maximum(df['h'] - df['l'],
                              np.maximum(abs(df['h'] - df['c'].shift(1)), abs(df['l'] - df['c'].shift(1))))

        atr_14 = df['tr'].ewm(alpha=1 / 14, adjust=False).mean()
        plus_di = 100 * (pd.Series(df['+dm']).ewm(alpha=1 / 14, adjust=False).mean() / atr_14)
        minus_di = 100 * (pd.Series(df['-dm']).ewm(alpha=1 / 14, adjust=False).mean() / atr_14)
        denominator = plus_di + minus_di
        dx = np.where(denominator != 0, 100 * abs(plus_di - minus_di) / denominator, 0)
        adx_val = pd.Series(dx).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
        cond_adx = adx_val > 22

        # --- 3️⃣ 成交量濾網 (抗極端值優化版 - 已修復未收盤陷阱) ---
        # 🚀 改用「上一根已完整收盤」的 K 線 (-2)，避開當前 K 線歸零問題
        completed_v = df['v'].iloc[-2]
        # 🚀 計算過去 24 根「已收盤」K 線的中位數 (避開最後一根未完成的)
        median_v_24 = df['v'].iloc[-25:-1].median()
        target_vol = median_v_24 * 0.8
        cond_vol = completed_v > target_vol

        # --- 4️⃣ 整合訊號與輸出 ---
        tick_t = "✅" if cond_trend else "❌"
        tick_a = f"✅ (ADX: {adx_val:.1f})" if cond_adx else f"❌ (ADX: {adx_val:.1f})"
        tick_v = f"✅ (Vol: {completed_v:.0f} > 目標:{target_vol:.0f})" if cond_vol else f"❌ (Vol: {completed_v:.0f} < 目標:{target_vol:.0f})"

        if cond_trend and cond_adx and cond_vol:
            status, signal = "🟢 GREEN   (Trend, ADX & Vol Validated)", 1
        elif cond_trend or cond_adx:
            status, signal = "🟡 YELLOW  (Standby - Waiting for confluence)", 0
        else:
            status, signal = "🔴 RED     (Sideways / Bearish)", -1

        log_status_to_csv({
            'btc_price': round(curr_p, 2), 'target_price': round(hma50_val, 2),
            'hma20': round(hma20_val, 2), 'hma50': round(hma50_val, 2), 'adx': round(adx_val, 2),
            'signal_code': signal, 'decision_text': status
        })

        print("-" * 60)
        print(f"📊 BTC 實時戰報 (HMA+ADX+Vol版) | 現價: {curr_p:.0f}")
        print(f"1️⃣ 極速趨勢: HMA20({hma20_val:.0f}) > HMA50({hma50_val:.0f}) {tick_t}")
        print(f"2️⃣ 趨勢強度: ADX > 22 {tick_a}")
        print(f"3️⃣ 動能確認: 上根已收盤量 > 24H中位數(80%) {tick_v}")
        print(f"🚦 最終決策: {status}")
        print("-" * 60)

        # 🚀 升級改動：回傳詳細動能字典
        return {
            'signal': signal,
            'adx': float(adx_val),
            'di_spread': float(plus_di.iloc[-1]) - float(minus_di.iloc[-1])
        }

    except Exception as e:
        print(f"⚠️ 導航故障: {e}")
        return {'signal': 0, 'adx': 0.0, 'di_spread': 0.0}


def scouting_strong_coins(scouting_coins=12):
    """妖幣海選 (放寬 Spread，絕對成交量過濾)"""
    try:
        tickers = exchange.fetch_tickers()
        data = []
        for s, t in tickers.items():
            if s.endswith(':USDT') and s not in BLACKLIST and t['percentage'] is not None:
                ask = t.get('ask')
                bid = t.get('bid')
                if ask and bid and bid > 0:
                    spread = (ask - bid) / bid
                    # 🚀 妖幣特化 1：放寬 Spread 門檻到 0.0030 (0.3%)
                    if spread < 0.0030:
                        data.append({'symbol': s, 'volume': t['quoteVolume'], 'change': t['percentage']})

        df = pd.DataFrame(data)
        if df.empty: return []

        # 🚀 妖幣特化 2：絕對硬門檻 (24小時成交量 > 1,000萬 U)，過濾死水幣
        MIN_VOLUME_ALT = 10000000
        df_filtered = df[df['volume'] >= MIN_VOLUME_ALT].copy()  # 加上 .copy() 避免 pandas 警告

        # 二次防護：如果篩選後冇幣符合條件，直接 return 空 list
        if df_filtered.empty: return []

        # 🚀 尋找升幅最勁嘅前 n 隻妖幣 (只做多 Long 的話用呢行)
        return df_filtered.sort_values('change', ascending=False).head(scouting_coins)['symbol'].tolist()
    except Exception as e:
        logger.error(f"⚠️ Altcoin Scouting Error: {e}")
        return []


def check_flow_reversal(symbol):
    """【防守專用】輕量級資金流反轉檢測 (防砸盤雷達)"""
    try:
        trades = exchange.fetch_trades(symbol, limit=100)
        if not trades or len(trades) < 50: return False

        df = pd.DataFrame(trades)
        df['price_change'] = df['price'].diff()
        df['direction'] = np.where(df['price_change'] > 0, 1, np.where(df['price_change'] < 0, -1, 0))
        df['direction'] = df['direction'].replace(0, np.nan).ffill().fillna(0)

        # 瞬時資金流 (加權)
        avg_vol = df['amount'].mean()
        df['weight'] = np.where(df['amount'] > avg_vol * 2, 2.0, 1.0)
        df['net_flow'] = df['direction'] * df['amount'] * df['price'] * df['weight']

        flow_mean = df['net_flow'].mean()
        flow_std = df['net_flow'].std()

        if flow_std == 0: return False

        recent_25_flow = df['net_flow'].tail(25).sum()
        z_score = (recent_25_flow - (flow_mean * 25)) / (flow_std * np.sqrt(25))

        # 嚴格閾值：-3.0 Sigma 先斬，防假訊號
        if z_score < -3.0:
            print(f"🚨 {symbol} 偵測到極端砸盤資金流！Z-Score: {z_score:.2f}")
            return True

        return False
    except Exception as e:
        return False


def apply_lee_ready_long_logic(symbol):
    """【進攻專用】正向 Lee-Ready 狙擊模式 (含大單加權、加速度與防砸盤陷阱)"""
    try:
        trades = exchange.fetch_trades(symbol, limit=200)
        if not trades: return 0, 0, False

        df = pd.DataFrame(trades)
        df['price_change'] = df['price'].diff()
        df['direction'] = np.where(df['price_change'] > 0, 1, np.where(df['price_change'] < 0, -1, 0))
        df['direction'] = df['direction'].replace(0, np.nan).ffill().fillna(0)  # 修復 Pandas Bug

        # 大單加權 (大於平均量 2 倍的單，權重 x 2)
        avg_vol = df['amount'].mean()
        df['weight'] = np.where(df['amount'] > avg_vol * 2, 2.0, 1.0)
        df['net_flow'] = df['direction'] * df['amount'] * df['price'] * df['weight']

        # 計算資金流與加速度
        short_window_flow = df['net_flow'].tail(50).sum()
        acceleration = df['net_flow'].tail(25).sum() - df['net_flow'].iloc[-50:-25].sum()

        # 訂單簿失衡度計算
        try:
            ob = exchange.fetch_order_book(symbol, limit=20)
            bids_vol = sum([b[1] for b in ob['bids']])
            asks_vol = sum([a[1] for a in ob['asks']])
            imbalance = (bids_vol - asks_vol) / (bids_vol + asks_vol) if (bids_vol + asks_vol) > 0 else 0
        except:
            imbalance = 0

        is_strong = False
        std_val = df['net_flow'].std()

        if df['net_flow'].std() > 0:
            z_score = short_window_flow / (df['net_flow'].std() * np.sqrt(50))
        else:
            z_score = 0

        # 🚀 升級改動：計算資金流新鮮度
        flow_50 = df['net_flow'].tail(50)
        flow_25_recent = flow_50.tail(25).sum()
        flow_25_older = flow_50.head(25).sum()

        z_recent = (flow_25_recent / (std_val * np.sqrt(25))) if std_val > 0 else 0
        z_older = (flow_25_older / (std_val * np.sqrt(25))) if std_val > 0 else 0

        # 新鮮度要求：近端必須大於等於遠端的 80%
        flow_is_fresh = z_recent >= (max(0, z_older) * 0.8)

        # 🛠️ V6.1 Fix：原本硬碼 0.15，但全局 MIN_IMBALANCE_RATIO=0.2，改用常數保持一致
        if (short_window_flow > 0) and (acceleration > 0) and (imbalance > MIN_IMBALANCE_RATIO):
            is_strong = True
            print(f"🔥 {symbol} Long Sniper! Accel: {acceleration:.0f} | Imbalance: {imbalance:.2f}")
        elif z_score > NET_FLOW_SIGMA:
            is_strong = True
            print(f"📈 {symbol} Long Z-Score Validated: {z_score:.2f}")

        # 🛡️ 防護網 4：防砸盤陷阱 (Bull Trap) - 若升得急但賣盤極厚壓頂，取消做多
        if is_strong and imbalance < -0.1:
            is_strong = False
            print(f"⚠️ {symbol} 發現莊家砸盤陷阱！賣盤極厚，取消做多！")

        # 🚀 升級改動：回傳 6 個變數
        return short_window_flow, df['price'].iloc[-1], is_strong, flow_is_fresh, acceleration, z_score
    except Exception as e:
        print(f"⚠️ LR Logic Error [{symbol}]: {e}")
        return 0, 0, False, False, 0, 0


# ==========================================
# 🛡️ [執行與風控] 持倉管理與入場執行
# ==========================================
def sync_positions_on_startup():
    """啟動時同步交易所真實倉位 (防止重啟導致孤兒倉與止損倒退)"""
    print("🔄 正在同步交易所現有倉位...")
    try:
        live_positions_raw = exchange.fetch_positions()
        live_symbols = [p for p in live_positions_raw if float(p.get('contracts', 0) or p.get('size', 0)) > 0]

        recovered_count = 0
        for p in live_symbols:
            symbol = p['symbol']
            side = p.get('side', '').lower()
            info_side = p.get('info', {}).get('side', '').lower()

            # 🚀 [Big Long 修改] 只恢復多單 (Long)
            if side in ['long', 'buy'] or info_side in ['buy', 'long']:

                entry_price = float(p.get('entryPrice', 0))
                amount = float(p.get('contracts', 0) or p.get('size', 0))

                sl_p, tp_p = float(p.get('stopLoss', 0)), float(p.get('takeProfit', 0))
                atr, _ = get_market_metrics(symbol)
                if not atr: atr = entry_price * 0.01

                # 🚀 [Big Long 修改] 多單止損在下，止盈在上
                if sl_p == 0: sl_p = float(exchange.price_to_precision(symbol, entry_price - (SL_ATR_MULT * atr)))
                if tp_p == 0: tp_p = float(exchange.price_to_precision(symbol, entry_price + (TP_ATR_MULT * atr)))
                # 🚀 [修正] 判斷多單 SL 係咪已經高過入場價，防止 Trail SL 倒退
                is_be = True if sl_p > entry_price else False

                positions[symbol] = {
                    'amount': amount, 'entry_price': entry_price, 'tp_price': tp_p, 'sl_price': sl_p,
                    'is_breakeven': is_be, 'atr': atr, 'max_pnl_pct': 0.0, 'entry_time': time.time()
                }
                recovered_count += 1
                print(f"✅ 成功尋回孤兒多單: {symbol} | 入場價: {entry_price} | 已保本狀態: {is_be}")

        print(f"🔄 同步完成！共尋回 {recovered_count} 個倉位。")
    except Exception as e:
        logger.error(f"❌ 啟動同步失敗: {e}")


def manage_long_positions():
    """管理在途多單 (Native Exit 檢查、Trail SL 更新、回撤鎖利、動態孤兒接管)"""
    try:
        # 🛠️ 修復 1：強制指定 'linear'，確保 Bybit V5 100% 準確回傳 USDT 合約
        live_positions_raw = exchange.fetch_positions(params={'category': 'linear'})
        live_symbols = {p['symbol']: p for p in live_positions_raw if
                        float(p.get('contracts', 0) or p.get('size', 0)) > 0}

        # ==========================================
        # 🛠️ 修復 2：動態孤兒倉位接管 (Auto-Adopt 機制)
        # 只要發現 Bybit 有單，但 Bot 記憶體無，即刻接管並重設防護網！
        # ==========================================
        for s, p in live_symbols.items():
            if s not in positions:
                side = p.get('side', '').lower()
                info_side = p.get('info', {}).get('side', '').lower()

                if side in ['long', 'buy'] or info_side in ['buy', 'long']:
                    entry_p = float(p.get('entryPrice', 0))
                    amt = float(p.get('contracts', 0) or p.get('size', 0))
                    atr, _ = get_market_metrics(s)
                    if not atr: atr = entry_p * 0.01

                    # 嘗試抓取 Bybit 現有 TP/SL，如果被撤銷咗就根據 ATR 重新計算
                    sl_p = float(p.get('stopLoss') or 0)
                    tp_p = float(p.get('takeProfit') or 0)

                    if sl_p == 0: sl_p = float(exchange.price_to_precision(s, entry_p - (SL_ATR_MULT * atr)))
                    if tp_p == 0: tp_p = float(exchange.price_to_precision(s, entry_p + (TP_ATR_MULT * atr)))
                    is_be = True if sl_p > entry_p else False

                    # 寫入腦海，正式接管
                    positions[s] = {
                        'amount': amt, 'entry_price': entry_p, 'tp_price': tp_p, 'sl_price': sl_p,
                        'is_breakeven': is_be, 'atr': atr, 'max_pnl_pct': 0.0, 'entry_time': time.time()
                    }
                    print(f"🚨 [系統自癒] 發現並自動接管孤兒多單: {s} | 入場價: {entry_p} | 數量: {amt}")
        # ==========================================

        # 1. 處理已經被交易所平倉的訂單
        for s in list(positions.keys()):
            if s not in live_symbols:
                print(f"🧹 交易所已自動平倉，處理真實 PnL 結算單: {s}")
                real_pnl = process_native_exit_log(s, positions[s], position_type='long')
                cancel_all_v5(s)

                if real_pnl > 0:
                    print(f"🏆 {s} 贏錢平倉！解除冷卻，允許乘勝追擊！")
                    if s in cooldown_tracker: del cooldown_tracker[s]
                del positions[s]
                continue

        # 2. 處理仍在途的持倉
        for s in list(positions.keys()):
            curr_p, pos = exchange.fetch_ticker(s)['last'], positions[s]
            pnl_pct = (curr_p - pos['entry_price']) / pos['entry_price']

            coin_volatility_pct = pos['atr'] / pos['entry_price']
            sl_updated = False

            if 'max_pnl_pct' not in pos: pos['max_pnl_pct'] = pnl_pct
            pos['max_pnl_pct'] = max(pos['max_pnl_pct'], pnl_pct)

            # 階段一 & 二：爬升期推保本
            if not pos['is_breakeven'] and pnl_pct > (coin_volatility_pct * 2.0):
                pos['sl_price'], pos['is_breakeven'], sl_updated = pos['entry_price'] * 1.002, True, True

            # 階段三：三段式放風箏追蹤止損 (Trail SL)
            if pos['is_breakeven']:
                if pnl_pct > (coin_volatility_pct * 3.5):
                    trail_sl = curr_p - (1.5 * pos['atr'])
                else:
                    trail_sl = curr_p - (2.0 * pos['atr'])

                if trail_sl > pos['sl_price']:
                    if (trail_sl - pos['sl_price']) / pos['sl_price'] > 0.0005:
                        sl_updated = True
                        pos['sl_price'] = trail_sl

            # 發送更新到交易所
            if sl_updated:
                f_sl = exchange.price_to_precision(s, pos['sl_price'])
                try:
                    exchange.private_post_v5_position_trading_stop({
                        'category': 'linear', 'symbol': exchange.market_id(s), 'stopLoss': str(f_sl),
                        'tpslMode': 'Full', 'positionIdx': 0
                    })
                except Exception as e:
                    logger.warning(f"⚠️ {s} 追蹤止損 API 更新失敗 (本地腦海仍保持最新): {e}")

            exit_reason = None
            time_held = time.time() - pos.get('entry_time', time.time())

            # 聰明時間止損
            if not exit_reason and time_held > 300 and pnl_pct < 0:
                exit_reason = "Time Stop (Failed to ignite)"

            # 資金流反轉檢測
            if not exit_reason and not pos['is_breakeven'] and pnl_pct < 0:
                if check_flow_reversal(s):
                    exit_reason = "Flow Reversal (Smart Exit)"

            # 常規本地 TP/SL 檢查
            if not exit_reason:
                if curr_p >= pos['tp_price']:
                    exit_reason = "TP (Long IOC Exit)"
                elif curr_p <= pos['sl_price']:
                    exit_reason = "Trail SL (Long IOC Exit)" if pos['is_breakeven'] else "SL (Long IOC Exit)"

            # 執行本地主動平倉 (IOC)
            if exit_reason:
                print(f"⚔️ 觸發 {exit_reason}，執行 IOC 平單: {s} | Max PnL: {pos['max_pnl_pct'] * 100:.2f}%")
                ioc_price = get_3_layer_avg_price(s, 'bids') or curr_p
                try:
                    exchange.create_order(s, 'limit', 'sell', pos['amount'], ioc_price,
                                          {'timeInForce': 'IOC', 'reduceOnly': True})
                except:
                    exchange.create_market_sell_order(s, pos['amount'], {'reduceOnly': True})

                ioc_pnl = round((ioc_price - pos['entry_price']) * pos['amount'], 4)

                log_to_csv({'symbol': s, 'action': 'LONG_EXIT', 'price': curr_p, 'amount': pos['amount'],
                            'reason': exit_reason, 'realized_pnl': ioc_pnl})
                cancel_all_v5(s)

                if ioc_pnl > 0:
                    print(f"🏆 {s} Bot 主動止盈平倉！解除冷卻，允許乘勝追擊！")
                    if s in cooldown_tracker: del cooldown_tracker[s]

                del positions[s]

    except Exception as e:
        if "10006" in str(e): time.sleep(5)


def execute_live_long(symbol, net_flow, current_price, is_strong, atr, is_volatile, flow_is_fresh, regime_info):
    """計算倉位並執行多單入場"""
    if symbol in cooldown_tracker:
        if time.time() < cooldown_tracker[symbol]:
            return
        else:
            del cooldown_tracker[symbol]

    # 🛠️ V6.1 攔截器 0：死水幣硬性過濾
    # 數據顯示 AKE（ATR=0）連續 5 次入場合計虧 -12.2 USDT
    # ATR 為零代表幣種無波動，TP/SL 設置無意義，絕不入場
    if atr == 0 or current_price == 0:
        print(f"🚫 {symbol} 跳過：ATR=0 死水幣，波動為零無法設置有效 TP/SL")
        return

    atr_pct = atr / current_price

    # 🛠️ V6.1 攔截器 1：妖幣版 ADX 動能過濾（沿用 BTC regime 確認）
    adx_strong = regime_info['adx'] >= 25 and regime_info['di_spread'] >= 15
    if not adx_strong:
        print(f"⏸️ {symbol} 跳過：BTC ADX={regime_info['adx']:.1f}, +DI-(-DI)={regime_info['di_spread']:.1f}，大市動能不足")
        return

    # 🛠️ V6.1 攔截器 2：妖幣專屬 L2 資金流過濾（不可與 big_long 共用門檻）
    # 邏輯：flow > ALTCOIN_MIN_FLOW(800) OR 高波動妖幣豁免(atr_pct > 2%)
    # 原因：RAVE/MAGMA 等妖幣 flow 天生低（300-900），但爆發力極強，不能被 flow 門檻誤殺
    flow_ok = (net_flow > ALTCOIN_MIN_FLOW) or (atr_pct > ALTCOIN_HIGH_VOL_ATR)
    if not flow_ok:
        print(f"⏸️ {symbol} 跳過：flow={net_flow:.0f} < {ALTCOIN_MIN_FLOW} 且 atr_pct={atr_pct*100:.2f}% < {ALTCOIN_HIGH_VOL_ATR*100:.0f}%，動能不足")
        return

    # 🛠️ V6.1 攔截器 3：資金流新鮮度（沿用）
    if not flow_is_fresh:
        print(f"⏸️ {symbol} 跳過：Net Flow 近端動能已衰退，避免追尾段入場")
        return

    if not (is_strong and is_volatile and symbol not in positions): return

    cancel_all_v5(symbol)
    actual_bal = get_live_usdt_balance()
    eff_bal = min(WORKING_CAPITAL, actual_bal)

    # 🛡️ 防護網 1：MAX_NOTIONAL_PER_TRADE 硬性截斷
    trade_val = min((eff_bal * RISK_PER_TRADE) / ((SL_ATR_MULT * atr) / current_price), eff_bal * MAX_LEVERAGE * 0.95,
                    MAX_NOTIONAL_PER_TRADE)
    amount = float(exchange.amount_to_precision(symbol, trade_val / current_price))

    if amount < exchange.markets[symbol]['limits']['amount']['min']: return
    ioc_p = get_3_layer_avg_price(symbol, 'asks') or current_price
    if amount * ioc_p < MIN_NOTIONAL: return

    try:
        exchange.set_leverage(int(MAX_LEVERAGE), symbol)
    except Exception as e:
        if "110043" not in str(e):
            if "110026" in str(e): return
            logger.warning(f"⚠️ {symbol} 槓桿異常: {e}")

    try:
        order = exchange.create_order(symbol, 'limit', 'buy', amount, ioc_p, {'timeInForce': 'IOC', 'positionIdx': 0})
        time.sleep(1)

        actual_price, actual_amount = ioc_p, 0

        # 確認成交狀態
        try:
            order_detail = exchange.fetch_order(order['id'], symbol, params={"acknowledged": True})
            actual_price = float(order_detail.get('average') or order_detail.get('price') or ioc_p)
            actual_amount = float(order_detail.get('filled', 0))
        except Exception as e:
            logger.warning(f"⚠️ {symbol} 獲取訂單失敗，啟動備用持倉同步: {e}")
            time.sleep(0.5)
            for p in exchange.fetch_positions():
                if p['symbol'] == symbol and float(p.get('contracts', 0) or p.get('size', 0)) > 0:
                    actual_amount = float(p.get('contracts', 0) or p.get('size', 0))
                    actual_price = float(p.get('entryPrice') or ioc_p)
                    break

        if actual_amount == 0:
            print(f"⏩ {symbol} IOC 未成交，執行核彈撤單並退出。")
            cancel_all_v5(symbol)
            return

        tp_p = float(exchange.price_to_precision(symbol, actual_price + (TP_ATR_MULT * atr)))
        sl_p = float(exchange.price_to_precision(symbol, actual_price - (SL_ATR_MULT * atr)))
        expected_profit_margin = (tp_p - actual_price) / actual_price

        # 預期利潤防護：空間太細連手續費都唔夠俾
        if expected_profit_margin < 0.01:
            print(f"🟡 放棄做多 [{symbol}]: 預期利潤空間 ({expected_profit_margin * 100:.2f}%) 太細，立即市價平倉！")
            try:
                exchange.create_market_sell_order(symbol, actual_amount, {'reduceOnly': True})
            except Exception as e:
                logger.error(f"❌ 緊急平倉失敗！需人工介入: {e}")
            cancel_all_v5(symbol)
            return

        # 設置交易所 TP/SL
        try:
            exchange.private_post_v5_position_trading_stop({
                'category': 'linear', 'symbol': exchange.market_id(symbol), 'stopLoss': str(sl_p),
                'takeProfit': str(tp_p), 'tpslMode': 'Full', 'positionIdx': 0
            })
            print(f"✅ {symbol} 止盈止損已設置 | TP: {tp_p} | SL: {sl_p}")
        except Exception as e:
            logger.warning(f"⚠️ {symbol} 止盈止損設置異常 (不影響本地追蹤): {e}")

        # 寫入本地記憶體
        # 🛠️ V6.1 Fix：補回 entry_time，否則 time_held 永遠為 0，Time Stop 永遠不觸發
        positions[symbol] = {
            'amount': actual_amount, 'entry_price': actual_price, 'tp_price': tp_p, 'sl_price': sl_p,
            'is_breakeven': False, 'atr': atr, 'max_pnl_pct': 0.0, 'entry_time': time.time()
        }
        cooldown_tracker[symbol] = time.time() + 480  # 配合 1.2 ATR 止損，冷卻 8 分鐘

        log_to_csv({
            'symbol': symbol, 'action': 'LONG_ENTRY', 'price': actual_price, 'amount': actual_amount,
            'trade_value': round(actual_amount * actual_price, 2), 'atr': round(atr, 4),
            'net_flow': round(net_flow, 2), 'tp_price': tp_p, 'sl_price': sl_p,
            'actual_balance': round(actual_bal, 2), 'effective_balance': eff_bal
        })
        print(f"📈 [已入貨做多] {symbol} @ {actual_price:.4f} | 數量: {actual_amount}")

    except Exception as e:
        logger.error(f"❌ {symbol} 做多核心執行失敗: {e}")


# ==========================================
# 🚀 [主程序] 主迴圈與事件驅動
# ==========================================
def main():
    print(f"🚀 AI 實戰 V6.1 ALTCOIN LONG (妖幣狙擊版) 啟動...")
    print(f"Lee-Ready 資金流 + 訂單簿失衡度 + 妖幣動態海選 [終極做多版] 初始化中...")
    print(f"🛠️ V6.1 改動：死水幣(ATR=0)攔截 | 妖幣專屬 flow>800/atr>2% | Time Stop 修正 | entry_time 補回")

    # 啟動時先同步遺留的倉位
    sync_positions_on_startup()

    last_scout_time = 0
    target_coins = []

    while True:
        try:
            manage_long_positions()
            curr_t = time.time()

            # 2. 定期偵測與進攻
            if curr_t - last_scout_time > SCOUTING_INTERVAL:
                # 🚀 接收 Dictionary
                regime_info = get_btc_regime()

                # 🚀 判斷 signal
                if regime_info['signal'] == 1:
                    print("🟢 綠燈確認：執行多單大幣海選掃描...")
                    target_coins = scouting_strong_coins(SCOUTING_COINS)

                    for s in target_coins:
                        try:
                            # 🚀 接收 6 個回傳值
                            flow, last_p, is_strong, flow_is_fresh, acc, z = apply_lee_ready_long_logic(s)
                            atr, is_v = get_market_metrics(s)
                            if last_p > 0:
                                # 🚀 傳入新參數
                                execute_live_long(s, flow, last_p, is_strong, atr, is_v, flow_is_fresh, regime_info)
                        except Exception as e:
                            continue
                        time.sleep(0.5)
                else:
                    # ✅ 修正：使用 regime_info['signal'] 讀取狀態
                    print(f"🚦 目前大盤狀態為 Signal: {regime_info['signal']}，海選暫停。")
                    target_coins = []  # 黃/紅燈時清空孤兒名單

                last_scout_time = curr_t
                print(f"⏳ 多軍巡邏完畢 | 持倉: {list(positions.keys())} | 餘額: {get_live_usdt_balance():.2f}")

            # 3. 持倉巡邏間隔
            time.sleep(POSITION_CHECK_INTERVAL)

        except KeyboardInterrupt:
            print(f"\n👋 指揮官手動終止。餘額: {get_live_usdt_balance():.2f} USDT | 持倉: {list(positions.keys())}")
            sys.exit(0)
        except Exception as e:
            # 🚀 加上 logger，令程式出錯時有跡可尋，唔會靜靜雞重啟
            logger.error(f"❌ 主迴圈發生未知錯誤: {e}")
            time.sleep(30 if "10006" in str(e) else 10)


if __name__ == "__main__":
    main()