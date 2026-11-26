import os
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import pandas as pd
import yfinance as yf

TICKER = "418660.KS"
NAVER_CODE = "418660"
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
TEST_MODE = False  # 테스트 알림을 바로 받아보고 싶을 때만 True로 변경

# 알림 터치 시 열릴 차트 URL (트레이딩뷰를 원하시면 아래 줄 주석을 바꾸세요)
CHART_URL = f"https://m.stock.naver.com/domestic/stock/{NAVER_CODE}/total"
# CHART_URL = f"https://kr.tradingview.com/chart/?symbol=KRX:{NAVER_CODE}"

EMA_LEN = 20
ENV_PERCENT = 2.0
RSI_LEN = 14
RSI_OVERSOLD = 35   # 1차 매수 기준 (3H & 5M RSI)
RSI_EXTREME = 25    # 2차 극과매도 기준 (3H RSI)
BRIEF_HOURS = {9, 10, 11, 12, 13, 14}  # 정각 점검 시간대 (9시 장시작, 10~14시 정각 점검)
HOLIDAY_HOURS = {9, 12, 15}            # 휴장일 점검 시간대 (총 3회)
KST = ZoneInfo("Asia/Seoul")
ALERT_FILE = "last_alert.txt"


def fmt_pct(pct):
    """등락률에 따라 🔴(상승) / 🔵(하락) / ⚪(보합) 기호 추가"""
    if pct > 0:
        dot = "🔴"
    elif pct < 0:
        dot = "🔵"
    else:
        dot = "⚪"
    return f"{pct:+.2f}% {dot}"


def send_alert(title, msg):
    """한글 Title 헤더가 깨지지 않도록 JSON 방식으로 ntfy 전송 (모든 알림 첫 줄 아래 한 칸 띄움)"""
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": f"\u200b\n{msg}",
        "click": CHART_URL,
    }
    requests.post(
        "https://ntfy.sh",
        json=payload,
        timeout=20,
    )


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


