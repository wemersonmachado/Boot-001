"""
DCA Engine — Dollar Cost Averaging adaptativo por volatilidade (ATR)
Estratégia: entra 30% no sinal, adiciona 35% no Nível 1, 35% no Nível 2 (espaçamento por ATR).
Gatilho condicionado ao Stochastic RSI 1m (filtro de exaustão) com Hard-Trigger de segurança.
Stop e alvo calculados sobre o preço médio ponderado.
"""
import asyncio
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field
import numpy as np
import pandas as pd

# ── Config padrão ─────────────────────────────────────────────────────────────
DCA_LEVELS = [
    # (multiplicador_de_ATR, fração_da_banca)
    (0.0,  0.30),   # nível 0 — entrada inicial
    (1.5,  0.35),   # nível 1 — adiciona a -1.5x ATR
    (3.0,  0.35),   # nível 2 — adiciona a -3.0x ATR
]
DCA_TP_MULT  = 2.5   # alvo = preco_medio + ATR * DCA_TP_MULT
DCA_SL_MULT  = 1.5   # stop  = preco_medio - ATR * DCA_SL_MULT (abaixo do último nível)
DCA_MAX_LOSS_PCT = 8.0  # cancela DCA se posição >8% contra (proteção máxima)


@dataclass
class DCAPosition:
    asset:        str
    direction:    str          # "LONG" | "SHORT"
    atr:          float        # ATR do ativo no momento do sinal
    banca_usdt:   float        # banca efetiva para calcular tamanhos
    levels_done:  list = field(default_factory=list)   # índices de níveis executados
    entries:      list = field(default_factory=list)   # [(price, qty_usdt), ...]
    dca_targets:  list = field(default_factory=list)   # preços-alvo para cada nível [L0, L1, L2]
    stop_loss:    float = 0.0
    take_profit:  float = 0.0
    opened_at:    str   = ""
    status:       str   = "ACTIVE"  # ACTIVE | COMPLETED | CANCELLED

    @property
    def avg_entry(self) -> float:
        total_usdt = sum(q for _, q in self.entries)
        if total_usdt == 0:
            return 0.0
        return sum(p * q for p, q in self.entries) / total_usdt

    @property
    def total_usdt(self) -> float:
        return sum(q for _, q in self.entries)

    @property
    def current_level(self) -> int:
        return len(self.levels_done)

    def recalc_levels(self):
        """Recalcula stop e alvo baseado no preço médio atual."""
        avg = self.avg_entry
        if avg <= 0:
            return
        if self.direction == "LONG":
            self.stop_loss   = round(avg - self.atr * DCA_SL_MULT, 8)
            self.take_profit = round(avg + self.atr * DCA_TP_MULT, 8)
        else:
            self.stop_loss   = round(avg + self.atr * DCA_SL_MULT, 8)
            self.take_profit = round(avg - self.atr * DCA_TP_MULT, 8)


# ── Estado global ─────────────────────────────────────────────────────────────
_dca_positions: dict[str, DCAPosition] = {}   # asset → DCAPosition
_dca_enabled:   bool = False


def enable_dca(enabled: bool):
    global _dca_enabled
    _dca_enabled = enabled
    print(f"[DCA] Modo DCA {'ATIVADO' if enabled else 'DESATIVADO'}")


def is_dca_enabled() -> bool:
    return _dca_enabled


# ── Abertura de posição DCA ───────────────────────────────────────────────────

