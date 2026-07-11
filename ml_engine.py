"""
ML Engine — auto-treino por ativo (estilo FreqAI)
Treina RandomForest + XGBoost sobre trades fechados do banco.
Retorna probabilidade [0..1] que é convertida em bônus de score (-10..+15 pts).
Retreina automaticamente a cada RETRAIN_INTERVAL novos trades fechados.
"""
import os
import json
import asyncio
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Optional
from pathlib import Path

import joblib
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import cross_val_score
from sklearn.calibration import CalibratedClassifierCV
import xgboost as xgb

MODELS_DIR   = Path("ml_models")
MODELS_DIR.mkdir(exist_ok=True)

RETRAIN_INTERVAL  = 15       # FIX 2026-07-11: 30→15 — retreina mais rápido enquanto a amostra é pequena
MIN_SAMPLES_LOCAL = 25       # mínimo para modelo por ativo (senão usa global)
MIN_SAMPLES_GLOBAL= 15       # mínimo para SEQUER TREINAR o modelo global (não confundir com MIN_SAMPLES_TO_APPLY)
GLOBAL_MODEL_KEY  = "__global__"

# FIX 2026-07-11 — trava de confiabilidade do bônus de ML (não do treino):
# em produção, com 22 amostras (acima do MIN_SAMPLES_GLOBAL de 15, que só
# controla se treina), o modelo teve AUC 0.30 e 0.38 — PIOR que cara-ou-coroa
# (0.5) — e mesmo assim aplicava até ±15pts na decisão de abrir ou não um
# trade. is_model_reliable() exige amostra maior E qualidade mínima (AUC)
# antes do bônus poder influenciar qualquer decisão real — o modelo continua
# treinando/aprendendo em background, só não "vota" enquanto não provar que
# é melhor que chute.
MIN_SAMPLES_TO_APPLY = 40
MIN_AUC_TO_APPLY     = 0.55

# Cache em memória
_models: dict        = {}   # asset → {"clf": ..., "scaler": ..., "trained_at": ..., "n_samples": ...}
_trade_count_at_last_train: int = 0


# ── Extração de features ──────────────────────────────────────────────────────

def extract_features(df: pd.DataFrame, direction: str, confidence: float,
                     rsi_val: float = 50.0, atr_pct: float = 1.0,
                     vol_ratio: float = 1.0, funding: float = 0.0) -> Optional[np.ndarray]:
    """
    Extrai vetor de features de um sinal para predição.
    Combina dados de klines + metadados do sinal.
    """
    if df is None or len(df) < 50:
        return None
    try:
        close  = df["close"]
        volume = df["volume"]
        high   = df["high"]
        low    = df["low"]

        # EMAs
        e9   = close.ewm(span=9,   adjust=False).mean().iloc[-1]
        e21  = close.ewm(span=21,  adjust=False).mean().iloc[-1]
        e50  = close.ewm(span=50,  adjust=False).mean().iloc[-1]
        e200 = close.ewm(span=200, adjust=False).mean().iloc[-1] if len(df) >= 200 else e50
        price = float(close.iloc[-1])

        ema_align_long  = int(e9 > e21 > e50)
        ema_align_short = int(e9 < e21 < e50)
        above_e200      = int(price > e200)

        # RSI
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi_14 = float((100 - 100/(1+rs)).iloc[-1])
        rsi_7  = float((100 - 100/(1 + delta.clip(lower=0).rolling(7).mean() /
                         (-delta.clip(upper=0)).rolling(7).mean().replace(0,np.nan))).iloc[-1])

        # MACD hist direction
        macd_fast = close.ewm(span=12, adjust=False).mean()
        macd_slow = close.ewm(span=26, adjust=False).mean()
        macd_hist = (macd_fast - macd_slow) - (macd_fast - macd_slow).ewm(span=9, adjust=False).mean()
        macd_rising = int(float(macd_hist.iloc[-1]) > float(macd_hist.iloc[-2]))

        # BB width
        bb_mid   = close.rolling(20).mean()
        bb_std   = close.rolling(20).std()
        bb_width = float(((bb_mid + 2*bb_std) - (bb_mid - 2*bb_std)).iloc[-1] / bb_mid.iloc[-1] * 100)

        # Volume
        vol_avg  = float(volume.rolling(20).mean().iloc[-1])
        vol_last = float(volume.iloc[-1])
        vol_r    = vol_last / vol_avg if vol_avg > 0 else 1.0

        # ATR%
        hl  = high - low
        hc  = (high - close.shift()).abs()
        lc  = (low  - close.shift()).abs()
        atr_val = float(pd.concat([hl,hc,lc],axis=1).max(axis=1).rolling(14).mean().iloc[-1])
        atr_p   = atr_val / price * 100 if price > 0 else 1.0

        # Hora do dia e dia da semana
        ts    = df.index[-1]
        hour  = ts.hour  if hasattr(ts, 'hour')  else 12
        dow   = ts.dayofweek if hasattr(ts,'dayofweek') else 2

        # Candle body %
        c1 = df.iloc[-1]
        body_pct = abs(float(c1["close"]) - float(c1["open"])) / float(c1["open"]) * 100

        dir_bin = 1 if direction.upper() == "LONG" else 0

        feats = np.array([
            confidence,          # score original do sinal
            dir_bin,             # direção
            rsi_14,              # RSI 14
            rsi_7,               # RSI 7
            ema_align_long,      # EMAs alinhadas long
            ema_align_short,     # EMAs alinhadas short
            above_e200,          # preço acima EMA200
            macd_rising,         # MACD hist crescendo
            bb_width,            # BB width %
            vol_r,               # volume ratio
            atr_p,               # ATR %
            funding,             # funding rate
            body_pct,            # corpo do candle %
            hour,                # hora
            dow,                 # dia da semana
        ], dtype=float)

        feats = np.nan_to_num(feats, nan=0.0, posinf=5.0, neginf=-5.0)
        return feats
    except Exception:
        return None


