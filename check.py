import os
import requests
import pandas as pd
import yfinance as yf
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# ================= [ 사용자 설정 ] =================
TEST_MODE = True            # 테스트 끝나면 반드시 False로 변경하세요!

TICKER = "418660.KS"        # 야후 파이낸스 종목코드 (3시간봉용)
NAVER_CODE = "418660"       # 네이버 증권 종목코드 (실시간 5분봉/현재가용)
NTFY_TOPIC = "ksw7809_bot"  # ntfy 앱 구독 토픽 이름

EMA_LEN = 20
ENV_PERCENT = 2.0
RSI_LEN = 14
RSI_OVERSOLD = 35

RECORD_FILE = "last_buy.txt"
STATUS_FILE = "last_status.txt"
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

def get_naver_realtime(code):
    headers = {"User-Agent": "Mozilla/5.0"}

    # 1) 네이버 실시간 현재가 및 전일 종가 조회
    rt_url = f"https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{code}"
    rt_res = requests.get(rt_url, headers=headers, timeout=10).json()
    item_data = rt_res["result"]["areas"][0]["datas"][0]
    current_close = float(item_data["nv"])
    prev_close = float(item_data["sv"])

    # 2) 네이버 실시간 1분봉(최근 1,500개) -> 5분봉 변환
    chart_url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=minute&count=1500&requestType=0"
    chart_res = requests.get(chart_url, headers=headers, timeout=10)
    root = ET.fromstring(chart_res.text)

    rows = []
    for item in root.findall(".//item"):
        parts = item.attrib["data"].split("|")
        if len(parts) < 5 or "null" in parts[1:5]:
            continue
        rows.append({
            "Datetime": pd.to_datetime(parts[0], format="%Y%m%d%H%M"),
            "Open": float(parts[1]),
            "High": float(parts[2]),
            "Low": float(parts[3]),
            "Close": float(parts[4]),
        })

    df_1m = pd.DataFrame(rows).set_index("Datetime")
    df_5m_rt = df_1m.resample("5min").agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last"
    }).dropna()

    # 최신 5분봉의 종가/저가를 실시간 현재가와 동기화
    df_5m_rt.iloc[-1, df_5m_rt.columns.get_loc("Close")] = current_close
    latest_low = min(float(df_5m_rt["Low"].iloc[-1]), current_close)
    df_5m_rt.iloc[-1, df_5m_rt.columns.get_loc("Low")] = latest_low

    return df_5m_rt, current_close, latest_low, prev_close

