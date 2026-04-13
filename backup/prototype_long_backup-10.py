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
logger = logging.getLogger('AlgoTrade_Long_V6.6_Thermostat')  # 🚀 [V6.6 修改] 版本號更新

# Name: yamato
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
RISK_PER_TRADE = 0.005         # 🛡️ 縮細注碼：0.5% 風險
MIN_NOTIONAL = 5.0
MAX_NOTIONAL_PER_TRADE = 200.0

NET_FLOW_SIGMA = 1.2
# 🚀 [V6.6 修改] TP_ATR_MULT 維持 5.0，給予足夠空間騎趨勢
# 🚀 [V6.6 修改] SL_ATR_MULT 從 0.8 → 1.2：原 0.8 過窄，在正常 0.5-1% tick 噪聲下必然被掃。
#    實戰中 5m ATR 代表平均蠟燭波幅，初始 SL 應給予至少 1~1.5x ATR 空間以存活噪聲。
#    提高至 1.2 可顯著減少 Whipsaw Native SL，代價是每手最大虧損輕微增加，但 R:R 仍維持 4:1 以上。
TP_ATR_MULT = 5.0
SL_ATR_MULT = 1.2              # * 核心修改：0.8 → 1.2，減少 Whipsaw SL 觸發

MAX_CONSECUTIVE_LOSSES = 3     # 連輸 3 次封禁
DYNAMIC_BAN_DURATION = 86400   # 封禁 24 小時

SCOUTING_INTERVAL = 125
POSITION_CHECK_INTERVAL = 4    # 4秒極速貼盤巡邏

# 🚀 [V6.6 修改] 恆溫器敏感度設定：分離「入場攔截」與「出場收緊」兩個影響
# 問題根源：原版 1m HMA 死叉即觸發 global brake，冰封所有入場且逼倉。
# 1m HMA 死叉在正常趨勢延續中每幾分鐘就會出現，造成過度攔截。
# 新策略：1m 信號只在「5m 也同步轉弱」時才觸發完整 BRAKE；單獨 1m 死叉僅觸發 SOFT_BRAKE (不攔截入場，但收緊持倉 SL)。
BRAKE_ADX_HIGH_THRESHOLD = 40  # 5m ADX 高位閾值 (原值，保持合理)
BRAKE_ADX_DROP_CONFIRM = True  # 5m ADX 需「高位」後才算回落觸發
TIMEOUT_SECONDS = 2700         # 45分鐘殭屍超時 (見後文分析，此值可接受)

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
        return atr, (atr / df['c'].iloc[-1]) > 0.0015
    except:
        return None, False


# ==========================================
# 🛠️ [輔助模組] JSON 記憶與動態黑名單
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