# ── Carregar trades do banco ──────────────────────────────────────────────────

async def _load_closed_trades() -> pd.DataFrame:
    """Carrega trades fechados com PnL do banco SQLite."""
    try:
        import aiosqlite
        from config import DB_PATH
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT asset, direction, entry_price, exit_price, stop_loss, "
                "tp2, rr, leverage, confidence, reason, timeframe, pnl_pct, pnl_usdt, "
                "opened_at, closed_at, score_json "
                "FROM trades WHERE status='CLOSED' AND exit_price IS NOT NULL "
                "ORDER BY closed_at DESC LIMIT 2000"
            ) as cur:
                rows = await cur.fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([dict(r) for r in rows])
    except Exception as e:
        print(f"[ML] Erro ao carregar trades: {e}")
        return pd.DataFrame()


# ── Treinar modelo ────────────────────────────────────────────────────────────

def _train_model(X: np.ndarray, y: np.ndarray, label: str) -> Optional[dict]:
    """Treina ensemble RF + XGB com validação cruzada. Retorna dict do modelo."""
    if len(X) < MIN_SAMPLES_GLOBAL:
        return None
    try:
        scaler = StandardScaler()
        Xs     = scaler.fit_transform(X)

        rf  = CalibratedClassifierCV(
            RandomForestClassifier(n_estimators=100, max_depth=6, random_state=42, n_jobs=-1),
            cv=3, method='isotonic'
        )
        xgb_clf = CalibratedClassifierCV(
            xgb.XGBClassifier(n_estimators=100, max_depth=4, learning_rate=0.1,
                               random_state=42, eval_metric='logloss',
                               verbosity=0, use_label_encoder=False),
            cv=3, method='isotonic'
        )

        rf.fit(Xs, y)
        xgb_clf.fit(Xs, y)

        # CV score
        cv_rf  = cross_val_score(rf,  Xs, y, cv=3, scoring='roc_auc').mean()
        cv_xgb = cross_val_score(xgb_clf, Xs, y, cv=3, scoring='roc_auc').mean()

        model_dict = {
            "rf":         rf,
            "xgb":        xgb_clf,
            "scaler":     scaler,
            "cv_rf":      round(cv_rf, 3),
            "cv_xgb":     round(cv_xgb, 3),
            "n_samples":  len(X),
            "win_rate":   round(float(y.mean()), 3),
            "trained_at": datetime.utcnow().isoformat(),
            "label":      label,
        }

        path = MODELS_DIR / f"{label.replace('/', '_')}.joblib"
        joblib.dump(model_dict, path)
        print(f"[ML] Modelo '{label}' treinado | n={len(X)} | AUC RF={cv_rf:.3f} XGB={cv_xgb:.3f}")
        return model_dict
    except Exception as e:
        print(f"[ML] Erro treinando '{label}': {e}")
        return None


