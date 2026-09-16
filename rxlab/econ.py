"""Mining economics snapshot: Monero difficulty and reward, XMR/USD, break-even H/W.

Expected revenue of 1 H/s for one day is 86400 * reward / difficulty XMR
(each hash wins a block with probability 1/difficulty). Chain data comes from
public monerod JSON-RPC nodes (every node is asked; the answers are recorded),
price from CoinGecko with Kraken as a cross-check.
"""
import http.client
import json
import statistics
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ATOMIC = 10 ** 12
TAIL_EMISSION_XMR = 0.6
SECONDS_PER_DAY = 86400
USER_AGENT = "rxlab/0.1 (RandomX research)"
# a node or price API that fails in any of these ways is recorded as failed, not fatal
_NET_ERRORS = (urllib.error.URLError, http.client.HTTPException, OSError, ValueError)

NODES = [
    "http://xmr-node.cakewallet.com:18081",
    "http://nodes.hashvault.pro:18081",
    "http://node.monerodevs.org:18089",
    "http://node.sethforprivacy.com:18089",
    "http://monero.stackwallet.com:18081",
    "http://node.moneroworld.com:18089",
    "http://node.community.rino.io:18081",
    "http://p2pmd.xmrvsbeast.com:18081",
]
COINGECKO = "https://api.coingecko.com/api/v3/simple/price?ids=monero&vs_currencies=usd&include_last_updated_at=true"
KRAKEN = "https://api.kraken.com/0/public/Ticker?pair=XMRUSD"


def _http_json(url, body=None, timeout=15):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": USER_AGENT, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def rpc(node, method, params=None, timeout=15):
    body = {"jsonrpc": "2.0", "id": "0", "method": method}
    if params:
        body["params"] = params
    reply = _http_json(node + "/json_rpc", body, timeout)
    if "result" not in reply:
        raise RuntimeError(f"{method}: {reply.get('error')}")
    return reply["result"]


def _ask(node):
    try:
        h = rpc(node, "get_last_block_header")["block_header"]
        return {"node": node, "ok": True, "height": h["height"], "hash": h["hash"]}
    except _NET_ERRORS + (RuntimeError, KeyError) as err:
        return {"node": node, "ok": False, "error": type(err).__name__}


def chain_snapshot(nodes=NODES, window=720):
    """Tip header, get_info and the last `window` headers from the best-agreed node."""
    with ThreadPoolExecutor(4) as pool:
        polls = list(pool.map(_ask, nodes))
    live = [p for p in polls if p["ok"]]
    if not live:
        raise RuntimeError("no Monero node answered")
    # the tip most nodes agree on, then the tallest; avoids a lagging or forked node
    tips = [(p["height"], p["hash"]) for p in live]
    height, tip_hash = max(sorted(set(tips)), key=lambda t: (tips.count(t), t[0]))
    node = next(p["node"] for p in live if (p["height"], p["hash"]) == (height, tip_hash))
    info = rpc(node, "get_info")
    headers = rpc(node, "get_block_headers_range",
                  {"start_height": height - window + 1, "end_height": height})["headers"]
    top = headers[-1]
    if top["hash"] != tip_hash:
        raise RuntimeError(f"{node} changed tip during the snapshot")
    rewards = [h["reward"] / ATOMIC for h in headers]
    return {
        "source": node,
        "nodes_polled": polls,
        "nodes_agreeing": sum(t == (height, tip_hash) for t in tips),
        "height": height,
        "block_hash": tip_hash,
        "block_timestamp": top["timestamp"],
        "difficulty": int(top["wide_difficulty"], 16),
        "reward_xmr": top["reward"] / ATOMIC,
        "target_seconds": info.get("target"),
        "window_blocks": len(headers),
        "window_mean_reward_xmr": statistics.fmean(rewards),
        "window_mean_difficulty": statistics.fmean(int(h["wide_difficulty"], 16) for h in headers),
        "window_seconds": headers[-1]["timestamp"] - headers[0]["timestamp"],
    }


def xmr_usd():
    """XMR/USD from CoinGecko (primary) and Kraken (cross-check); records failures."""
    quotes = []
    try:
        cg = _http_json(COINGECKO)["monero"]
        quotes.append({"source": "coingecko", "url": COINGECKO, "usd": float(cg["usd"]),
                       "source_timestamp": cg.get("last_updated_at")})
    except _NET_ERRORS + (KeyError,) as err:
        quotes.append({"source": "coingecko", "url": COINGECKO, "error": type(err).__name__})
    try:
        kr = _http_json(KRAKEN)["result"]
        pair = next(iter(kr.values()))
        quotes.append({"source": "kraken", "url": KRAKEN, "usd": float(pair["c"][0])})
    except _NET_ERRORS + (KeyError, StopIteration) as err:
        quotes.append({"source": "kraken", "url": KRAKEN, "error": type(err).__name__})
    ok = [q for q in quotes if "usd" in q]
    if not ok:
        raise RuntimeError("no XMR/USD quote")
    return ok[0]["usd"], quotes


def usd_per_hs_day(reward_xmr, usd_per_xmr, difficulty):
    """Expected USD earned by 1 H/s running for one day."""
    return SECONDS_PER_DAY * reward_xmr * usd_per_xmr / difficulty


def breakeven_h_per_w(revenue_per_hs_day, usd_per_kwh):
    """Efficiency (H/s per watt) at which 1 W of draw exactly pays for itself."""
    return 24 / 1000 * usd_per_kwh / revenue_per_hs_day
