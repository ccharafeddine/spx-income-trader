"""0DTE option pricing for the backtest: Black-Scholes (r=q=0, intraday) on trading time,
ATM vol from VIX (scaled to VIX1D), a parametric put-skew smile, and a bid/ask model."""
import numpy as np
from scipy.special import ndtr
MIN_PER_YEAR = 390 * 252

def smile(K, S, sig_atm, T, b=0.06, c_put=0.08, c_call=0.02):
    z = np.log(K / S) / (sig_atm * np.sqrt(T))
    z = np.clip(z, -4, 4)
    c = np.where(z < 0, c_put, c_call)
    m = 1 - b * z + c * z * z
    m = np.where(m > 1, 1 + 0.7 * np.tanh((m - 1) / 0.7), np.maximum(m, 0.7))  # soft cap ~1.7x ATM in the wings
    return sig_atm * m

def bs(S, K, T, sig, call):
    T = np.maximum(T, 1e-9)
    sd = sig * np.sqrt(T)
    d1 = (np.log(S / K) + 0.5 * sd * sd) / sd; d2 = d1 - sd
    if call: return S * ndtr(d1) - K * ndtr(d2)
    return K * ndtr(-d2) - S * ndtr(-d1)

def leg_price(S, K, minutes_left, sig_atm, call, sk):
    T = np.maximum(minutes_left, 0.25) / MIN_PER_YEAR
    sig = smile(K, S, sig_atm, T, **sk)
    p = bs(S, K, T, sig, call)
    intr = np.maximum(S - K, 0) if call else np.maximum(K - S, 0)
    return np.maximum(p, intr)

def half_spread(price, vix):
    w = np.clip(0.05 + 0.015 * price, 0.05, 0.60) * (1.3 if vix >= 25 else 1.0)
    return w / 2

def delta(S, K, minutes_left, sig_atm, call, sk):
    T = max(minutes_left, 0.25) / MIN_PER_YEAR
    sig = smile(np.asarray(K, float), S, sig_atm, T, **sk)
    sd = sig * np.sqrt(T)
    d1 = (np.log(S / np.asarray(K, float)) + 0.5 * sd * sd) / sd
    return ndtr(d1) if call else ndtr(d1) - 1