async def train_all_models():
    """Treina modelos por ativo e global com trades do banco."""
    global _trade_count_at_last_train

    df = await _load_closed_trades()
    if df.empty:
        print("[ML] Sem trades fechados para treinar.")
        return

    total = len(df)
    if total - _trade_count_at_last_train < RETRAIN_INTERVAL and _models:
        return  # ainda não precisa retreinar

    print(f"[ML] Iniciando treino com {total} trades fechados...")
    _trade_count_at_last_train = total

    # Label: 1 = lucrativo, 0 = prejuízo
    df["won"] = (df["pnl_pct"] > 0).astype(int)

    # Features simples extraídas do banco (sem klines — rápido)
    def _feats_from_row(row) -> Optional[np.ndarray]:
        try:
            score_data = json.loads(row.get("score_json") or "{}") if row.get("score_json") else {}
            conf  = float(row.get("confidence", 60))
            rr    = float(row.get("rr", 2.0))
            lev   = float(row.get("leverage", 5))
            dir_b = 1 if str(row.get("direction","")).upper() == "LONG" else 0
            trend = score_data.get("trend", 50)
            vol_s = score_data.get("volume", 50)
            mom_s = score_data.get("momentum", 50)
            mkt_s = score_data.get("market_structure", 50)
            fund_s= score_data.get("funding_oi", 50)
            hour  = 12
            try:
                ts   = datetime.fromisoformat(str(row.get("opened_at","")).replace("Z",""))
                hour = ts.hour
                dow  = ts.weekday()
            except Exception:
                dow  = 2
            return np.array([conf, rr, lev, dir_b, trend, vol_s, mom_s, mkt_s,
                              fund_s, hour, dow], dtype=float)
        except Exception:
            return None

    rows_list = df.to_dict('records')
    X_all, y_all, assets = [], [], []
    for row in rows_list:
        f = _feats_from_row(row)
        if f is not None and not np.isnan(f).any():
            X_all.append(f)
            y_all.append(int(row["won"]))
            assets.append(str(row.get("asset", "")))

    if len(X_all) < MIN_SAMPLES_GLOBAL:
        print(f"[ML] Amostras insuficientes: {len(X_all)}")
        return

    X_all = np.array(X_all)
    y_all = np.array(y_all)

    # Modelo global
    m = _train_model(X_all, y_all, GLOBAL_MODEL_KEY)
    if m:
        _models[GLOBAL_MODEL_KEY] = m

    # Modelos por ativo (se tiver amostras suficientes)
    unique_assets = set(assets)
    for asset in unique_assets:
        idx = [i for i,a in enumerate(assets) if a == asset]
        if len(idx) >= MIN_SAMPLES_LOCAL:
            Xa = X_all[idx]
            ya = y_all[idx]
            m  = _train_model(Xa, ya, asset)
            if m:
                _models[asset] = m

    print(f"[ML] Treino concluído. Modelos: {list(_models.keys())[:8]}")


def load_saved_models():
    """Carrega modelos salvos em disco na inicialização."""
    for path in MODELS_DIR.glob("*.joblib"):
        try:
            m = joblib.load(path)
            label = path.stem.replace("_", "/") if "/" in path.stem else path.stem
            label = "__global__" if path.stem == "__global__" else label
            _models[label] = m
            print(f"[ML] Carregado: {label} (n={m.get('n_samples',0)}, AUC_RF={m.get('cv_rf',0):.3f})")
        except Exception as e:
            print(f"[ML] Falha ao carregar {path}: {e}")


# ── Predição ──────────────────────────────────────────────────────────────────

