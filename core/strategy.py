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


# def get_btc_regime():
#     """BTC 導航：判斷整體市場多空環境"""
#     try:
#         ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=60)
#         df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
#         curr_p = df['c'].iloc[-1]
#         sma20 = df['c'].rolling(20).mean().iloc[-1]
#         sma50 = df['c'].rolling(50).mean().iloc[-1]
#
#         dev_threshold = config['STRATEGY']['btc_dev_threshold']
#         target_long = sma20 * (1 + dev_threshold)
#
#         cond_price = curr_p > target_long
#         cond_trend = sma20 > sma50
#
#         tick_p = "✅" if cond_price else "❌"
#         tick_t = "✅" if cond_trend else "❌"
#
#         if cond_price and cond_trend:
#             status, signal = "🟢 GREEN   (Bullish - All in)", 1
#         elif cond_price or cond_trend:
#             status, signal = "🟡 YELLOW  (Conditions unmet - Standby)", 0
#         else:
#             status, signal = "🔴 RED     (Bearish - Do not enter)", -1
#
#         report_data = {
#             'btc_price': round(curr_p, 2),
#             'target_price': round(target_long, 2),
#             'sma20': round(sma20, 2),
#             'sma50': round(sma50, 2),
#             'signal_code': signal,
#             'decision_text': status
#         }
#         log_status_to_csv(report_data)
#
#         print("-" * 60)
#         print(f"📈 BTC Live Status (Long) | Price: {curr_p:.0f}")
#         print(f"1️⃣ Price Threshold: Current({curr_p:.0f}) > Target({target_long:.0f}) {tick_p}")
#         print(f"2️⃣ Trend Confirmation: SMA20({sma20:.0f}) > SMA50({sma50:.0f}) {tick_t}")
#         print(f"🚦 Final Decision: {status}")
#         print("-" * 60)
#
#         return signal
#     except Exception as e:
#         print(f"⚠️ Navigation Fault: {e}")
#         return 0


