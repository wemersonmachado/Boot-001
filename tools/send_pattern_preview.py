"""
Envia preview da análise MTF de padrões ao Telegram para aprovação.
Executa standalone — não requer o bot principal rodando.

Uso:
    python send_pattern_preview.py
    python send_pattern_preview.py ETHUSDT
"""
import asyncio
import sys
import os

# Garante que o path inclui o diretório do bot
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()


async def main():
    symbol = sys.argv[1].upper() if len(sys.argv) > 1 else "BTCUSDT"

    print(f"[PREVIEW] Analisando padrões MTF para {symbol}...")

    from candle_pattern_engine import (
        analyze_asset_mtf, summarize_bias, MTF_TIMEFRAMES,
        format_mtf_telegram, MTF_LABELS, SIGNAL_ICONS, STRENGTH_LABEL,
    )

    mtf_results = await analyze_asset_mtf(symbol)
    bias_info   = summarize_bias(mtf_results)

    # Log resumo no terminal (sem emoji para evitar encoding issues no Windows)
    sig_map = {"bullish": "[ALTA]", "bearish": "[BAIXA]", "neutral": "[NEUTRO]"}
    print(f"\n{'='*55}")
    print(f"  ANALISE MTF -- {symbol}")
    print(f"  Vies: {bias_info['bias']}  |  "
          f"Bull {bias_info['bull_score']}  Bear {bias_info['bear_score']}")
    print(f"{'='*55}")
    for tf in MTF_TIMEFRAMES:
        label    = MTF_LABELS.get(tf, tf)
        patterns = mtf_results.get(tf, [])
        if patterns:
            top = sorted(patterns, key=lambda x: -x.strength)[:3]
            pat_str = " | ".join(
                f"{sig_map.get(p.signal,'[?]')} {p.name_pt} {'*'*p.strength}"
                for p in top
            )
            print(f"  {label:>3}  {pat_str}")
        else:
            print(f"  {label:>3}  -- sem padrao")
    print(f"{'='*55}\n")

    # Envia ao Telegram
    from notifier import send_pattern_approval_preview
    ok = await send_pattern_approval_preview(symbol, mtf_results, bias_info)

    if ok:
        print("[PREVIEW] OK - Mensagem enviada ao Telegram com sucesso!")
    else:
        print("[PREVIEW] FALHA - Verifique TELEGRAM_TOKEN e TELEGRAM_CHAT_ID.")


if __name__ == "__main__":
    asyncio.run(main())
