"""
PREVIEW (somente exemplos p/ aprovacao) — NAO altera o gerador atual.

Mantem 100% do estilo atual do grafico de sinal (tema escuro, velas,
EMAs, zona OB, linhas Entry/SL/TP, seta de direcao, painel de volume,
marca d'agua) e ACRESCENTA por cima:

  1) Linhas de TENDENCIA (suporte/resistencia via swings reais)
  2) ALVOS FUTUROS projetados (zona-alvo + caminho pontilhado ate TP1/TP2)
  3) Rotulo de FIGURA GRAFICA (triangulo, canal, bandeira, range)

Gera 3 exemplos com klines REAIS e envia ao chat PESSOAL (TELEGRAM_CHAT_ID).
Uso: python preview_enhancements.py
"""
import os, io, sys, time, requests
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv()

from chart_preview_send import fetch_real_klines, ema_calc  # reusa o que ja existe

TOKEN   = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')   # 739231436 = chat pessoal (regra de testes)


# ── Deteccao leve de swings / tendencia / figura ──────────────────────────────
def _swings(vals, kind, w=3):
    out = []
    for i in range(w, len(vals) - w):
        seg = vals[i - w:i + w + 1]
        if kind == 'high' and vals[i] == max(seg):
            out.append(i)
        if kind == 'low' and vals[i] == min(seg):
            out.append(i)
    return out


def _fit_line(p1, p2):
    (x1, y1), (x2, y2) = p1, p2
    if x2 == x1:
        return 0.0, y1
    m = (y2 - y1) / (x2 - x1)
    b = y1 - m * x1
    return m, b


def _classify(ms, mr, price):
    """Classifica a figura pela inclinacao das duas linhas (normalizada)."""
    flat = price * 0.0008
    if ms > flat and mr < -flat:
        return 'Triangulo Simetrico'
    if abs(mr) <= flat and ms > flat:
        return 'Triangulo Ascendente'
    if abs(ms) <= flat and mr < -flat:
        return 'Triangulo Descendente'
    if ms > flat and mr > flat:
        return 'Canal de Alta'
    if ms < -flat and mr < -flat:
        return 'Canal de Baixa'
    return 'Consolidacao / Range'


