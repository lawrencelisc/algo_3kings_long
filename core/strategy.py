import pandas as pd
import numpy as np
from core.connect import exchange, config


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
            status, signal = "🟢 GREEN (Bullish - All in)", 1
        elif cond_price or cond_trend:
            status, signal = "🟡 YELLOW (Conditions unmet - Standby)", 0
        else:
            status, signal = "🔴 RED (Bearish - Do not enter)", -1

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


def scouting_top_coins(n=5):
    """海選模組：尋找高交易量且強勢的幣種"""
    try:
        tickers = exchange.fetch_tickers()
        blacklist = config['BLACKLIST']
        data = [{'symbol': s, 'volume': t['quoteVolume'], 'change': t['percentage']}
                for s, t in tickers.items() if
                s.endswith(':USDT') and s not in blacklist and t['percentage'] is not None]
        df = pd.DataFrame(data)
        return df.sort_values('volume', ascending=False).head(20).sort_values('change', ascending=False).head(n)[
            'symbol'].tolist()
    except:
        return []


def apply_lee_ready_logic(symbol):
    """Lee-Ready 資金流邏輯 + 訂單簿失衡度 (Imbalance)"""
    try:
        ob = exchange.fetch_order_book(symbol, limit=20)
        midpoint = (ob['bids'][0][0] + ob['asks'][0][0]) / 2

        bid_vol = sum([b[1] for b in ob['bids']])
        ask_vol = sum([a[1] for a in ob['asks']])
        imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol) if (bid_vol + ask_vol) > 0 else 0

        # ✅ 修正：確保 imbalance 在合理範圍防呆
        imbalance = max(-1, min(1, imbalance))

        trades = exchange.fetch_trades(symbol, limit=200)
        df = pd.DataFrame(trades, columns=['price', 'amount', 'timestamp'])
        df['dir'] = np.where(df['price'] > midpoint, 1, np.where(df['price'] < midpoint, -1, 0))
        df['tick'] = df['price'].diff().apply(np.sign).replace(0, np.nan).ffill().fillna(0)
        df['final'] = np.where(df['dir'] != 0, df['dir'], df['tick'])
        df['weighted_flow'] = df['final'] * df['amount'] * df['price']

        net_flow = df['weighted_flow'].sum()
        flow_std = df['weighted_flow'].std()

        # ✅ 修正：更安全的 NaN 處理與極端值防護
        if pd.isna(flow_std) or flow_std <= 0:
            z_score = 0
        else:
            z_score = net_flow / flow_std
            z_score = np.clip(z_score, -10, 10)

        sigma_threshold = config['STRATEGY']['net_flow_sigma']
        min_imbalance = config['RISK_MANAGEMENT']['min_imbalance_ratio']

        is_strong = (z_score > sigma_threshold) and (imbalance > min_imbalance)

        if is_strong:
            print(f"📊 {symbol} Signal Validated | Z-Score: {z_score:.2f} | Imbalance: {imbalance:.2f}")
        elif z_score > sigma_threshold:
            print(
                f"⚠️ {symbol} Fakeout Prevented | Z-Score: {z_score:.2f} but Imbalance is {imbalance:.2f} (Sell Wall)")

        return net_flow, df['price'].iloc[-1], is_strong, z_score
    except Exception as e:
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