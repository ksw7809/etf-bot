import os
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timezone, timedelta

# ================= [ 사용자 설정 ] =================
TICKER = "418660.KS"        # TIGER 미국나스닥100레버리지(합성)
NTFY_TOPIC = "ksw7809_bot"  # ntfy 앱 구독 토픽 이름

EMA_LEN = 20
ENV_PERCENT = 2.0
RSI_LEN = 14
RSI_OVERSOLD = 35
RECORD_FILE = "last_buy.txt"
# ===================================================

KST = timezone(timedelta(hours=9))
now = datetime.now(KST)
today_str = now.strftime("%Y-%m-%d")

def send_alert(msg):
    url = f"https://ntfy.sh/{NTFY_TOPIC}"
    try:
        res = requests.post(url, data=msg.encode("utf-8"), timeout=20)
        if res.status_code == 200:
            print("📩 ntfy 알림 전송 성공!")
        else:
            print(f"ntfy 응답 오류: {res.status_code}")
    except Exception as e:
        print(f"ntfy 전송 실패: {e}")

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
    if os.path.exists(RECORD_FILE):
        with open(RECORD_FILE, "r") as f:
            if f.read().strip() == today_str:
                print(f"[{now.strftime('%H:%M:%S')}] 오늘 이미 매수 신호 발송 완료 (종료)")
                return

    df_5m = yf.download(TICKER, period="60d", interval="5m", progress=False)
    if df_5m.empty:
        print("데이터를 불러오지 못했습니다.")
        return

    if isinstance(df_5m.columns, pd.MultiIndex):
        df_5m.columns = df_5m.columns.get_level_values(0)

    df_1d = yf.download(TICKER, period="5d", interval="1d", progress=False)
    if isinstance(df_1d.columns, pd.MultiIndex):
        df_1d.columns = df_1d.columns.get_level_values(0)
    prev_close = float(df_1d['Close'].iloc[-2])

    df_5m['rsi_5m'] = calc_tv_rsi(df_5m['Close'], RSI_LEN)
    current_close = float(df_5m['Close'].iloc[-1])
    current_low = float(df_5m['Low'].iloc[-1])
    current_rsi_5m = float(df_5m['rsi_5m'].iloc[-1])

    df_3h = df_5m.resample('180min').agg({
        'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'
    }).dropna()

    df_3h['ema'] = calc_tv_ema(df_3h['Close'], EMA_LEN)
    df_3h['lower_env'] = df_3h['ema'] * (1 - ENV_PERCENT / 100)
    df_3h['rsi_3h'] = calc_tv_rsi(df_3h['Close'], RSI_LEN)

    htf_lower_env = float(df_3h['lower_env'].iloc[-2])
    htf_rsi_val = float(df_3h['rsi_3h'].iloc[-2])

    cond_env = current_low <= htf_lower_env
    cond_3h_rsi = htf_rsi_val <= RSI_OVERSOLD
    cond_5m_rsi = current_rsi_5m <= RSI_OVERSOLD
    cond_neg_day = current_close < prev_close

    print(f"[{now.strftime('%H:%M:%S')}] 현재가:{current_close:,.0f} | 전일종가:{prev_close:,.0f} | "
          f"3H하단:{htf_lower_env:,.0f} | 3H_RSI:{htf_rsi_val:.1f} | 5M_RSI:{current_rsi_5m:.1f}")

    # 테스트 완료 후 아래 1줄 앞에 #을 붙이면 실전 모드로 전환됩니다
    send_alert(f"[테스트] {TICKER} 현재가:{current_close:,.0f} | 3H_RSI:{htf_rsi_val:.1f} | 5M_RSI:{current_rsi_5m:.1f}")

    if cond_env and cond_3h_rsi and cond_5m_rsi and cond_neg_day:
        with open(RECORD_FILE, "w") as f:
            f.write(today_str)
        msg = (
            f"★ BUY 매수 신호 발생 ★\n"
            f"종목: {TICKER}\n"
            f"현재가: {current_close:,.0f}원 (전일대비 하락)\n"
            f"3시간봉 엔벨로프 하단: {htf_lower_env:,.0f}원\n"
            f"3시간봉 RSI: {htf_rsi_val:.1f}\n"
            f"5분봉 RSI: {current_rsi_5m:.1f}"
        )
        print("🚨 매수 조건 충족! ntfy 알림 발송")
        send_alert(msg)

if __name__ == "__main__":
    main()
