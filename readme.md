### Nexus explorer backend

### Features

1. Proxifies all HTTP requests over HTTPS
2. Caches responses on the server, based on the Cache-control header (reduces direct API hits to the Nexus core)

### Python3.10 fix 
`git checkout python310`

### Installation: 
`pip3 install -r requirements.txt`

### Usage:
#### Method 1 - Waiterss:
To run the production server
```sh
sudo apt install python3-waitress
SERVER_URL="http://api.nexus-interactions.io:8080" waitress-serve --listen=*:5001 app:app
```
#### or Method 2 - Gunicorn:
```sh
sudo apt install gunicorn3
SERVER_URL="http://api.nexus-interactions.io:8080" gunicorn3 -w 1 -b 0.0.0.0:5001 app:app
```
To increase number of worker threads for gunicorn3 use -w option.
## Optional indexer

`indexer.py` keeps a SQLite index next to the node (`INDEX_DB`, default `explorer_index.sqlite`):
blocks, transactions and value moves for the whole chain, plus cached copies of the slow
register lists (rich list, trust list, global names, namespaces, tokens, assets and supply items), of the ledger
metrics and of the on-chain market (open and filled orders of every NXS/<token> pair),
refreshed every 10 minutes, and of per UTC day figures (`daily`: blocks and minted NXS per channel, transactions, transfers; the first run reads the whole chain, about 6 minutes, later runs only the last two days). Mining and staking rewards, legacy ones included, do not count as transfers. Once an hour the indexer also scans every register list (about 80 s on mainnet) into an owner
index, the `registers` table: who owns which account, trust account, token, asset, name,
namespace and crypto register (key hashes), with each register's address also in hex (`id`). The token holders (holder count and largest accounts of every token) come from
the same scan.

```
python indexer.py
```

It follows the tip every 10 seconds and backfills older blocks in the background
(a full mainnet backfill takes about an hour). `app.py` serves the index read-only:

- `GET /explorer/status` - indexed height range and cache times
- `GET /explorer/transactions?limit=25&transfers=1&before=<height>` - newest first, `next` = cursor for the following page
- `GET /explorer/address/<address>/transactions?limit=25&registers=1&before=<height>` - newest first, `registers=1` = only operations on the register itself (create, write, append, transfer, claim: its history), `next` = cursor
- `GET /explorer/register/<64 hex>` - base58 address of a register given in hex, the form the node uses for the owner of a tokenized register (owned by a token)
- `GET /explorer/owner/<genesis>` - accounts, trust accounts, tokens, assets, names and namespaces owned by one signature chain (hourly owner index, up to 1000 rows, `total` = all)
- `GET /explorer/owner/<genesis>/transactions?limit=25&transfers=1&before=<height>` - value moves to or from any account, trust account or token of that signature chain, newest first; `addresses` = the chain's registers counted, `next` = cursor
- `GET /explorer/search?q=<text>` - tokens, global names, namespaces and assets whose name starts with the text (any case), exact matches first, up to 50
- `GET /explorer/cache/<richlist|trustlist|globalnames|namespaces|tokens|ledgermetrics|markets|assets|tokenholders|daily>`

Without the index these routes answer 503 and the frontend falls back to the node API.