def gerar_grafico_plus(symbol: str, tf: str) -> tuple[bytes, str, str]:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from datetime import datetime

    candles = fetch_real_klines(symbol, tf, limit=100)
    n = len(candles)
    closes = [c['c'] for c in candles]
    highs  = [c['h'] for c in candles]
    lows   = [c['l'] for c in candles]
    ema21 = ema_calc(closes, 21)
    ema55 = ema_calc(closes, 55)

    price_range = max(highs) - min(lows)
    atr = sum(highs[i] - lows[i] for i in range(n - 14, n)) / 14

    # Direcao segue a tendencia das EMAs
    direction = 'LONG' if ema21[-1] >= ema55[-1] else 'SHORT'
    is_long = direction == 'LONG'
    entry = closes[-1]
    if is_long:
        sl  = entry - 1.2 * atr
        tp1 = entry + 1.8 * atr
        tp2 = entry + 3.0 * atr
    else:
        sl  = entry + 1.2 * atr
        tp1 = entry - 1.8 * atr
        tp2 = entry - 3.0 * atr
    rr = round(abs(tp2 - entry) / abs(entry - sl), 1)

    # ── Cores (identicas ao gerador atual) ────────────────────────────────────
    BG = '#181a20'; UP = '#0ecb81'; DOWN = '#f6465d'
    GRID = '#2b2f36'; TEXT = '#eaecef'; MUTED = '#848e9c'
    SIG_COLOR = '#f0b90b' if is_long else '#f6465d'
    TREND = '#5b9bd5'   # azul das linhas de tendencia (estilo do canal)
    PROJ  = '#36c5f0'   # azul claro do caminho projetado

    FUTURE = 24  # espaco a direita p/ alvos futuros

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

    # ── Velas + volume ────────────────────────────────────────────────────────
    W = 0.55
    for i, c in enumerate(candles):
        color = UP if c['c'] >= c['o'] else DOWN
        ax.plot([i, i], [c['l'], c['h']], color=color, linewidth=0.7, zorder=2)
        body = max(abs(c['c'] - c['o']), price_range * 0.0005)
        ax.add_patch(mpatches.FancyBboxPatch(
            (i - W/2, min(c['o'], c['c'])), W, body,
            boxstyle='square,pad=0', facecolor=color, edgecolor=color,
            linewidth=0, zorder=3))
        axv.bar(i, c['v'] / 1e6, color=color, alpha=0.6, width=0.55)

    xs = list(range(n))
    ax.plot(xs, ema21, color='#c97cff', linewidth=1.4, label='EMA 21', zorder=4)
    ax.plot(xs, ema55, color='#2196f3', linewidth=1.2, label='EMA 55', zorder=4)

    # ── Zona OB (igual ao atual) ──────────────────────────────────────────────
    ob_bot = min(sl, entry) + abs(entry - sl) * 0.15
    ob_top = max(sl, entry) - abs(entry - sl) * 0.0
    ob_color = '#00b894' if is_long else '#d63031'
    ob_start = max(0, n - 28)
    ax.add_patch(mpatches.FancyBboxPatch(
        (ob_start, min(ob_bot, ob_top)), n - ob_start - 0.5, abs(ob_top - ob_bot),
        boxstyle='square,pad=0', facecolor=ob_color, alpha=0.12,
        edgecolor=ob_color, linewidth=0.8, linestyle='--', zorder=2))
    ax.text(ob_start + 0.5, max(ob_bot, ob_top) + price_range * 0.004,
            'OB Bullish' if is_long else 'OB Bearish',
            color=ob_color, fontsize=7, fontweight='bold', va='bottom')

    # ════════════════ NOVO 1: LINHAS DE TENDENCIA ════════════════════════════
    sh = _swings(highs, 'high'); sl_idx = _swings(lows, 'low')
    ms = mr = 0.0
    if len(sh) >= 2 and len(sl_idx) >= 2:
        r1, r2 = sh[-2], sh[-1]            # resistencia: 2 ultimos topos
        s1, s2 = sl_idx[-2], sl_idx[-1]    # suporte: 2 ultimos fundos
        mr, br = _fit_line((r1, highs[r1]), (r2, highs[r2]))
        msup, bs = _fit_line((s1, lows[s1]), (s2, lows[s2]))
        ms = msup
        x_end = n + FUTURE
        ax.plot([r1, x_end], [mr * r1 + br, mr * x_end + br],
                color=TREND, linewidth=1.4, alpha=0.9, zorder=4)
        ax.plot([s1, x_end], [msup * s1 + bs, msup * x_end + bs],
                color=TREND, linewidth=1.4, alpha=0.9, zorder=4)
        ax.scatter([r1, r2], [highs[r1], highs[r2]], s=14, color=TREND, zorder=6)
        ax.scatter([s1, s2], [lows[s1], lows[s2]], s=14, color=TREND, zorder=6)

    # ════════════════ NOVO 3: FIGURA GRAFICA (rotulo) ════════════════════════
    figura = _classify(ms, mr, entry)
    ax.text(0.012, 0.965, f'◈ {figura}', transform=ax.transAxes,
            color=TREND, fontsize=10, fontweight='bold', va='top', ha='left',
            bbox=dict(boxstyle='round,pad=0.35', facecolor='#10243a',
                      edgecolor=TREND, linewidth=1.0, alpha=0.92))

    # ════════════════ NOVO 2: ALVOS FUTUROS PROJETADOS ═══════════════════════
    # zona-alvo translucida no espaco futuro
    z_lo, z_hi = (entry, tp2) if is_long else (tp2, entry)
    ax.add_patch(mpatches.Rectangle(
        (n - 0.5, z_lo), FUTURE, z_hi - z_lo,
        facecolor=UP if is_long else DOWN, alpha=0.06, edgecolor='none', zorder=1))
    # caminho projetado (zig-zag pontilhado ate TP1 e TP2)
    dip = (sl - entry) * 0.35
    px = [n - 1, n + 4, n + 12, n + 16, n + FUTURE]
    py = [entry, entry + dip, tp1, tp1 - (tp1 - entry) * 0.30, tp2]
    ax.plot(px, py, color=PROJ, linewidth=1.6, linestyle=(0, (4, 3)), zorder=6)
    ax.annotate('', xy=(px[-1], py[-1]), xytext=(px[-2], py[-2]),
                arrowprops=dict(arrowstyle='-|>', color=PROJ, lw=1.8, mutation_scale=16),
                zorder=6)
    ax.text(n + FUTURE, tp2, '  alvo', color=PROJ, fontsize=8,
            fontweight='bold', va='center', ha='left')

    # ── Linhas Entry / SL / TP1 / TP2 (igual ao atual) ────────────────────────
    px_off = price_range * 0.006
    x1 = n + FUTURE + 0.3
    fmt = '{:,.4f}' if entry < 100 else '{:,.2f}' if entry < 1000 else '{:,.1f}'

    def hline(y, color, ls, lw, label):
        ax.axhline(y, color=color, linewidth=lw, linestyle=ls, zorder=5, alpha=0.9)
        ax.text(x1 - 0.5, y + px_off, label, color=color,
                fontsize=7.5, fontweight='bold', va='bottom', ha='right')

    hline(entry, SIG_COLOR, '--', 1.5, f'Entry  ${fmt.format(entry)}')
    hline(sl,    DOWN,      '-',  1.2, f'SL  ${fmt.format(sl)}')
    hline(tp1,   UP,        '--', 1.2, f'TP1  ${fmt.format(tp1)}')
    hline(tp2,   UP,        ':',  1.0, f'TP2  ${fmt.format(tp2)}')

    # ── Seta na ultima vela (igual ao atual) ──────────────────────────────────
    sig_i = n - 1
    tip_y = candles[sig_i]['l'] - price_range * 0.022 if is_long else candles[sig_i]['h'] + price_range * 0.022
    base_y = tip_y - price_range * 0.044 if is_long else tip_y + price_range * 0.044
    ax.annotate('', xy=(sig_i, tip_y), xytext=(sig_i, base_y),
                arrowprops=dict(arrowstyle='-|>', color=SIG_COLOR, lw=2.0, mutation_scale=18))
    ax.text(sig_i, base_y - price_range * 0.018 if is_long else base_y + price_range * 0.018,
            direction, color=SIG_COLOR, fontsize=11, fontweight='bold',
            ha='center', va='top' if is_long else 'bottom')

    # ── Marca d'agua ──────────────────────────────────────────────────────────
    ax.text(0.5, 0.5, '@mestressinais_br', color=TEXT, fontsize=28,
            alpha=0.04, transform=ax.transAxes, ha='center', va='center',
            rotation=20, fontweight='bold')

    # ── Eixos ─────────────────────────────────────────────────────────────────
    step = max(1, n // 8)
    ax.set_xticks([])
    ticks = list(range(0, n, step))
    axv.set_xticks(ticks)
    tf_fmt = {'1m': '%H:%M', '3m': '%H:%M', '5m': '%H:%M',
              '15m': '%d/%m %Hh', '1h': '%d/%m %Hh', '4h': '%d/%m',
              '1d': '%d/%m'}.get(tf, '%d/%m %Hh')
    axv.set_xticklabels(
        [datetime.utcfromtimestamp(candles[i]['ts'] / 1000).strftime(tf_fmt) for i in ticks],
        fontsize=7.5, color=MUTED, rotation=0)
    axv.set_yticks([]); axv.set_ylabel('Vol', color=MUTED, fontsize=7, labelpad=2)
    axv.yaxis.set_label_position('right')

    all_prices = highs + lows + [tp2, sl]
    ymin = min(all_prices) - price_range * 0.10
    ymax = max(all_prices) + price_range * 0.16
    ax.set_ylim(ymin, ymax)
    pf = (lambda x, _: f'{x:,.4f}') if entry < 10 else \
         (lambda x, _: f'{x:,.2f}') if entry < 1000 else (lambda x, _: f'{x:,.1f}')
    ax.yaxis.set_major_formatter(plt.FuncFormatter(pf))
    ax.yaxis.set_label_position('right'); ax.yaxis.tick_right()
    ax.tick_params(axis='y', labelsize=8, colors=MUTED)
    ax.legend(loc='upper left', fontsize=8, facecolor=BG, edgecolor=GRID,
              labelcolor=TEXT, framealpha=0.85, borderpad=0.5, handlelength=1.5,
              bbox_to_anchor=(0.0, 0.90))

    ax.set_title(
        f'{symbol} / USDT  ·  {tf.upper()}  ·  Binance Futures     '
        f'{direction}  ▪  R:R 1:{rr}  ▪  {figura}',
        color=TEXT, fontsize=10, pad=10, loc='left', fontweight='bold')
    ax.set_xlim(-1, n + FUTURE + 1.5)

    fig.subplots_adjust(left=0.02, right=0.88, top=0.93, bottom=0.06, hspace=0.0)
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=130, facecolor=BG, bbox_inches=None)
    plt.close()
    buf.seek(0)
    return buf.read(), direction, figura


