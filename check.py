import os
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import pandas as pd
import yfinance as yf

TICKER = "418660.KS"
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
TEST_MODE = False  # 테스트 알림을 바로 받아보고 싶을 때만 True로 변경

EMA_LEN = 20
ENV_PERCENT = 2.0
RSI_LEN = 14
RSI_OVERSOLD = 35
KST = ZoneInfo("Asia/Seoul")
ALERT_FILE = "last_alert.txt"


def send_alert(msg):
    requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"), timeout=20)


def calc_tv_rsi(series, length=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_tv_ema(series, length=20):
    return series.ewm(span=length, adjust=False).mean()


def get_etf_ath_text(curr_price):
    try:
        df_max = yf.download(TICKER, period="max", interval="1wk", progress=False)
        if isinstance(df_max.columns, pd.MultiIndex):
            df_max.columns = df_max.columns.get_level_values(0)
        ath = max(float(df_max["High"].max()), curr_price)
        dd_pct = (curr_price - ath) / ath * 100
        return f"종목 역대고점 대비: {dd_pct:+.2f}% (고점 {ath:,.0f}원)"
    except Exception:
        return "종목 역대고점 대비: 조회 실패"


def get_nasdaq_futures_text():
    try:
        df_max = yf.download("NQ=F", period="max", interval="1wk", progress=False)
        df_now = yf.download("NQ=F", period="1d", interval="5m", progress=False)
        if isinstance(df_max.columns, pd.MultiIndex):
            df_max.columns = df_max.columns.get_level_values(0)
        if isinstance(df_now.columns, pd.MultiIndex):
            df_now.columns = df_now.columns.get_level_values(0)

        curr = float(df_now["Close"].dropna().iloc[-1])
        ath = max(float(df_max["High"].max()), curr)
        dd_pct = (curr - ath) / ath * 100
        return f"나스닥선물: {curr:,.2f} (역대고점 대비 {dd_pct:+.2f}%)"
    except Exception:
        return "나스닥선물: 조회 실패"


def get_usdkrw_text():
    try:
        df_1d = yf.download("KRW=X", period="5d", interval="1d", progress=False)
        df_now = yf.download("KRW=X", period="1d", interval="5m", progress=False)
        if isinstance(df_1d.columns, pd.MultiIndex):
            df_1d.columns = df_1d.columns.get_level_values(0)
        if isinstance(df_now.columns, pd.MultiIndex):
            df_now.columns = df_now.columns.get_level_values(0)

        curr = float(df_now["Close"].dropna().iloc[-1])
        prev = float(df_1d["Close"].dropna().iloc[-2])
        chg_pct = (curr - prev) / prev * 100
        return f"달러/원: {curr:,.2f}원 ({chg_pct:+.2f}%)"
    except Exception:
        return "달러/원: 조회 실패"


def get_vix_text():
    try:
        df_vix = yf.download("^VIX", period="5d", interval="1d", progress=False)
        if isinstance(df_vix.columns, pd.MultiIndex):
            df_vix.columns = df_vix.columns.get_level_values(0)
        curr = float(df_vix["Close"].dropna().iloc[-1])
        prev = float(df_vix["Close"].dropna().iloc[-2])
        chg_pct = (curr - prev) / prev * 100
        return f"공포지수(VIX): {curr:.2f} ({chg_pct:+.2f}%)"
    except Exception:
        return "공포지수(VIX): 조회 실패"


def main():
    now_kst = datetime.now(KST)
    today_kst_str = now_kst.strftime("%Y-%m-%d")

    # 1) 거래 시간 필터 (09:05 ~ 15:15)
    if not TEST_MODE:
        hhmm = now_kst.hour * 100 + now_kst.minute
        if not (905 <= hhmm <= 1515):
            print(f"[{now_kst.strftime('%H:%M')}] 거래 시간 외(09:05~15:15) -> 패스")
            return

    # 2) 하루 1회 중복 알림 방지
    if not TEST_MODE and os.path.exists(ALERT_FILE):
        with open(ALERT_FILE, "r", encoding="utf-8") as f:
            if f.read().strip() == today_kst_str:
                print(f"[{today_kst_str}] 오늘 이미 알림 발송 완료 -> 패스")
                return

    # 3) 5분봉 데이터 로드 및 한국 시간 변환
    df_5m = yf.download(TICKER, period="60d", interval="5m", progress=False)
    if df_5m.empty:
        print("5분봉 데이터 없음")
        return
    if isinstance(df_5m.columns, pd.MultiIndex):
        df_5m.columns = df_5m.columns.get_level_values(0)

    if df_5m.index.tz is None:
        df_5m.index = df_5m.index.tz_localize("UTC").tz_convert(KST)
    else:
        df_5m.index = df_5m.index.tz_convert(KST)

    df_5m["rsi_5m"] = calc_tv_rsi(df_5m["Close"], RSI_LEN)

    # 4) 기본 3시간봉 생성
    df_3h = df_5m.resample("180min").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last"
    }).dropna()

    # 5) 직전 거래일 종가 및 당일 고가/저가 구하기
    latest_ts = df_5m.index[-1]
    today_str = latest_ts.strftime("%Y-%m-%d")
    today_5m = df_5m[df_5m.index.strftime("%Y-%m-%d") == today_str]
    day_high = float(today_5m["High"].max())
    day_low = float(today_5m["Low"].min())

    df_1d = yf.download(TICKER, period="10d", interval="1d", progress=False)
    if isinstance(df_1d.columns, pd.MultiIndex):
        df_1d.columns = df_1d.columns.get_level_values(0)
    prev_days = df_1d[df_1d.index.strftime("%Y-%m-%d") < today_str]
    prev_close = float(prev_days["Close"].iloc[-1])

    # 6) 최신 5분봉 + 실시간 진행 중인 3시간봉 계산
    latest_row = df_5m.iloc[-1]
    c_close = float(latest_row["Close"])
    c_low = float(latest_row["Low"])
    c_rsi_5m = float(latest_row["rsi_5m"])

    prev_3h_closes = df_3h[df_3h.index < latest_ts.floor("180min")]["Close"]
    if len(prev_3h_closes) < RSI_LEN:
        print("3시간봉 데이터 부족")
        return

    live_3h_closes = pd.concat([
        prev_3h_closes,
        pd.Series([c_close], index=[latest_ts.floor("180min")])
    ])

    htf_ema = float(calc_tv_ema(live_3h_closes, EMA_LEN).iloc[-1])
    htf_lower_env = htf_ema * (1 - ENV_PERCENT / 100)
    htf_rsi_val = float(calc_tv_rsi(live_3h_closes, RSI_LEN).iloc[-1])

    # 7) 4가지 매수 조건 판별
    cond_env = c_low <= htf_lower_env
    cond_3h_rsi = htf_rsi_val <= RSI_OVERSOLD
    cond_5m_rsi = c_rsi_5m <= RSI_OVERSOLD
    cond_neg_day = c_close < prev_close

    is_signal = cond_env and cond_3h_rsi and cond_5m_rsi and cond_neg_day

    if is_signal or TEST_MODE:
        day_chg_pct = (c_close - prev_close) / prev_close * 100
        env_diff_pct = (c_close - htf_lower_env) / htf_lower_env * 100

        etf_ath_text = get_etf_ath_text(c_close)
        nq_text = get_nasdaq_futures_text()
        fx_text = get_usdkrw_text()
        vix_text = get_vix_text()

        prefix = "[테스트] " if TEST_MODE and not is_signal else ""
        msg = (
            f"{prefix}[매수 신호 | 418660] ({latest_ts.strftime('%H:%M')})\n"
            f"현재가: {c_close:,.0f}원 ({day_chg_pct:+.2f}%)\n"
            f"당일 고/저: {day_high:,.0f}원 / {day_low:,.0f}원\n"
            f"전일종가: {prev_close:,.0f}원\n"
            f"엔벨로프 하단: {htf_lower_env:,.0f}원 (대비 {env_diff_pct:+.2f}%)\n"
            f"RSI: 3H {htf_rsi_val:.1f} / 5M {c_rsi_5m:.1f}\n"
            f"{etf_ath_text}\n"
            f"{nq_text}\n"
            f"{fx_text}\n"
            f"{vix_text}"
        )
        print(msg)
        send_alert(msg)

        if not TEST_MODE:
            with open(ALERT_FILE, "w", encoding="utf-8") as f:
                f.write(today_kst_str)
    else:
        print(
            f"[{latest_ts.strftime('%H:%M')}] 조건 미충족 | "
            f"현재가:{c_close:,.0f} | 엔벨로프하단:{htf_lower_env:,.0f} | "
            f"3H_RSI:{htf_rsi_val:.1f} | 5M_RSI:{c_rsi_5m:.1f}"
        )


if __name__ == "__main__":
    main()
