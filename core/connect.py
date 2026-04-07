import ccxt
import yaml
import os

# 讀取配置文件
def load_config():
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'config.yaml')
    with open(config_path, 'r', encoding='utf-8') as file:
        return yaml.safe_load(file)

config = load_config()

# 建立交易所連線
exchange = ccxt.bybit({
    'apiKey': config['ACCOUNTS']['main_account']['apiKey'],
    'secret': config['ACCOUNTS']['main_account']['secret'],
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'},
    'hostname': 'bytick.com',
})
exchange.load_markets()

def get_live_usdt_balance():
    """獲取實時 USDT 餘額"""
    try:
        return float(exchange.fetch_balance()['USDT']['free'])
    except:
        return 0.0

def cancel_all_v5(symbol):
    """💣 核彈級撤單：清理所有掛單與倉位綁定的 TP/SL"""
    try:
        exchange.cancel_all_orders(symbol, params={'category': 'linear'})
        exchange.cancel_all_orders(symbol, params={'category': 'linear', 'orderFilter': 'StopOrder'})
        exchange.cancel_all_orders(symbol, params={'category': 'linear', 'orderFilter': 'tpslOrder'}) # 專殺止盈止損單
    except:
        pass
    try:
        # 徹底將倉位上嘅 TP/SL 強制歸零
        exchange.private_post_v5_position_trading_stop({
            'category': 'linear',
            'symbol': exchange.market_id(symbol),
            'takeProfit': "0",
            'stopLoss': "0",
            'positionIdx': 0
        })
    except:
        pass

def get_3_layer_avg_price(symbol, side='asks'):
    """獲取訂單簿前三層的平均價格"""
    try:
        ob = exchange.fetch_order_book(symbol, limit=5)
        levels = ob[side][:3]
        return sum([level[0] for level in levels]) / len(levels)
    except:
        return None