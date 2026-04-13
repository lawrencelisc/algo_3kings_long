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
# ⚙️ [系統/參數] 模組初始化與 API 配置 V6.6 RateFixed
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('AlgoTrade_Long_V6.6_RateFixed')

# Name: yamato
API_KEY = "fpirpvJmwub1uAzqA4"
API_SECRET = "9QQwKuZEg8e3YFKTYXSGj3MW9YBHlomeCrtJ"

exchange = ccxt.bybit({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'enableRateLimit': True,          # ccxt 內建令牌桶
    'rateLimit': 120,                 # * [Rate Fix] Bybit linear 120ms/request
    'options': {'defaultType': 'swap'},
})
exchange.load_markets()

# 檔案與路徑設定
LOG_DIR    = "result"
STATUS_DIR = "../status"
LOG_FILE       = f"{LOG_DIR}/live_long_log.csv"
STATUS_FILE    = f"{STATUS_DIR}/btc_regime_long.csv"
BLACKLIST_FILE = f"{STATUS_DIR}/dynamic_blacklist_long.json"

if not os.path.exists(LOG_DIR):    os.makedirs(LOG_DIR)
if not os.path.exists(STATUS_DIR): os.makedirs(STATUS_DIR)

# 系統狀態記憶體
positions          = {}
cooldown_tracker   = {}
consecutive_losses = {}

# ==========================================
# ⚙️ [系統/參數] 策略與風控全局變數
# ==========================================
WORKING_CAPITAL        = 1000.0
MAX_LEVERAGE           = 10.0
RISK_PER_TRADE         = 0.005
MIN_NOTIONAL           = 5.0
MAX_NOTIONAL_PER_TRADE = 200.0

NET_FLOW_SIGMA = 1.2
TP_ATR_MULT    = 5.0
SL_ATR_MULT    = 1.2   # * V6.6：0.8 → 1.2，減少 Whipsaw SL

MAX_CONSECUTIVE_LOSSES = 3
DYNAMIC_BAN_DURATION   = 86400

SCOUTING_INTERVAL       = 125
POSITION_CHECK_INTERVAL = 4

BRAKE_ADX_HIGH_THRESHOLD = 40
TIMEOUT_SECONDS          = 2700

# ==========================================
# 🚀 [Rate Fix] 緩存設定
# ==========================================
# 問題根源：每 4 秒主迴圈都呼叫 get_btc_regime_v6_5()
#   = 3x fetch_ohlcv + N×fetch_ticker + fetch_positions
#   → 每分鐘 45+ 次 API 請求 → Bybit 10006 Rate Limit
#
# 三層緩存策略：
#   Regime   緩存 60s → 把 3x ohlcv 從每分鐘15次降至每分鐘1次
#   ATR      緩存 60s → 5m K線300秒一根，完全無影響
#   Positions緩存  8s → private API 壓力減半

REGIME_CACHE_TTL    = 60
POSITIONS_CACHE_TTL = 8
ATR_CACHE_TTL       = 60

_regime_cache    = {'data': None, 'ts': 0}
_positions_cache = {'data': None, 'ts': 0}
_atr_cache       = {}  # symbol -> {atr, is_volatile, ts}

BLACKLIST = [
    'USDC/USDT:USDT', 'DAI/USDT:USDT',  'FDUSD/USDT:USDT', 'BUSD/USDT:USDT',
    'TUSD/USDT:USDT', 'PYUSD/USDT:USDT','USDP/USDT:USDT',  'EURS/USDT:USDT',
    'USDE/USDT:USDT', 'USAT/USDT:USDT', 'USD0/USDT:USDT',  'USTC/USDT:USDT',
    'LUSD/USDT:USDT', 'FRAX/USDT:USDT', 'MIM/USDT:USDT',   'RLUSD/USDT:USDT',
    'WBTC/USDT:USDT', 'WETH/USDT:USDT', 'WBNB/USDT:USDT',  'WAVAX/USDT:USDT',
    'stETH/USDT:USDT','cbETH/USDT:USDT','WHT/USDT:USDT'
]

