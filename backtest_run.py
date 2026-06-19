import os, json, glob, numpy as np

tool_dir = r'C:\Users\welli\.claude\projects\C--Users-welli-OneDrive-Desktop-Trade-Claude-code---Bot-01\b3eb0599-5796-4b89-8bef-62aeb6222ab8\tool-results'

files = sorted(glob.glob(os.path.join(tool_dir, '*.json')))

# Carrega todos os candles
raw = {}
for fp in files:
    with open(fp, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if isinstance(data, list) and len(data) > 0:
        item = data[0]
        if isinstance(item, dict) and 'text' in item:
            try:
                inner = json.loads(item['text'])
                candles = inner.get('Data', [])
                if candles:
                    sym = candles[0]['MAPPED_INSTRUMENT']
                    raw.setdefault(sym, []).extend(candles)
            except Exception:
                pass

# Deduplica e ordena
ALL_DATA = {}
for sym, clist in raw.items():
    seen = {}
    for c in clist:
        ts = c['TIMESTAMP']
        seen[ts] = c
    ALL_DATA[sym] = sorted(seen.values(), key=lambda x: x['TIMESTAMP'])

# PARAMETROS
BANCA_BRL       = 1000.0
CAMBIO          = 5.40
BANCA_USDT      = BANCA_BRL / CAMBIO
LEVERAGE        = 10
FEE_TAKER       = 0.0004
RISK_PER_TRADE  = 0.02
SCORE_THRESH    = 3
SL_ATR_MULT     = 1.5
TP1_ATR_MULT    = 2.0
TP2_ATR_MULT    = 3.0
TP3_ATR_MULT    = 4.5
TP1_PCT, TP2_PCT, TP3_PCT = 0.35, 0.35, 0.30
COOLDOWN_BARS   = 5

def ema(arr, span):
    k = 2/(span+1)
    e = arr[0]
    result = [e]
    for x in arr[1:]:
        e = x*k + e*(1-k)
        result.append(e)
    return np.array(result)

def rsi14(closes):
    d = np.diff(closes)
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    ag = np.convolve(gain, np.ones(14)/14, mode='full')[:len(gain)]
    al = np.convolve(loss, np.ones(14)/14, mode='full')[:len(loss)]
    rs = np.where(al > 0, ag/al, 100)
    return 100 - 100/(1+rs)

def atr14(h, l, c):
    hl = h - l
    hc = np.abs(h[1:] - c[:-1])
    lc = np.abs(l[1:] - c[:-1])
    tr = np.maximum(hl[1:], np.maximum(hc, lc))
    pad = np.array([tr[0]])
    tr_full = np.concatenate([pad, tr])
    atr = np.convolve(tr_full, np.ones(14)/14, mode='full')[:len(tr_full)]
    return atr

def score_signal(i, closes, e9, e21, e55, rsi_v, vol_ratio, macd_hist, direction):
    s = 0
    if direction == "LONG":
        if e9[i] > e21[i] > e55[i]: s += 1
        if 45 <= rsi_v[i] <= 70: s += 1
        if vol_ratio[i] >= 1.5: s += 1
        if i > 0 and macd_hist[i] > macd_hist[i-1]: s += 1
        if closes[i] > e21[i]: s += 1
    else:
        if e9[i] < e21[i] < e55[i]: s += 1
        if 30 <= rsi_v[i] <= 55: s += 1
        if vol_ratio[i] >= 1.5: s += 1
        if i > 0 and macd_hist[i] < macd_hist[i-1]: s += 1
        if closes[i] < e21[i]: s += 1
    return s

def _fechar_r(pos, banca, trades, symbol):
    en   = pos["entrada"]
    sa   = pos.get("saida", en)
    noc  = pos["noc"]
    tipo = pos["tipo"]
    t1   = pos.get("t1", False)
    t2   = pos.get("t2", False)

    def mv(p):
        return (p-en)/en if tipo == "LONG" else (en-p)/en

    pnl = 0.0
    f_tp1 = f_tp2 = 0.0
    if t1:
        pnl += noc * TP1_PCT * mv(pos["tp1"])
        f_tp1 = noc * TP1_PCT * FEE_TAKER
    if t2:
        pnl += noc * TP2_PCT * mv(pos["tp2"])
        f_tp2 = noc * TP2_PCT * FEE_TAKER
    frac = 1.0 - (TP1_PCT if t1 else 0) - (TP2_PCT if t2 else 0)
    pnl += noc * frac * mv(sa)
    fee_s = noc * FEE_TAKER

    pnl_liq = pnl - f_tp1 - f_tp2 - fee_s
    nova    = banca + pnl_liq
    trades.append({
        "symbol": symbol, "tipo": tipo, "status": pos.get("status","?"),
        "pnl": round(pnl_liq, 4), "t1": t1, "t2": t2,
        "dur": pos.get("bar_s",0) - pos.get("bar_e",0),
    })
    return nova, pnl_liq

def run_backtest(symbol, candles):
    if len(candles) < 60:
        return None
    opens  = np.array([c["OPEN"]  for c in candles], dtype=float)
    highs  = np.array([c["HIGH"]  for c in candles], dtype=float)
    lows   = np.array([c["LOW"]   for c in candles], dtype=float)
    closes = np.array([c["CLOSE"] for c in candles], dtype=float)
    vols   = np.array([c.get("QUOTE_VOLUME", c.get("VOLUME", 1)) for c in candles], dtype=float)

    e9  = ema(closes, 9)
    e21 = ema(closes, 21)
    e55 = ema(closes, 55)
    rsi_v = np.concatenate([[50]*14, rsi14(closes)])[:len(closes)]

    vol_avg = np.convolve(vols, np.ones(20)/20, mode='full')[:len(vols)]
    vol_ratio = np.where(vol_avg > 0, vols/vol_avg, 1.0)

    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd_line = ema12 - ema26
    macd_sig  = ema(macd_line, 9)
    macd_hist = macd_line - macd_sig

    atr_v = atr14(highs, lows, closes)

    banca = BANCA_USDT
    pos   = None
    cool  = 0
    trades = []

    for i in range(55, len(closes)):
        px = closes[i]
        at = atr_v[i]
        if at <= 0 or np.isnan(at):
            continue

        if pos is not None:
            hi, lo = highs[i], lows[i]
            if pos["tipo"] == "LONG":
                if lo <= pos["sl"]:
                    pos.update({"status":"SL","saida":pos["sl"],"bar_s":i})
                    banca, _ = _fechar_r(pos, banca, trades, symbol)
                    pos = None; cool = COOLDOWN_BARS; continue
                if not pos["t1"] and hi >= pos["tp1"]:
                    pos["t1"] = True; pos["sl"] = pos["entrada"]
                if pos["t1"] and not pos["t2"] and hi >= pos["tp2"]:
                    pos["t2"] = True; pos["sl"] = pos["tp1"]
                if pos["t2"] and hi >= pos["tp3"]:
                    pos.update({"status":"TP3","saida":pos["tp3"],"bar_s":i})
                    banca, _ = _fechar_r(pos, banca, trades, symbol)
                    pos = None; cool = COOLDOWN_BARS; continue
            else:
                if hi >= pos["sl"]:
                    pos.update({"status":"SL","saida":pos["sl"],"bar_s":i})
                    banca, _ = _fechar_r(pos, banca, trades, symbol)
                    pos = None; cool = COOLDOWN_BARS; continue
                if not pos["t1"] and lo <= pos["tp1"]:
                    pos["t1"] = True; pos["sl"] = pos["entrada"]
                if pos["t1"] and not pos["t2"] and lo <= pos["tp2"]:
                    pos["t2"] = True; pos["sl"] = pos["tp1"]
                if pos["t2"] and lo <= pos["tp3"]:
                    pos.update({"status":"TP3","saida":pos["tp3"],"bar_s":i})
                    banca, _ = _fechar_r(pos, banca, trades, symbol)
                    pos = None; cool = COOLDOWN_BARS; continue
            continue

        if cool > 0:
            cool -= 1; continue
        if banca < 5:
            break

        if rsi_v[i] > 76 or vol_ratio[i] > 6.5:
            continue

        sl_dist = at * SL_ATR_MULT
        sl_pct  = sl_dist / px if px > 0 else 0.02
        risco   = banca * RISK_PER_TRADE
        noc     = min(risco / sl_pct, banca * LEVERAGE * 0.8)
        if noc < 3:
            continue

        fee_e = noc * FEE_TAKER

        long_s  = score_signal(i, closes, e9, e21, e55, rsi_v, vol_ratio, macd_hist, "LONG")
        short_s = score_signal(i, closes, e9, e21, e55, rsi_v, vol_ratio, macd_hist, "SHORT")

        if long_s >= SCORE_THRESH and long_s >= short_s:
            pos = {"tipo":"LONG","entrada":px,"sl":px-sl_dist,
                   "tp1":px+at*TP1_ATR_MULT,"tp2":px+at*TP2_ATR_MULT,"tp3":px+at*TP3_ATR_MULT,
                   "noc":noc,"fee_e":fee_e,"t1":False,"t2":False,"bar_e":i}
            banca -= fee_e
        elif short_s >= SCORE_THRESH:
            pos = {"tipo":"SHORT","entrada":px,"sl":px+sl_dist,
                   "tp1":px-at*TP1_ATR_MULT,"tp2":px-at*TP2_ATR_MULT,"tp3":px-at*TP3_ATR_MULT,
                   "noc":noc,"fee_e":fee_e,"t1":False,"t2":False,"bar_e":i}
            banca -= fee_e

    if pos is not None:
        pos.update({"status":"TIMEOUT","saida":closes[-1],"bar_s":len(closes)-1})
        banca, _ = _fechar_r(pos, banca, trades, symbol)

    if not trades:
        return {"symbol":symbol,"trades":0}

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    loss = [p for p in pnls if p <= 0]
    wr   = len(wins)/len(pnls)*100
    pf   = abs(sum(wins)/sum(loss)) if loss and sum(loss) != 0 else 0
    ret  = (banca - BANCA_USDT) / BANCA_USDT * 100
    ret_brl = ret / 100 * BANCA_BRL
    eq = [BANCA_USDT]
    for p in pnls:
        eq.append(eq[-1]+p)
    peak, dd = BANCA_USDT, 0.0
    for e in eq:
        if e > peak: peak = e
        d = (peak-e)/peak*100 if peak > 0 else 0
        if d > dd: dd = d
    longs  = [t for t in trades if t["tipo"]=="LONG"]
    shorts = [t for t in trades if t["tipo"]=="SHORT"]
    return {
        "symbol": symbol,
        "trades": len(pnls),
        "win_rate": round(wr,1),
        "profit_factor": round(pf,2),
        "retorno_pct": round(ret,2),
        "retorno_brl": round(ret_brl,2),
        "max_dd": round(dd,1),
        "banca_final_usdt": round(banca,2),
        "tp1_hits": sum(1 for t in trades if t["t1"]),
        "tp2_hits": sum(1 for t in trades if t["t2"]),
        "sl_count": sum(1 for t in trades if t["status"]=="SL"),
        "tp3_count": sum(1 for t in trades if t["status"]=="TP3"),
        "longs": len(longs),
        "shorts": len(shorts),
    }

resultados = []
for sym, candles in ALL_DATA.items():
    if candles:
        r = run_backtest(sym, candles)
        if r:
            resultados.append(r)

resultados.sort(key=lambda x: x.get("retorno_pct", -999), reverse=True)

pfs   = [r["profit_factor"] for r in resultados if r.get("trades",0)>0]
rets  = [r["retorno_pct"] for r in resultados if r.get("trades",0)>0]
wrs   = [r["win_rate"] for r in resultados if r.get("trades",0)>0]
dds   = [r["max_dd"] for r in resultados if r.get("trades",0)>0]
total_t = sum(r.get("trades",0) for r in resultados)

output = {
    "resultados": resultados,
    "consolidado": {
        "pf_medio": round(sum(pfs)/len(pfs),2) if pfs else 0,
        "retorno_medio_pct": round(sum(rets)/len(rets),2) if rets else 0,
        "wr_medio": round(sum(wrs)/len(wrs),1) if wrs else 0,
        "dd_medio": round(sum(dds)/len(dds),1) if dds else 0,
        "total_trades": total_t,
        "pares_analisados": len(resultados),
    }
}

print(json.dumps(output, ensure_ascii=False, indent=2))