# ==========================================
# 🧠 [核心邏輯] 靈敏級 BTC 恆溫器 (1m/5m/15m 混合版) V6.6
# ==========================================
def get_btc_regime_v6_5():
    """
    終極恆溫器：15m 趨勢 + 5m 衰竭 + 1m 制動

    🚀 [V6.6 修改] 核心重構：引入 SOFT_BRAKE 與 HARD_BRAKE 分層制動。
    ─────────────────────────────────────────────────────────────────
    原版問題：
      1. 1m HMA 死叉 → 立即 BRAKE，凍結所有入場 + 強制收緊所有 Trail SL 至 0.3 ATR。
         1m HMA 在趨勢行情中每幾分鐘就來回交叉，造成大量「攔截後立刻反彈」的假剎車。
      2. BRAKE 期間每 125 秒重複打印相同訊息，佔用 log 難以閱讀。

    新邏輯 (SOFT vs HARD)：
      SOFT_BRAKE  = 只有 1m 翻陰，但 5m 趨勢仍健康
                  → 不攔截新入場，但持倉 Trail SL 收緊一級 (1.5→1.0 ATR 等)
      HARD_BRAKE  = 1m 翻陰 且 5m HMA 死叉 / 5m ADX 高位回落
                  → 完整 BRAKE：凍結入場 + Trail SL 收緊至 0.3 ATR (原行為)
      brake_1m_only = 純 1m 死叉，但 5m 健康 → SOFT only
    ─────────────────────────────────────────────────────────────────
    """
    try:
        # 1. 獲取不同時框數據
        o15 = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='15m', limit=150)
        o5  = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='5m',  limit=150)
        o1  = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1m',  limit=150)

        df15 = pd.DataFrame(o15, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df5  = pd.DataFrame(o5,  columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df1  = pd.DataFrame(o1,  columns=['ts', 'o', 'h', 'l', 'c', 'v'])
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
            df['up']   = df['h'] - df['h'].shift(1)
            df['down'] = df['l'].shift(1) - df['l']
            df['+dm']  = np.where((df['up'] > df['down']) & (df['up'] > 0), df['up'], 0)
            df['-dm']  = np.where((df['down'] > df['up']) & (df['down'] > 0), df['down'], 0)
            df['tr']   = np.maximum(df['h'] - df['l'],
                                    np.maximum(abs(df['h'] - df['c'].shift(1)), abs(df['l'] - df['c'].shift(1))))
            atr_14   = df['tr'].ewm(alpha=1/14, adjust=False).mean()
            plus_di  = 100 * (pd.Series(df['+dm']).ewm(alpha=1/14, adjust=False).mean() / atr_14)
            minus_di = 100 * (pd.Series(df['-dm']).ewm(alpha=1/14, adjust=False).mean() / atr_14)
            denominator = plus_di + minus_di
            dx = np.where(denominator != 0, 100 * abs(plus_di - minus_di) / denominator, 0)
            return pd.Series(dx).ewm(alpha=1/14, adjust=False).mean()

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
        h1_dead_cross = h1_20.iloc[-1] < h1_50.iloc[-1]

        # 🚀 [V6.6 修改] 分層制動邏輯
        # HARD_BRAKE = 完整制動 (凍結入場 + 極限收緊 SL)
        # SOFT_BRAKE = 輕度警告 (不凍結入場，但收緊持倉 SL 一級)
        hard_brake = False
        soft_brake = False
        brake_reason = ""

        # 5m 衰竭條件 (獨立評估)
        h5_dead_cross = h5_20.iloc[-1] < h5_50.iloc[-1]
        adx5_high_drop = (adx5_val < adx5_prev) and (adx5_prev > BRAKE_ADX_HIGH_THRESHOLD)

        if h1_dead_cross:
            if h5_dead_cross:
                hard_brake = True
                brake_reason = "1m+5m HMA 雙重死叉"  # * 雙重確認才觸發完整制動
            elif adx5_high_drop:
                hard_brake = True
                brake_reason = "1m 死叉 + 5m ADX 高位回落"  # * 5m 動能正在衰竭，確認
            else:
                soft_brake = True
                brake_reason = "1m HMA 死叉 (5m 仍健康，輕度警戒)"  # * 單獨 1m 訊號，僅軟剎車
        elif h5_dead_cross:
            hard_brake = True
            brake_reason = "5m HMA 死叉"  # * 5m 死叉獨立觸發完整制動
        elif adx5_high_drop:
            soft_brake = True
            brake_reason = "5m ADX 高位回落 (無死叉，輕度警戒)"  # * ADX 回落但未死叉，輕度

        # 最終 brake 狀態向外暴露 (兼容原接口)
        brake = hard_brake  # HARD_BRAKE 才觸發原 brake 行為

        # 15m 基礎條件
        cond_trend = h15_20_val > h15_50_val
        cond_adx   = adx15_val > 22
        completed_v = df15['v'].iloc[-2]
        target_vol  = df15['v'].iloc[-25:-1].median() * 0.8
        cond_vol    = completed_v > target_vol

        # 🟢 [進場許可] - SOFT_BRAKE 不阻止入場
        if cond_trend and cond_adx and cond_vol and not hard_brake:
            status, signal = "🟢 GREEN   (Trend, ADX & Vol Validated)", 1
        elif hard_brake:
            status, signal = f"🔴 RED     (HARD BRAKE: {brake_reason})", -1
        elif soft_brake:
            # * SOFT_BRAKE：入場仍允許 (signal=1)，但外部邏輯會收緊 Trail SL
            status, signal = f"🟡 YELLOW  (SOFT BRAKE: {brake_reason})", 1
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

        # 🚀 [V6.6 修改] 精簡 brake 狀態顯示
        if hard_brake:
            print(f"3️⃣ 制動狀態: 🚨 HARD BRAKE ({brake_reason})")
        elif soft_brake:
            print(f"3️⃣ 制動狀態: ⚠️  SOFT BRAKE ({brake_reason})")
        else:
            print(f"3️⃣ 制動狀態: ✅ 安全 (無制動)")

        print(f"🚦 最終決策: {status}")
        print("-" * 60)

        return {'signal': signal, 'brake': hard_brake, 'soft_brake': soft_brake, 'brake_reason': brake_reason}

    except Exception as e:
        logger.error(f"⚠️ 恆溫器故障: {e}")
        return {'signal': 0, 'brake': True, 'soft_brake': False, 'brake_reason': 'Error'}


# ==========================================
# 📡 [市場掃描] 大幣海選
# ==========================================
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
                    if spread < 0.0010:
                        data.append({'symbol': s, 'volume': t['quoteVolume'], 'change': t['percentage']})

        df = pd.DataFrame(data)
        if df.empty: return []

        top_majors = df.sort_values('volume', ascending=False).head(scouting_coins)
        return top_majors.sort_values('change', ascending=False).head(scouting_coins)['symbol'].tolist()
    except Exception as e:
        print(f"⚠️ Scouting Error: {e}")
        return []


# ==========================================
# 🔍 [Lee-Ready 引擎] 資金流分析
# ==========================================
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
        df['weight']   = np.where(df['amount'] > avg_vol * 2, 2.0, 1.0)
        df['net_flow'] = df['direction'] * df['amount'] * df['price'] * df['weight']

        flow_std = df['net_flow'].std()
        if flow_std == 0: return None

        flow_mean        = df['net_flow'].mean()
        recent_25_flow   = df['net_flow'].tail(25).sum()
        z_score          = (recent_25_flow - (flow_mean * 25)) / (flow_std * np.sqrt(25))

        if z_score < -3.0:
            return "Flow Reversal (Long Dump Detected)"

        flow_older_25 = df['net_flow'].iloc[-50:-25].sum()
        acceleration  = recent_25_flow - flow_older_25
        accel_z       = acceleration / (flow_std * np.sqrt(25))

        if accel_z < -2.0 and recent_25_flow < 0:
            try:
                ob        = exchange.fetch_order_book(symbol, limit=20)
                bids_vol  = sum([b[1] for b in ob['bids']])
                asks_vol  = sum([a[1] for a in ob['asks']])
                imbalance = (bids_vol - asks_vol) / (bids_vol + asks_vol) if (bids_vol + asks_vol) > 0 else 0

                if imbalance < -0.15:
                    return "Flow Deceleration (Momentum Died)"
            except:
                pass

        return None
    except:
        return None


def apply_lee_ready_long_logic(symbol):
    """正向 Lee-Ready 狙擊模式"""
    try:
        trades = exchange.fetch_trades(symbol, limit=200)
        if not trades: return 0, 0, False

        df = pd.DataFrame(trades)
        df['price_change'] = df['price'].diff()
        df['direction']    = np.where(df['price_change'] > 0, 1, np.where(df['price_change'] < 0, -1, 0))
        df['direction']    = df['direction'].replace(0, np.nan).ffill().fillna(0)

        avg_vol       = df['amount'].mean()
        df['weight']  = np.where(df['amount'] > avg_vol * 2, 2.0, 1.0)
        df['net_flow'] = df['direction'] * df['amount'] * df['price'] * df['weight']

        short_window_flow = df['net_flow'].tail(50).sum()
        acceleration      = df['net_flow'].tail(25).sum() - df['net_flow'].iloc[-50:-25].sum()

        try:
            ob        = exchange.fetch_order_book(symbol, limit=20)
            bids_vol  = sum([b[1] for b in ob['bids']])
            asks_vol  = sum([a[1] for a in ob['asks']])
            imbalance = (bids_vol - asks_vol) / (bids_vol + asks_vol) if (bids_vol + asks_vol) > 0 else 0
        except:
            imbalance = 0

        is_strong = False
        if df['net_flow'].std() > 0:
            z_score = short_window_flow / (df['net_flow'].std() * np.sqrt(50))
        else:
            z_score = 0

        if (short_window_flow > 0) and (acceleration > 0) and (imbalance > 0.15):
            is_strong = True
            print(f"🔥 {symbol} Long Sniper! Accel: {acceleration:.0f} | Imbalance: {imbalance:.2f}")
        elif z_score > NET_FLOW_SIGMA:
            is_strong = True
            print(f"📈 {symbol} Long Z-Score Validated: {z_score:.2f}")

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
    print("🔄 正在同步交易所現有倉位...")
    try:
        live_positions_raw = exchange.fetch_positions()
        live_symbols = [p for p in live_positions_raw if float(p.get('contracts', 0) or p.get('size', 0)) > 0]

        recovered_count = 0
        for p in live_symbols:
            symbol    = p['symbol']
            side      = p.get('side', '').lower()
            info_side = p.get('info', {}).get('side', '').lower()

            if side in ['long', 'buy'] or info_side in ['buy', 'long']:
                entry_price = float(p.get('entryPrice', 0))
                amount      = float(p.get('contracts', 0) or p.get('size', 0))

                sl_p, tp_p = float(p.get('stopLoss', 0)), float(p.get('takeProfit', 0))
                atr, _ = get_market_metrics(symbol)
                if not atr: atr = entry_price * 0.01

                if sl_p == 0: sl_p = float(exchange.price_to_precision(symbol, entry_price - (SL_ATR_MULT * atr)))
                if tp_p == 0: tp_p = float(exchange.price_to_precision(symbol, entry_price + (TP_ATR_MULT * atr)))
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


def manage_long_positions(regime=None):
    """
    管理在途多單 (包含恆溫器緊急制動)

    🚀 [V6.6 修改] Trail SL 分層邏輯更新：
      - HARD_BRAKE → 收緊至 0.3 ATR (原行為保留，針對真實危機)
      - SOFT_BRAKE → 收緊至 0.6 ATR (新增：輕度警戒但不過激)
      - Deceleration → 收緊至 0.5 ATR (原行為保留)
      - 正常盈利 ≥ 5x ATR% → 0.8 ATR
      - 正常盈利 ≥ 3.5x ATR% → 1.2 ATR
      - 正常保本後 → 1.8 ATR
    """
    try:
        live_positions_raw = exchange.fetch_positions(params={'category': 'linear'})
        live_symbols = {p['symbol']: p for p in live_positions_raw if
                        float(p.get('contracts', 0) or p.get('size', 0)) > 0}

        for s, p in live_symbols.items():
            if s not in positions:
                side      = p.get('side', '').lower()
                info_side = p.get('info', {}).get('side', '').lower()

                if side in ['long', 'buy'] or info_side in ['buy', 'long']:
                    entry_p = float(p.get('entryPrice', 0))
                    amt     = float(p.get('contracts', 0) or p.get('size', 0))
                    atr, _  = get_market_metrics(s)
                    if not atr: atr = entry_p * 0.01

                    real_entry_time_ms = float(p.get('createdTime') or (time.time() * 1000))
                    real_entry_time    = real_entry_time_ms / 1000.0

                    sl_p = float(p.get('stopLoss') or 0)
                    tp_p = float(p.get('takeProfit') or 0)

                    if sl_p == 0: sl_p = float(exchange.price_to_precision(s, entry_p - (SL_ATR_MULT * atr)))
                    if tp_p == 0: tp_p = float(exchange.price_to_precision(s, entry_p + (TP_ATR_MULT * atr)))
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
            try:
                curr_p, pos = exchange.fetch_ticker(s)['last'], positions[s]
                pnl_pct     = (curr_p - pos['entry_price']) / pos['entry_price']

                coin_volatility_pct = pos['atr'] / pos['entry_price']
                sl_updated = False

                if 'max_pnl_pct' not in pos: pos['max_pnl_pct'] = pnl_pct
                pos['max_pnl_pct'] = max(pos['max_pnl_pct'], pnl_pct)

                # ── 保本觸發 ──
                if not pos['is_breakeven'] and pnl_pct > (coin_volatility_pct * 2.0):
                    pos['sl_price'], pos['is_breakeven'], sl_updated = pos['entry_price'] * 1.002, True, True

                if pos['is_breakeven']:
                    # 🚀 [V6.6 修改] 分層 Trail SL：HARD → SOFT → Decel → 正常梯度
                    if regime and regime.get('brake', False):
                        trail_sl = curr_p - (0.3 * pos['atr'])   # 🚨 HARD BRAKE：極限鎖死
                    elif regime and regime.get('soft_brake', False):
                        trail_sl = curr_p - (0.6 * pos['atr'])   # * SOFT BRAKE：適度收緊，不過激
                    elif pos.get('deceleration_detected', False) and pnl_pct > (coin_volatility_pct * 2.5):
                        trail_sl = curr_p - (0.5 * pos['atr'])   # ✅ 高位收油：貼身防守
                    elif pnl_pct > (coin_volatility_pct * 5.0):
                        trail_sl = curr_p - (0.8 * pos['atr'])
                    elif pnl_pct > (coin_volatility_pct * 3.5):
                        trail_sl = curr_p - (1.2 * pos['atr'])
                    else:
                        trail_sl = curr_p - (1.8 * pos['atr'])

                    if trail_sl > pos['sl_price']:
                        if (trail_sl - pos['sl_price']) / pos['sl_price'] > 0.0005:
                            sl_updated = True
                            pos['sl_price'] = trail_sl

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
                time_held   = time.time() - pos.get('entry_time', time.time())

                # ── 殭屍超時退出 ──
                if not exit_reason:
                    if time_held > TIMEOUT_SECONDS and pnl_pct < 0.005:
                        exit_reason = "Momentum Timeout (Stalled Zombie)"

                # ── 資金流健康偵測 ──
                curr_t     = time.time()
                last_check = pos.get('last_flow_check', 0)

                if not exit_reason and (curr_t - last_check > 15):
                    pos['last_flow_check'] = curr_t

                    if time_held > 120:
                        flow_status = check_flow_health(s)

                        if flow_status == "Flow Reversal (Long Dump Detected)":
                            exit_reason = flow_status
                        elif flow_status == "Flow Deceleration (Momentum Died)":
                            if not pos.get('deceleration_detected', False):
                                pos['deceleration_detected'] = True
                                print(f"⚠️ {s} 偵測到高位收油 (Deceleration)！已啟動極限防禦標記，若利潤充足將自動收緊至 0.5 ATR！")

                # ── IOC TP/SL 本地觸發 ──
                if not exit_reason:
                    if curr_p >= pos['tp_price']:
                        exit_reason = "TP (Long IOC Exit)"
                    elif curr_p <= pos['sl_price']:
                        exit_reason = "Trail SL (Long IOC Exit)" if pos['is_breakeven'] else "SL (Long IOC Exit)"

                if exit_reason:
                    print(f"⚔️ 觸發 {exit_reason}，執行 IOC 平單: {s} | 持倉: {time_held / 60:.1f}分鐘 | Max PnL: {pos['max_pnl_pct'] * 100:.2f}% | 現盈虧: {pnl_pct * 100:.2f}%")

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
                    handle_trade_result(s, ioc_pnl)
                    del positions[s]

            except Exception as e:
                if "10006" in str(e): time.sleep(5)

    except Exception as e:
        logger.error(f"❌ manage_long_positions 外層錯誤: {e}")


def execute_live_long(symbol, net_flow, current_price, is_strong, atr, is_volatile):
    if symbol in cooldown_tracker:
        if time.time() < cooldown_tracker[symbol]:
            return
        else:
            del cooldown_tracker[symbol]

    if atr is None or atr == 0 or current_price == 0: return
    if not (is_strong and is_volatile and symbol not in positions): return

    cancel_all_v5(symbol)
    actual_bal = get_live_usdt_balance()
    eff_bal    = min(WORKING_CAPITAL, actual_bal)

    trade_val = min(
        (eff_bal * RISK_PER_TRADE) / ((SL_ATR_MULT * atr) / current_price),
        eff_bal * MAX_LEVERAGE * 0.95,
        MAX_NOTIONAL_PER_TRADE
    )
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

        try:
            order_detail = exchange.fetch_order(order['id'], symbol, params={"acknowledged": True})
            actual_price  = float(order_detail.get('average') or order_detail.get('price') or ioc_p)
            actual_amount = float(order_detail.get('filled', 0))
        except Exception as e:
            logger.warning(f"⚠️ {symbol} 獲取訂單失敗，啟動備用持倉同步: {e}")
            time.sleep(0.5)
            for p in exchange.fetch_positions():
                if p['symbol'] == symbol and float(p.get('contracts', 0) or p.get('size', 0)) > 0:
                    actual_amount = float(p.get('contracts', 0) or p.get('size', 0))
                    actual_price  = float(p.get('entryPrice') or ioc_p)
                    break

        if actual_amount == 0:
            print(f"⏩ {symbol} IOC 未成交，執行核彈撤單並退出。")
            cancel_all_v5(symbol)
            return

        tp_p = float(exchange.price_to_precision(symbol, actual_price + (TP_ATR_MULT * atr)))
        sl_p = float(exchange.price_to_precision(symbol, actual_price - (SL_ATR_MULT * atr)))

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
    """
    主事件迴圈

    🚀 [V6.6 修改] Log Spam 修復：
      原版在 regime['signal'] != 1 時每次迴圈都打印「🚦 恆溫器攔截中...」。
      由於 SCOUTING_INTERVAL=125 秒，每 125 秒打印一次，看似不多，
      但加上 manage_long_positions 每 4 秒的 BTC regime 讀取，
      在 HARD_BRAKE 情況下，主迴圈的制動訊息每 125 秒重複。

      修復：只在 brake 狀態「發生變化」時打印，避免重複刷屏。
      使用 `_last_brake_state` 追蹤前一次狀態。
    """
    print(f"🚀 AI 實戰 V6.6 LONG (恆溫器制動升級版) 啟動...")
    print(f"Lee-Ready 資金流 + 訂單簿失衡度 + AI 變速 Trail SL + 1m/5m/15m 恆溫器 [終極做多版] 初始化中...")
    print(f"📋 關鍵參數: SL_ATR_MULT={SL_ATR_MULT} | TP_ATR_MULT={TP_ATR_MULT} | RISK={RISK_PER_TRADE*100:.1f}%")

    load_dynamic_blacklist()
    sync_positions_on_startup()

    last_scout_time  = 0
    target_coins     = []

    # 🚀 [V6.6 新增] Log Spam 防護：追蹤前一次制動狀態，只在狀態改變時打印
    _last_brake_state     = None  # None / 'GREEN' / 'SOFT' / 'HARD'
    _last_regime_signal   = None

    while True:
        try:
            regime = get_btc_regime_v6_5()
            manage_long_positions(regime)

            curr_t = time.time()

            if curr_t - last_scout_time > SCOUTING_INTERVAL:

                # 🚀 [V6.6 修改] 計算當前制動狀態字串，只在改變時輸出
                if regime.get('brake', False):
                    _current_state = 'HARD'
                elif regime.get('soft_brake', False):
                    _current_state = 'SOFT'
                else:
                    _current_state = 'GREEN'

                if regime['signal'] == 1:
                    # GREEN 或 SOFT_BRAKE 均允許入場
                    if _current_state != _last_brake_state:
                        print(f"🟢 {'綠燈' if _current_state == 'GREEN' else '軟剎車 (仍允許入場)'}確認：執行多單大幣海選掃描...")

                    for s in target_coins:
                        try:
                            flow, last_p, is_strong = apply_lee_ready_long_logic(s)
                            atr, is_v = get_market_metrics(s)
                            if last_p > 0:
                                execute_live_long(s, flow, last_p, is_strong, atr, is_v)
                        except Exception:
                            continue
                        time.sleep(0.5)

                else:
                    # HARD_BRAKE 或 RED：凍結入場
                    # * 只在狀態「首次」進入此分支或從 GREEN/SOFT 切換過來時打印，避免每 125 秒重複
                    if _current_state != _last_brake_state:
                        brake_reason = regime.get('brake_reason', '未知原因')
                        print(f"🚦 恆溫器攔截中 (🚨 HARD BRAKE: {brake_reason})，海選暫停。")

                _last_brake_state = _current_state

                last_scout_time = curr_t
                target_coins = scouting_strong_coins(20)
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