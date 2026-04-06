import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# ==========================================
# 봇 설정 (본인의 정보로 수정해주세요)
# ==========================================
TELEGRAM_TOKEN = "TELEGRAM_TOKEN"
CHAT_ID = "CHAT_ID"

# 기준 시간대 (매일 08:30 알림 기준)
TIMEZONE = ZoneInfo("Asia/Seoul")
DAILY_REPORT_HOUR = 8
DAILY_REPORT_MINUTE = 30

# 검사할 타임프레임 (바이비트 API 기준 값 : 표시할 이름)
INTERVALS = {
    "W": "주봉",
    "240": "4시간봉",
    "360": "6시간봉",
}

# 중복 알림 방지용 메모리
alerted_candles = set()
last_daily_report_date = None


def send_telegram_message(text: str) -> None:
    """텔레그램 메시지 전송 함수"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"텔레그램 전송 오류: {e}")


def get_all_usdt_symbols() -> list[str]:
    """바이비트 USDT 무기한 선물 전 종목 가져오기"""
    url = "https://api.bybit.com/v5/market/tickers?category=linear"
    response = requests.get(url, timeout=10).json()
    return [
        item["symbol"]
        for item in response.get("result", {}).get("list", [])
        if item["symbol"].endswith("USDT")
    ]


def get_top_15_gainers() -> list[dict]:
    """24시간 등락률 기준 상위 15개 종목 조회"""
    url = "https://api.bybit.com/v5/market/tickers?category=linear"
    response = requests.get(url, timeout=10).json()
    tickers = response.get("result", {}).get("list", [])

    gainers = []
    for ticker in tickers:
        symbol = ticker.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue

        try:
            change_pct = float(ticker.get("price24hPcnt", "0")) * 100
            last_price = float(ticker.get("lastPrice", "0"))
            gainers.append(
                {
                    "symbol": symbol,
                    "change_pct": change_pct,
                    "last_price": last_price,
                }
            )
        except (TypeError, ValueError):
            continue

    gainers.sort(key=lambda x: x["change_pct"], reverse=True)
    return gainers[:15]


def send_daily_top_gainers_report(now: datetime) -> None:
    """매일 아침 08:30 상위 상승 15개 종목 알림"""
    top_15 = get_top_15_gainers()
    if not top_15:
        send_telegram_message("⚠️ 오늘 아침 상승률 상위 종목 데이터를 가져오지 못했습니다.")
        return

    lines = []
    for idx, item in enumerate(top_15, start=1):
        symbol = item["symbol"]
        price = item["last_price"]
        change_pct = item["change_pct"]
        trade_url = f"https://www.bybit.com/trade/usdt/{symbol}"
        lines.append(
            f"{idx:02d}. <b>{symbol}</b> | {change_pct:+.2f}% | 현재가 {price:.6g} USDT\n"
            f"🔗 <a href='{trade_url}'>거래화면</a>"
        )

    report_title = (
        f"📈 <b>일일 상승률 TOP 15</b>\n"
        f"🕣 기준시각: {now.strftime('%Y-%m-%d %H:%M %Z')}\n\n"
    )
    send_telegram_message(report_title + "\n\n".join(lines))


def should_send_daily_report(now: datetime) -> bool:
    """매일 08:30 알림 1회만 전송하도록 제어"""
    global last_daily_report_date

    if now.hour == DAILY_REPORT_HOUR and now.minute == DAILY_REPORT_MINUTE:
        if last_daily_report_date != now.date():
            last_daily_report_date = now.date()
            return True
    return False


def check_volume_spike(
    symbol: str,
    interval: str,
    interval_name: str,
    avg_candle_count: int,
    spike_multiple: float,
):
    """거래량 급증 여부 확인 (현재 캔들 vs 직전 N개 캔들 평균)"""
    limit = avg_candle_count + 1
    url = (
        "https://api.bybit.com/v5/market/kline"
        f"?category=linear&symbol={symbol}&interval={interval}&limit={limit}"
    )
    response = requests.get(url, timeout=10).json()

    if "result" not in response or not response["result"].get("list"):
        return None

    klines = response["result"]["list"]
    if len(klines) < limit:
        return None

    # klines[0]은 현재 진행 중인 캔들
    current_candle_time = klines[0][0]
    current_price = float(klines[0][4])
    current_volume = float(klines[0][5])

    # 중복 알림 방지
    alert_id = f"{symbol}_{interval}_{current_candle_time}"
    if alert_id in alerted_candles:
        return None

    past_volume_sum = sum(float(candle[5]) for candle in klines[1 : avg_candle_count + 1])
    avg_volume = past_volume_sum / avg_candle_count

    if avg_volume == 0:
        return None

    if current_volume >= (avg_volume * spike_multiple):
        increase_ratio = (current_volume / avg_volume) * 100
        alerted_candles.add(alert_id)
        return current_volume, increase_ratio, current_price, spike_multiple

    return None


def scan_multi_timeframes() -> None:
    """모든 종목과 타임프레임 스캔"""
    now = datetime.now(TIMEZONE)
    print(f"\n[{now.strftime('%Y-%m-%d %H:%M:%S %Z')}] 바이비트 모니터링 스캔 시작...")

    symbols = get_all_usdt_symbols()
    alert_list = []

    for symbol in symbols:
        for interval, interval_name in INTERVALS.items():
            try:
                if interval == "W":
                    # 주봉: 직전 15개 평균 대비 500%(5배)
                    result = check_volume_spike(symbol, interval, interval_name, 15, 5)
                else:
                    # 4h/6h: 직전 10개 평균 대비 800%(8배)
                    result = check_volume_spike(symbol, interval, interval_name, 10, 8)

                if result:
                    curr_vol, ratio, curr_price, spike_multiple = result
                    trade_url = f"https://www.bybit.com/trade/usdt/{symbol}"
                    msg_item = (
                        f"🚀 <b>{symbol} ({interval_name})</b>\n"
                        f"💰 현재가: <b>{curr_price} USDT</b>\n"
                        f"📊 거래량: {curr_vol:.0f} "
                        f"(직전 평균 대비 {ratio:.0f}% / 기준 {int(spike_multiple * 100)}%)\n"
                        f"🔗 <a href='{trade_url}'>바이비트 거래화면 바로가기</a>"
                    )
                    alert_list.append(msg_item)

                time.sleep(0.05)

            except Exception as e:
                print(f"{symbol} ({interval_name}) 처리 중 오류: {e}")
                continue

    if alert_list:
        msg = "🚨 <b>거래량 급증 알림</b> 🚨\n\n" + "\n-----------------------------------\n".join(alert_list)
        send_telegram_message(msg)
        print(f"알림 전송 완료: {len(alert_list)}건")
    else:
        print("조건에 부합하는 새로운 종목이 없습니다.")


def main() -> None:
    send_telegram_message("✅ 바이비트 모니터링 봇이 시작되었습니다.")

    while True:
        now = datetime.now(TIMEZONE)

        if should_send_daily_report(now):
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S %Z')}] 일일 TOP 15 리포트 전송")
            send_daily_top_gainers_report(now)

        scan_multi_timeframes()
        time.sleep(60)  # 1분 주기


if __name__ == "__main__":
    main()