def get_btc_regime():
    """🚀 終極導航 (做多版)：HMA 交叉 + ADX 趨勢過濾 + 均量過濾"""
    try:
        # ⚠️ 必須拉長到 150，確保 HMA50 和 ADX 有足夠數據計算
        ohlcv = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1h', limit=150)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        curr_p = df['c'].iloc[-1]
        curr_v = df['v'].iloc[-1]

        # ==========================================
        # 1️⃣ 極速趨勢引擎：計算 HMA 20 與 HMA 50
        # ==========================================
        def calc_hma(s, period):
            half_length = int(period / 2)
            sqrt_length = int(np.sqrt(period))
            # WMA (加權移動平均) 輔助函數
            weights_half = np.arange(1, half_length + 1)
            weights_full = np.arange(1, period + 1)
            weights_sqrt = np.arange(1, sqrt_length + 1)

            wma_half = s.rolling(half_length).apply(lambda x: np.dot(x, weights_half) / weights_half.sum(), raw=True)
            wma_full = s.rolling(period).apply(lambda x: np.dot(x, weights_full) / weights_full.sum(), raw=True)

            s_diff = (2 * wma_half) - wma_full
            hma = s_diff.rolling(sqrt_length).apply(lambda x: np.dot(x, weights_sqrt) / weights_sqrt.sum(), raw=True)
            return hma

        df['hma20'] = calc_hma(df['c'], 20)
        df['hma50'] = calc_hma(df['c'], 50)

        # 🚀 修改：條件 1：HMA20 升穿 HMA50 (無滯後升勢確立)
        hma20_val = df['hma20'].iloc[-1]
        hma50_val = df['hma50'].iloc[-1]
        cond_trend = hma20_val > hma50_val  # <--- LONG 關鍵：20 大過 50

        # ==========================================
        # 2️⃣ 趨勢強度濾網：計算 ADX (14)
        # ==========================================
        df['up'] = df['h'] - df['h'].shift(1)
        df['down'] = df['l'].shift(1) - df['l']
        df['+dm'] = np.where((df['up'] > df['down']) & (df['up'] > 0), df['up'], 0)
        df['-dm'] = np.where((df['down'] > df['up']) & (df['down'] > 0), df['down'], 0)
        df['tr'] = np.maximum(df['h'] - df['l'],
                              np.maximum(abs(df['h'] - df['c'].shift(1)), abs(df['l'] - df['c'].shift(1))))

        atr_14 = df['tr'].ewm(alpha=1 / 14, adjust=False).mean()
        plus_di = 100 * (pd.Series(df['+dm']).ewm(alpha=1 / 14, adjust=False).mean() / atr_14)
        minus_di = 100 * (pd.Series(df['-dm']).ewm(alpha=1 / 14, adjust=False).mean() / atr_14)

        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        adx_val = dx.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]

        # 條件 2：ADX > 22 (過濾無方向橫盤，多空通用)
        cond_adx = adx_val > 22

        # ==========================================
        # 3️⃣ 成交量濾網 (抗極端值優化版)：24H 中位數 (Median)
        # ==========================================
        # 改用 24 小時中位數，完美無視單一巨量插針的干擾
        median_v_24 = df['v'].rolling(24).median().iloc[-1]

        # 動能容錯：只需要大於中位數的 80% (0.8)，就視為有足夠健康動能
        target_vol = median_v_24 * 0.8
        cond_vol = curr_v > target_vol

        # ==========================================
        # 4️⃣ 整合訊號與輸出
        # ==========================================
        tick_t = "✅" if cond_trend else "❌"
        tick_a = f"✅ (ADX: {adx_val:.1f})" if cond_adx else f"❌ (ADX: {adx_val:.1f})"
        tick_v = f"✅ (Vol: {curr_v:.0f} > 目標:{target_vol:.0f})" \
            if cond_vol else f"❌ (Vol: {curr_v:.0f} < 目標:{target_vol:.0f})"

        # 必須三個條件同時滿足才開綠燈
        if cond_trend and cond_adx and cond_vol:
            status, signal = "🟢 GREEN   (Bullish Trend, ADX & Vol Validated)", 1
        elif cond_trend or cond_adx:
            status, signal = "🟡 YELLOW  (Standby - Waiting for confluence)", 0
        else:
            status, signal = "🔴 RED     (Sideways / Bearish)", -1  # <--- 沒趨勢或是跌勢就停火

        # 兼容 CSV 紀錄 (借用欄位名)
        report = {
            'btc_price': round(curr_p, 2),
            'target_price': round(hma50_val, 2),
            'sma20': round(hma20_val, 2),
            'adx': round(adx_val, 2),
            'signal_code': signal,
            'decision_text': status
        }
        log_status_to_csv(report)

        print("-" * 60)
        print(f"📊 BTC 實時戰報 (Long 多頭 HMA+ADX版) | 現價: {curr_p:.0f}")
        print(f"1️⃣ 極速升勢: HMA20({hma20_val:.0f}) > HMA50({hma50_val:.0f}) {tick_t}")  # <--- 改為大於
        print(f"2️⃣ 趨勢強度: ADX > 22 {tick_a}")
        print(f"3️⃣ 動能確認: 當前量 > 24H中位數(80%) {tick_v}")
        print(f"🚦 最終決策: {status}")
        print("-" * 60)

        return signal
    except Exception as e:
        print(f"⚠️ 導航故障: {e}")
        return 0


# # ✅ 新增代碼：修正函數名配合 main.py，並將 ascending=True 改為 False (尋找強勢暴升幣)
# def scouting_top_coins(n=5):
#     """海選強勢幣 (過濾 Spread)"""
#     try:
#         tickers = exchange.fetch_tickers()
#         data = []
#         for s, t in tickers.items():
#             if s.endswith(':USDT') and s not in BLACKLIST and t['percentage'] is not None:
#                 ask = t.get('ask')
#                 bid = t.get('bid')
#                 if ask and bid and bid > 0:
#                     spread = (ask - bid) / bid
#                     if spread < 0.0015:
#                         data.append({'symbol': s, 'volume': t['quoteVolume'], 'change': t['percentage']})
#
#         df = pd.DataFrame(data)
#         if df.empty: return []
#
#         # 🚀 修正：做多要搵升得最勁嘅 (ascending=False)
#         return df.sort_values('volume', ascending=False).head(20).sort_values('change', ascending=False).head(n)['symbol'].tolist()
#     except Exception as e:
#         print(f"⚠️ Scouting Error: {e}")
#         return []