if __name__ == '__main__':
    # 3 exemplos com simbolos/timeframes variados (klines REAIS)
    exemplos = [
        ('BTCUSDT', '1h'),
        ('SOLUSDT', '15m'),
        ('ETHUSDT', '4h'),
    ]
    for k, (sym, tf) in enumerate(exemplos, 1):
        print(f'[{k}/3] {sym} {tf} ...', flush=True)
        try:
            img, direction, figura = gerar_grafico_plus(sym, tf)
        except Exception as e:
            print('  ERRO ao gerar:', e)
            continue
        cap = (
            f'\U0001f9ea EXEMPLO {k}/3 — MELHORIA DE GRAFICO (aprovar/reprovar)\n'
            f'{sym} {tf} | {direction}\n\n'
            f'Mantido: estilo atual (EMAs, OB, Entry/SL/TP, volume, marca).\n'
            f'NOVO nesta imagem:\n'
            f'  • Linhas de tendencia (suporte/resistencia)\n'
            f'  • Figura grafica detectada: {figura}\n'
            f'  • Alvos futuros projetados (zona + caminho ate TP1/TP2)'
        )
        resp = requests.post(
            f'https://api.telegram.org/bot{TOKEN}/sendPhoto',
            data={'chat_id': CHAT_ID, 'caption': cap},
            files={'photo': (f'preview_{k}.png', img, 'image/png')},
            timeout=30,
        ).json()
        print('  ok' if resp.get('ok') else f'  ERRO envio: {resp}')
        time.sleep(1.5)
    print('FIM — 3 exemplos enviados ao chat pessoal.')
