import sqlite3
import pandas as pd

try:
    conn = sqlite3.connect('trader_001.db')
    
    # 1. PnL Total e Taxa de Acerto
    print("=== PERFORMANCE GERAL DAS TRADES ===")
    trades_df = pd.read_sql_query("SELECT id, asset, direction, status, pnl_pct, pnl_usdt, reason, closed_at FROM trades WHERE status != 'OPEN'", conn)
    
    if not trades_df.empty:
        # Tenta calcular acertos baseando-se no PnL percentual
        trades_df['is_win'] = trades_df['pnl_pct'] > 0
        total_trades = len(trades_df)
        wins = trades_df['is_win'].sum()
        losses = total_trades - wins
        win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0
        
        print(f"Total de Trades Fechados: {total_trades}")
        print(f"Wins: {wins} | Losses: {losses} | Win Rate: {win_rate:.2f}%")
        
        # O PNL USDT pode estar como string se não foi salvo corretamente
        trades_df['pnl_usdt'] = pd.to_numeric(trades_df['pnl_usdt'], errors='coerce').fillna(0)
        trades_df['pnl_pct'] = pd.to_numeric(trades_df['pnl_pct'], errors='coerce').fillna(0)
        
        print(f"Lucro Total (USDT): ${trades_df['pnl_usdt'].sum():.2f}")
        print(f"Média PnL por trade: {trades_df['pnl_pct'].mean():.2f}%\n")
        
        # Piores e Melhores
        best_trade = trades_df.loc[trades_df['pnl_pct'].idxmax()]
        worst_trade = trades_df.loc[trades_df['pnl_pct'].idxmin()]
        print(f"Melhor Trade: {best_trade['asset']} ({best_trade['direction']}) -> {best_trade['pnl_pct']}% / ${best_trade['pnl_usdt']}")
        print(f"Pior Trade: {worst_trade['asset']} ({worst_trade['direction']}) -> {worst_trade['pnl_pct']}% / ${worst_trade['pnl_usdt']}\n")
        
        print("=== TRADES POR MOTIVO ===")
        print(trades_df.groupby('reason')['pnl_pct'].mean())
    else:
        print("Nenhum trade fechado no banco de dados com valores numéricos.")

    # 2. Sinais
    print("\n=== ESTATÍSTICA DE SINAIS ===")
    sig_df = pd.read_sql_query("SELECT direction, executed, COUNT(*) as count FROM signals GROUP BY direction, executed", conn)
    print(sig_df)
    
except Exception as e:
    print("Erro ao ler banco:", e)
finally:
    if 'conn' in locals():
        conn.close()
