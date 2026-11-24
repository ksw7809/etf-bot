import requests
import pandas as pd
import yfinance as yf

TICKER = "418660.KS"
NTFY_TOPIC = "ksw7809_bot"
TARGET_DATE = "2026-10-20"  # 테스트할 날짜로 변경 가능

EMA_LEN = 20
ENV_PERCENT = 2.0
RSI_LEN = 14
RSI_OVERSOLD = 35

def send_alert(msg):
    requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"), timeout=20)

def calc_tv_rsi(series, length=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_tv_ema(series, length=20):
    return series.ewm(span=length, adjust=False).mean()

def main():
    # 1) 5분봉 및 기본 3시간봉 생성
    df_5m = yf.download(TICKER, period="60d", interval="5m", progress=False)
    if isinstance(df_5m.columns, pd.MultiIndex):
        df_5m.columns = df_5m.columns.get_level_values(0)

    if df_5m.index.tz is None:
        df_5m.index = df_5m.index.tz_localize("UTC").tz_convert("Asia/Seoul")
    else:
        df_5m.index = df_5m.index.tz_convert("Asia/Seoul")

    df_5m["rsi_5m"] = calc_tv_rsi(df_5m["Close"], RSI_LEN)

    df_3h = df_5m.resample("180min").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last"
    }).dropna()

    # 2) 대상 날짜 직전 거래일 종가 구하기
    df_1d = yf.download(TICKER, period="60d", interval="1d", progress=False)
    if isinstance(df_1d.columns, pd.MultiIndex):
        df_1d.columns = df_1d.columns.get_level_values(0)
    prev_days = df_1d[df_1d.index.strftime("%Y-%m-%d") < TARGET_DATE]
    prev_close = float(prev_days["Close"].iloc[-1])

    # 3) 대상 날짜 5분봉 전체 리플레이 (실시간 진행 3시간봉 반영)
    day_5m = df_5m[df_5m.index.strftime("%Y-%m-%d") == TARGET_DATE]
    if day_5m.empty:
        msg = f"[{TARGET_DATE} 실시간3H 백테스트] 해당 날짜의 5분봉 데이터가 없습니다."
        print(msg)
        send_alert(msg)
        return

    hits = []
    for ts, row in day_5m.iterrows():
        c_close = float(row["Close"])
        c_low = float(row["Low"])
        c_rsi_5m = float(row["rsi_5m"])

        # 직전 마감 3시간봉들 + 현재 5분봉 종가를 '진행 중인 3시간봉'으로 합치기
        prev_3h_closes = df_3h[df_3h.index < ts.floor("180min")]["Close"]
        if len(prev_3h_closes) < RSI_LEN:
            continue

        live_3h_closes = pd.concat([
            prev_3h_closes,
            pd.Series([c_close], index=[ts.floor("180min")])
        ])

        htf_ema = float(calc_tv_ema(live_3h_closes, EMA_LEN).iloc[-1])
        htf_lower_env = htf_ema * (1 - ENV_PERCENT / 100)
        htf_rsi_val = float(calc_tv_rsi(live_3h_closes, RSI_LEN).iloc[-1])

        cond_env = c_low <= htf_lower_env
        cond_3h_rsi = htf_rsi_val <= RSI_OVERSOLD
        cond_5m_rsi = c_rsi_5m <= RSI_OVERSOLD
        cond_neg_day = c_close < prev_close

        if cond_env and cond_3h_rsi and cond_5m_rsi and cond_neg_day:
            hits.append(
                f"{ts.strftime('%H:%M')} | 저가:{c_low:,.0f} (실시간3H하단:{htf_lower_env:,.0f}) | "
                f"실시간3H_RSI:{htf_rsi_val:.1f} | 5M_RSI:{c_rsi_5m:.1f}"
            )

    if hits:
        msg = f"[{TARGET_DATE} 실시간3H 성공] 총 {len(hits)}회 포착\n첫 신호: {hits[0]}"
        print("\n".join(hits))
    else:
        min_rsi_row = day_5m.loc[day_5m["rsi_5m"].idxmin()]
        msg = (
            f"[{TARGET_DATE} 실시간3H] 조건 충족 없음\n"
            f"전일종가:{prev_close:,.0f} | 당일최저 5M_RSI:{min_rsi_row['rsi_5m']:.1f}"
        )
        print(msg)

    send_alert(msg)

if __name__ == "__main__":
    main()
