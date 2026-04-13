import ccxt
import pandas as pd
import time
import numpy as np
import os
import logging
import sys
import json
from datetime import datetime

# ==========================================
# ⚙️ [系統/參數] 模組初始化與 API 配置
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
# logger = logging.getLogger('AlgoTrade_Long_V6.0')  # [舊版保留]
logger = logging.getLogger('AlgoTrade_Long_V6.5_Thermostat')  # 🚀 [V6.5 修改]

# Name: dreadnought
API_KEY = ""
API_SECRET = ""

exchange = ccxt.bybit({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
})
exchange.load_markets()

# 檔案與路徑設定
LOG_DIR = "result"
STATUS_DIR = "../status"
LOG_FILE = f"{LOG_DIR}/live_long_log.csv"
STATUS_FILE = f"{STATUS_DIR}/btc_regime_long.csv"
BLACKLIST_FILE = f"{STATUS_DIR}/dynamic_blacklist_long.json"

if not os.path.exists(LOG_DIR): os.makedirs(LOG_DIR)
if not os.path.exists(STATUS_DIR): os.makedirs(STATUS_DIR)

# 系統狀態記憶體
positions = {}
cooldown_tracker = {}
consecutive_losses = {}

# ==========================================
# ⚙️ [系統/參數] 策略與風控全局變數
# ==========================================
WORKING_CAPITAL = 1000.0
MAX_LEVERAGE = 10.0
RISK_PER_TRADE = 0.005  # 🛡️ 縮細注碼：0.5% 風險
MIN_NOTIONAL = 5.0
MAX_NOTIONAL_PER_TRADE = 200.0

NET_FLOW_SIGMA = 1.2
TP_ATR_MULT = 5.0  # 做多版：給予足夠暴漲空間
SL_ATR_MULT = 0.8  # 初始止損 0.8 ATR

MAX_CONSECUTIVE_LOSSES = 3  # 連輸 3 次封禁
DYNAMIC_BAN_DURATION = 86400  # 封禁 24 小時

SCOUTING_INTERVAL = 125
POSITION_CHECK_INTERVAL = 4  # 4秒極速貼盤巡邏

BLACKLIST = [
    'USDC/USDT:USDT', 'DAI/USDT:USDT', 'FDUSD/USDT:USDT', 'BUSD/USDT:USDT',
    'TUSD/USDT:USDT', 'PYUSD/USDT:USDT', 'USDP/USDT:USDT', 'EURS/USDT:USDT',
    'USDE/USDT:USDT', 'USAT/USDT:USDT', 'USD0/USDT:USDT', 'USTC/USDT:USDT',
    'LUSD/USDT:USDT', 'FRAX/USDT:USDT', 'MIM/USDT:USDT', 'RLUSD/USDT:USDT',
    'WBTC/USDT:USDT', 'WETH/USDT:USDT', 'WBNB/USDT:USDT', 'WAVAX/USDT:USDT',
    'stETH/USDT:USDT', 'cbETH/USDT:USDT', 'WHT/USDT:USDT'
]


# ==========================================
# 🛠️ [輔助模組] JSON 記憶與狀態管理
# ==========================================
def save_dynamic_blacklist():
    data = {'consecutive_losses': consecutive_losses, 'cooldown_tracker': cooldown_tracker}
    try:
        with open(BLACKLIST_FILE, 'w') as f:
            json.dump(data, f, indent=4)
    except:
        pass


def load_dynamic_blacklist():
    global consecutive_losses, cooldown_tracker
    if os.path.exists(BLACKLIST_FILE):
        try:
            with open(BLACKLIST_FILE, 'r') as f:
                data = json.load(f)
                consecutive_losses.update(data.get('consecutive_losses', {}))
                cooldown_tracker.update(data.get('cooldown_tracker', {}))
            curr_t = time.time()
            expired = [k for k, v in cooldown_tracker.items() if v < curr_t]
            for k in expired:
                del cooldown_tracker[k]
                if k in consecutive_losses: del consecutive_losses[k]
            if expired: save_dynamic_blacklist()
        except:
            pass


def handle_trade_result(symbol, pnl):
    global consecutive_losses, cooldown_tracker
    if pnl > 0:
        consecutive_losses[symbol] = 0
        if symbol in cooldown_tracker: del cooldown_tracker[symbol]
    elif pnl < 0:
        consecutive_losses[symbol] = consecutive_losses.get(symbol, 0) + 1
        if consecutive_losses[symbol] >= MAX_CONSECUTIVE_LOSSES:
            cooldown_tracker[symbol] = time.time() + DYNAMIC_BAN_DURATION
        else:
            cooldown_tracker[symbol] = max(cooldown_tracker.get(symbol, 0), time.time() + 480)
    save_dynamic_blacklist()


