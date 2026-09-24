"""Snapshot of RandomX mining economics -> results/economics_snapshot.json.

Revenue per (H/s x day) from the chain tip and the XMR/USD price, and the
efficiency a miner needs (H/s per watt of wall power) to cover electricity.
Network data changes every block, so this file is a dated snapshot, keyed by
block height rather than by wall-clock time.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rxlab import econ, sysinfo  # noqa: E402

OUT = ROOT / "results" / "economics_snapshot.json"
KWH_PRICES = (0.05, 0.10, 0.15, 0.20)


def main():
    chain = econ.chain_snapshot()
    usd, quotes = econ.xmr_usd()
    tip = econ.usd_per_hs_day(chain["reward_xmr"], usd, chain["difficulty"])
    day = econ.usd_per_hs_day(chain["window_mean_reward_xmr"], usd, chain["window_mean_difficulty"])
    result = {
        "machine": sysinfo.describe(),
        "chain": chain,
        "price": {"usd_per_xmr": usd, "quotes": quotes},
        "formula": "usd_per_hs_day = 86400 * reward_xmr * usd_per_xmr / difficulty; "
                   "breakeven_h_per_w = 0.024 kWh/(W*day) * usd_per_kwh / usd_per_hs_day",
        "network_hashrate_hs": chain["difficulty"] / chain["target_seconds"],
        "tail_emission_xmr": econ.TAIL_EMISSION_XMR,
        "fees_share_of_tip_reward": 1 - econ.TAIL_EMISSION_XMR / chain["reward_xmr"],
        "usd_per_hs_day": tip,
        "usd_per_khs_day": tip * 1000,
        "xmr_per_khs_day": 1000 * econ.SECONDS_PER_DAY * chain["reward_xmr"] / chain["difficulty"],
        "usd_per_hs_day_window_mean": day,
        "breakeven_h_per_w": {f"{p:.2f}": econ.breakeven_h_per_w(tip, p) for p in KWH_PRICES},
        "breakeven_h_per_w_window_mean": {f"{p:.2f}": econ.breakeven_h_per_w(day, p) for p in KWH_PRICES},
        "notes": [
            "Break-even ignores pool fees (typically 0-1%), hardware cost and luck variance.",
            "H/W means hashrate per watt at the wall, not per watt of CPU package power.",
            "Window-mean values use the last window_blocks headers from the same node.",
        ],
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2) + "\n")
    print(f"height {chain['height']}  difficulty {chain['difficulty']:.4g}  "
          f"reward {chain['reward_xmr']:.4f} XMR  price ${usd:.2f}")
    print(f"USD per kH/s-day {tip * 1000:.4f}  break-even H/W: " + ", ".join(
        f"${k}/kWh -> {v:.1f}" for k, v in result["breakeven_h_per_w"].items()))


if __name__ == "__main__":
    main()
