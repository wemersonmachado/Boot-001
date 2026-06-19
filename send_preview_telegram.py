"""Envia preview do sinal estilo Binance ao Telegram para aprovação."""
import os, io, requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
load_dotenv()

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

TOKEN   = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_VIP_ID") or os.getenv("TELEGRAM_CHAT_ID")


def ema_calc(closes, period):
    k = 2 / (period + 1)
    prev = sum(closes[:period]) / period
    out = []
    for v in closes:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def gerar_chart_binance(symbol, tf, entry, sl, tp1, tp2, score, rr, conf_label, direction="LONG"):
    print(f"Buscando klines {symbol} {tf}...")
    r = requests.get(
        "https://fapi.binance.com/fapi/v1/klines",
        params={"symbol": symbol, "interval": tf, "limit": 100},
        timeout=15,
    )
    rows = r.json()
    opens  = [float(x[1]) for x in rows]
    highs  = [float(x[2]) for x in rows]
    lows   = [float(x[3]) for x in rows]
    closes = [float(x[4]) for x in rows]
    vols   = [float(x[5]) for x in rows]
    times  = [datetime.utcfromtimestamp(int(x[0]) / 1000) for x in rows]
    n = len(closes)
    print(f"  {n} velas recebidas, último preço: ${closes[-1]:,.2f}")

    ema21 = ema_calc(closes, 21)
    ema55 = ema_calc(closes, 55)

    is_long = direction == "LONG"
    BG    = "#181a20"; UP = "#0ecb81"; DOWN = "#f6465d"
    GRID  = "#2b2f36"; TEXT = "#eaecef"; MUTED = "#848e9c"
    SIG   = "#f0b90b" if is_long else "#f6465d"
    OB_C  = UP if is_long else DOWN

    fig = plt.figure(figsize=(14, 8), facecolor=BG)
    gs  = fig.add_gridspec(2, 1, height_ratios=[5, 1], hspace=0.0)
    ax  = fig.add_subplot(gs[0])
    axv = fig.add_subplot(gs[1], sharex=ax)

    for a in [ax, axv]:
        a.set_facecolor(BG)
        a.tick_params(colors=MUTED, labelsize=8, top=False, bottom=False,
                      left=False, right=True, labelleft=False, labelright=True)
        for sp in a.spines.values():
            sp.set_edgecolor(GRID); sp.set_linewidth(0.6)
        a.yaxis.grid(True, color=GRID, linewidth=0.4, alpha=0.5)
        a.xaxis.grid(True, color=GRID, linewidth=0.3, alpha=0.3)

    W = 0.55
    price_range = max(highs) - min(lows)
    for i in range(n):
        col = UP if closes[i] >= opens[i] else DOWN
        ax.plot([i, i], [lows[i], highs[i]], color=col, linewidth=0.7, zorder=2)
        body = max(abs(closes[i] - opens[i]), price_range * 0.0005)
        ax.add_patch(mpatches.FancyBboxPatch(
            (i - W/2, min(opens[i], closes[i])), W, body,
            boxstyle="square,pad=0", facecolor=col, edgecolor=col, linewidth=0, zorder=3))
        axv.bar(i, vols[i] / 1e6, color=col, alpha=0.6, width=0.55)

    ax.plot(range(n), ema21, color="#c97cff", linewidth=1.4, label="EMA 21", zorder=4)
    ax.plot(range(n), ema55, color="#2196f3", linewidth=1.2, label="EMA 55", zorder=4)

    risk = abs(entry - sl)
    ob_bot = sl + risk * 0.15 if is_long else entry - risk * 0.12
    ob_top = entry + risk * 0.12 if is_long else sl - risk * 0.15
    ob_x0  = max(0, n - 28)
    ob_label = "OB Bullish" if is_long else "OB Bearish"
    ax.add_patch(mpatches.FancyBboxPatch(
        (ob_x0, min(ob_bot, ob_top)), n - ob_x0 - 0.5, abs(ob_top - ob_bot),
        boxstyle="square,pad=0", facecolor=OB_C, alpha=0.12,
        edgecolor=OB_C, linewidth=0.8, linestyle="--", zorder=2))
    ax.text(ob_x0 + 0.5, max(ob_bot, ob_top) + price_range * 0.004, ob_label,
            color=OB_C, fontsize=7, fontweight="bold", va="bottom")

    px_off = price_range * 0.006
    x1 = n + 0.3
    fmt = "{:,.1f}" if entry > 100 else "{:,.4f}"

    def hline_full(y, color, ls, lw, label):
        ax.axhline(y, color=color, linewidth=lw, linestyle=ls, zorder=5, alpha=0.9)
        ax.text(x1 - 0.5, y + px_off, label, color=color,
                fontsize=7.5, fontweight="bold", va="bottom", ha="right")

    hline_full(entry, SIG,  "--", 1.5, f"Entry  ${fmt.format(entry)}")
    hline_full(sl,    DOWN, "-",  1.2, f"SL  ${fmt.format(sl)}")
    hline_full(tp1,   UP,   "--", 1.2, f"TP1  ${fmt.format(tp1)}")
    hline_full(tp2,   UP,   ":",  1.0, f"TP2  ${fmt.format(tp2)}")
    ax.axhspan(min(sl, entry), max(sl, entry), alpha=0.05, color=DOWN, zorder=1)
    ax.axhspan(min(entry, tp2), max(entry, tp2), alpha=0.05, color=UP, zorder=1)

    sig_i  = n - 1
    tip_y  = lows[sig_i]  - price_range * 0.022 if is_long else highs[sig_i] + price_range * 0.022
    base_y = tip_y - price_range * 0.044 if is_long else tip_y + price_range * 0.044
    ax.annotate("", xy=(sig_i, tip_y), xytext=(sig_i, base_y),
                arrowprops=dict(arrowstyle="-|>", color=SIG, lw=2.0, mutation_scale=18))
    ax.text(sig_i, base_y - price_range * 0.018 if is_long else base_y + price_range * 0.018,
            direction, color=SIG, fontsize=11, fontweight="bold",
            ha="center", va="top" if is_long else "bottom")

    ax.text(0.5, 0.5, "@mestressinais_br", color=TEXT, fontsize=28, alpha=0.04,
            transform=ax.transAxes, ha="center", va="center", rotation=20, fontweight="bold")

    step = max(1, n // 8)
    ticks = list(range(0, n, step))
    tf_fmt = {"1m": "%H:%M", "3m": "%H:%M", "5m": "%H:%M",
              "15m": "%d/%m %Hh", "1h": "%d/%m %Hh", "4h": "%d/%m", "1d": "%d/%m"}.get(tf, "%d/%m %Hh")
    labels = [times[i].strftime(tf_fmt) for i in ticks]
    ax.set_xticks([])
    axv.set_xticks(ticks); axv.set_xticklabels(labels, fontsize=7.5, color=MUTED, rotation=0)
    axv.tick_params(axis="x", colors=MUTED, labelsize=7.5)
    axv.set_yticks([]); axv.set_ylabel("Vol", color=MUTED, fontsize=7, labelpad=2)
    axv.yaxis.set_label_position("right")

    all_prices = highs + lows + [tp2, sl]
    ymin = min(all_prices) - price_range * 0.08
    ymax = max(all_prices) + price_range * 0.14
    ax.set_ylim(ymin, ymax)
    pf = (lambda x, _: f"{x:,.1f}") if entry > 100 else (lambda x, _: f"{x:,.4f}")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(pf))
    ax.yaxis.set_label_position("right"); ax.yaxis.tick_right()
    ax.tick_params(axis="y", labelsize=8, colors=MUTED)
    ax.legend(loc="upper left", fontsize=8, facecolor=BG, edgecolor=GRID,
              labelcolor=TEXT, framealpha=0.85, borderpad=0.5, handlelength=1.5)
    ax.set_title(
        f"{symbol} / USDT  ·  {tf.upper()}  ·  Binance Futures     "
        f"{direction}  ▪  Score {score}  ▪  R:R 1:{rr:.1f}  ▪  {conf_label}",
        color=TEXT, fontsize=10, pad=10, loc="left", fontweight="bold")
    ax.set_xlim(-1, n + 1.5)
    fig.subplots_adjust(left=0.02, right=0.88, top=0.93, bottom=0.06, hspace=0.0)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=130, facecolor=BG, bbox_inches=None)
    plt.close(fig)
    buf.seek(0)
    b = buf.read()
    print(f"  Chart gerado: {len(b):,} bytes")
    return b