# [舊版保留] def get_btc_regime():
# [舊版保留]     """終極導航：HMA 交叉 + ADX 趨勢過濾 + 均量過濾"""
# [舊版保留]     try:
# [舊版保留]         ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='15m', limit=150)
# [舊版保留]         df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
# [舊版保留]         curr_p = df['c'].iloc[-1]
# [舊版保留]
# [舊版保留]         def calc_hma(s, period):
# [舊版保留]             half_length = int(period / 2)
# [舊版保留]             sqrt_length = int(np.sqrt(period))
# [舊版保留]             weights_half = np.arange(1, half_length + 1)
# [舊版保留]             weights_full = np.arange(1, period + 1)
# [舊版保留]             weights_sqrt = np.arange(1, sqrt_length + 1)
# [舊版保留]
# [舊版保留]             wma_half = s.rolling(half_length).apply(lambda x: np.dot(x, weights_half) / weights_half.sum(), raw=True)
# [舊版保留]             wma_full = s.rolling(period).apply(lambda x: np.dot(x, weights_full) / weights_full.sum(), raw=True)
# [舊版保留]             s_diff = (2 * wma_half) - wma_full
# [舊版保留]             return s_diff.rolling(sqrt_length).apply(lambda x: np.dot(x, weights_sqrt) / weights_sqrt.sum(), raw=True)
# [舊版保留]
# [舊版保留]         df['hma20'], df['hma50'] = calc_hma(df['c'], 20), calc_hma(df['c'], 50)
# [舊版保留]         hma20_val, hma50_val = df['hma20'].iloc[-1], df['hma50'].iloc[-1]
# [舊版保留]
# [舊版保留]         # 多軍 (Long) 邏輯
# [舊版保留]         cond_trend = hma20_val > hma50_val
# [舊版保留]
# [舊版保留]         # 趨勢強度濾網：計算 ADX (14)
# [舊版保留]         df['up'] = df['h'] - df['h'].shift(1)
# [舊版保留]         df['down'] = df['l'].shift(1) - df['l']
# [舊版保留]         df['+dm'] = np.where((df['up'] > df['down']) & (df['up'] > 0), df['up'], 0)
# [舊版保留]         df['-dm'] = np.where((df['down'] > df['up']) & (df['down'] > 0), df['down'], 0)
# [舊版保留]         df['tr'] = np.maximum(df['h'] - df['l'],
# [舊版保留]                               np.maximum(abs(df['h'] - df['c'].shift(1)), abs(df['l'] - df['c'].shift(1))))
# [舊版保留]
# [舊版保留]         atr_14 = df['tr'].ewm(alpha=1 / 14, adjust=False).mean()
# [舊版保留]         plus_di = 100 * (pd.Series(df['+dm']).ewm(alpha=1 / 14, adjust=False).mean() / atr_14)
# [舊版保留]         minus_di = 100 * (pd.Series(df['-dm']).ewm(alpha=1 / 14, adjust=False).mean() / atr_14)
# [舊版保留]         denominator = plus_di + minus_di
# [舊版保留]         dx = np.where(denominator != 0, 100 * abs(plus_di - minus_di) / denominator, 0)
# [舊版保留]         adx_val = pd.Series(dx).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
# [舊版保留]         cond_adx = adx_val > 22
# [舊版保留]
# [舊版保留]         # 成交量濾網 (抗極端值優化版)
# [舊版保留]         completed_v = df['v'].iloc[-2]
# [舊版保留]         median_v_24 = df['v'].iloc[-25:-1].median()
# [舊版保留]         target_vol = median_v_24 * 0.8
# [舊版保留]         cond_vol = completed_v > target_vol
# [舊版保留]
# [舊版保留]         tick_t = "✅" if cond_trend else "❌"
# [舊版保留]         tick_a = f"✅ (ADX: {adx_val:.1f})" if cond_adx else f"❌ (ADX: {adx_val:.1f})"
# [舊版保留]         tick_v = f"✅ (Vol: {completed_v:.0f} > 目標:{target_vol:.0f})" if cond_vol else f"❌ (Vol: {completed_v:.0f} < 目標:{target_vol:.0f})"
# [舊版保留]
# [舊版保留]         if cond_trend and cond_adx and cond_vol:
# [舊版保留]             status, signal = "🟢 GREEN   (Trend, ADX & Vol Validated)", 1
# [舊版保留]         elif cond_trend or cond_adx:
# [舊版保留]             status, signal = "🟡 YELLOW  (Standby - Waiting for confluence)", 0
# [舊版保留]         else:
# [舊版保留]             status, signal = "🔴 RED     (Sideways / Bearish)", -1
# [舊版保留]
# [舊版保留]         log_status_to_csv({
# [舊版保留]             'btc_price': round(curr_p, 2), 'target_price': round(hma50_val, 2),
# [舊版保留]             'hma20': round(hma20_val, 2), 'hma50': round(hma50_val, 2), 'adx': round(adx_val, 2),
# [舊版保留]             'signal_code': signal, 'decision_text': status
# [舊版保留]         })
# [舊版保留]
# [舊版保留]         print("-" * 60)
# [舊版保留]         print(f"📊 BTC 實時戰報 (HMA+ADX+Vol版) | 現價: {curr_p:.0f}")
# [舊版保留]         print(f"1️⃣ 極速趨勢: HMA20({hma20_val:.0f}) > HMA50({hma50_val:.0f}) {tick_t}")
# [舊版保留]         print(f"2️⃣ 趨勢強度: ADX > 22 {tick_a}")
# [舊版保留]         print(f"3️⃣ 動能確認: 上根已收盤量 > 24H中位數(80%) {tick_v}")
# [舊版保留]         print(f"🚦 最終決策: {status}")
# [舊版保留]         print("-" * 60)
# [舊版保留]
# [舊版保留]         return signal
# [舊版保留]     except Exception as e:
# [舊版保留]         print(f"⚠️ 導航故障: {e}")
# [舊版保留]         return 0