# 🚀 新增：妖幣/山寨幣專用海選邏輯 (ver 2026-04-06)
def scouting_top_coins(n=5):
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
                    # 🚀 妖幣特化 1：放寬 Spread 門檻到 0.0030 (0.3%)，容許流動性稍差嘅潛力妖幣入選
                    if spread < 0.0030:
                        data.append({'symbol': s, 'volume': t['quoteVolume'], 'change': t['percentage']})

        df = pd.DataFrame(data)
        if df.empty: return []

        # 🚀 妖幣特化 2：廢除 quantile(0.8) 大幣資金池，改用絕對硬門檻 (24小時成交量 > 1,000萬 U)
        MIN_VOLUME_ALT = 10000000
        df_filtered = df[df['volume'] >= MIN_VOLUME_ALT]

        # 尋找升幅最勁嘅前 n 隻妖幣
        return df_filtered.sort_values('change', ascending=False).head(n)['symbol'].tolist()
    except Exception as e:
        print(f"⚠️ Altcoin Scouting Error: {e}")
        return []


# # ✅ 新增代碼：修正回傳值，增加 z_score 給 main.py 用嚟計 Risk
# def apply_lee_ready_logic(symbol):
#     """Lee-Ready 資金流邏輯 + 訂單簿失衡度 (Imbalance) + P95濾網 [終極做多版]"""
#     try:
#         ob = exchange.fetch_order_book(symbol, limit=20)
#         midpoint = (ob['bids'][0][0] + ob['asks'][0][0]) / 2
#
#         bid_vol = sum([b[1] for b in ob['bids']])
#         ask_vol = sum([a[1] for a in ob['asks']])
#         imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol) if (bid_vol + ask_vol) > 0 else 0
#         imbalance = max(-1, min(1, imbalance))
#
#         trades = exchange.fetch_trades(symbol, limit=200)
#         df = pd.DataFrame(trades, columns=['price', 'amount', 'timestamp'])
#         df['dir'] = np.where(df['price'] > midpoint, 1, np.where(df['price'] < midpoint, -1, 0))
#         df['tick'] = df['price'].diff().apply(np.sign).replace(0, np.nan).ffill().fillna(0)
#         df['final'] = np.where(df['dir'] != 0, df['dir'], df['tick'])
#
#         df['usd_val'] = df['amount'] * df['price']
#         p95 = df['usd_val'].quantile(0.95)
#         df['usd_val_clipped'] = df['usd_val'].clip(upper=p95)
#
#         df['weighted_flow'] = df['final'] * df['usd_val_clipped']
#         net_flow = df['weighted_flow'].sum()
#         flow_std = df['weighted_flow'].std()
#
#         if pd.isna(flow_std) or flow_std <= 0:
#             z_score = 0
#         else:
#             z_score = net_flow / flow_std
#             z_score = np.clip(z_score, -10, 10)
#
#         is_strong = (z_score > NET_FLOW_SIGMA) and (imbalance > MIN_IMBALANCE_RATIO)
#
#         if is_strong:
#             print(f"📈 {symbol} Long Validated | Z-Score: {z_score:.2f} | Imbalance: {imbalance:.2f} | P95 Cap: {p95:.0f}")
#         elif z_score > NET_FLOW_SIGMA:
#             print(f"⚠️ {symbol} Fake-Pump Prevented | Z-Score: {z_score:.2f} but Imbalance is {imbalance:.2f} (Sell Wall in the way)")
#
#         # 🚀 修正：回傳值增加 z_score 以配合 main.py 需求 (4 個變數)
#         return net_flow, df['price'].iloc[-1], is_strong, z_score
#
#     except Exception as e:
#         print(f"❌ 錯誤 [{symbol}] Lee-Ready: {e}")
#         # 🚀 修正：錯誤時也要回傳 4 個值，防止 ValueError
#         return 0, 0, False, 0