if __name__ == "__main__":
    symbol = "BTCUSDT"; tf = "1h"

    r = requests.get("https://fapi.binance.com/fapi/v1/ticker/price",
                     params={"symbol": symbol}, timeout=10)
    price = float(r.json()["price"])

    entry = price
    sl    = entry * 0.964
    tp1   = entry * 1.050
    tp2   = entry * 1.090
    rr    = 2.5; score = 84; leverage = 5

    chart_bytes = gerar_chart_binance(symbol, tf, entry, sl, tp1, tp2, score, rr, "Alta")

    sl_pct  = abs(entry - sl)  / entry * 100
    tp1_pct = abs(tp1 - entry) / entry * 100
    tp2_pct = abs(tp2 - entry) / entry * 100
    brt = (datetime.utcnow() - timedelta(hours=3)).strftime("%d/%m/%Y • %H:%M BRT")
    SEP = "━━━━━━━━━━━━━━━━━━"

    msg = (
        f"MODO: SINAIS | PERFIL AGRESSIVO\n\n"
        f"BTCUSDT | 🟢 LONG | 1H | 📅 DAY TRADE\n"
        f"{SEP}\n"
        f"📊 Score: {score}/100\n"
        f"⚖️ R:R: {rr:.1f}:1\n"
        f"🎯 Confiança: Alta\n"
        f"💹 Vol. 24h: 28.4B USDT\n\n"
        f"💰 Entrada: ${entry:,.1f}\n"
        f"🛑 Stop: ${sl:,.1f} (-{sl_pct:.2f}%)\n\n"
        f"🎯 TP1: ${tp1:,.1f} (+{tp1_pct:.2f}%)\n"
        f"🎯 TP2: ${tp2:,.1f} (+{tp2_pct:.2f}%)\n"
        f"⚡️ Alavancagem Sugerida: {leverage}x\n"
        f"{SEP}\n"
        f"📈 LEITURA DO MERCADO\n\n"
        f"🔺 Tendência: ALTA\n"
        f"🔺 Estrutura: UPTREND\n"
        f"🔺 EMA21 ACIMA\n"
        f"🔺 EMA200 ACIMA\n"
        f"📊 RSI: 62.4 - Neutro alto\n\n"
        f"🕯️ Força da Vela: ██████░░░░ 60%\n"
        f"📦 Volume: 1.8x da média\n"
        f"{SEP}\n"
        f"✅ CONFIRMAÇÕES\n\n"
        f"• EMA50 acima da EMA200 — tendência de alta confirmada\n"
        f"• Estrutura de mercado favorável à direção\n"
        f"• Volume acima da média — força compradora\n"
        f"{SEP}\n"
        f"💡 RECOMENDAÇÃO:\n\n"
        f"Sinal LONG de alta qualidade (score 84). Entrada confirmada. R:R 2.5:1 favorável. Operar tamanho normal.\n"
        f"{SEP}\n"
        f"📈 PROJEÇÃO DE LUCRO ({leverage}x)\n\n"
        f"+1% → +5%\n+2% → +10%\n+3% → +15%\n+4% → +20%\n+5% → +25%\n"
        f"{SEP}\n\n"
        f"⏰ {brt}\n\n"
        f"🤖 TRADER 001 SIGNAL ENGINE\n"
        f"⚠️ PREVIEW — aguardando aprovação"
    )

    BASE = f"https://api.telegram.org/bot{TOKEN}"

    print("Enviando foto (sem caption)...")
    r1 = requests.post(f"{BASE}/sendPhoto",
        data={"chat_id": CHAT_ID},
        files={"photo": ("chart.png", chart_bytes, "image/png")},
        timeout=30)
    print(f"  sendPhoto ok={r1.json().get('ok')}", r1.json().get("description", ""))

    print("Enviando texto...")
    r2 = requests.post(f"{BASE}/sendMessage",
        json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
        timeout=15)
    print(f"  sendMessage ok={r2.json().get('ok')}", r2.json().get("description", ""))