CSV_COLUMNS = [
    'timestamp', 'symbol', 'action', 'price', 'amount', 'trade_value',
    'atr', 'net_flow', 'tp_price', 'sl_price', 'reason',
    'realized_pnl', 'actual_balance', 'effective_balance'
]
STATUS_COLUMNS = [
    'timestamp', 'btc_price', 'target_price', 'hma20', 'hma50',
    'adx', 'signal_code', 'decision_text'
]


# ==========================================
# 🛠️ [輔助模組] 記錄、帳戶與訂單管理
# ==========================================
def log_to_csv(data_dict):
    row = {col: '' for col in CSV_COLUMNS}
    row.update(data_dict)
    row['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pd.DataFrame([row], columns=CSV_COLUMNS).to_csv(
        LOG_FILE, mode='a', index=False, header=not os.path.exists(LOG_FILE)
    )


def log_status_to_csv(data_dict):
    row = {col: '' for col in STATUS_COLUMNS}
    row.update(data_dict)
    row['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pd.DataFrame([row], columns=STATUS_COLUMNS).to_csv(
        STATUS_FILE, mode='a', index=False, header=not os.path.exists(STATUS_FILE)
    )


def process_native_exit_log(symbol, pos, position_type='long'):
    """Native Exit PnL 結算 (Long: 出場價 - 入場價)"""
    real_exit_price = pos['entry_price']
    real_pnl        = 0.0
    try:
        pnl_res = exchange.private_get_v5_position_closed_pnl({
            'category': 'linear',
            'symbol': exchange.market_id(symbol),
            'limit': 1
        })
        if pnl_res and pnl_res.get('result') and pnl_res['result'].get('list'):
            last_trade      = pnl_res['result']['list'][0]
            real_exit_price = float(last_trade['avgExitPrice'])
            real_pnl        = float(last_trade['closedPnl'])
        else:
            raise ValueError("empty")
    except Exception as e:
        logger.debug(f"⚠️ {symbol} PnL 備用估算: {e}")
        try:
            curr_p          = exchange.fetch_ticker(symbol)['last']
            real_exit_price = curr_p
            real_pnl        = round((curr_p - pos['entry_price']) * pos['amount'], 4)
        except:
            pass

    log_to_csv({
        'symbol': symbol, 'action': 'NATIVE_EXIT', 'price': real_exit_price,
        'amount': pos['amount'], 'reason': 'Bybit Native TP/SL', 'realized_pnl': real_pnl
    })
    return real_pnl


def get_live_usdt_balance():
    try:
        return float(exchange.fetch_balance()['USDT']['free'])
    except:
        return 0.0


def cancel_all_v5(symbol):
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
    try:
        ob     = exchange.fetch_order_book(symbol, limit=5)
        levels = ob[side][:3]
        return sum([lv[0] for lv in levels]) / len(levels)
    except:
        return None


def get_market_metrics(symbol):
    """
    ATR 計算 — 加入 60 秒緩存

    * [Rate Fix] 原版每 4 秒對每個持倉都 fetch_ohlcv(50根)。
      5m K線 300 秒才更新一根，60 秒緩存對策略零影響，
      但把 API 請求頻率從 N×15次/分鐘 降至 N×1次/分鐘。
    """
    cached = _atr_cache.get(symbol)
    if cached and (time.time() - cached['ts']) < ATR_CACHE_TTL:
        return cached['atr'], cached['is_volatile']  # * 命中緩存

    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=50)
        df    = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df['tr'] = np.maximum(
            df['h'] - df['l'],
            np.maximum(abs(df['h'] - df['c'].shift(1)), abs(df['l'] - df['c'].shift(1)))
        )
        atr         = df['tr'].rolling(14, min_periods=1).mean().iloc[-1]
        is_volatile = (atr / df['c'].iloc[-1]) > 0.0015

        if pd.isna(atr) or atr == 0:
            return None, False

        _atr_cache[symbol] = {'atr': atr, 'is_volatile': is_volatile, 'ts': time.time()}
        return atr, is_volatile
    except:
        return None, False


def get_live_positions_cached():
    """
    fetch_positions 加入 8 秒緩存

    * [Rate Fix] Private API 限制嚴格，8 秒緩存把每分鐘請求從 15 次降至 7 次。
      倉位狀態在 8 秒內不會突變，安全。
    """
    if (time.time() - _positions_cache['ts']) < POSITIONS_CACHE_TTL and _positions_cache['data'] is not None:
        return _positions_cache['data']  # * 命中緩存

    try:
        data = exchange.fetch_positions(params={'category': 'linear'})
        _positions_cache['data'] = data
        _positions_cache['ts']   = time.time()
        return data
    except Exception as e:
        logger.warning(f"⚠️ fetch_positions 失敗: {e}")
        return _positions_cache['data'] or []


