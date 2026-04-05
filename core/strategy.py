import os
import pandas as pd
import numpy as np

from datetime import datetime
from core.connect import exchange, config


# ✅ 新增：定義全域變數，防止 NameError 崩潰
BLACKLIST = ['USDC/USDT:USDT', 'BUSD/USDT:USDT', 'EUR/USDT:USDT'] # 穩定幣黑名單
NET_FLOW_SIGMA = 2.0         # Z-Score 資金流入門檻
MIN_IMBALANCE_RATIO = 0.2    # 買盤牆厚度門檻

STATUS_DIR = "status"
STATUS_FILE = f"{STATUS_DIR}/btc_status_long.csv"
STATUS_COLUMNS = ['timestamp', 'btc_price', 'target_price', 'sma20', 'sma50', 'signal_code', 'decision_text']

if not os.path.exists(STATUS_DIR): os.makedirs(STATUS_DIR)


def log_status_to_csv(data_dict):
    row = {col: '' for col in STATUS_COLUMNS}
    row.update(data_dict)
    row['timestamp'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pd.DataFrame([row], columns=STATUS_COLUMNS).to_csv(STATUS_FILE, mode='a', index=False,
                                                       header=not os.path.exists(STATUS_FILE))


def get_btc_regime():
    """BTC 導航：判斷整體市場多空環境"""
    try:
        ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=60)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        curr_p = df['c'].iloc[-1]
        sma20 = df['c'].rolling(20).mean().iloc[-1]
        sma50 = df['c'].rolling(50).mean().iloc[-1]

        dev_threshold = config['STRATEGY']['btc_dev_threshold']
        target_long = sma20 * (1 + dev_threshold)

        cond_price = curr_p > target_long
        cond_trend = sma20 > sma50

        tick_p = "✅" if cond_price else "❌"
        tick_t = "✅" if cond_trend else "❌"

        if cond_price and cond_trend:
            status, signal = "🟢 GREEN   (Bullish - All in)", 1
        elif cond_price or cond_trend:
            status, signal = "🟡 YELLOW  (Conditions unmet - Standby)", 0
        else:
            status, signal = "🔴 RED     (Bearish - Do not enter)", -1

        report_data = {
            'btc_price': round(curr_p, 2),
            'target_price': round(target_long, 2),
            'sma20': round(sma20, 2),
            'sma50': round(sma50, 2),
            'signal_code': signal,
            'decision_text': status
        }
        log_status_to_csv(report_data)

        print("-" * 60)
        print(f"📈 BTC Live Status (Long) | Price: {curr_p:.0f}")
        print(f"1️⃣ Price Threshold: Current({curr_p:.0f}) > Target({target_long:.0f}) {tick_p}")
        print(f"2️⃣ Trend Confirmation: SMA20({sma20:.0f}) > SMA50({sma50:.0f}) {tick_t}")
        print(f"🚦 Final Decision: {status}")
        print("-" * 60)

        return signal
    except Exception as e:
        print(f"⚠️ Navigation Fault: {e}")
        return 0


# ✅ 新增代碼：修正函數名配合 main.py，並將 ascending=True 改為 False (尋找強勢暴升幣)
def scouting_top_coins(n=5):
    """海選強勢幣 (過濾 Spread)"""
    try:
        tickers = exchange.fetch_tickers()
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
        if df.empty: return []

        # 🚀 修正：做多要搵升得最勁嘅 (ascending=False)
        return df.sort_values('volume', ascending=False).head(20).sort_values('change', ascending=False).head(n)['symbol'].tolist()
    except Exception as e:
        print(f"⚠️ Scouting Error: {e}")
        return []


# ✅ 新增代碼：修正回傳值，增加 z_score 給 main.py 用嚟計 Risk
def apply_lee_ready_logic(symbol):
    """Lee-Ready 資金流邏輯 + 訂單簿失衡度 (Imbalance) + P95濾網 [終極做多版]"""
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
            print(f"📈 {symbol} Long Validated | Z-Score: {z_score:.2f} | Imbalance: {imbalance:.2f} | P95 Cap: {p95:.0f}")
        elif z_score > NET_FLOW_SIGMA:
            print(f"⚠️ {symbol} Fake-Pump Prevented | Z-Score: {z_score:.2f} but Imbalance is {imbalance:.2f} (Sell Wall in the way)")

        # 🚀 修正：回傳值增加 z_score 以配合 main.py 需求 (4 個變數)
        return net_flow, df['price'].iloc[-1], is_strong, z_score

    except Exception as e:
        print(f"❌ 錯誤 [{symbol}] Lee-Ready: {e}")
        # 🚀 修正：錯誤時也要回傳 4 個值，防止 ValueError
        return 0, 0, False, 0


def get_market_metrics(symbol):
    """計算 ATR 與波動率"""
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