from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime
from enum import Enum


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalScore(BaseModel):
    trend: float = 0
    volume: float = 0
    momentum: float = 0
    market_structure: float = 0
    funding_oi: float = 0
    news_context: float = 0
    # Permite override do total calculado (usado pelo signal_engine com pesos adaptativos)
    total_override: Optional[float] = None

    @property
    def total(self) -> float:
        if self.total_override is not None:
            return self.total_override
        return (
            self.trend * 0.25
            + self.volume * 0.20
            + self.momentum * 0.15
            + self.market_structure * 0.15
            + self.funding_oi * 0.15
            + self.news_context * 0.10
        )

    @property
    def label(self) -> str:
        t = self.total
        if t >= 90: return "EXCEPTIONAL"
        if t >= 80: return "STRONG"
        if t >= 70: return "MODERATE"
        return "IGNORE"


class TradeSignal(BaseModel):
    asset: str
    direction: Direction
    entry: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    rr: float
    confidence: float
    reason: str
    score: SignalScore
    timeframe: str
    trade_type: str = "DAY_TRADE"   # SCALP | DAY_TRADE | SWING
    anomaly: str = ""               # descrição de anomalia detectada (volume spike, etc.)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    # Campos de qualidade para exibição no Telegram
    body_pct: float = 0.0           # % corpo/range da última vela (0-1)
    vol_ratio: float = 1.0          # volume atual vs média (ex: 2.5 = 2.5x)
    rsi_val: float = 50.0           # RSI atual
    confirmed_signals: list = []    # lista de sinais confirmados (texto)
    recommendation: str = ""        # recomendação textual
    suggested_leverage: int = 0     # alavancagem sugerida pelo risk manager
    leverage_reason: str = ""       # motivo da alavancagem sugerida
    # Padrões de candlestick detectados pelo CandlePatternEngine
    patterns_detected: list = []    # padrões no TF do sinal [{name_pt, signal, strength}]
    patterns_mtf: dict  = {}        # padrões em TFs maiores {tf: [{name_pt, signal, strength}]}


class ActiveTrade(BaseModel):
    id: str
    asset: str
    direction: Direction
    entry_price: float
    current_price: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    rr: float
    leverage: int
    size_usdt: float
    pnl_pct: float = 0.0
    pnl_usdt: float = 0.0
    trailing_level: int = 0  # which milestone reached
    tp1_hit: bool = False     # TP1 ja foi atingido (scale-out 35% feito)
    tp2_hit: bool = False     # TP2 ja foi atingido (scale-out 70% feito)
    status: Literal["OPEN", "CLOSED", "CANCELLED"] = "OPEN"
    reason: str
    confidence: float
    opened_at: datetime = Field(default_factory=datetime.utcnow)
    closed_at: Optional[datetime] = None


class WebhookAlert(BaseModel):
    secret: str
    asset: str
    direction: str
    price: Optional[float] = None
    timeframe: Optional[str] = "15m"
    reason: Optional[str] = ""


class MarketSnapshot(BaseModel):
    btc_price: float
    btc_funding: float
    btc_oi: float
    btc_change_24h: float
    eth_price: float
    eth_funding: float
    sol_price: float
    market_sentiment: str
    long_bias: float
    short_bias: float
    timestamp: datetime = Field(default_factory=datetime.utcnow)