def main():
    cur_mins = now.hour * 60 + now.minute

    if not TEST_MODE:
        # 주말 및 장 운영시간(09:00 ~ 15:35) 외 실행 방지
        if now.weekday() >= 5:
            print("주말 휴장일입니다.")
            return
        if cur_mins < 9 * 60 or cur_mins > 15 * 60 + 35:
            print(f"[{now.strftime('%H:%M:%S')}] 장 운영시간 외 종료")
            return

    # 1) 야후 파이낸스 데이터로 직전 마감 3시간봉 지표 계산
    df_yf = yf.download(TICKER, period="60d", interval="5m", progress=False)
    if df_yf.empty:
        print("야후 3시간봉 데이터를 불러오지 못했습니다.")
        return

    if isinstance(df_yf.columns, pd.MultiIndex):
        df_yf.columns = df_yf.columns.get_level_values(0)

    if df_yf.index.tz is None:
        df_yf.index = df_yf.index.tz_localize("UTC").tz_convert("Asia/Seoul")
    else:
        df_yf.index = df_yf.index.tz_convert("Asia/Seoul")

    df_3h = df_yf.resample("180min").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last"
    }).dropna()

    df_3h["ema"] = calc_tv_ema(df_3h["Close"], EMA_LEN)
    df_3h["lower_env"] = df_3h["ema"] * (1 - ENV_PERCENT / 100)
    df_3h["rsi_3h"] = calc_tv_rsi(df_3h["Close"], RSI_LEN)

    htf_lower_env = float(df_3h["lower_env"].iloc[-2])
    htf_rsi_val = float(df_3h["rsi_3h"].iloc[-2])

    # 2) 네이버 증권 실시간 데이터로 5분봉 RSI 및 현재가/저가/전일종가 계산
    try:
        df_5m_rt, current_close, current_low, prev_close = get_naver_realtime(NAVER_CODE)
        df_5m_rt["rsi_5m"] = calc_tv_rsi(df_5m_rt["Close"], RSI_LEN)
        current_rsi_5m = float(df_5m_rt["rsi_5m"].iloc[-1])
        source_tag = "네이버 실시간"
    except Exception as e:
        print(f"네이버 실시간 조회 실패, 야후 데이터로 대체: {e}")
        df_yf["rsi_5m"] = calc_tv_rsi(df_yf["Close"], RSI_LEN)
        current_close = float(df_yf["Close"].iloc[-1])
        current_low = float(df_yf["Low"].iloc[-1])
        current_rsi_5m = float(df_yf["rsi_5m"].iloc[-1])
        df_1d = yf.download(TICKER, period="5d", interval="1d", progress=False)
        if isinstance(df_1d.columns, pd.MultiIndex):
            df_1d.columns = df_1d.columns.get_level_values(0)
        prev_close = float(df_1d["Close"].iloc[-2])
        source_tag = "야후 대체"

    print(f"[{now.strftime('%H:%M:%S')} | {source_tag}] 현재가:{current_close:,.0f} | 전일종가:{prev_close:,.0f} | "
          f"3H하단:{htf_lower_env:,.0f} | 3H_RSI:{htf_rsi_val:.1f} | 5M_RSI:{current_rsi_5m:.1f}")

    # 테스트 모드일 경우 즉시 테스트 알림 발송 후 종료
    if TEST_MODE:
        send_alert(
            f"[테스트 성공 | {source_tag}]\n"
            f"현재가: {current_close:,.0f}원 (전일: {prev_close:,.0f}원)\n"
            f"3H하단: {htf_lower_env:,.0f}원 | 3H_RSI: {htf_rsi_val:.1f} | 5M_RSI: {current_rsi_5m:.1f}"
        )
        return

    # 3) 장 시작(09:00~09:05) / 장 마감(15:30~15:35) 생존 신고 알림 (각 1회)
    last_status = ""
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE, "r") as f:
            last_status = f.read().strip()

    if 9 * 60 <= cur_mins <= 9 * 60 + 5 and last_status != f"{today_str}_OPEN":
        with open(STATUS_FILE, "w") as f:
            f.write(f"{today_str}_OPEN")
        send_alert(
            f"[장 시작] 봇 정상 가동 중\n"
            f"현재가: {current_close:,.0f}원 | 3H_RSI: {htf_rsi_val:.1f} | 5M_RSI: {current_rsi_5m:.1f}"
        )
    elif 15 * 60 + 30 <= cur_mins <= 15 * 60 + 35 and last_status != f"{today_str}_CLOSE":
        with open(STATUS_FILE, "w") as f:
            f.write(f"{today_str}_CLOSE")
        send_alert(
            f"[장 마감] 오늘 감시 종료\n"
            f"종가: {current_close:,.0f}원 | 3H_RSI: {htf_rsi_val:.1f} | 5M_RSI: {current_rsi_5m:.1f}"
        )

    # 4) 오늘 이미 매수 알림을 보냈는지 확인
    if os.path.exists(RECORD_FILE):
        with open(RECORD_FILE, "r") as f:
            if f.read().strip() == today_str:
                print(f"[{now.strftime('%H:%M:%S')}] 오늘 이미 매수 신호 발송 완료")
                return

    # 5) 4대 핵심 매수 조건 체크
    cond_env = current_low <= htf_lower_env
    cond_3h_rsi = htf_rsi_val <= RSI_OVERSOLD
    cond_5m_rsi = current_rsi_5m <= RSI_OVERSOLD
    cond_neg_day = current_close < prev_close

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