def predict_win_probability(asset: str, features_dict: dict) -> tuple[float, str]:
    """
    Retorna (probabilidade_vitoria [0..1], model_usado).
    Usa modelo do ativo se disponível, senão global.
    Retorna (0.5, 'none') se nenhum modelo disponível.
    """
    model_key = asset if asset in _models else GLOBAL_MODEL_KEY
    if model_key not in _models:
        return 0.5, "none"

    m = _models[model_key]
    try:
        conf  = features_dict.get("confidence", 60)
        rr    = features_dict.get("rr", 2.0)
        lev   = features_dict.get("leverage", 5)
        dir_b = 1 if str(features_dict.get("direction","")).upper() == "LONG" else 0
        score = features_dict.get("score", {})
        if isinstance(score, dict):
            trend = score.get("trend", 50)
            vol_s = score.get("volume", 50)
            mom_s = score.get("momentum", 50)
            mkt_s = score.get("market_structure", 50)
            fund_s= score.get("funding_oi", 50)
        else:
            trend = vol_s = mom_s = mkt_s = fund_s = 50
        hour = datetime.utcnow().hour
        dow  = datetime.utcnow().weekday()

        feats = np.array([[conf, rr, lev, dir_b, trend, vol_s, mom_s, mkt_s,
                           fund_s, hour, dow]], dtype=float)
        feats = np.nan_to_num(feats)

        Xs = m["scaler"].transform(feats)
        # Ensemble: média ponderada por AUC
        w_rf  = m.get("cv_rf",  0.5)
        w_xgb = m.get("cv_xgb", 0.5)
        w_tot = w_rf + w_xgb + 1e-9
        p_rf  = m["rf"].predict_proba(Xs)[0][1]
        p_xgb = m["xgb"].predict_proba(Xs)[0][1]
        prob  = (p_rf * w_rf + p_xgb * w_xgb) / w_tot
        return round(float(prob), 4), model_key
    except Exception as e:
        return 0.5, f"error:{e}"


def ml_score_bonus(asset: str, features_dict: dict) -> float:
    """
    Converte probabilidade ML em bônus de score (-10 a +15 pts).
    prob > 0.65  → +15 (alta confiança de ganho)
    prob > 0.55  → +8
    prob > 0.45  → 0  (neutro)
    prob < 0.40  → -5
    prob < 0.30  → -10 (ML acha que vai perder)
    """
    prob, source = predict_win_probability(asset, features_dict)
    if source == "none":
        return 0.0
    if prob >= 0.65:
        return 15.0
    elif prob >= 0.55:
        return 8.0
    elif prob >= 0.45:
        return 0.0
    elif prob >= 0.35:
        return -5.0
    else:
        return -10.0


def get_ml_status() -> dict:
    """Retorna resumo do estado do ML Engine."""
    return {
        "models_loaded": len(_models),
        "assets_with_model": [k for k in _models if k != GLOBAL_MODEL_KEY],
        "global_model": GLOBAL_MODEL_KEY in _models,
        "global_auc_rf":  _models[GLOBAL_MODEL_KEY].get("cv_rf",  0) if GLOBAL_MODEL_KEY in _models else 0,
        "global_auc_xgb": _models[GLOBAL_MODEL_KEY].get("cv_xgb", 0) if GLOBAL_MODEL_KEY in _models else 0,
        "global_n_samples": _models[GLOBAL_MODEL_KEY].get("n_samples", 0) if GLOBAL_MODEL_KEY in _models else 0,
        "trade_count_at_last_train": _trade_count_at_last_train,
        "reliable": is_model_reliable(),
    }


def is_model_reliable() -> bool:
    """True se o modelo global tem amostra E qualidade (AUC) suficientes para
    o bônus de score influenciar decisões reais de entrada (ver MIN_SAMPLES_
    TO_APPLY / MIN_AUC_TO_APPLY). O modelo continua treinando e acumulando
    dados normalmente mesmo quando isto retorna False — só não "vota" na
    decisão de abrir trade enquanto não provar que é melhor que chute."""
    if GLOBAL_MODEL_KEY not in _models:
        return False
    m = _models[GLOBAL_MODEL_KEY]
    if m.get("n_samples", 0) < MIN_SAMPLES_TO_APPLY:
        return False
    if m.get("cv_rf", 0) < MIN_AUC_TO_APPLY or m.get("cv_xgb", 0) < MIN_AUC_TO_APPLY:
        return False
    return True