# 🚀 妖幣專用版：Lee-Ready 防接盤狙擊模式 (ver 2026-04-06)
def apply_lee_ready_logic(symbol):
    try:
        # 拉取長視窗 (200筆)
        trades = exchange.fetch_trades(symbol, limit=200)
        if not trades: return 0, 0, False, 0

        df = pd.DataFrame(trades)
        df['price_change'] = df['price'].diff()
        df['direction'] = np.where(df['price_change'] > 0, 1, np.where(df['price_change'] < 0, -1, 0))

        # 🚀 修正：解決 Pandas 報錯 Bug
        df['direction'] = df['direction'].replace(0, np.nan).ffill().fillna(0)

        # 🚀 新增 D：大單加權 (大於平均量 2 倍的單，權重 x 2)
        avg_vol = df['amount'].mean()
        df['weight'] = np.where(df['amount'] > avg_vol * 2, 2.0, 1.0)
        df['net_flow'] = df['direction'] * df['amount'] * df['price'] * df['weight']

        # 計算長窗 (200) 與 短窗 (最近 50) 的 Net Flow
        long_window_flow = df['net_flow'].sum()
        short_window_flow = df['net_flow'].tail(50).sum()

        # 🚀 新增 A：加速度 (Acceleration)
        recent_25_flow = df['net_flow'].tail(25).sum()
        prev_25_flow = df['net_flow'].iloc[-50:-25].sum()
        acceleration = recent_25_flow - prev_25_flow

        # 🚀 新增 C：結合訂單簿 (Orderbook) 失衡預判
        try:
            ob = exchange.fetch_order_book(symbol, limit=20)
            bids_vol = sum([b[1] for b in ob['bids']])
            asks_vol = sum([a[1] for a in ob['asks']])
            imbalance = (bids_vol - asks_vol) / (bids_vol + asks_vol) if (bids_vol + asks_vol) > 0 else 0
        except:
            imbalance = 0

        is_strong = False
        z_score = 0
        if df['net_flow'].std() > 0:
            z_score = short_window_flow / df['net_flow'].std()

        # ================= 🚀 妖幣防接盤核心判斷 =================

        # 🎯 條件 1 [極速狙擊]：短窗有正向流入 + 加速度爆發 + 買盤失衡 > 25%
        # (將 Imbalance 提高到 25%，過濾莊家假單)
        if (short_window_flow > 0) and (acceleration > 0) and (imbalance > 0.25):
            is_strong = True
            print(f"🔥 {symbol} Altcoin Sniper Entry! Accel: {acceleration:.0f} | Imbalance: {imbalance:.2f}")

        # 🎯 條件 2 [趨勢確認]：Z-Score 必須突破 2.0 甚至 2.5
        # (絕對不能用 1.2！恢復您原本 strategy.py 的高門檻設計)
        elif z_score > 2.0:
            is_strong = True
            print(f"📈 {symbol} Altcoin Z-Score Validated: {z_score:.2f}")

        # 🛡️ 終極防接盤機制：如果 Z-Score 很高，但訂單簿出現極大賣壓 (Imbalance < -0.1)
        # 代表莊家正在利用散戶的買盤瘋狂出貨 (Sell Wall)，立刻取消信號！
        if is_strong and imbalance < -0.1:
            is_strong = False
            print(f"⚠️ {symbol} 發現莊家砸盤陷阱！Z-score 雖達標但賣壓極重 (Imbalance: {imbalance:.2f})，取消進場！")

        last_p = df['price'].iloc[-1]
        return short_window_flow, last_p, is_strong, z_score

    except Exception as e:
        print(f"⚠️ LR Logic Error [{symbol}]: {e}")
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