def fetch_tickers_for_positions(symbols):
    """
    批次取得持倉現價

    * [Rate Fix] 原版每個持倉獨立 fetch_ticker → N 次請求。
      改用 fetch_tickers(symbols) → 1 次請求取得全部。
      持倉 5 個幣：每 4 秒省 4 次 API 請求。
    """
    if not symbols:
        return {}
    try:
        result = exchange.fetch_tickers(symbols)
        return {s: t['last'] for s, t in result.items() if t.get('last')}
    except Exception as e:
        logger.warning(f"⚠️ batch fetch_tickers 失敗，逐一降級: {e}")
        prices = {}
        for s in symbols:
            try:
                prices[s] = exchange.fetch_ticker(s)['last']
                time.sleep(0.05)
            except:
                pass
        return prices


# ==========================================
# 🛠️ JSON 記憶與動態黑名單
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
            curr_t  = time.time()
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
            cooldown_tracker[symbol] = max(
                cooldown_tracker.get(symbol, 0), time.time() + 480
            )
    save_dynamic_blacklist()


# ==========================================
# 🧠 BTC 多單恆溫器 + 60 秒緩存
# ==========================================
def get_btc_regime_v6_5():
    """
    多單恆溫器 (1m/5m/15m) — 加入 60 秒緩存

    * [Rate Fix] 主兇：原版每 4 秒呼叫 = 3x fetch_ohlcv/分鐘×15 = 45次。
      60 秒緩存 → 3次/分鐘。API 請求降低 93%。

    故障降級：若 API 失敗但有舊緩存，繼續用舊值而非直接 brake=True 砍倉。
    """
    if (time.time() - _regime_cache['ts']) < REGIME_CACHE_TTL and _regime_cache['data'] is not None:
        return _regime_cache['data']  # * 命中緩存，0 API 請求

    try:
        o15 = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='15m', limit=150)
        o5  = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='5m',  limit=150)
        o1  = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1m',  limit=150)

        df15 = pd.DataFrame(o15, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df5  = pd.DataFrame(o5,  columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        df1  = pd.DataFrame(o1,  columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        curr_p = df15['c'].iloc[-1]

        def calc_hma(s, period):
            half   = int(period / 2)
            sq     = int(np.sqrt(period))
            w_half = np.arange(1, half + 1)
            w_full = np.arange(1, period + 1)
            w_sq   = np.arange(1, sq + 1)
            wma_h  = s.rolling(half).apply(lambda x: np.dot(x, w_half) / w_half.sum(), raw=True)
            wma_f  = s.rolling(period).apply(lambda x: np.dot(x, w_full) / w_full.sum(), raw=True)
            diff   = (2 * wma_h) - wma_f
            return diff.rolling(sq).apply(lambda x: np.dot(x, w_sq) / w_sq.sum(), raw=True)

        def calc_adx(df):
            df = df.copy()
            df['up']   = df['h'] - df['h'].shift(1)
            df['down'] = df['l'].shift(1) - df['l']
            df['+dm']  = np.where((df['up'] > df['down']) & (df['up'] > 0), df['up'], 0)
            df['-dm']  = np.where((df['down'] > df['up']) & (df['down'] > 0), df['down'], 0)
            df['tr']   = np.maximum(df['h'] - df['l'],
                         np.maximum(abs(df['h'] - df['c'].shift(1)),
                                    abs(df['l'] - df['c'].shift(1))))
            atr14    = df['tr'].ewm(alpha=1/14, adjust=False).mean()
            plus_di  = 100 * (pd.Series(df['+dm']).ewm(alpha=1/14, adjust=False).mean() / atr14)
            minus_di = 100 * (pd.Series(df['-dm']).ewm(alpha=1/14, adjust=False).mean() / atr14)
            denom    = plus_di + minus_di
            dx       = np.where(denom != 0, 100 * abs(plus_di - minus_di) / denom, 0)
            return pd.Series(dx).ewm(alpha=1/14, adjust=False).mean()

        h15_20, h15_50 = calc_hma(df15['c'], 20), calc_hma(df15['c'], 50)
        h15_20_val, h15_50_val = h15_20.iloc[-1], h15_50.iloc[-1]
        adx15_val = calc_adx(df15).iloc[-1]

        h5_20, h5_50 = calc_hma(df5['c'], 20), calc_hma(df5['c'], 50)
        adx5_series  = calc_adx(df5)
        adx5_val, adx5_prev = adx5_series.iloc[-1], adx5_series.iloc[-2]

        h1_20, h1_50    = calc_hma(df1['c'], 20), calc_hma(df1['c'], 50)
        h1_dead_cross   = h1_20.iloc[-1] < h1_50.iloc[-1]
        h5_dead_cross   = h5_20.iloc[-1] < h5_50.iloc[-1]
        adx5_high_drop  = (adx5_val < adx5_prev) and (adx5_prev > BRAKE_ADX_HIGH_THRESHOLD)

        hard_brake, soft_brake, brake_reason = False, False, ""

        if h1_dead_cross:
            if h5_dead_cross:
                hard_brake, brake_reason = True, "1m+5m HMA 雙重死叉"
            elif adx5_high_drop:
                hard_brake, brake_reason = True, "1m 死叉 + 5m ADX 高位回落"
            else:
                soft_brake, brake_reason = True, "1m HMA 死叉 (5m 仍健康，輕度警戒)"
        elif h5_dead_cross:
            hard_brake, brake_reason = True, "5m HMA 死叉"
        elif adx5_high_drop:
            soft_brake, brake_reason = True, "5m ADX 高位回落 (無死叉，輕度警戒)"

        cond_trend  = h15_20_val > h15_50_val
        cond_adx    = adx15_val > 22
        completed_v = df15['v'].iloc[-2]
        cond_vol    = completed_v > df15['v'].iloc[-25:-1].median() * 0.8

        if cond_trend and cond_adx and cond_vol and not hard_brake:
            status, signal = "🟢 GREEN   (Trend, ADX & Vol Validated)", 1
        elif hard_brake:
            status, signal = f"🔴 RED     (HARD BRAKE: {brake_reason})", -1
        elif soft_brake:
            status, signal = f"🟡 YELLOW  (SOFT BRAKE: {brake_reason})", 0
        elif cond_trend or cond_adx:
            status, signal = "🟡 YELLOW  (Standby - Waiting for confluence)", 0
        else:
            status, signal = "🔴 RED     (Sideways / Bearish)", -1

        log_status_to_csv({
            'btc_price': round(curr_p, 2), 'target_price': round(h15_50_val, 2),
            'hma20': round(h15_20_val, 2), 'hma50': round(h15_50_val, 2),
            'adx': round(adx15_val, 2), 'signal_code': signal, 'decision_text': status
        })

        print("-" * 60)
        print(f"🌡️ BTC 多單恆溫器 (1m/5m/15m) | 現價: {curr_p:.0f}")
        print(f"1️⃣ 15m 趨勢: HMA20({h15_20_val:.0f}) > HMA50({h15_50_val:.0f}) {'✅' if cond_trend else '❌'}")
        print(f"2️⃣ 15m 動能: ADX > 22 {'✅' if cond_adx else '❌'} (值: {adx15_val:.1f})")
        if hard_brake:
            print(f"3️⃣ 制動: 🚨 HARD BRAKE ({brake_reason})")
        elif soft_brake:
            print(f"3️⃣ 制動: ⚠️  SOFT BRAKE ({brake_reason})")
        else:
            print(f"3️⃣ 制動: ✅ 安全")
        print(f"🚦 最終決策: {status}")
        print("-" * 60)

        result = {
            'signal': signal, 'brake': hard_brake,
            'soft_brake': soft_brake, 'brake_reason': brake_reason
        }
        _regime_cache['data'] = result
        _regime_cache['ts']   = time.time()
        return result

    except Exception as e:
        logger.error(f"⚠️ 恆溫器故障: {e}")
        # * 故障降級：有舊緩存就繼續用，避免因單次 API 失敗錯殺所有倉位
        if _regime_cache['data'] is not None:
            logger.warning("⚠️ 恆溫器使用上次緩存結果繼續運行")
            return _regime_cache['data']
        return {'signal': 0, 'brake': True, 'soft_brake': False, 'brake_reason': 'API Error'}


# ==========================================
# 📡 市場掃描
# ==========================================
def scouting_strong_coins(scouting_coins=20):
    """大幣海選：最強幣種"""
    try:
        tickers = exchange.fetch_tickers()
        data    = []
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
# 🔍 Lee-Ready 引擎
# ==========================================
def check_flow_health(symbol):
    """防守雷達：多單 Dump 偵測"""
    try:
        trades = exchange.fetch_trades(symbol, limit=100)
        if not trades or len(trades) < 50: return None

        df = pd.DataFrame(trades)
        df['price_change'] = df['price'].diff()
        df['direction']    = np.where(df['price_change'] > 0, 1, np.where(df['price_change'] < 0, -1, 0))
        df['direction']    = df['direction'].replace(0, np.nan).ffill().fillna(0)

        avg_vol        = df['amount'].mean()
        df['weight']   = np.where(df['amount'] > avg_vol * 2, 2.0, 1.0)
        df['net_flow'] = df['direction'] * df['amount'] * df['price'] * df['weight']

        flow_std = df['net_flow'].std()
        if flow_std == 0: return None

        flow_mean      = df['net_flow'].mean()
        recent_25_flow = df['net_flow'].tail(25).sum()
        z_score        = (recent_25_flow - (flow_mean * 25)) / (flow_std * np.sqrt(25))

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
    """正向 Lee-Ready 多單狙擊"""
    try:
        trades = exchange.fetch_trades(symbol, limit=200)
        if not trades: return 0, 0, False

        df = pd.DataFrame(trades)
        df['price_change'] = df['price'].diff()
        df['direction']    = np.where(df['price_change'] > 0, 1, np.where(df['price_change'] < 0, -1, 0))
        df['direction']    = df['direction'].replace(0, np.nan).ffill().fillna(0)

        avg_vol        = df['amount'].mean()
        df['weight']   = np.where(df['amount'] > avg_vol * 2, 2.0, 1.0)
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

        z_score   = 0
        is_strong = False
        if df['net_flow'].std() > 0:
            z_score = short_window_flow / (df['net_flow'].std() * np.sqrt(50))

        if (short_window_flow > 0) and (acceleration > 0) and (imbalance > 0.15):
            is_strong = True
            print(f"🔥 {symbol} Long Sniper! Accel: {acceleration:.0f} | Imbalance: {imbalance:.2f}")
        elif z_score > NET_FLOW_SIGMA:
            is_strong = True
            print(f"📈 {symbol} Long Z-Score Validated: {z_score:.2f}")

        if is_strong and imbalance < -0.1:
            is_strong = False
            print(f"⚠️ {symbol} 假突破陷阱！取消做多！")

        return short_window_flow, df['price'].iloc[-1], is_strong
    except Exception as e:
        print(f"⚠️ LR Logic Error [{symbol}]: {e}")
        return 0, 0, False


# ==========================================
# 🛡️ 持倉管理
# ==========================================
def sync_positions_on_startup():
    print("🔄 正在同步交易所現有多倉...")
    try:
        live_positions_raw = exchange.fetch_positions()
        live_symbols       = [p for p in live_positions_raw
                              if float(p.get('contracts', 0) or p.get('size', 0)) > 0]
        recovered_count    = 0
        for p in live_symbols:
            symbol    = p['symbol']
            side      = p.get('side', '').lower()
            info_side = p.get('info', {}).get('side', '').lower()
            if side in ['long', 'buy'] or info_side in ['buy', 'long']:
                entry_price = float(p.get('entryPrice', 0))
                amount      = float(p.get('contracts', 0) or p.get('size', 0))
                sl_p        = float(p.get('stopLoss', 0))
                tp_p        = float(p.get('takeProfit', 0))
                atr, _      = get_market_metrics(symbol)
                if not atr: atr = entry_price * 0.01
                if sl_p == 0: sl_p = float(exchange.price_to_precision(symbol, entry_price - (SL_ATR_MULT * atr)))
                if tp_p == 0: tp_p = float(exchange.price_to_precision(symbol, entry_price + (TP_ATR_MULT * atr)))
                is_be = True if (sl_p > entry_price and sl_p > 0) else False
                positions[symbol] = {
                    'amount': amount, 'entry_price': entry_price,
                    'tp_price': tp_p, 'sl_price': sl_p,
                    'is_breakeven': is_be, 'atr': atr, 'max_pnl_pct': 0.0,
                    'entry_time': time.time()
                }
                recovered_count += 1
                print(f"✅ 尋回孤兒多單: {symbol} | 入場價: {entry_price} | 保本: {is_be}")
        print(f"🔄 同步完成！共尋回 {recovered_count} 個多倉。")
    except Exception as e:
        logger.error(f"❌ 啟動同步失敗: {e}")


def manage_long_positions(regime=None):
    """
    多單持倉管理

    * [Rate Fix] 兩項核心改動：
      1. fetch_positions → get_live_positions_cached() (8 秒緩存)
      2. 逐倉 fetch_ticker → fetch_tickers_for_positions() (批次單次請求)
    """
    try:
        live_positions_raw = get_live_positions_cached()  # * 緩存版
        live_symbols = {
            p['symbol']: p for p in live_positions_raw
            if float(p.get('contracts', 0) or p.get('size', 0)) > 0
        }

        # ── 孤兒多單自動接管 ──
        for s, p in live_symbols.items():
            if s not in positions:
                side      = p.get('side', '').lower()
                info_side = p.get('info', {}).get('side', '').lower()
                if side in ['long', 'buy'] or info_side in ['buy', 'long']:
                    entry_p = float(p.get('entryPrice', 0))
                    amt     = float(p.get('contracts', 0) or p.get('size', 0))
                    atr, _  = get_market_metrics(s)
                    if not atr: atr = entry_p * 0.01
                    real_entry_time = float(p.get('createdTime') or (time.time() * 1000)) / 1000.0
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
                    print(f"🚨 [自癒] 接管孤兒多單: {s} | 入場價: {entry_p}")

        # ── Native Exit 偵測 ──
        for s in list(positions.keys()):
            if s not in live_symbols:
                print(f"🧹 交易所已自動平多倉: {s}")
                real_pnl = process_native_exit_log(s, positions[s], 'long')
                cancel_all_v5(s)
                handle_trade_result(s, real_pnl)
                del positions[s]
                continue

        if not positions:
            return

        # * [Rate Fix] 批次取得所有持倉現價 (1 次請求)
        current_prices = fetch_tickers_for_positions(list(positions.keys()))

        for s in list(positions.keys()):
            try:
                curr_p = current_prices.get(s)
                if curr_p is None:
                    logger.warning(f"⚠️ {s} 無現價，跳過")
                    continue

                pos    = positions[s]
                pnl_pct             = (curr_p - pos['entry_price']) / pos['entry_price']
                coin_volatility_pct = pos['atr'] / pos['entry_price']
                sl_updated          = False

                if 'max_pnl_pct' not in pos: pos['max_pnl_pct'] = pnl_pct
                pos['max_pnl_pct'] = max(pos['max_pnl_pct'], pnl_pct)

                # ── 保本 ──
                if not pos['is_breakeven'] and pnl_pct > (coin_volatility_pct * 2.0):
                    pos['sl_price']     = pos['entry_price'] * 1.002
                    pos['is_breakeven'] = True
                    sl_updated          = True

                if pos['is_breakeven']:
                    if regime and regime.get('brake', False):
                        trail_sl = curr_p - (0.3 * pos['atr'])
                    elif regime and regime.get('soft_brake', False):
                        trail_sl = curr_p - (0.6 * pos['atr'])
                    elif pos.get('deceleration_detected', False) and pnl_pct > (coin_volatility_pct * 2.5):
                        trail_sl = curr_p - (0.5 * pos['atr'])
                    elif pnl_pct > (coin_volatility_pct * 5.0):
                        trail_sl = curr_p - (0.8 * pos['atr'])
                    elif pnl_pct > (coin_volatility_pct * 3.5):
                        trail_sl = curr_p - (1.2 * pos['atr'])
                    else:
                        trail_sl = curr_p - (1.8 * pos['atr'])

                    if trail_sl > pos['sl_price']:
                        if (trail_sl - pos['sl_price']) / pos['sl_price'] > 0.0005:
                            sl_updated      = True
                            pos['sl_price'] = trail_sl

                if sl_updated:
                    f_sl = exchange.price_to_precision(s, pos['sl_price'])
                    try:
                        exchange.private_post_v5_position_trading_stop({
                            'category': 'linear', 'symbol': exchange.market_id(s),
                            'stopLoss': str(f_sl), 'tpslMode': 'Full', 'positionIdx': 0
                        })
                    except Exception as e:
                        logger.warning(f"⚠️ {s} Trail SL 更新失敗: {e}")

                exit_reason = None
                time_held   = time.time() - pos.get('entry_time', time.time())

                if not exit_reason and time_held > TIMEOUT_SECONDS and pnl_pct < 0.005:
                    exit_reason = "Momentum Timeout (Stalled Zombie)"

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
                                print(f"⚠️ {s} 高位收油偵測！啟動防禦標記！")

                if not exit_reason:
                    if curr_p >= pos['tp_price']:
                        exit_reason = "TP (Long IOC Exit)"
                    elif curr_p <= pos['sl_price']:
                        exit_reason = "Trail SL (Long IOC Exit)" if pos['is_breakeven'] else "SL (Long IOC Exit)"

                if exit_reason:
                    print(f"⚔️ {exit_reason} | {s} | {time_held/60:.1f}分 | "
                          f"MaxPnL:{pos['max_pnl_pct']*100:.2f}% | 現:{pnl_pct*100:.2f}%")
                    ioc_price = get_3_layer_avg_price(s, 'bids') or curr_p
                    try:
                        exchange.create_order(s, 'limit', 'sell', pos['amount'], ioc_price,
                                              {'timeInForce': 'IOC', 'reduceOnly': True})
                    except:
                        exchange.create_market_sell_order(s, pos['amount'], {'reduceOnly': True})

                    ioc_pnl = round((ioc_price - pos['entry_price']) * pos['amount'], 4)
                    log_to_csv({
                        'symbol': s, 'action': 'LONG_EXIT', 'price': curr_p,
                        'amount': pos['amount'], 'reason': exit_reason, 'realized_pnl': ioc_pnl
                    })
                    cancel_all_v5(s)
                    handle_trade_result(s, ioc_pnl)
                    del positions[s]

                    # * 平倉後清除 positions cache
                    _positions_cache['ts'] = 0

            except Exception as e:
                if "10006" in str(e):
                    logger.warning("⚠️ Rate limit in position loop, sleeping 10s")
                    time.sleep(10)

    except Exception as e:
        logger.error(f"❌ manage_long_positions 外層錯誤: {e}")


def execute_live_long(symbol, net_flow, current_price, is_strong, atr, is_volatile):
    """多單入場執行"""
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
        order = exchange.create_order(symbol, 'limit', 'buy', amount, ioc_p,
                                      {'timeInForce': 'IOC', 'positionIdx': 0})
        time.sleep(1)

        actual_price, actual_amount = ioc_p, 0
        try:
            order_detail  = exchange.fetch_order(order['id'], symbol, params={"acknowledged": True})
            actual_price  = float(order_detail.get('average') or order_detail.get('price') or ioc_p)
            actual_amount = float(order_detail.get('filled', 0))
        except Exception as e:
            logger.warning(f"⚠️ {symbol} 訂單確認失敗，備用同步: {e}")
            time.sleep(0.5)
            for p in exchange.fetch_positions():
                if (p['symbol'] == symbol and
                    float(p.get('contracts', 0) or p.get('size', 0)) > 0):
                    actual_amount = float(p.get('contracts', 0) or p.get('size', 0))
                    actual_price  = float(p.get('entryPrice') or ioc_p)
                    break

        if actual_amount == 0:
            print(f"⏩ {symbol} IOC 未成交，撤單退出。")
            cancel_all_v5(symbol)
            return

        tp_p = float(exchange.price_to_precision(symbol, actual_price + (TP_ATR_MULT * atr)))
        sl_p = float(exchange.price_to_precision(symbol, actual_price - (SL_ATR_MULT * atr)))

        if (tp_p - actual_price) / actual_price < 0.003:
            print(f"🟡 放棄做多 [{symbol}]: 利潤空間太細，市價平倉！")
            try:
                exchange.create_market_sell_order(symbol, actual_amount, {'reduceOnly': True})
            except Exception as e:
                logger.error(f"❌ 緊急平倉失敗: {e}")
            cancel_all_v5(symbol)
            return

        try:
            exchange.private_post_v5_position_trading_stop({
                'category': 'linear', 'symbol': exchange.market_id(symbol),
                'stopLoss': str(sl_p), 'takeProfit': str(tp_p),
                'tpslMode': 'Full', 'positionIdx': 0
            })
            print(f"✅ {symbol} TP/SL 設置 | TP: {tp_p} | SL: {sl_p}")
        except Exception as e:
            logger.warning(f"⚠️ {symbol} TP/SL 設置異常: {e}")

        positions[symbol] = {
            'amount': actual_amount, 'entry_price': actual_price,
            'tp_price': tp_p, 'sl_price': sl_p,
            'is_breakeven': False, 'atr': atr, 'max_pnl_pct': 0.0,
            'entry_time': time.time()
        }
        cooldown_tracker[symbol] = time.time() + 480
        # * 新開倉後清除 positions cache
        _positions_cache['ts'] = 0
        save_dynamic_blacklist()

        log_to_csv({
            'symbol': symbol, 'action': 'LONG_ENTRY', 'price': actual_price,
            'amount': actual_amount, 'trade_value': round(actual_amount * actual_price, 2),
            'atr': round(atr, 4), 'net_flow': round(net_flow, 2),
            'tp_price': tp_p, 'sl_price': sl_p,
            'actual_balance': round(actual_bal, 2), 'effective_balance': eff_bal
        })
        print(f"📈 [已入貨做多] {symbol} @ {actual_price:.4f} | 數量: {actual_amount}")

    except Exception as e:
        logger.error(f"❌ {symbol} 做多執行失敗: {e}")


# ==========================================
# 🚀 主程序
# ==========================================
def main():
    print("🚀 AI 實戰 V6.6 LONG (Rate Limit 修復版) 啟動...")
    print(f"📋 SL={SL_ATR_MULT}×ATR | TP={TP_ATR_MULT}×ATR | "
          f"Regime 緩存={REGIME_CACHE_TTL}s | ATR 緩存={ATR_CACHE_TTL}s | "
          f"Positions 緩存={POSITIONS_CACHE_TTL}s")

    load_dynamic_blacklist()
    sync_positions_on_startup()

    last_scout_time   = 0
    target_coins      = []
    _last_brake_state = None

    while True:
        try:
            regime = get_btc_regime_v6_5()        # * 緩存版
            manage_long_positions(regime)          # * 緩存版

            curr_t = time.time()

            if curr_t - last_scout_time > SCOUTING_INTERVAL:

                _current_state = ('HARD' if regime.get('brake') else
                                  'SOFT' if regime.get('soft_brake') else 'GREEN')

                if regime['signal'] == 1:
                    if _current_state != _last_brake_state:
                        label = '綠燈' if _current_state == 'GREEN' else '軟剎車 (仍允許入場)'
                        print(f"🟢 {label}確認：執行多單大幣海選掃描...")

                    for s in target_coins:
                        try:
                            flow, last_p, is_strong = apply_lee_ready_long_logic(s)
                            atr, is_v = get_market_metrics(s)  # * 緩存版
                            if last_p > 0:
                                execute_live_long(s, flow, last_p, is_strong, atr, is_v)
                        except Exception:
                            continue
                        time.sleep(0.3)

                else:
                    if _current_state != _last_brake_state:
                        print(f"🚦 恆溫器攔截 ({regime.get('brake_reason', '市場偏空')})，海選暫停。")

                _last_brake_state = _current_state
                last_scout_time   = curr_t
                target_coins      = scouting_strong_coins(20)
                print(f"⏳ 多軍巡邏 | 持倉: {list(positions.keys())} | 餘額: {get_live_usdt_balance():.2f}")

            time.sleep(POSITION_CHECK_INTERVAL)

        except KeyboardInterrupt:
            print(f"\n👋 手動終止。餘額: {get_live_usdt_balance():.2f} | 持倉: {list(positions.keys())}")
            sys.exit(0)
        except Exception as e:
            logger.error(f"❌ 主迴圈錯誤: {e}")
            time.sleep(30 if "10006" in str(e) else 10)


if __name__ == "__main__":
    main()