def open_dca_position(asset: str, direction: str, price: float,
                      atr: float, banca_usdt: float) -> Optional[DCAPosition]:
    """Inicia posição DCA — calcula níveis dinâmicos e executa o nível 0 (30%)."""
    if asset in _dca_positions and _dca_positions[asset].status == "ACTIVE":
        return None  # já tem posição aberta

    _, frac0 = DCA_LEVELS[0]
    qty0 = banca_usdt * frac0

    # Calcula preços-alvo dinâmicos de safety order baseados no ATR
    if direction == "LONG":
        dca_targets = [
            price,
            price - atr * 1.5,
            price - atr * 3.0
        ]
    else:
        dca_targets = [
            price,
            price + atr * 1.5,
            price + atr * 3.0
        ]

    pos = DCAPosition(
        asset      = asset,
        direction  = direction,
        atr        = atr,
        banca_usdt = banca_usdt,
        dca_targets = dca_targets,
        opened_at  = datetime.utcnow().isoformat(),
    )
    pos.entries.append((price, qty0))
    pos.levels_done.append(0)
    pos.recalc_levels()
    _dca_positions[asset] = pos

    print(f"[DCA] {asset} {direction} | Nível 0 @ ${price:.6f} | ${qty0:.2f} USDT | "
          f"L1_Tgt=${dca_targets[1]:.6f} L2_Tgt=${dca_targets[2]:.6f} | "
          f"SL=${pos.stop_loss:.6f} TP=${pos.take_profit:.6f}")
    return pos


# ── Filtro de Exaustão (Stochastic RSI 1m) ───────────────────────────────────

async def check_stoch_rsi_exhaustion(asset: str, direction: str) -> bool:
    """Verifica se o Stochastic RSI 1m indica exaustão (sobrevenda para LONG / sobrecompra para SHORT)."""
    try:
        from klines_cache import get_klines_cached
        # Busca klines de 1m (TTL curto, 25 velas bastam)
        df = await get_klines_cached(asset, "1m", limit=30)
        if df is None or len(df) < 15:
            return True  # se falhar busca de dados, autoriza por segurança

        close = df["close"]
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi_vals = 100 - 100 / (1 + rs)
        rsi_vals = rsi_vals.fillna(50.0)

        min_rsi = rsi_vals.rolling(14).min()
        max_rsi = rsi_vals.rolling(14).max()
        rng = max_rsi - min_rsi
        k_vals = np.where(rng == 0, 50.0, 100 * (rsi_vals - min_rsi) / rng.replace(0, np.nan))
        k_series = pd.Series(k_vals, index=df.index).rolling(3).mean()
        k_val = k_series.iloc[-1]

        if direction == "LONG":
            # Sobrevendida (K < 30) = exaustão de venda (ideal para comprar DCA)
            return k_val < 30
        else:
            # Sobrecomprada (K > 70) = exaustão de compra (ideal para vender DCA)
            return k_val > 70
    except Exception as e:
        print(f"[DCA EXHAUSTION] Erro Stoch RSI para {asset}: {e}")
        return True  # fallback seguro se der erro


# ── Checagem de níveis ────────────────────────────────────────────────────────

async def get_next_dca_level(asset: str, current_price: float) -> Optional[dict]:
    """
    Verifica se o preço atingiu o próximo nível DCA.
    Gatilho condicionado ao Stochastic RSI 1m e salvaguarda Hard-Trigger de queda maior.
    """
    pos = _dca_positions.get(asset)
    if not pos or pos.status != "ACTIVE":
        return None

    next_idx = pos.current_level
    if next_idx >= len(DCA_LEVELS):
        return None  # todos os níveis executados

    target_price = pos.dca_targets[next_idx]
    _, frac = DCA_LEVELS[next_idx]

    # Preço passou da linha do trigger?
    if pos.direction == "LONG":
        triggered = current_price <= target_price
    else:
        triggered = current_price >= target_price

    if triggered:
        # 1. Filtro Stochastic RSI 1m & Salvaguarda Hard Trigger (caiu/subiu mais que 2% além do target)
        if pos.direction == "LONG":
            hard_trigger = current_price <= target_price * 0.98
        else:
            hard_trigger = current_price >= target_price * 1.02

        stoch_ok = await check_stoch_rsi_exhaustion(asset, pos.direction)
        
        if not stoch_ok and not hard_trigger:
            # Aguardando confirmação de exaustão do Stoch RSI
            return None

        # 2. Verifica proteção de perda máxima
        avg = pos.avg_entry
        if avg > 0:
            loss_pct = abs(current_price - avg) / avg * 100
            if (pos.direction == "LONG"  and current_price < avg and loss_pct > DCA_MAX_LOSS_PCT) or \
               (pos.direction == "SHORT" and current_price > avg and loss_pct > DCA_MAX_LOSS_PCT):
                print(f"[DCA] {asset} — perda máxima {loss_pct:.1f}% atingida. Cancelando DCA.")
                pos.status = "CANCELLED"
                return None

        qty = pos.banca_usdt * frac
        return {
            "asset":      asset,
            "level":      next_idx,
            "price":      current_price,
            "qty_usdt":   qty,
            "direction":  pos.direction,
            "hard_triggered": hard_trigger,
        }
    return None