def load_today_state(today_str):
    """오늘의 (매수알림 횟수, 마지막 브리핑 시간) 반환"""
    if not os.path.exists(ALERT_FILE):
        return 0, -1
    with open(ALERT_FILE, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if not content:
        return 0, -1
    parts = content.split(",")
    if parts[0] != today_str:
        return 0, -1
    alert_count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    last_brief_hour = int(parts[2]) if len(parts) > 2 and parts[2].lstrip("-").isdigit() else -1
    return alert_count, last_brief_hour


def save_today_state(today_str, alert_count, last_brief_hour):
    with open(ALERT_FILE, "w", encoding="utf-8") as f:
        f.write(f"{today_str},{alert_count},{last_brief_hour}")


def get_naver_realtime(code=NAVER_CODE):
    """네이버 금융 실시간 API에서 현재가, 당일 고/저가, 전일종가, 장상태(ms) 조회"""
    try:
        url = f"https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{code}"
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        data = res.json()["result"]["areas"][0]["datas"][0]

        curr = float(data["nv"])
        high = float(data["hv"]) if data.get("hv", 0) > 0 else curr
        low = float(data["lv"]) if data.get("lv", 0) > 0 else curr
        prev_close = float(data["pcv"])
        market_status = data.get("ms", "")  # OPEN: 개장, CLOSE: 휴장/마감

        if curr > 0 and prev_close > 0:
            return curr, high, low, prev_close, market_status
    except Exception as e:
        print(f"네이버 실시간 조회 실패 (야후 데이터로 대체): {e}")
    return None


def get_etf_ath_text(curr_price):
    try:
        df_max = yf.download(TICKER, period="max", interval="1wk", progress=False)
        if isinstance(df_max.columns, pd.MultiIndex):
            df_max.columns = df_max.columns.get_level_values(0)
        ath = max(float(df_max["High"].max()), curr_price)
        dd_pct = (curr_price - ath) / ath * 100
        return f"종목 고점 대비: {dd_pct:+.2f}%\n(고점 {ath:,.0f}원)"
    except Exception:
        return "종목 고점 대비: 조회 실패"


def get_nasdaq_futures_text():
    try:
        df_max = yf.download("NQ=F", period="max", interval="1wk", progress=False)
        df_1d = yf.download("NQ=F", period="5d", interval="1d", progress=False)
        df_now = yf.download("NQ=F", period="1d", interval="5m", progress=False)
        if isinstance(df_max.columns, pd.MultiIndex):
            df_max.columns = df_max.columns.get_level_values(0)
        if isinstance(df_1d.columns, pd.MultiIndex):
            df_1d.columns = df_1d.columns.get_level_values(0)
        if isinstance(df_now.columns, pd.MultiIndex):
            df_now.columns = df_now.columns.get_level_values(0)

        curr = float(df_now["Close"].dropna().iloc[-1])
        prev = float(df_1d["Close"].dropna().iloc[-2])
        chg_pct = (curr - prev) / prev * 100
        ath = max(float(df_max["High"].max()), curr)
        dd_pct = (curr - ath) / ath * 100
        return f"나스닥100지수: {curr:,.2f} ({fmt_pct(chg_pct)})\n(역대고점 대비 {dd_pct:+.2f}%)"
    except Exception:
        return "나스닥100지수: 조회 실패"


def get_sp500_futures_text():
    try:
        df_max = yf.download("ES=F", period="max", interval="1wk", progress=False)
        df_1d = yf.download("ES=F", period="5d", interval="1d", progress=False)
        df_now = yf.download("ES=F", period="1d", interval="5m", progress=False)
        if isinstance(df_max.columns, pd.MultiIndex):
            df_max.columns = df_max.columns.get_level_values(0)
        if isinstance(df_1d.columns, pd.MultiIndex):
            df_1d.columns = df_1d.columns.get_level_values(0)
        if isinstance(df_now.columns, pd.MultiIndex):
            df_now.columns = df_now.columns.get_level_values(0)

        curr = float(df_now["Close"].dropna().iloc[-1])
        prev = float(df_1d["Close"].dropna().iloc[-2])
        chg_pct = (curr - prev) / prev * 100
        ath = max(float(df_max["High"].max()), curr)
        dd_pct = (curr - ath) / ath * 100
        return f"S&P500지수: {curr:,.2f} ({fmt_pct(chg_pct)})\n(역대고점 대비 {dd_pct:+.2f}%)"
    except Exception:
        return "S&P500지수: 조회 실패"


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
        return f"달러/원: {curr:,.2f}원 ({fmt_pct(chg_pct)})"
    except Exception:
        return "달러/원: 조회 실패"


def get_jpykrw_text():
    try:
        df_1d = yf.download("JPYKRW=X", period="5d", interval="1d", progress=False)
        df_now = yf.download("JPYKRW=X", period="1d", interval="5m", progress=False)
        if isinstance(df_1d.columns, pd.MultiIndex):
            df_1d.columns = df_1d.columns.get_level_values(0)
        if isinstance(df_now.columns, pd.MultiIndex):
            df_now.columns = df_now.columns.get_level_values(0)

        curr = float(df_now["Close"].dropna().iloc[-1]) * 100
        prev = float(df_1d["Close"].dropna().iloc[-2]) * 100
        chg_pct = (curr - prev) / prev * 100
        return f"엔/원(100엔): {curr:,.2f}원 ({fmt_pct(chg_pct)})"
    except Exception:
        return "엔/원(100엔): 조회 실패"


def get_dxy_text():
    try:
        df_1d = yf.download("DX-Y.NYB", period="5d", interval="1d", progress=False)
        df_now = yf.download("DX-Y.NYB", period="1d", interval="5m", progress=False)
        if isinstance(df_1d.columns, pd.MultiIndex):
            df_1d.columns = df_1d.columns.get_level_values(0)
        if isinstance(df_now.columns, pd.MultiIndex):
            df_now.columns = df_now.columns.get_level_values(0)

        curr = float(df_now["Close"].dropna().iloc[-1])
        prev = float(df_1d["Close"].dropna().iloc[-2])
        chg_pct = (curr - prev) / prev * 100
        return f"달러인덱스(DXY): {curr:,.2f} ({fmt_pct(chg_pct)})"
    except Exception:
        return "달러인덱스(DXY): 조회 실패"


def get_vix_text():
    try:
        df_vix = yf.download("^VIX", period="5d", interval="1d", progress=False)
        if isinstance(df_vix.columns, pd.MultiIndex):
            df_vix.columns = df_vix.columns.get_level_values(0)
        curr = float(df_vix["Close"].dropna().iloc[-1])
        prev = float(df_vix["Close"].dropna().iloc[-2])
        chg_pct = (curr - prev) / prev * 100
        return f"공포지수(VIX): {curr:.2f} ({fmt_pct(chg_pct)})"
    except Exception:
        return "공포지수(VIX): 조회 실패"


def get_fear_and_greed_text():
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://edition.cnn.com/",
    }
    try:
        res = requests.get(url, headers=headers, timeout=5)
        res.raise_for_status()
        data = res.json()["fear_and_greed"]
        score = round(data["score"])

        if score <= 24:
            label = "극단적 공포 / Extreme Fear"
        elif score <= 44:
            label = "공포 / Fear"
        elif score <= 54:
            label = "중립 / Neutral"
        elif score <= 74:
            label = "탐욕 / Greed"
        else:
            label = "극단적 탐욕 / Extreme Greed"

        return f"공포·탐욕지수: {score}점\n({label})"
    except Exception:
        return "공포·탐욕지수: 조회 실패"


