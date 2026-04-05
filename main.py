import time
import logging
import sys
from core.connect import get_live_usdt_balance, config
from core.strategy import get_btc_regime, scouting_top_coins, apply_lee_ready_logic, get_market_metrics
from core.engine import manage_long_positions, execute_live_long, positions

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('AlgoTrade_V6_Main')


def main():
    print(f"🚀 AI AlgoTrade V6.0 Modular Final (Event-Driven) Started...")
    print(f"Lee-Ready Money Flow + Order Book Imbalance + P95 Filter # Initializing...")
    last_scout_time = 0
    t_cfg = config['TRADING']

    while True:
        try:
            # 1. 倉位管理與防守，並獲取是否在「危險/獲利區」信號
            is_critical = manage_long_positions()
            curr_t = time.time()

            # 2. 定期偵測與進攻
            if curr_t - last_scout_time > t_cfg['scouting_interval']:
                regime = get_btc_regime()

                if regime == 1:
                    print("🟢 GREEN: Executing Top Coins Scouting...")
                    target_coins = scouting_top_coins(5)

                    for s in target_coins:
                        try:
                            flow, last_p, is_strong, z_score = apply_lee_ready_logic(s)
                            atr, is_v = get_market_metrics(s)
                            if last_p > 0:
                                execute_live_long(s, flow, last_p, is_strong, atr, is_v, z_score)
                        except Exception as e:
                            logger.warning(f"⚠️ {s} Analysis Failed: {e}")
                            continue
                        time.sleep(1.5)
                else:
                    print(f"🚦 Current Navigation Status: {regime}, Scouting Paused.")

                last_scout_time = curr_t
                print(
                    f"⏳ Patrol Complete | Positions: {list(positions.keys())} | Balance: {get_live_usdt_balance():.2f}")

            # 3. 🔥 事件驅動監控頻率 (Event-Driven Sleep)
            if is_critical:
                print("⚡ Critical Zone Detected: Accelerating patrol frequency to 2 seconds!")
                time.sleep(2)
            else:
                time.sleep(t_cfg['pos_check_interval'])

        # ✅ 修正：KeyboardInterrupt 必須放在 Exception 前面
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