def execute_dca_level(asset: str, price: float, qty_usdt: float, level: int):
    """Registra execução de um nível DCA e recalcula médias."""
    pos = _dca_positions.get(asset)
    if not pos:
        return
    pos.entries.append((price, qty_usdt))
    pos.levels_done.append(level)
    pos.recalc_levels()
    print(f"[DCA] {asset} Nível {level} executado @ ${price:.6f} | "
          f"Preço médio: ${pos.avg_entry:.6f} | Total: ${pos.total_usdt:.2f} USDT | "
          f"Novo SL=${pos.stop_loss:.6f} Novo TP=${pos.take_profit:.6f}")


def check_dca_exit(asset: str, current_price: float) -> Optional[str]:
    """Verifica se atingiu TP ou SL. Retorna 'TP' | 'SL' | None."""
    pos = _dca_positions.get(asset)
    if not pos or pos.status != "ACTIVE" or not pos.entries:
        return None

    if pos.direction == "LONG":
        if current_price >= pos.take_profit:
            return "TP"
        if current_price <= pos.stop_loss:
            return "SL"
    else:
        if current_price <= pos.take_profit:
            return "TP"
        if current_price >= pos.stop_loss:
            return "SL"
    return None


def close_dca_position(asset: str, exit_price: float, reason: str) -> Optional[dict]:
    """Fecha posição DCA e retorna resumo do resultado."""
    pos = _dca_positions.get(asset)
    if not pos:
        return None

    avg = pos.avg_entry
    if avg <= 0:
        return None

    if pos.direction == "LONG":
        pnl_pct = (exit_price - avg) / avg * 100
    else:
        pnl_pct = (avg - exit_price) / avg * 100

    pnl_usdt = pos.total_usdt * pnl_pct / 100
    pos.status = "COMPLETED"

    result = {
        "asset":       asset,
        "direction":   pos.direction,
        "avg_entry":   round(avg, 8),
        "exit_price":  round(exit_price, 8),
        "pnl_pct":     round(pnl_pct, 2),
        "pnl_usdt":    round(pnl_usdt, 2),
        "levels_done": len(pos.levels_done),
        "total_usdt":  round(pos.total_usdt, 2),
        "reason":      reason,
    }
    print(f"[DCA] {asset} FECHADO por {reason} | PnL: {pnl_pct:+.2f}% (${pnl_usdt:+.2f})")
    del _dca_positions[asset]
    return result


def get_dca_status() -> dict:
    """Retorna estado atual de todas as posições DCA."""
    return {
        "enabled": _dca_enabled,
        "open_positions": {
            asset: {
                "direction":   pos.direction,
                "avg_entry":   round(pos.avg_entry, 8),
                "total_usdt":  round(pos.total_usdt, 2),
                "level":       pos.current_level,
                "max_levels":  len(DCA_LEVELS),
                "stop_loss":   round(pos.stop_loss, 8),
                "take_profit": round(pos.take_profit, 8),
                "opened_at":   pos.opened_at,
            }
            for asset, pos in _dca_positions.items()
            if pos.status == "ACTIVE"
        }
    }
