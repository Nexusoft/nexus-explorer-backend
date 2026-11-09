"""Optional block indexer for the explorer.

Keeps a SQLite copy of blocks, transactions and value moves so the explorer can
list transfers across the whole chain, and caches the slow register lists
(rich list, trust list, global names). Runs next to the node:

    python indexer.py            # forever: follow the tip, backfill, refresh caches

The Flask app (app.py) serves the data under /explorer/... when INDEX_DB exists.
"""
import json
import logging
import os
import sqlite3
import time

import requests
from dotenv import load_dotenv

load_dotenv("local.env")

SERVER_URL = os.getenv("SERVER_URL", "http://127.0.0.1:8080").rstrip("/")
INDEX_DB = os.getenv("INDEX_DB", "explorer_index.sqlite")
PAGE = 100
FOLLOW_SECONDS = 10
CACHE_SECONDS = 600
REORG_DEPTH = 20
REWARD_OPS = ("COINBASE", "TRUST", "GENESIS")
#pre Tritium mining, staking and first staking rewards carry no contracts, only a type
LEGACY_REWARD_TYPES = ("legacy base", "legacy trust", "legacy genesis")
#node answers 500 or drops the connection on transactions it cannot decode
UNREADABLE = (requests.HTTPError, requests.ConnectionError)

log = logging.getLogger("indexer")

SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks (
  height INTEGER PRIMARY KEY, hash TEXT, time INTEGER, channel INTEGER,
  mint REAL, txcount INTEGER, size INTEGER);
CREATE TABLE IF NOT EXISTS txs (
  txid TEXT PRIMARY KEY, height INTEGER, time INTEGER, type TEXT,
  contracts INTEGER, is_reward INTEGER, amount REAL, ticker TEXT, ops TEXT);
CREATE INDEX IF NOT EXISTS txs_height ON txs(height);
CREATE INDEX IF NOT EXISTS txs_transfer ON txs(is_reward, height);
CREATE TABLE IF NOT EXISTS moves (
  txid TEXT, idx INTEGER, height INTEGER, time INTEGER, op TEXT,
  from_addr TEXT, from_name TEXT, to_addr TEXT, to_name TEXT,
  amount REAL, ticker TEXT, PRIMARY KEY (txid, idx));
CREATE INDEX IF NOT EXISTS moves_from ON moves(from_addr, height);
CREATE INDEX IF NOT EXISTS moves_to ON moves(to_addr, height);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, updated INTEGER, data TEXT);
CREATE TABLE IF NOT EXISTS registers (
  address TEXT PRIMARY KEY, owner TEXT, kind TEXT, name TEXT, token TEXT, ticker TEXT,
  balance REAL, info TEXT, created INTEGER, modified INTEGER, id TEXT);
CREATE INDEX IF NOT EXISTS registers_owner ON registers(owner);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS daily (
  day INTEGER PRIMARY KEY, first_height INTEGER,
  blocks_stake INTEGER, blocks_prime INTEGER, blocks_hash INTEGER,
  mint_stake REAL, mint_prime REAL, mint_hash REAL, txs INTEGER, transfers INTEGER);