# ==========================================
# 🧠 [核心邏輯] 靈敏級 BTC 恆溫器 (1m/5m/15m 混合版) # 🚀 [V6.5 新增]
# ==========================================
def get_btc_regime_v6_5():
    """終極恆溫器：15m 趨勢 + 5m 衰竭 + 1m 制動"""
    try:
        # 1. 獲取不同時框數據
        o15 = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='15m', limit=150)
        o5 = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='5m', limit=150)
        o1 = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1m', limit=150)

        df15 = pd.DataFrame(o15, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df5 = pd.DataFrame(o5, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df1 = pd.DataFrame(o1, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        curr_p = df15['c'].iloc[-1]

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

        def calc_adx(df):
            df = df.copy()
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
            return pd.Series(dx).ewm(alpha=1 / 14, adjust=False).mean()

        # 計算 15m (大腦 - 入場許可)
        h15_20, h15_50 = calc_hma(df15['c'], 20), calc_hma(df15['c'], 50)
        h15_20_val, h15_50_val = h15_20.iloc[-1], h15_50.iloc[-1]
        adx15_series = calc_adx(df15)
        adx15_val = adx15_series.iloc[-1]

        # 計算 5m (眼睛 - 衰竭觀察)
        h5_20, h5_50 = calc_hma(df5['c'], 20), calc_hma(df5['c'], 50)
        adx5_series = calc_adx(df5)
        adx5_val, adx5_prev = adx5_series.iloc[-1], adx5_series.iloc[-2]

        # 計算 1m (手腳 - 極速制動)
        h1_20, h1_50 = calc_hma(df1['c'], 20), calc_hma(df1['c'], 50)

        # 🚨 [緊急制動邏輯] (Brake Status)
        brake = False
        brake_reason = ""
        if h1_20.iloc[-1] < h1_50.iloc[-1]:
            brake = True
            brake_reason = "1m HMA 死叉"
        elif adx5_val < adx5_prev and adx5_prev > 40:
            brake = True
            brake_reason = "5m ADX 高位回落"
        elif h5_20.iloc[-1] < h5_50.iloc[-1]:
            brake = True
            brake_reason = "5m HMA 死叉"

        # 15m 基礎條件
        cond_trend = h15_20_val > h15_50_val
        cond_adx = adx15_val > 22
        completed_v = df15['v'].iloc[-2]
        target_vol = df15['v'].iloc[-25:-1].median() * 0.8
        cond_vol = completed_v > target_vol

        # 🟢 [進場許可] (Entry Signal)
        if cond_trend and cond_adx and cond_vol and not brake:
            status, signal = "🟢 GREEN   (Trend, ADX & Vol Validated)", 1
        elif brake:
            status, signal = f"🔴 RED     (BRAKE TRIGGERED: {brake_reason})", -1
        elif cond_trend or cond_adx:
            status, signal = "🟡 YELLOW  (Standby - Waiting for confluence)", 0
        else:
            status, signal = "🔴 RED     (Sideways / Bearish)", -1

        log_status_to_csv({
            'btc_price': round(curr_p, 2), 'target_price': round(h15_50_val, 2),
            'hma20': round(h15_20_val, 2), 'hma50': round(h15_50_val, 2), 'adx': round(adx15_val, 2),
            'signal_code': signal, 'decision_text': status
        })

        print("-" * 60)
        print(f"🌡️ BTC 恆溫器戰報 (1m/5m/15m) | 現價: {curr_p:.0f}")
        print(f"1️⃣ 15m 趨勢: HMA20({h15_20_val:.0f}) > HMA50({h15_50_val:.0f}) {'✅' if cond_trend else '❌'}")
        print(f"2️⃣ 15m 動能: ADX > 22 {'✅' if cond_adx else '❌'} (值: {adx15_val:.1f})")
        print(f"3️⃣ 1m/5m 防護: {'🚨 觸發制動 (' + brake_reason + ')' if brake else '✅ 安全 (無制動)'}")
        print(f"🚦 最終決策: {status}")
        print("-" * 60)

        return {'signal': signal, 'brake': brake}
    except Exception as e:
        logger.error(f"⚠️ 恆溫器故障: {e}")
        return {'signal': 0, 'brake': True}


def scouting_strong_coins(scouting_coins=20):
    """動態大幣海選：於全市場 Top 20 流動性巨無霸中，尋找最強勢幣種"""
    try:
        tickers = exchange.fetch_tickers()
        data = []
        for s, t in tickers.items():
            if s.endswith(':USDT') and s not in BLACKLIST and t['percentage'] is not None:
                ask, bid = t.get('ask'), t.get('bid')
                if ask and bid and bid > 0:
                    spread = (ask - bid) / bid
                    if spread < 0.0010:  # 嚴格大幣門檻：差價必須 < 0.1%
                        data.append({'symbol': s, 'volume': t['quoteVolume'], 'change': t['percentage']})

        df = pd.DataFrame(data)
        if df.empty: return []

        top_majors = df.sort_values('volume', ascending=False).head(scouting_coins)
        # 於 Top 大幣中，選出升幅最大 (最強勢) 的 n 隻幣
        return top_majors.sort_values('change', ascending=False).head(scouting_coins)['symbol'].tolist()
    except Exception as e:
        print(f"⚠️ Scouting Error: {e}")
        return []


def check_flow_health(symbol):
    """【防守專用 - 多單版】資金流健康雷達：拋盤傾瀉(Dump)與升勢衰退(Deceleration)檢測"""
    try:
        trades = exchange.fetch_trades(symbol, limit=100)
        if not trades or len(trades) < 50: return None

        df = pd.DataFrame(trades)
        df['price_change'] = df['price'].diff()
        df['direction'] = np.where(df['price_change'] > 0, 1, np.where(df['price_change'] < 0, -1, 0))
        df['direction'] = df['direction'].replace(0, np.nan).ffill().fillna(0)

        avg_vol = df['amount'].mean()
        df['weight'] = np.where(df['amount'] > avg_vol * 2, 2.0, 1.0)
        df['net_flow'] = df['direction'] * df['amount'] * df['price'] * df['weight']

        flow_std = df['net_flow'].std()
        if flow_std == 0: return None

        flow_mean = df['net_flow'].mean()
        recent_25_flow = df['net_flow'].tail(25).sum()
        z_score = (recent_25_flow - (flow_mean * 25)) / (flow_std * np.sqrt(25))

        # 偵測極端狂暴賣盤 (Z-Score < -3.0) 才是多軍的威脅！
        if z_score < -3.0:
            return "Flow Reversal (Long Dump Detected)"

        # 動能衰退預判 (原本升緊，突然有大戶瘋狂賣出)
        flow_older_25 = df['net_flow'].iloc[-50:-25].sum()
        acceleration = recent_25_flow - flow_older_25
        accel_z = acceleration / (flow_std * np.sqrt(25))

        # 煞車轉向：加速度極強向下 (accel_z < -2.0) 且 當前資金流變為淨流出 (recent_25_flow < 0)
        if accel_z < -2.0 and recent_25_flow < 0:
            try:
                ob = exchange.fetch_order_book(symbol, limit=20)
                bids_vol = sum([b[1] for b in ob['bids']])
                asks_vol = sum([a[1] for a in ob['asks']])
                imbalance = (bids_vol - asks_vol) / (bids_vol + asks_vol) if (bids_vol + asks_vol) > 0 else 0

                if imbalance < -0.15:  # 賣盤極厚，確認升勢已死
                    return "Flow Deceleration (Momentum Died)"
            except:
                pass

        return None
    except Exception as e:
        return None


def apply_lee_ready_long_logic(symbol):
    """正向 Lee-Ready 狙擊模式 (含大單加權、加速度與防假突破陷阱)"""
    try:
        trades = exchange.fetch_trades(symbol, limit=200)
        if not trades: return 0, 0, False

        df = pd.DataFrame(trades)
        df['price_change'] = df['price'].diff()
        df['direction'] = np.where(df['price_change'] > 0, 1, np.where(df['price_change'] < 0, -1, 0))
        df['direction'] = df['direction'].replace(0, np.nan).ffill().fillna(0)

        avg_vol = df['amount'].mean()
        df['weight'] = np.where(df['amount'] > avg_vol * 2, 2.0, 1.0)
        df['net_flow'] = df['direction'] * df['amount'] * df['price'] * df['weight']

        short_window_flow = df['net_flow'].tail(50).sum()
        acceleration = df['net_flow'].tail(25).sum() - df['net_flow'].iloc[-50:-25].sum()

        try:
            ob = exchange.fetch_order_book(symbol, limit=20)
            bids_vol = sum([b[1] for b in ob['bids']])
            asks_vol = sum([a[1] for a in ob['asks']])
            imbalance = (bids_vol - asks_vol) / (bids_vol + asks_vol) if (bids_vol + asks_vol) > 0 else 0
        except:
            imbalance = 0

        is_strong = False
        if df['net_flow'].std() > 0:
            z_score = short_window_flow / (df['net_flow'].std() * np.sqrt(50))
        else:
            z_score = 0

        # ✅ 做多條件：淨買壓、加速度向上、買盤厚於賣盤
        if (short_window_flow > 0) and (acceleration > 0) and (imbalance > 0.15):
            is_strong = True
            print(f"🔥 {symbol} Long Sniper! Accel: {acceleration:.0f} | Imbalance: {imbalance:.2f}")
        elif z_score > NET_FLOW_SIGMA:
            is_strong = True
            print(f"📈 {symbol} Long Z-Score Validated: {z_score:.2f}")

        # 防假突破 (Fake Breakout) - 若升得急但賣盤極厚，取消做多
        if is_strong and imbalance < -0.1:
            is_strong = False
            print(f"⚠️ {symbol} 發現假突破陷阱！賣盤極厚，取消做多！")

        return short_window_flow, df['price'].iloc[-1], is_strong
    except Exception as e:
        print(f"⚠️ LR Logic Error [{symbol}]: {e}")
        return 0, 0, False


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

            # 只恢復多單 (Long)
            if side in ['long', 'buy'] or info_side in ['buy', 'long']:

                entry_price = float(p.get('entryPrice', 0))
                amount = float(p.get('contracts', 0) or p.get('size', 0))

                sl_p, tp_p = float(p.get('stopLoss', 0)), float(p.get('takeProfit', 0))
                atr, _ = get_market_metrics(symbol)
                if not atr: atr = entry_price * 0.01

                # ✅ 多單 SL 在下方，TP 在上方
                if sl_p == 0: sl_p = float(exchange.price_to_precision(symbol, entry_price - (SL_ATR_MULT * atr)))
                if tp_p == 0: tp_p = float(exchange.price_to_precision(symbol, entry_price + (TP_ATR_MULT * atr)))
                # ✅ 多單保本 = SL 已移到入場價上方
                is_be = True if (sl_p > entry_price and sl_p > 0) else False

                positions[symbol] = {
                    'amount': amount, 'entry_price': entry_price, 'tp_price': tp_p, 'sl_price': sl_p,
                    'is_breakeven': is_be, 'atr': atr, 'max_pnl_pct': 0.0,
                    'entry_time': time.time()
                }
                recovered_count += 1
                print(f"✅ 成功尋回孤兒多單: {symbol} | 入場價: {entry_price} | 已保本狀態: {is_be}")

        print(f"🔄 同步完成！共尋回 {recovered_count} 個倉位。")
    except Exception as e:
        logger.error(f"❌ 啟動同步失敗: {e}")


# def manage_long_positions(): # [舊版保留]
def manage_long_positions(regime=None):  # 🚀 [V6.5 修改] 加入 regime 參數，控制緊急制動
    """管理在途多單 (Native Exit 檢查、Trail SL 更新、回撤鎖利、動態孤兒接管)"""
    try:
        live_positions_raw = exchange.fetch_positions(params={'category': 'linear'})
        live_symbols = {p['symbol']: p for p in live_positions_raw if
                        float(p.get('contracts', 0) or p.get('size', 0)) > 0}

        for s, p in live_symbols.items():
            if s not in positions:
                side = p.get('side', '').lower()
                info_side = p.get('info', {}).get('side', '').lower()

                # ✅ 接管多單
                if side in ['long', 'buy'] or info_side in ['buy', 'long']:
                    entry_p = float(p.get('entryPrice', 0))
                    amt = float(p.get('contracts', 0) or p.get('size', 0))
                    atr, _ = get_market_metrics(s)
                    if not atr: atr = entry_p * 0.01

                    real_entry_time_ms = float(p.get('createdTime') or (time.time() * 1000))
                    real_entry_time = real_entry_time_ms / 1000.0

                    sl_p = float(p.get('stopLoss') or 0)
                    tp_p = float(p.get('takeProfit') or 0)

                    # ✅ 多單 SL 在下方，TP 在上方
                    if sl_p == 0: sl_p = float(exchange.price_to_precision(s, entry_p - (SL_ATR_MULT * atr)))
                    if tp_p == 0: tp_p = float(exchange.price_to_precision(s, entry_p + (TP_ATR_MULT * atr)))
                    # ✅ 多單保本 = SL 已移到入場價上方
                    is_be = True if (sl_p > entry_p and sl_p > 0) else False

                    positions[s] = {
                        'amount': amt, 'entry_price': entry_p, 'tp_price': tp_p, 'sl_price': sl_p,
                        'is_breakeven': is_be, 'atr': atr, 'max_pnl_pct': 0.0,
                        'entry_time': real_entry_time
                    }
                    print(f"🚨 [系統自癒] 發現並自動接管孤兒多單: {s} | 入場價: {entry_p} | 數量: {amt}")

        for s in list(positions.keys()):
            if s not in live_symbols:
                print(f"🧹 交易所已自動平倉，處理真實 PnL 結算單: {s}")
                real_pnl = process_native_exit_log(s, positions[s], position_type='long')
                cancel_all_v5(s)

                handle_trade_result(s, real_pnl)
                del positions[s]
                continue

        for s in list(positions.keys()):
            curr_p, pos = exchange.fetch_ticker(s)['last'], positions[s]
            # ✅ 做多是升才賺錢！
            pnl_pct = (curr_p - pos['entry_price']) / pos['entry_price']

            coin_volatility_pct = pos['atr'] / pos['entry_price']
            sl_updated = False

            if 'max_pnl_pct' not in pos: pos['max_pnl_pct'] = pnl_pct
            pos['max_pnl_pct'] = max(pos['max_pnl_pct'], pnl_pct)

            # 階段一 & 二：爬升期推保本
            # ✅ 做多保本：SL 移到入場價上方 1.002 鎖住 0.2% 獲利
            if not pos['is_breakeven'] and pnl_pct > (coin_volatility_pct * 2.0):
                pos['sl_price'], pos['is_breakeven'], sl_updated = pos['entry_price'] * 1.002, True, True

            # ==========================================
            # 🚀 階段三：AI 視覺變速追蹤止損 (Long 版 - 結合 Net Flow 與利潤深度)
            # ==========================================
            if pos['is_breakeven']:
                # [舊版保留] # 👑 終極防護：如果已經有 2.5 ATR 利潤，且雷達發現「莊家收油」
                # [舊版保留] if pos.get('deceleration_detected', False) and pnl_pct > (coin_volatility_pct * 2.5):
                # [舊版保留]     trail_sl = curr_p - (0.5 * pos['atr'])  # ✅ 減號：SL 在現價下方極限貼身防守！
                # [舊版保留]
                # [舊版保留] # 正常深水區：賺超過 5 ATR，大貪變小貪
                # [舊版保留] elif pnl_pct > (coin_volatility_pct * 5.0):
                # [舊版保留]     trail_sl = curr_p - (0.8 * pos['atr'])
                # [舊版保留]
                # [舊版保留] # 正常發展區：賺超過 3.5 ATR
                # [舊版保留] elif pnl_pct > (coin_volatility_pct * 3.5):
                # [舊版保留]     trail_sl = curr_p - (1.2 * pos['atr'])
                # [舊版保留]
                # [舊版保留] # 剛過保本區：俾空間佢震盪
                # [舊版保留] else:
                # [舊版保留]     trail_sl = curr_p - (1.8 * pos['atr'])
                # 🚀 [V6.5 新增] 加入恆溫器緊急制動 (Thermostat Brake)
                if regime and regime.get('brake', False):
                    trail_sl = curr_p - (0.3 * pos['atr'])  # 🚀 BTC 翻臉，極限鎖死至 0.3 ATR！
                elif pos.get('deceleration_detected', False) and pnl_pct > (coin_volatility_pct * 2.5):
                    trail_sl = curr_p - (0.5 * pos['atr'])  # ✅ 減號：SL 在現價下方極限貼身防守！
                elif pnl_pct > (coin_volatility_pct * 5.0):
                    trail_sl = curr_p - (0.8 * pos['atr'])
                elif pnl_pct > (coin_volatility_pct * 3.5):
                    trail_sl = curr_p - (1.2 * pos['atr'])
                else:
                    trail_sl = curr_p - (1.8 * pos['atr'])

                # ✅ 確保多軍風箏線只准向上移 (新止損 > 舊止損)
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

            # ==========================================
            # 🛠️ 雙重 Timeout 終極機制
            # ==========================================
            if not exit_reason:
                # 🔪 條件 B (喪失動能/變死水)：持倉 > 45 分鐘 (2700秒) 且利潤極度微薄 (< 0.5%)
                if time_held > 2700 and pnl_pct < 0.005:
                    exit_reason = "Momentum Timeout (Stalled Zombie)"
            # ==========================================

            # ==========================================
            # 🛡️ 3. 資金流健康雷達 (結合 Claude 建議：只標記，不盲目平倉)
            # ==========================================
            curr_t = time.time()
            last_check = pos.get('last_flow_check', 0)

            if not exit_reason and (curr_t - last_check > 15):
                pos['last_flow_check'] = curr_t

                # 俾 120 秒時間避開開倉第一分鐘嘅極端雜訊
                if time_held > 120:
                    flow_status = check_flow_health(s)

                    if flow_status == "Flow Reversal (Long Dump Detected)":
                        # 極端拋盤 (Z < -3.0)，呢個真係要即刻逃生
                        exit_reason = flow_status

                    elif flow_status == "Flow Deceleration (Momentum Died)":
                        # 🚀 核心改動：莊家收油，唔好即刻平倉！
                        # 標記呢張單，通知上面嘅 Trail Stop 系統進入「極限作戰狀態」
                        if not pos.get('deceleration_detected', False):
                            pos['deceleration_detected'] = True
                            print(
                                f"⚠️ {s} 偵測到高位收油 (Deceleration)！已啟動極限防禦標記，若利潤充足將自動收緊至 0.5 ATR！")

            # 常規本地 TP/SL 檢查
            if not exit_reason:
                # ✅ 做多：升穿止盈，跌穿止損
                if curr_p >= pos['tp_price']:
                    exit_reason = "TP (Long IOC Exit)"
                elif curr_p <= pos['sl_price']:
                    exit_reason = "Trail SL (Long IOC Exit)" if pos['is_breakeven'] else "SL (Long IOC Exit)"

            # 執行本地主動平倉 (IOC)
            if exit_reason:
                print(
                    f"⚔️ 觸發 {exit_reason}，執行 IOC 平單: {s} | 持倉: {time_held / 60:.1f}分鐘 | Max PnL: {pos['max_pnl_pct'] * 100:.2f}% | 現盈虧: {pnl_pct * 100:.2f}%")

                # ✅ 做多平倉：取 bids 價賣出
                ioc_price = get_3_layer_avg_price(s, 'bids') or curr_p
                try:
                    exchange.create_order(s, 'limit', 'sell', pos['amount'], ioc_price,
                                          {'timeInForce': 'IOC', 'reduceOnly': True})
                except:
                    exchange.create_market_sell_order(s, pos['amount'], {'reduceOnly': True})

                # ✅ 做多利潤 = (離場價 - 入場價) * 數量
                ioc_pnl = round((ioc_price - pos['entry_price']) * pos['amount'], 4)

                log_to_csv({'symbol': s, 'action': 'LONG_EXIT', 'price': curr_p, 'amount': pos['amount'],
                            'reason': exit_reason, 'realized_pnl': ioc_pnl})

                cancel_all_v5(s)

                handle_trade_result(s, ioc_pnl)
                del positions[s]

    except Exception as e:
        if "10006" in str(e): time.sleep(5)


def execute_live_long(symbol, net_flow, current_price, is_strong, atr, is_volatile):
    """計算倉位並執行多單入場"""
    if symbol in cooldown_tracker:
        if time.time() < cooldown_tracker[symbol]:
            return
        else:
            del cooldown_tracker[symbol]

    if atr is None or atr == 0 or current_price == 0: return

    if not (is_strong and is_volatile and symbol not in positions): return

    cancel_all_v5(symbol)
    actual_bal = get_live_usdt_balance()
    eff_bal = min(WORKING_CAPITAL, actual_bal)

    trade_val = min((eff_bal * RISK_PER_TRADE) / ((SL_ATR_MULT * atr) / current_price), eff_bal * MAX_LEVERAGE * 0.95,
                    MAX_NOTIONAL_PER_TRADE)
    amount = float(exchange.amount_to_precision(symbol, trade_val / current_price))

    if amount < exchange.markets[symbol]['limits']['amount']['min']: return

    # ✅ 做多 IOC 入場取 asks 價
    ioc_p = get_3_layer_avg_price(symbol, 'asks') or current_price
    if amount * ioc_p < MIN_NOTIONAL: return

    try:
        exchange.set_leverage(int(MAX_LEVERAGE), symbol)
    except Exception as e:
        if "110043" not in str(e):
            if "110026" in str(e): return
            logger.warning(f"⚠️ {symbol} 槓桿異常: {e}")

    try:
        # ✅ 執行多單開倉
        order = exchange.create_order(symbol, 'limit', 'buy', amount, ioc_p, {'timeInForce': 'IOC', 'positionIdx': 0})
        time.sleep(1)

        actual_price, actual_amount = ioc_p, 0

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

        # ✅ 做多：TP 在上方，SL 在下方
        tp_p = float(exchange.price_to_precision(symbol, actual_price + (TP_ATR_MULT * atr)))
        sl_p = float(exchange.price_to_precision(symbol, actual_price - (SL_ATR_MULT * atr)))

        # ✅ 做多利潤空間防護
        expected_profit_margin = (tp_p - actual_price) / actual_price
        if expected_profit_margin < 0.003:
            print(f"🟡 放棄做多 [{symbol}]: 預期利潤空間 ({expected_profit_margin * 100:.2f}%) 太細，立即市價平倉！")
            try:
                exchange.create_market_sell_order(symbol, actual_amount, {'reduceOnly': True})
            except Exception as e:
                logger.error(f"❌ 緊急平倉失敗！需人工介入: {e}")
            cancel_all_v5(symbol)
            return

        try:
            exchange.private_post_v5_position_trading_stop({
                'category': 'linear', 'symbol': exchange.market_id(symbol), 'stopLoss': str(sl_p),
                'takeProfit': str(tp_p), 'tpslMode': 'Full', 'positionIdx': 0
            })
            print(f"✅ {symbol} 止盈止損已設置 | TP: {tp_p} | SL: {sl_p}")
        except Exception as e:
            logger.warning(f"⚠️ {symbol} 止盈止損設置異常 (不影響本地追蹤): {e}")

        positions[symbol] = {
            'amount': actual_amount, 'entry_price': actual_price, 'tp_price': tp_p, 'sl_price': sl_p,
            'is_breakeven': False, 'atr': atr, 'max_pnl_pct': 0.0,
            'entry_time': time.time()
        }
        cooldown_tracker[symbol] = time.time() + 480

        # 🚀 入倉成功後儲存冷卻時間到 JSON，防斷線遺失
        save_dynamic_blacklist()

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
    print(f"🚀 AI 實戰 V6.4 FINAL LONG (動態學習防護版) 啟動...")
    print(f"Lee-Ready 資金流 + 訂單簿失衡度 + AI 變速 Trail SL + 動態 JSON 黑名單 [終極做多版] 初始化中...")

    # 1. 🚀 啟動時先讀取 JSON 還原黑名單與連輸記憶
    load_dynamic_blacklist()

    # 2. 同步遺留倉位
    sync_positions_on_startup()

    last_scout_time = 0
    target_coins = []

    while True:
        try:
            # regime = get_btc_regime() # [舊版保留]
            regime = get_btc_regime_v6_5()  # 🚀 [V6.5 修改] 獲取恆溫器狀態字典

            # manage_long_positions() # [舊版保留]
            manage_long_positions(regime)  # 🚀 [V6.5 修改] 傳入恆溫器狀態進行動態鎖利

            curr_t = time.time()

            if curr_t - last_scout_time > SCOUTING_INTERVAL:
                # if regime == 1: # [舊版保留]
                if regime['signal'] == 1:  # 🚀 [V6.5 修改] 判斷字典內的 signal
                    print("🟢 綠燈確認：執行多單大幣海選掃描...")

                    for s in target_coins:
                        try:
                            flow, last_p, is_strong = apply_lee_ready_long_logic(s)
                            atr, is_v = get_market_metrics(s)
                            if last_p > 0:
                                execute_live_long(s, flow, last_p, is_strong, atr, is_v)
                        except Exception as e:
                            continue
                        time.sleep(0.5)
                else:
                    # print(f"🚦 目前導航狀態為 {regime}，海選暫停。") # [舊版保留]
                    brake_status = '🚨 觸發緊急制動' if regime.get('brake') else '🟡 等待條件'
                    print(f"🚦 恆溫器攔截中 ({brake_status})，海選暫停。")  # 🚀 [V6.5 修改]

                last_scout_time = curr_t
                print(f"⏳ 多軍巡邏完畢 | 持倉: {list(positions.keys())} | 餘額: {get_live_usdt_balance():.2f}")

            time.sleep(POSITION_CHECK_INTERVAL)

        except KeyboardInterrupt:
            print(f"\n👋 指揮官手動終止。餘額: {get_live_usdt_balance():.2f} USDT | 持倉: {list(positions.keys())}")
            sys.exit(0)
        except Exception as e:
            logger.error(f"❌ 主迴圈發生未知錯誤: {e}")
            time.sleep(30 if "10006" in str(e) else 10)


if __name__ == "__main__":
    main()