def build_market_report(
    c_close,
    day_chg_pct,
    day_high=None,
    day_low=None,
    prev_close=None,
    htf_lower_env=None,
    env_diff_pct=None,
    htf_rsi_val=None,
    c_rsi_5m=None,
    include_tech=True,
):
    etf_ath_text = get_etf_ath_text(c_close)
    nq_text = get_nasdaq_futures_text()
    sp500_text = get_sp500_futures_text()
    fx_text = get_usdkrw_text()
    jpy_text = get_jpykrw_text()
    dxy_text = get_dxy_text()
    vix_text = get_vix_text()
    fng_text = get_fear_and_greed_text()

    if include_tech:
        top_section = (
            f"현재가: {c_close:,.0f}원 ({fmt_pct(day_chg_pct)})\n"
            f"당일 고/저: {day_high:,.0f}원 / {day_low:,.0f}원\n"
            f"전일종가: {prev_close:,.0f}원\n"
            f"──────────────\n"
            f"엔벨로프 하단: {htf_lower_env:,.0f}원 (대비 {env_diff_pct:+.2f}%)\n"
            f"RSI: 3H {htf_rsi_val:.1f} / 5M {c_rsi_5m:.1f}\n"
        )
    else:
        top_section = f"현재가: {c_close:,.0f}원 ({fmt_pct(day_chg_pct)})\n"

    return (
        f"──────────────\n"
        f"{top_section}"
        f"──────────────\n"
        f"{etf_ath_text}\n"
        f"──────────────\n"
        f"{nq_text}\n\n"
        f"{sp500_text}\n\n"
        f"{vix_text}\n\n"
        f"{fng_text}\n"
        f"──────────────\n"
        f"{fx_text}\n"
        f"{jpy_text}\n"
        f"{dxy_text}\n"
        f"──────────────"
    )


def send_holiday_check(now_kst, today_kst_str, alert_count, note=""):
    suffix = f" ({note})" if note else ""
    fng_text = get_fear_and_greed_text()
    title = f"[휴장일 점검 | 418660] ({now_kst.strftime('%H:%M')})"
    msg = (
        f"오늘은 증시 휴장일입니다. 봇 서버 정상 작동 중{suffix}\n"
        f"{fng_text}"
    )
    print(f"{title}\n\n{msg}")
    send_alert(title, msg)
    save_today_state(today_kst_str, alert_count, now_kst.hour)


