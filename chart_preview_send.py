"""
Preview: busca klines REAIS da Binance Futures e envia ao Telegram.
Uso: python chart_preview_send.py
"""
import os, io, sys, requests
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv()

BINANCE_FUTURES = 'https://fapi.binance.com/fapi/v1/klines'
TOKEN   = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')


def fetch_real_klines(symbol: str, interval: str, limit: int = 100) -> list[dict]:
    """Busca klines reais da Binance Futures (sem auth)."""
    r = requests.get(BINANCE_FUTURES, params={
        'symbol': symbol.upper(),
        'interval': interval,
        'limit': limit,
    }, timeout=10)
    r.raise_for_status()
    rows = r.json()
    candles = []
    for row in rows:
        candles.append({
            'ts': int(row[0]),
            'o':  float(row[1]),
            'h':  float(row[2]),
            'l':  float(row[3]),
            'c':  float(row[4]),
            'v':  float(row[5]),
        })
    return candles


def ema_calc(closes: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    prev = sum(closes[:period]) / period
    out = []
    for v in closes:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def gerar_grafico_real(
    symbol: str, direction: str, tf: str,
    entry: float, sl: float, tp1: float, tp2: float,
    rr: float, score: int, conf_label: str,
    ob_type: str | None = None,
) -> bytes:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import numpy as np
    from datetime import datetime

    # ── Busca dados reais ─────────────────────────────────────────────────────
    candles = fetch_real_klines(symbol, tf, limit=100)
    n = len(candles)
    print(f'  Klines recebidos: {n} velas de {symbol} {tf}')

    closes = [c['c'] for c in candles]
    ema21 = ema_calc(closes, 21)
    ema55 = ema_calc(closes, 55)

    # ── Zona OB (região em torno do SL→Entry) ─────────────────────────────────
    ob_bot = sl + (entry - sl) * 0.15
    ob_top = entry + (entry - sl) * 0.12
    ob_label = ob_type or ('OB Bullish' if direction == 'LONG' else 'OB Bearish')
    ob_color = '#00b894' if direction == 'LONG' else '#d63031'

    # ── Binance-style colors ──────────────────────────────────────────────────
    BG   = '#181a20'; UP   = '#0ecb81'; DOWN = '#f6465d'
    GRID = '#2b2f36'; TEXT = '#eaecef'; MUTED = '#848e9c'
    SIG_COLOR = '#f0b90b' if direction == 'LONG' else '#f6465d'

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

    # ── Velas ─────────────────────────────────────────────────────────────────
    W = 0.55
    price_range = max(c['h'] for c in candles) - min(c['l'] for c in candles)
    for i, c in enumerate(candles):
        color = UP if c['c'] >= c['o'] else DOWN
        ax.plot([i, i], [c['l'], c['h']], color=color, linewidth=0.7, zorder=2)
        body = max(abs(c['c'] - c['o']), price_range * 0.0005)
        rect = mpatches.FancyBboxPatch(
            (i - W/2, min(c['o'], c['c'])), W, body,
            boxstyle='square,pad=0', facecolor=color, edgecolor=color,
            linewidth=0, zorder=3)
        ax.add_patch(rect)
        axv.bar(i, c['v'] / 1e6, color=color, alpha=0.6, width=0.55)

    # ── EMAs ──────────────────────────────────────────────────────────────────
    xs = list(range(n))
    ax.plot(xs, ema21, color='#c97cff', linewidth=1.4, label='EMA 21', zorder=4)
    ax.plot(xs, ema55, color='#2196f3', linewidth=1.2, label='EMA 55', zorder=4)

    # ── Zona OB ───────────────────────────────────────────────────────────────
    ob_start = max(0, n - 28)
    ob_rect = mpatches.FancyBboxPatch(
        (ob_start, ob_bot), n - ob_start - 0.5, ob_top - ob_bot,
        boxstyle='square,pad=0', facecolor=ob_color, alpha=0.12,
        edgecolor=ob_color, linewidth=0.8, linestyle='--', zorder=2)
    ax.add_patch(ob_rect)
    ax.text(ob_start + 0.5, ob_top + price_range * 0.004, ob_label,
            color=ob_color, fontsize=7, fontweight='bold', va='bottom')

    # ── Linhas Entry / SL / TP1 / TP2 (full-width, label à direita) ───────────
    px_off = price_range * 0.006
    x1 = n + 0.3
    fmt = '{:,.4f}' if entry < 100 else '{:,.2f}' if entry < 1000 else '{:,.1f}'

    def hline(y, color, ls, lw, label):
        ax.axhline(y, color=color, linewidth=lw, linestyle=ls, zorder=5, alpha=0.9)
        ax.text(x1 - 0.5, y + px_off, label, color=color,
                fontsize=7.5, fontweight='bold', va='bottom', ha='right')

    hline(entry, SIG_COLOR, '--', 1.5, f'Entry  ${fmt.format(entry)}')
    hline(sl,    DOWN,      '-',  1.2, f'SL  ${fmt.format(sl)}')
    hline(tp1,   UP,        '--', 1.2, f'TP1  ${fmt.format(tp1)}')
    hline(tp2,   UP,        ':',  1.0, f'TP2  ${fmt.format(tp2)}')

    # Faixas risco/retorno full-width
    ax.axhspan(min(sl, entry), max(sl, entry), alpha=0.05, color=DOWN, zorder=1)
    ax.axhspan(min(entry, tp2), max(entry, tp2), alpha=0.05, color=UP,  zorder=1)

    # ── Seta na ultima vela ───────────────────────────────────────────────────
    sig_i  = n - 1
    is_long = direction == 'LONG'
    tip_y  = candles[sig_i]['l'] - price_range * 0.022 if is_long else candles[sig_i]['h'] + price_range * 0.022
    base_y = tip_y - price_range * 0.044 if is_long else tip_y + price_range * 0.044
    ax.annotate('', xy=(sig_i, tip_y), xytext=(sig_i, base_y),
                arrowprops=dict(arrowstyle='-|>', color=SIG_COLOR, lw=2.0, mutation_scale=18))
    ax.text(sig_i, base_y - price_range * 0.018 if is_long else base_y + price_range * 0.018,
            direction, color=SIG_COLOR, fontsize=11, fontweight='bold',
            ha='center', va='top' if is_long else 'bottom')

    # ── Watermark ─────────────────────────────────────────────────────────────
    ax.text(0.5, 0.5, '@mestressinais_br', color=TEXT, fontsize=28,
            alpha=0.04, transform=ax.transAxes, ha='center', va='center',
            rotation=20, fontweight='bold')

    # ── Eixo X (datas reais) ──────────────────────────────────────────────────
    step = max(1, n // 8)
    ax.set_xticks([])
    ticks = list(range(0, n, step))
    axv.set_xticks(ticks)
    tf_fmt = {'1m': '%H:%M', '3m': '%H:%M', '5m': '%H:%M',
              '15m': '%d/%m %Hh', '1h': '%d/%m %Hh', '4h': '%d/%m',
              '1d': '%d/%m'}.get(tf, '%d/%m %Hh')
    axv.set_xticklabels(
        [datetime.utcfromtimestamp(candles[i]['ts'] / 1000).strftime(tf_fmt)
         for i in ticks],
        fontsize=7.5, color=MUTED, rotation=0)
    axv.tick_params(axis='x', colors=MUTED, labelsize=7.5)
    axv.set_yticks([])
    axv.set_ylabel('Vol', color=MUTED, fontsize=7, labelpad=2)
    axv.yaxis.set_label_position('right')

    # ── Eixo Y à direita ──────────────────────────────────────────────────────
    all_prices = [c['h'] for c in candles] + [c['l'] for c in candles] + [tp2, sl]
    ymin = min(all_prices) - price_range * 0.08
    ymax = max(all_prices) + price_range * 0.14
    ax.set_ylim(ymin, ymax)
    pf = (lambda x, _: f'{x:,.4f}') if entry < 10 else \
         (lambda x, _: f'{x:,.2f}') if entry < 1000 else \
         (lambda x, _: f'{x:,.1f}')
    ax.yaxis.set_major_formatter(plt.FuncFormatter(pf))
    ax.yaxis.set_label_position('right'); ax.yaxis.tick_right()
    ax.tick_params(axis='y', labelsize=8, colors=MUTED)

    ax.legend(loc='upper left', fontsize=8, facecolor=BG,
              edgecolor=GRID, labelcolor=TEXT, framealpha=0.85, borderpad=0.5, handlelength=1.5)

    ax.set_title(
        f'{symbol} / USDT  ·  {tf.upper()}  ·  Binance Futures     '
        f'{direction}  ▪  Score {score}  ▪  R:R 1:{rr}  ▪  {conf_label}',
        color=TEXT, fontsize=10, pad=10, loc='left', fontweight='bold')
    ax.set_xlim(-1, n + 1.5)

    fig.subplots_adjust(left=0.02, right=0.88, top=0.93, bottom=0.06, hspace=0.0)
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=130, facecolor=BG, bbox_inches=None)
    plt.close()
    buf.seek(0)
    return buf.read()


def montar_caption(
    symbol, direction, tf, trade_type, score, rr, conf_label,
    vol24h, entry, sl, tp1, tp2, leverage,
    tendencia, estrutura, ema21_pos, ema200_pos, rsi, rsi_label,
    vela_forca_pct, vol_ratio, confirmacoes, recomendacao, modo, perfil,
) -> str:
    dir_icon  = '\U0001f7e2' if direction == 'LONG' else '\U0001f534'
    trade_icon = '\U0001f4c5' if trade_type == 'DAY TRADE' else '⏱'
    up_icon = '\U0001f53a'

    sl_pct  = ((sl  - entry) / entry) * 100
    tp1_pct = ((tp1 - entry) / entry) * 100
    tp2_pct = ((tp2 - entry) / entry) * 100

    filled = round(vela_forca_pct / 10)
    bar_str = '█' * filled + '░' * (10 - filled)

    lev = int(str(leverage).replace('x', ''))
    proj = '\n'.join(f'+{p}% → +{p * lev}%' for p in range(1, 6))
    conf_lines = '\n'.join(f'• {c}' for c in confirmacoes)

    from datetime import datetime
    now = datetime.now().strftime('%d/%m/%Y • %H:%M BRT')

    msg = (
        f'MODO: {modo} | PERFIL {perfil}\n\n'
        f'{symbol} | {dir_icon} {direction} | {tf.upper()} | {trade_icon} {trade_type}\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'\U0001f4ca Score: {score}/100\n'
        f'⚖️ R:R: {rr}:1\n'
        f'\U0001f3af Confianca: {conf_label}\n'
        f'\U0001f4b9 Vol. 24h: {vol24h}\n\n'
        f'\U0001f4b0 Entrada: ${entry:,.4f}\n'
        f'\U0001f6d1 Stop: ${sl:,.4f} ({sl_pct:.2f}%)\n\n'
        f'\U0001f3af TP1: ${tp1:,.4f} (+{tp1_pct:.2f}%)\n'
        f'\U0001f3af TP2: ${tp2:,.4f} (+{tp2_pct:.2f}%)\n'
        f'⚡️ Alavancagem Sugerida: {leverage}\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'\U0001f4c8 LEITURA DO MERCADO\n\n'
        f'{up_icon} Tendencia: {tendencia}\n'
        f'{up_icon} Estrutura: {estrutura}\n'
        f'{up_icon} EMA21 {ema21_pos}\n'
        f'{up_icon} EMA200 {ema200_pos}\n'
        f'\U0001f4ca RSI: {rsi} - {rsi_label}\n\n'
        f'\U0001f56f️ Forca da Vela: {bar_str} {vela_forca_pct}%\n'
        f'\U0001f4e6 Volume: {vol_ratio}x da media\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'✅ CONFIRMACOES\n\n'
        f'{conf_lines}\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'\U0001f4a1 RECOMENDACAO:\n\n'
        f'{recomendacao}\n'
        f'━━━━━━━━━━━━━━━━━━\n'
        f'\U0001f4c8 PROJECAO DE LUCRO ({leverage})\n\n'
        f'{proj}\n'
        f'━━━━━━━━━━━━━━━━━━\n\n'
        f'⏰ {now}\n\n'
        f'⚠️ Nao e conselho financeiro. Gerencie seu risco.'
    )
    return msg


if __name__ == '__main__':
    signal = dict(
        symbol    = 'ZECUSDT',
        direction = 'LONG',
        tf        = '1h',
        trade_type= 'DAY TRADE',
        score     = 82,
        rr        = 2.3,
        conf_label= 'Media',
        vol24h    = '963M USDT',
        entry     = 487.7300,
        sl        = 470.4961,
        tp1       = 510.7086,
        tp2       = 527.9425,
        leverage  = '3x',
        tendencia = 'ALTA',
        estrutura = 'UPTREND',
        ema21_pos = 'ACIMA',
        ema200_pos= 'ACIMA',
        rsi       = 87.1,
        rsi_label = 'Sobrecomprado',
        vela_forca_pct = 20,
        vol_ratio = 1.0,
        confirmacoes = [
            'Estrutura de mercado favoravel a direcao',
            'Regime de tendencia — operar a favor do fluxo direcional',
        ],
        recomendacao = (
            'LONG em sobrecompra (RSI 87) — operar apenas com confirmacao de continuacao. '
            'Reduzir tamanho para 50%.\nAguardar pullback antes de entrar se possivel.'
        ),
        modo  = 'SINAIS',
        perfil= 'AGRESSIVO',
    )

    print(f'Buscando klines reais de {signal["symbol"]} {signal["tf"]} na Binance...')
    chart_bytes = gerar_grafico_real(
        symbol    = signal['symbol'],
        direction = signal['direction'],
        tf        = signal['tf'],
        entry     = signal['entry'],
        sl        = signal['sl'],
        tp1       = signal['tp1'],
        tp2       = signal['tp2'],
        rr        = signal['rr'],
        score     = signal['score'],
        conf_label= signal['conf_label'],
    )
    print(f'  Grafico gerado: {len(chart_bytes):,} bytes')

    caption = montar_caption(**signal)
    # Limite Telegram para foto: 1024 chars
    if len(caption) > 1024:
        caption = caption[:1020] + '...'

    print(f'  Caption: {len(caption)} chars')
    print('Enviando ao Telegram...')

    resp = requests.post(
        f'https://api.telegram.org/bot{TOKEN}/sendPhoto',
        data={'chat_id': CHAT_ID, 'caption': caption, 'parse_mode': 'HTML'},
        files={'photo': ('signal.png', chart_bytes, 'image/png')},
        timeout=20,
    ).json()

    if resp.get('ok'):
        print('ENVIADO COM SUCESSO')
    else:
        print('ERRO:', resp)