"""


def connect(path=INDEX_DB):
    db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    #registers made before the id column
    if "id" not in [c[1] for c in db.execute("PRAGMA table_info(registers)")]:
        db.execute("ALTER TABLE registers ADD COLUMN id TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS registers_id ON registers(id)")
    if get_meta(db, "legacy_rewards_fixed") is None:
        fix_legacy_rewards(db)
    return db


def fix_legacy_rewards(db, step=100000):
    """Indexes made before LEGACY_REWARD_TYPES stored legacy rewards as transfers"""
    marks = ",".join("?" * len(LEGACY_REWARD_TYPES))
    top = db.execute("SELECT MAX(height) FROM txs").fetchone()[0] or 0
    for low in range(0, top + 1, step):
        db.execute(f"UPDATE txs SET is_reward = 1 WHERE height >= ? AND height < ? AND is_reward = 0 "
                   f"AND type IN ({marks})", (low, low + step, *LEGACY_REWARD_TYPES))
        db.commit()
    #the per day transfer counts came from the same flag
    db.execute("DELETE FROM daily")
    set_meta(db, "legacy_rewards_fixed", 1)
    db.commit()


BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def register_id(address):
    """64 hex form of a base58 register address, as the node prints owners of tokenized
    registers: type byte + 32 bytes whose 4 byte words come in reverse order"""
    try:
        n = 0
        for ch in address:
            n = n * 58 + BASE58.index(ch)
        raw = n.to_bytes(37, "big")[1:33]
    except (TypeError, ValueError, OverflowError):
        return None
    return b"".join(raw[i:i + 4] for i in range(28, -1, -4)).hex()


def api(path, **params):
    r = requests.get(f"{SERVER_URL}/{path}", params=params, timeout=120)
    r.raise_for_status()
    return r.json()["result"]


def party(p):
    if not p:
        return None, None
    if isinstance(p, str):
        return p, None
    return p.get("address"), p.get("name")


def reward_contract(c):
    return c.get("OP") in REWARD_OPS or (c.get("OP") == "CREDIT" and c.get("for") == "COINBASE")


def is_reward(tx):
    """only rewards (a fee next to a reward claim does not make it a transfer)"""
    if tx.get("type") in LEGACY_REWARD_TYPES:
        return True
    contracts = tx.get("contracts") or []
    return any(reward_contract(c) for c in contracts) and all(
        reward_contract(c) or c.get("OP") == "FEE" for c in contracts)


def moves_of(tx):
    """(op, from, from_name, to, to_name, amount, ticker) per contract or legacy output"""
    out = []
    for c in tx.get("contracts") or []:
        op = c.get("OP", "")
        f, fn = party(c.get("from"))
        t, tn = party(c.get("to"))
        if op in ("TRUST", "GENESIS", "STAKE", "UNSTAKE"):
            t, tn = c.get("address"), c.get("name")
        elif op == "COINBASE":
            t = c.get("genesis")
        elif op in ("CREATE", "WRITE", "APPEND", "TRANSFER", "CLAIM") and c.get("address"):
            t, tn = c.get("address"), c.get("name")
        out.append((op, f, fn, t, tn, c.get("amount"), c.get("ticker")))
    #legacy (pre Tritium) transactions carry inputs / outputs instead of contracts
    for o in tx.get("outputs") or []:
        if isinstance(o, dict):
            out.append(("LEGACY", None, None, o.get("address"), None, o.get("amount"), "NXS"))
    return out


def store_block(db, b):
    txs = b.get("tx") or []
    db.execute("INSERT OR REPLACE INTO blocks VALUES (?,?,?,?,?,?,?)",
               (b["height"], b.get("hash"), b.get("timestamp"), b.get("channel"),
                b.get("mint"), len(txs), b.get("size")))
    for tx in txs:
        if not isinstance(tx, dict):
            continue
        mv = moves_of(tx)
        amount = sum(m[5] or 0 for m in mv)
        ticker = next((m[6] for m in mv if m[6]), None)
        ops = ",".join(sorted({m[0] for m in mv}))
        db.execute("INSERT OR REPLACE INTO txs VALUES (?,?,?,?,?,?,?,?,?)",
                   (tx["txid"], b["height"], tx.get("timestamp"), tx.get("type"),
                    len(tx.get("contracts") or []), int(is_reward(tx)), amount, ticker, ops))
        db.executemany("INSERT OR REPLACE INTO moves VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       [(tx["txid"], i, b["height"], tx.get("timestamp"), *m) for i, m in enumerate(mv)])


def delete_from(db, height):
    db.execute("DELETE FROM moves WHERE height >= ?", (height,))
    db.execute("DELETE FROM txs WHERE height >= ?", (height,))
    db.execute("DELETE FROM blocks WHERE height >= ?", (height,))


def get_meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))


def get_block(height):
    """one block; if the node cannot decode a transaction, keep the others and mark that one"""
    try:
        return api("ledger/get/block", height=height, verbose="summary")
    except UNREADABLE:
        b = api("ledger/get/block", height=height)
    txs = []
    for t in b.get("tx") or []:
        txid = t if isinstance(t, str) else t.get("txid")
        try:
            txs.append(api("ledger/get/transaction", txid=txid, verbose="summary"))
        except UNREADABLE:
            tip_height()  #raises if the node itself is down, so we retry later instead
            log.warning("block %s: node cannot read tx %s", height, txid)
            txs.append({"txid": txid, "timestamp": b.get("timestamp"), "type": "unreadable"})
    b["tx"] = txs
    return b


def list_blocks(page):
    """a page of blocks; falls back to block by block when one block in it fails"""
    try:
        return api("ledger/list/blocks", verbose="summary", limit=PAGE, page=page)
    except UNREADABLE:
        top = tip_height() - page * PAGE
        return [get_block(h) for h in range(top, max(0, top - PAGE), -1)]


def tip_height():
    return api("system/get/info")["blocks"]


def follow_tip(db):
    """Index blocks above the highest stored one, newest page first"""
    tip = tip_height()
    top = db.execute("SELECT MAX(height), (SELECT hash FROM blocks ORDER BY height DESC LIMIT 1) FROM blocks").fetchone()
    high, high_hash = top
    if high is None:
        blocks = list_blocks(0)
        for b in blocks:
            store_block(db, b)
        set_meta(db, "backfill_low", min(b["height"] for b in blocks))
        db.commit()
        return
    #reorg: our top block is no longer on the main chain
    node_hash = api("ledger/get/block", height=high, verbose="summary").get("hash")
    if node_hash != high_hash:
        log.warning("reorg at %s, dropping last %s blocks", high, REORG_DEPTH)
        delete_from(db, high - REORG_DEPTH)
        db.commit()
        return
    page = 0
    while high < tip:
        blocks = list_blocks(page)
        new = [b for b in blocks if b["height"] > high]
        for b in new:
            store_block(db, b)
        if len(new) < len(blocks) or not blocks:
            break
        page += 1
    db.commit()


def backfill(db, budget_seconds=20):
    """Walk down from backfill_low toward genesis for a while"""
    low = int(get_meta(db, "backfill_low", "0") or 0)
    if low <= 1:
        return False
    start = time.time()
    while low > 1 and time.time() - start < budget_seconds:
        tip = tip_height()
        page = max(0, (tip - (low - 1)) // PAGE)
        blocks = list_blocks(page)
        if not blocks:
            break
        for b in blocks:
            store_block(db, b)
        new_low = min(b["height"] for b in blocks)
        #tip moved while paging: fill the hole block by block
        if new_low >= low:
            for h in range(low - 1, max(0, low - 1 - PAGE), -1):
                store_block(db, get_block(h))
            new_low = max(1, low - PAGE)
        low = new_low
        set_meta(db, "backfill_low", low)
        db.commit()
    return True


NXS_UNITS = 1e6
MARKET_LIMIT = 500


def market_side(c):
    #market/list gives NXS amounts in base units and token amounts in whole units
    amount = c.get("amount") or 0
    if str(c.get("token")) == "0":
        amount = amount / NXS_UNITS
    return {"amount": amount, "ticker": c.get("ticker"), "token": c.get("token"),
            "account": c.get("from") or c.get("to")}


def market_order(o):
    return {"txid": o.get("txid"), "timestamp": o.get("timestamp"), "owner": o.get("owner"),
            "type": o.get("type"), "give": market_side(o.get("contract") or {}),
            "want": market_side(o.get("order") or {})}


def markets():
    """Open orders and filled orders of every NXS/<ticker> pair that has any"""
    tokens = api("register/list/finance:tokens", limit=1000)
    out = []
    for ticker in sorted({t["ticker"] for t in tokens if t.get("ticker")}):
        pair = f"NXS/{ticker}"
        try:
            book = api("market/list/order", market=pair, limit=MARKET_LIMIT)
            done = api("market/list/executed", market=pair, limit=MARKET_LIMIT)
        except Exception as e:
            log.debug("market %s: %s", pair, e)
            continue
        orders = [market_order(o) for k in ("asks", "bids") for o in book.get(k) or []]
        executed = [market_order(o) for k in ("asks", "bids") for o in done.get(k) or []]
        if orders or executed:
            out.append({"market": pair, "orders": orders, "executed": executed})
    return out


ASSET_LISTS = ("assets:asset", "assets:raw", "assets:readonly", "supply:items")
ASSET_TYPES = ("OBJECT", "RAW", "READONLY")
LIST_LIMIT = 1000
MAX_LIST_PAGES = 1000


def list_all(path):
    out = []
    for page in range(MAX_LIST_PAGES):
        rows = api(path, limit=LIST_LIMIT, page=page) or []
        out += rows
        if len(rows) < LIST_LIMIT:
            break
    return out


def asset_row(a):
    #RAW/READONLY assets keep their content in "json", objects in their own fields
    content = a.get("json") if isinstance(a.get("json"), dict) else {}
    fmt = a.get("type")
    #an object can have its own field called "type", which then hides the register type
    own_type = None
    if fmt not in ASSET_TYPES:
        own_type, fmt = fmt, "OBJECT"
    #supply items are objects too, told apart only by their form
    if a.get("form") == "SUPPLY":
        fmt = "SUPPLY"
    kind = a.get("distordia-type") or content.get("type") or own_type
    if not kind and a.get("bounty_id"):
        kind = "bounty"
    label = (a.get("title") or content.get("title") or content.get("name") or a.get("question")
             or a.get("name"))
    return {"address": a.get("address"), "name": a.get("name"),
            "label": label if isinstance(label, str) else None,
            "kind": kind if isinstance(kind, str) else None, "type": fmt,
            "owner": a.get("owner"), "created": a.get("created"), "modified": a.get("modified")}


def assets():
    """Every asset and supply item register, newest change first, without its content"""
    rows = [asset_row(a) for path in ASSET_LISTS for a in list_all(f"register/list/{path}")]
    return sorted(rows, key=lambda r: r.get("modified") or 0, reverse=True)


OWNERS_SECONDS = 3600
HOLDERS_TOP = 100


def token_holders(accounts_all):
    """Per token: holder count and the largest accounts"""
    by_token = {}
    for a in accounts_all:
        token = str(a.get("token"))
        if token != "0" and (a.get("balance") or 0) > 0:
            by_token.setdefault(token, []).append(
                {"address": a.get("address"), "owner": a.get("owner"),
                 "balance": a.get("balance"), "modified": a.get("modified")})
    out = {}
    for token, accounts in by_token.items():
        accounts.sort(key=lambda a: a["balance"], reverse=True)
        out[token] = {"accounts": len(accounts),
                      "owners": len({a["owner"] for a in accounts}),
                      "top": accounts[:HOLDERS_TOP]}
    return out


#key hash slots of a crypto register, TAO/Register/create.cpp
CRYPTO_KEYS = ("auth", "lisp", "network", "sign", "verify", "cert", "app1", "app2", "app3")


def register_row(kind, r, name=None, info=None):
    return (r.get("address"), r.get("owner"), kind, name if name is not None else r.get("name"),
            str(r["token"]) if r.get("token") is not None else None, r.get("ticker"),
            r.get("balance"), info, r.get("created"), r.get("modified"))


def scan_owners(db):
    """Fills the registers table (who owns what) and returns the token holders; about a minute"""
    accounts_all = list_all("register/list/finance:accounts")
    rows = [register_row("account", a) for a in accounts_all]
    rows += [register_row("trust", t, info=json.dumps({"stake": t.get("stake"), "trust": t.get("trust")}))
             for t in list_all("register/list/finance:trust")]
    rows += [register_row("token", t, name=t.get("ticker"),
                          info=json.dumps({"currentsupply": t.get("currentsupply"), "maxsupply": t.get("maxsupply")}))
             for t in list_all("register/list/finance:tokens")]
    for a in (asset_row(a) for path in ASSET_LISTS for a in list_all(f"register/list/{path}")):
        rows.append((a["address"], a["owner"], "asset", a["label"] or a["name"], None, None, None,
                     json.dumps({"format": a["type"], "kind": a["kind"]}), a["created"], a["modified"]))
    for n in list_all("register/list/names:names") + list_all("register/list/names:global"):
        rows.append(register_row("name", n, info=json.dumps({"register": n.get("register"),
                                                             "global": bool(n.get("global"))})))
    rows += [register_row("namespace", n, name=n.get("namespace"))
             for n in list_all("register/list/names:namespaces")]
    #one per signature chain, info = the key hashes that are set
    rows += [register_row("crypto", c, name="crypto", info=json.dumps(
        {k: c[k] for k in CRYPTO_KEYS if str(c.get(k) or "").strip("0")}))
             for c in list_all("register/list/crypto")]
    rows = [(*r, register_id(r[0])) for r in rows]
    with db:
        db.execute("DELETE FROM registers")
        db.executemany("INSERT OR REPLACE INTO registers (address, owner, kind, name, token, ticker, "
                       "balance, info, created, modified, id) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    log.info("owner index: %s registers", len(rows))
    return token_holders(accounts_all)


def cache_age(db, key):
    row = db.execute("SELECT updated FROM cache WHERE key=?", (key,)).fetchone()
    return time.time() - row[0] if row else None


DAY = 86400


def update_daily(db):
    """Per UTC day: blocks and minted NXS per channel, transactions, transfers. The first run
    reads the whole chain (minutes); later runs redo only the last two stored days."""
    if get_meta(db, "backfill_low") not in ("0", "1"):
        return None  #days before the backfilled part would be counted short
    last = db.execute("SELECT MAX(day) FROM daily").fetchone()[0]
    start = 0
    if last is not None:
        row = db.execute("SELECT first_height FROM daily WHERE day = ?", (last - 1,)).fetchone()
        start = row[0] if row else 0
    days = {}
    for day, channel, n, mint, height in db.execute(
            "SELECT time / ?, channel, COUNT(*), SUM(mint), MIN(height) FROM blocks "
            "WHERE height >= ? GROUP BY time / ?, channel", (DAY, start, DAY)):
        d = days.setdefault(day, {"first": height, "blocks": [0, 0, 0], "mint": [0.0, 0.0, 0.0],
                                  "txs": 0, "transfers": 0})
        d["first"] = min(d["first"], height)
        if channel in (0, 1, 2):
            d["blocks"][channel] = n
            d["mint"][channel] = mint or 0.0
    for day, n, transfers in db.execute(
            "SELECT time / ?, COUNT(*), SUM(is_reward = 0 AND type != 'unreadable') FROM txs "
            "WHERE height >= ? GROUP BY time / ?", (DAY, start, DAY)):
        if day in days:
            days[day]["txs"], days[day]["transfers"] = n, transfers or 0
    with db:
        db.executemany("INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?,?,?,?,?)",
                       [(day, d["first"], *d["blocks"], *(round(m, 6) for m in d["mint"]),
                         d["txs"], d["transfers"]) for day, d in days.items()])
    #[day, stake, prime, hash blocks, stake, prime, hash mint, txs, transfers]
    return [list(r) for r in db.execute(
        "SELECT day, blocks_stake, blocks_prime, blocks_hash, mint_stake, mint_prime, mint_hash, "
        "txs, transfers FROM daily ORDER BY day")]


def refresh_caches(db):
    jobs = {
        "richlist": lambda: api("register/list/finance:accounts,finance:trust",
                                sort="total", order="desc", limit=500, page=0,
                                where="results.token=0"),
        "trustlist": lambda: api("register/list/finance:trust", sort="trust", order="desc", limit=2000),
        "globalnames": lambda: api("register/list/names:global", limit=5000),
        "namespaces": lambda: api("register/list/names:namespaces", limit=5000),
        "tokens": lambda: api("register/list/finance:tokens", sort="maxsupply", limit=1000),
        #takes the node over 30 s, too slow to ask per page view
        "ledgermetrics": lambda: api("ledger/get/metrics"),
        "markets": markets,
        "assets": assets,
        "daily": lambda: update_daily(db),
    }
    #the full register scan only once an hour; token holders come out of it
    age = cache_age(db, "tokenholders")
    if age is None or age >= OWNERS_SECONDS:
        jobs["tokenholders"] = lambda: scan_owners(db)
    for key, job in jobs.items():
        try:
            data = job()
            if data is None:
                continue
            db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, int(time.time()), json.dumps(data)))
            db.commit()
        except Exception as e:
            log.warning("cache %s failed: %s", key, e)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db = connect()
    last_cache = 0
    while True:
        try:
            follow_tip(db)
            if time.time() - last_cache > CACHE_SECONDS:
                refresh_caches(db)
                last_cache = time.time()
            busy = backfill(db)
            log.info("indexed down to %s", get_meta(db, "backfill_low"))
            if not busy:
                time.sleep(FOLLOW_SECONDS)
        except Exception as e:
            #requests puts the url (with the node login) in the message
            log.error("indexer error: %s", str(e).split(" for url:")[0])
            time.sleep(FOLLOW_SECONDS)


if __name__ == "__main__":
    main()