def main():
    now_kst = datetime.now(KST)
    today_kst_str = now_kst.strftime("%Y-%m-%d")
    hhmm = now_kst.hour * 100 + now_kst.minute

    # 1) 전체 실행 시간 필터 (09:00 ~ 15:40)
    if not TEST_MODE:
        if not (900 <= hhmm <= 1540):
            print(f"[{now_kst.strftime('%H:%M')}] 거래 시간 외(09:00~15:40) -> 패스")
            return

    # 2) 당일 상태 로드 (매수 알림 횟수, 마지막 브리핑 시각)
    if TEST_MODE:
        alert_count, last_brief_hour = 0, -1
    else:
        alert_count, last_brief_hour = load_today_state(today_kst_str)

    is_brief_time = (
        (now_kst.hour in BRIEF_HOURS)
        and (now_kst.minute < 10)
        and (last_brief_hour != now_kst.hour)
    )
    is_close_time = (
        (now_kst.hour == 15)
        and (30 <= now_kst.minute < 40)
        and (last_brief_hour != 15)
    )
    is_signal_time = (905 <= hhmm <= 1515) or TEST_MODE

    if not TEST_MODE and (not is_signal_time or alert_count >= 2) and not is_brief_time and not is_close_time:
        print(f"[{today_kst_str}] 시세 감시 대기/완료 & 브리핑 시간 아님 -> 패스")
        return

    # 3) 네이버 실시간 시세 및 휴장일 여부 먼저 확인
    naver_data = get_naver_realtime(NAVER_CODE)
    if naver_data is not None:
        c_close, day_high, day_low, prev_close, market_status = naver_data
        # 15:30 장마감 시간대에는 장 종료(CLOSE) 상태이므로 아래에서 당일 거래 유무 확인 후 발송
        if not TEST_MODE and market_status != "OPEN" and not is_close_time:
            if now_kst.hour in HOLIDAY_HOURS and now_kst.minute < 10 and last_brief_hour != now_kst.hour:
                send_holiday_check(now_kst, today_kst_str, alert_count)
            else:
                print(f"[{now_kst.strftime('%H:%M')}] 휴장일 또는 장 마감 상태({market_status}) -> 패스")
            return

    # 4) 야후 파이낸스 과거 5분봉 데이터 로드 및 한국 시간 변환
    df_5m = yf.download(TICKER, period="60d", interval="5m", progress=False)
    if df_5m.empty:
        raise RuntimeError("야후 파이낸스 5분봉 데이터 조회 실패 (빈 데이터)")
    if isinstance(df_5m.columns, pd.MultiIndex):
        df_5m.columns = df_5m.columns.get_level_values(0)

    if df_5m.index.tz is None:
        df_5m.index = df_5m.index.tz_localize("UTC").tz_convert(KST)
    else:
        df_5m.index = df_5m.index.tz_convert(KST)

    # 15:30 장마감 시 휴장일 여부 최종 확인 (오늘 거래 데이터가 없으면 휴장일 -> 패스)
    if not TEST_MODE and is_close_time:
        latest_trade_date = df_5m.index[-1].strftime("%Y-%m-%d")
        if latest_trade_date != today_kst_str:
            print(f"[{now_kst.strftime('%H:%M')}] 오늘({today_kst_str}) 거래 데이터 없음(휴장일) -> 15:30 장마감 패스")
            return

    # 5) 기본 3시간봉 생성
    df_3h = df_5m.resample("180min").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last"
    }).dropna()

    # 6) 네이버 실시간 시세 결합 (실패 시 야후 데이터로 자동 대체)
    data_status = "정상"

    if naver_data is not None:
        c_low = c_close
        latest_ts = now_kst

        live_5m_closes = pd.concat([
            df_5m["Close"],
            pd.Series([c_close], index=[now_kst])
        ])
        c_rsi_5m = float(calc_tv_rsi(live_5m_closes, RSI_LEN).iloc[-1])

        curr_3h_bucket = pd.Timestamp(now_kst).floor("180min")
        prev_3h_closes = df_3h[df_3h.index < curr_3h_bucket]["Close"]
    else:
        data_status = "야후 지연"
        latest_ts = df_5m.index[-1]
        today_str = latest_ts.strftime("%Y-%m-%d")

        # 야후 백업 시에도 오늘 거래 데이터가 없으면 휴장일로 판단 (9, 12, 15시 점검 발송)
        if not TEST_MODE and today_str != today_kst_str:
            if now_kst.hour in HOLIDAY_HOURS and now_kst.minute < 10 and last_brief_hour != now_kst.hour:
                send_holiday_check(now_kst, today_kst_str, alert_count, note="네이버 지연")
            else:
                print(f"[{now_kst.strftime('%H:%M')}] 오늘({today_kst_str}) 거래 데이터 없음(휴장일) -> 패스")
            return

        today_5m = df_5m[df_5m.index.strftime("%Y-%m-%d") == today_str]
        day_high = float(today_5m["High"].max())
        day_low = float(today_5m["Low"].min())

        df_1d = yf.download(TICKER, period="10d", interval="1d", progress=False)
        if isinstance(df_1d.columns, pd.MultiIndex):
            df_1d.columns = df_1d.columns.get_level_values(0)
        prev_days = df_1d[df_1d.index.strftime("%Y-%m-%d") < today_str]
        prev_close = float(prev_days["Close"].iloc[-1])

        latest_row = df_5m.iloc[-1]
        c_close = float(latest_row["Close"])
        c_low = float(latest_row["Low"])
        c_rsi_5m = float(calc_tv_rsi(df_5m["Close"], RSI_LEN).iloc[-1])

        curr_3h_bucket = latest_ts.floor("180min")
        prev_3h_closes = df_3h[df_3h.index < curr_3h_bucket]["Close"]

    if len(prev_3h_closes) < RSI_LEN:
        raise RuntimeError("3시간봉 데이터 부족")

    live_3h_closes = pd.concat([
        prev_3h_closes,
        pd.Series([c_close], index=[curr_3h_bucket])
    ])

    htf_ema = float(calc_tv_ema(live_3h_closes, EMA_LEN).iloc[-1])
    htf_lower_env = htf_ema * (1 - ENV_PERCENT / 100)
    htf_rsi_val = float(calc_tv_rsi(live_3h_closes, RSI_LEN).iloc[-1])

    day_chg_pct = (c_close - prev_close) / prev_close * 100
    env_diff_pct = (c_close - htf_lower_env) / htf_lower_env * 100

    # 7) 1차 일반 매수 / 2차 극과매도 조건 판별 (09:05 ~ 15:15 적용)
    cond_env = c_low <= htf_lower_env
    cond_3h_rsi = htf_rsi_val <= RSI_OVERSOLD
    cond_5m_rsi = c_rsi_5m <= RSI_OVERSOLD
    cond_neg_day = c_close < prev_close

    is_first_signal = is_signal_time and (alert_count == 0) and cond_env and cond_3h_rsi and cond_5m_rsi and cond_neg_day
    is_second_signal = is_signal_time and (alert_count == 1) and (htf_rsi_val <= RSI_EXTREME) and cond_neg_day

    new_alert_count = alert_count
    new_brief_hour = last_brief_hour

    if is_first_signal or is_second_signal or TEST_MODE:
        if TEST_MODE and not (is_first_signal or is_second_signal):
            title = f"[테스트 | 418660] ({latest_ts.strftime('%H:%M')})"
        elif is_second_signal or (is_first_signal and htf_rsi_val <= RSI_EXTREME):
            title = f"[2차 극과매도 | 418660] ({latest_ts.strftime('%H:%M')})"
            new_alert_count = 2
        else:
            title = f"[매수 신호 | 418660] ({latest_ts.strftime('%H:%M')})"
            new_alert_count = 1

        msg = build_market_report(
            c_close=c_close,
            day_chg_pct=day_chg_pct,
            day_high=day_high,
            day_low=day_low,
            prev_close=prev_close,
            htf_lower_env=htf_lower_env,
            env_diff_pct=env_diff_pct,
            htf_rsi_val=htf_rsi_val,
            c_rsi_5m=c_rsi_5m,
            include_tech=True,
        )
        print(f"{title}\n\n{msg}")
        send_alert(title, msg)

        if is_brief_time:
            new_brief_hour = now_kst.hour

    elif is_brief_time or is_close_time:
        # 8) 9시 장시작(미국장·환율 브리핑) / 10~14시 정각 점검 / 15:30 장마감
        if now_kst.hour == 9:
            title = f"[장시작 | 418660] ({latest_ts.strftime('%H:%M')})"
            msg = build_market_report(
                c_close=c_close,
                day_chg_pct=day_chg_pct,
                include_tech=False,
            )
        else:
            brief_label = "장마감" if is_close_time else "정각 점검"
            fng_text = get_fear_and_greed_text()
            title = f"[{brief_label} | 418660] ({latest_ts.strftime('%H:%M')})"
            msg = (
                f"현재가: {c_close:,.0f}원 ({fmt_pct(day_chg_pct)}) ({data_status})\n"
                f"{fng_text}"
            )
        print(f"{title}\n\n{msg}")
        send_alert(title, msg)
        new_brief_hour = now_kst.hour

    else:
        stage = "2차 대기중" if alert_count == 1 else "조건 미충족"
        print(
            f"[{latest_ts.strftime('%H:%M')}] {stage} | "
            f"현재가:{c_close:,.0f} | 엔벨로프하단:{htf_lower_env:,.0f} | "
            f"3H_RSI:{htf_rsi_val:.1f} | 5M_RSI:{c_rsi_5m:.1f}"
        )

    if not TEST_MODE and (new_alert_count != alert_count or new_brief_hour != last_brief_hour):
        save_today_state(today_kst_str, new_alert_count, new_brief_hour)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        err_time = datetime.now(KST).strftime("%H:%M")
        err_title = f"[오류 점검 필요 | 418660] ({err_time})"
        err_msg = f"에러 내용: {e}"
        print(f"{err_title}\n\n{err_msg}")
        try:
            send_alert(err_title, err_msg)
        except Exception:
            pass
        raise
