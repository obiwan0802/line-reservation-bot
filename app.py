"""
飲食店向け LINE予約Bot（LINE Bot SDK v2 安定版）
- メニュー（コース）選択 → 人数選択 → 日時選択 → 予約確定
- Googleカレンダー連携で空き枠自動判定
- オーナーへのLINE通知
- 前日リマインド自動送信
"""

import os
import json
import datetime
import logging
from zoneinfo import ZoneInfo

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent,
    PostbackEvent,
    FollowEvent,
    TextMessage,
    TextSendMessage,
    FlexSendMessage,
)

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from apscheduler.schedulers.background import BackgroundScheduler

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 設定
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# LINE API設定
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
OWNER_LINE_USER_ID = os.environ.get("OWNER_LINE_USER_ID", "")

# Google Calendar設定
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

# 店舗設定
STORE_NAME = os.environ.get("STORE_NAME", "サンプルレストラン")
STORE_OPEN_HOUR = int(os.environ.get("STORE_OPEN_HOUR", "11"))
STORE_CLOSE_HOUR = int(os.environ.get("STORE_CLOSE_HOUR", "22"))
MAX_SEATS = int(os.environ.get("MAX_SEATS", "30"))
SLOT_INTERVAL_MINUTES = int(os.environ.get("SLOT_INTERVAL_MINUTES", "30"))

JST = ZoneInfo("Asia/Tokyo")

# LINE SDK初期化
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# メニュー設定（お店に合わせて変更）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MENU_ITEMS = [
    {"id": "lunch_a", "name": "ランチコースA", "price": 1500, "duration": 60, "emoji": "🍽️"},
    {"id": "lunch_b", "name": "ランチコースB", "price": 2500, "duration": 90, "emoji": "🥗"},
    {"id": "dinner_standard", "name": "ディナースタンダード", "price": 4000, "duration": 90, "emoji": "🍷"},
    {"id": "dinner_premium", "name": "ディナープレミアム", "price": 6000, "duration": 120, "emoji": "✨"},
    {"id": "party", "name": "パーティーコース", "price": 5000, "duration": 150, "emoji": "🎉"},
]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 予約セッション管理（インメモリ）
# 本番運用ではRedisやDBに置き換え推奨
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
reservation_sessions = {}
confirmed_reservations = []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Google Calendar連携
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_calendar_service():
    try:
        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
        return build("calendar", "v3", credentials=creds)
    except Exception as e:
        logger.error(f"Google Calendar接続エラー: {e}")
        return None


def get_reserved_slots(date_str):
    service = get_calendar_service()
    if not service:
        return []

    date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    time_min = datetime.datetime(date.year, date.month, date.day, 0, 0, 0, tzinfo=JST)
    time_max = datetime.datetime(date.year, date.month, date.day, 23, 59, 59, tzinfo=JST)

    try:
        events = (
            service.events()
            .list(
                calendarId=GOOGLE_CALENDAR_ID,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        return events.get("items", [])
    except Exception as e:
        logger.error(f"カレンダー取得エラー: {e}")
        return []


def count_reserved_seats_from_events(events, time_str, date_str):
    """事前取得済みのイベントリストから指定時刻の予約席数を計算（API呼び出しなし）"""
    target_time = datetime.datetime.strptime(
        f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=JST)

    total_seats = 0
    for event in events:
        start = datetime.datetime.fromisoformat(event["start"].get("dateTime", ""))
        end = datetime.datetime.fromisoformat(event["end"].get("dateTime", ""))
        if start <= target_time < end:
            desc = event.get("description", "")
            try:
                for line in desc.split("\n"):
                    if "人数:" in line:
                        total_seats += int(line.split("人数:")[1].strip().replace("名", ""))
                        break
            except (ValueError, IndexError):
                total_seats += 2
    return total_seats


def get_available_slots(date_str, guests, duration_minutes):
    now = datetime.datetime.now(JST)
    target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=JST)

    # ★ カレンダーAPIを1回だけ呼び出してキャッシュ
    events = get_reserved_slots(date_str)

    slots = []
    hour = STORE_OPEN_HOUR
    minute = 0

    while hour < STORE_CLOSE_HOUR:
        time_str = f"{hour:02d}:{minute:02d}"
        slot_time = target_date.replace(hour=hour, minute=minute)

        if slot_time <= now:
            minute += SLOT_INTERVAL_MINUTES
            if minute >= 60:
                hour += 1
                minute = 0
            continue

        end_time = slot_time + datetime.timedelta(minutes=duration_minutes)
        if end_time.hour > STORE_CLOSE_HOUR or (
            end_time.hour == STORE_CLOSE_HOUR and end_time.minute > 0
        ):
            break

        # ★ キャッシュしたイベントを使って計算（APIコールなし）
        reserved = count_reserved_seats_from_events(events, time_str, date_str)
        available = MAX_SEATS - reserved
        if available >= guests:
            slots.append({"time": time_str, "available": available})

        minute += SLOT_INTERVAL_MINUTES
        if minute >= 60:
            hour += 1
            minute = 0

    return slots


def create_calendar_event(reservation):
    service = get_calendar_service()
    if not service:
        return None

    menu = next((m for m in MENU_ITEMS if m["id"] == reservation["menu"]), None)
    if not menu:
        return None

    start_dt = datetime.datetime.strptime(
        f"{reservation['date']} {reservation['time']}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=JST)
    end_dt = start_dt + datetime.timedelta(minutes=menu["duration"])

    event = {
        "summary": f"【予約】{reservation.get('name', 'LINE予約')} {reservation['guests']}名",
        "description": (
            f"コース: {menu['name']}\n"
            f"人数: {reservation['guests']}名\n"
            f"お名前: {reservation.get('name', '未設定')}\n"
            f"電話番号: {reservation.get('phone', '未設定')}\n"
            f"LINE USER ID: {reservation['user_id']}\n"
            f"予約金額: ¥{menu['price'] * reservation['guests']:,}"
        ),
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Tokyo"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Tokyo"},
        "colorId": "9",
    }

    try:
        created = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        return created.get("id")
    except Exception as e:
        logger.error(f"カレンダー登録エラー: {e}")
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Flex Messageテンプレート
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def build_welcome_flex():
    return {
        "type": "bubble",
        "hero": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": STORE_NAME,
                    "weight": "bold",
                    "size": "xl",
                    "color": "#FFFFFF",
                    "align": "center",
                },
                {
                    "type": "text",
                    "text": "LINE予約システム",
                    "size": "sm",
                    "color": "#FFFFFFCC",
                    "align": "center",
                    "margin": "sm",
                },
            ],
            "backgroundColor": "#E05241",
            "paddingAll": "20px",
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "ご来店ありがとうございます！",
                    "weight": "bold",
                    "size": "md",
                },
                {
                    "type": "text",
                    "text": "下のボタンからかんたんに予約できます。",
                    "size": "sm",
                    "color": "#666666",
                    "margin": "md",
                    "wrap": True,
                },
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "action": {
                        "type": "postback",
                        "label": "📅 予約する",
                        "data": "action=start_reservation",
                    },
                    "style": "primary",
                    "color": "#E05241",
                },
                {
                    "type": "button",
                    "action": {
                        "type": "postback",
                        "label": "📋 予約を確認する",
                        "data": "action=check_reservation",
                    },
                    "style": "secondary",
                    "margin": "sm",
                },
            ],
        },
    }


def build_menu_flex():
    bubbles = []
    for item in MENU_ITEMS:
        bubble = {
            "type": "bubble",
            "size": "micro",
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type": "text", "text": item["emoji"], "size": "3xl", "align": "center"},
                    {
                        "type": "text",
                        "text": item["name"],
                        "weight": "bold",
                        "size": "sm",
                        "align": "center",
                        "margin": "md",
                        "wrap": True,
                    },
                    {
                        "type": "text",
                        "text": f"¥{item['price']:,}/人",
                        "size": "lg",
                        "color": "#E05241",
                        "align": "center",
                        "weight": "bold",
                        "margin": "sm",
                    },
                    {
                        "type": "text",
                        "text": f"所要時間: 約{item['duration']}分",
                        "size": "xs",
                        "color": "#999999",
                        "align": "center",
                        "margin": "sm",
                    },
                ],
                "paddingAll": "12px",
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "button",
                        "action": {
                            "type": "postback",
                            "label": "選択",
                            "data": f"action=select_menu&menu_id={item['id']}",
                        },
                        "style": "primary",
                        "color": "#E05241",
                        "height": "sm",
                    }
                ],
            },
        }
        bubbles.append(bubble)
    return {"type": "carousel", "contents": bubbles}


def build_guests_flex():
    buttons = []
    for n in range(1, 9):
        buttons.append(
            {
                "type": "button",
                "action": {
                    "type": "postback",
                    "label": f"{n}名",
                    "data": f"action=select_guests&guests={n}",
                },
                "style": "secondary",
                "height": "sm",
                "margin": "sm",
            }
        )

    rows = []
    for i in range(0, len(buttons), 2):
        row = {
            "type": "box",
            "layout": "horizontal",
            "contents": buttons[i : i + 2],
            "spacing": "sm",
        }
        rows.append(row)

    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "👥 人数を選択", "weight": "bold", "size": "lg"},
                {
                    "type": "text",
                    "text": "ご来店人数をお選びください",
                    "size": "sm",
                    "color": "#666666",
                    "margin": "md",
                },
                {"type": "separator", "margin": "lg"},
                *rows,
            ],
        },
    }


def build_date_flex():
    today = datetime.datetime.now(JST)
    buttons = []
    for i in range(1, 8):
        d = today + datetime.timedelta(days=i)
        weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]
        wd = weekday_jp[d.weekday()]
        date_str = d.strftime("%Y-%m-%d")
        label = f"{d.month}/{d.day}（{wd}）"
        buttons.append(
            {
                "type": "button",
                "action": {
                    "type": "postback",
                    "label": label,
                    "data": f"action=select_date&date={date_str}",
                },
                "style": "secondary",
                "height": "sm",
                "margin": "sm",
            }
        )
    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "📅 日付を選択", "weight": "bold", "size": "lg"},
                {
                    "type": "text",
                    "text": "ご希望の日をお選びください",
                    "size": "sm",
                    "color": "#666666",
                    "margin": "md",
                },
                {"type": "separator", "margin": "lg"},
                *buttons,
            ],
        },
    }


def build_time_flex(available_slots):
    if not available_slots:
        return {
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "😢 空き枠なし", "weight": "bold", "size": "lg"},
                    {
                        "type": "text",
                        "text": "この日は満席です。別の日をお選びください。",
                        "size": "sm",
                        "color": "#666666",
                        "margin": "md",
                        "wrap": True,
                    },
                ],
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "button",
                        "action": {
                            "type": "postback",
                            "label": "別の日を選ぶ",
                            "data": "action=reselect_date",
                        },
                        "style": "primary",
                        "color": "#E05241",
                    }
                ],
            },
        }

    buttons = []
    for slot in available_slots[:12]:
        label = f"{slot['time']}〜（残{slot['available']}席）"
        buttons.append(
            {
                "type": "button",
                "action": {
                    "type": "postback",
                    "label": label,
                    "data": f"action=select_time&time={slot['time']}",
                },
                "style": "secondary",
                "height": "sm",
                "margin": "sm",
            }
        )

    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "🕐 時間を選択", "weight": "bold", "size": "lg"},
                {
                    "type": "text",
                    "text": "ご希望の時間帯をお選びください",
                    "size": "sm",
                    "color": "#666666",
                    "margin": "md",
                },
                {"type": "separator", "margin": "lg"},
                *buttons,
            ],
        },
    }


def _detail_row(label, value, bold=False):
    return {
        "type": "box",
        "layout": "horizontal",
        "contents": [
            {"type": "text", "text": label, "size": "sm", "color": "#999999", "flex": 2},
            {
                "type": "text",
                "text": value,
                "size": "sm",
                "weight": "bold" if bold else "regular",
                "color": "#E05241" if bold else "#333333",
                "flex": 3,
                "wrap": True,
            },
        ],
    }


def build_confirm_flex(session):
    menu = next((m for m in MENU_ITEMS if m["id"] == session["menu"]), None)
    total = menu["price"] * session["guests"]
    date_obj = datetime.datetime.strptime(session["date"], "%Y-%m-%d")
    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]
    date_display = f"{date_obj.month}/{date_obj.day}（{weekday_jp[date_obj.weekday()]}）"

    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "📋 予約内容の確認", "weight": "bold", "size": "lg"},
                {"type": "separator", "margin": "lg"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        _detail_row("コース", f"{menu['emoji']} {menu['name']}"),
                        _detail_row("人数", f"{session['guests']}名"),
                        _detail_row("日付", date_display),
                        _detail_row("時間", f"{session['time']}〜"),
                        _detail_row("お名前", session.get("name", "未設定")),
                        _detail_row("電話番号", session.get("phone", "未設定")),
                        {"type": "separator", "margin": "lg"},
                        _detail_row("合計金額", f"¥{total:,}", bold=True),
                    ],
                    "margin": "lg",
                    "spacing": "md",
                },
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "action": {
                        "type": "postback",
                        "label": "✅ 予約を確定する",
                        "data": "action=confirm_reservation",
                    },
                    "style": "primary",
                    "color": "#E05241",
                },
                {
                    "type": "button",
                    "action": {
                        "type": "postback",
                        "label": "❌ キャンセル",
                        "data": "action=cancel_reservation",
                    },
                    "style": "secondary",
                    "margin": "sm",
                },
            ],
        },
    }


def build_complete_flex(session):
    menu = next((m for m in MENU_ITEMS if m["id"] == session["menu"]), None)
    date_obj = datetime.datetime.strptime(session["date"], "%Y-%m-%d")
    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]
    date_display = f"{date_obj.month}/{date_obj.day}（{weekday_jp[date_obj.weekday()]}）"

    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": "🎉", "size": "3xl", "align": "center"},
                {
                    "type": "text",
                    "text": "予約が完了しました！",
                    "weight": "bold",
                    "size": "lg",
                    "align": "center",
                    "margin": "md",
                },
                {"type": "separator", "margin": "lg"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        _detail_row("コース", menu["name"]),
                        _detail_row("日時", f"{date_display} {session['time']}〜"),
                        _detail_row("人数", f"{session['guests']}名"),
                    ],
                    "margin": "lg",
                    "spacing": "md",
                },
                {
                    "type": "text",
                    "text": "前日にリマインドをお送りします。\nご来店をお待ちしております！",
                    "size": "xs",
                    "color": "#999999",
                    "margin": "xl",
                    "wrap": True,
                    "align": "center",
                },
            ],
        },
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LINEメッセージ送信ヘルパー
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def reply_flex(reply_token, alt_text, flex_content):
    line_bot_api.reply_message(
        reply_token,
        FlexSendMessage(alt_text=alt_text, contents=flex_content),
    )


def reply_text(reply_token, text):
    line_bot_api.reply_message(reply_token, TextSendMessage(text=text))


def push_text(user_id, text):
    line_bot_api.push_message(user_id, TextSendMessage(text=text))


def push_flex(user_id, alt_text, flex_content):
    line_bot_api.push_message(
        user_id,
        FlexSendMessage(alt_text=alt_text, contents=flex_content),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Webhookエンドポイント
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    logger.info(f"Webhook受信: {body[:200]}")

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@app.route("/health", methods=["GET"])
def health():
    return "OK"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# イベントハンドラ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@handler.add(FollowEvent)
def handle_follow(event):
    reply_flex(event.reply_token, f"{STORE_NAME} LINE予約", build_welcome_flex())


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    session = reservation_sessions.get(user_id)
    if session:
        if session["step"] == "name":
            session["name"] = text
            session["step"] = "phone"
            reply_text(event.reply_token, "📱 お電話番号を入力してください（例: 090-1234-5678）")
            return
        elif session["step"] == "phone":
            session["phone"] = text
            session["step"] = "confirm"
            reply_flex(event.reply_token, "予約内容の確認", build_confirm_flex(session))
            return

    if text in ["予約", "予約する", "予約したい"]:
        reply_flex(event.reply_token, f"{STORE_NAME} LINE予約", build_welcome_flex())
    elif text in ["メニュー", "コース"]:
        reservation_sessions[user_id] = {"step": "menu"}
        reply_flex(event.reply_token, "メニュー選択", build_menu_flex())
    elif text in ["確認", "予約確認"]:
        check_user_reservations(event.reply_token, user_id)
    elif text in ["キャンセル", "取消"]:
        if user_id in reservation_sessions:
            del reservation_sessions[user_id]
        reply_text(event.reply_token, "予約フローをキャンセルしました。\n「予約」と送信するとやり直せます。")
    else:
        reply_flex(event.reply_token, f"{STORE_NAME} LINE予約", build_welcome_flex())


@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    data = dict(x.split("=") for x in event.postback.data.split("&"))
    action = data.get("action")

    if action == "start_reservation":
        reservation_sessions[user_id] = {"step": "menu", "user_id": user_id}
        reply_flex(event.reply_token, "メニュー選択", build_menu_flex())

    elif action == "select_menu":
        session = reservation_sessions.get(user_id, {"user_id": user_id})
        session["menu"] = data["menu_id"]
        session["step"] = "guests"
        reservation_sessions[user_id] = session
        reply_flex(event.reply_token, "人数選択", build_guests_flex())

    elif action == "select_guests":
        session = reservation_sessions.get(user_id, {})
        session["guests"] = int(data["guests"])
        session["step"] = "date"
        reservation_sessions[user_id] = session
        reply_flex(event.reply_token, "日付選択", build_date_flex())

    elif action == "select_date" or action == "reselect_date":
        session = reservation_sessions.get(user_id, {})
        if action == "select_date":
            session["date"] = data["date"]
        session["step"] = "time"
        reservation_sessions[user_id] = session

        if action == "reselect_date":
            reply_flex(event.reply_token, "日付選択", build_date_flex())
            return

        # 先に「検索中」と返信してから、空き枠をプッシュ送信
        reply_text(event.reply_token, "🔍 空き状況を確認しています...\n少々お待ちください")

        menu = next((m for m in MENU_ITEMS if m["id"] == session.get("menu")), None)
        duration = menu["duration"] if menu else 60
        slots = get_available_slots(session["date"], session["guests"], duration)
        push_flex(user_id, "時間選択", build_time_flex(slots))

    elif action == "select_time":
        session = reservation_sessions.get(user_id, {})
        session["time"] = data["time"]
        session["step"] = "name"
        reservation_sessions[user_id] = session
        reply_text(event.reply_token, "✏️ ご予約のお名前を入力してください")

    elif action == "confirm_reservation":
        session = reservation_sessions.get(user_id)
        if not session:
            reply_text(event.reply_token, "セッションが切れました。「予約」と送信してやり直してください。")
            return

        event_id = create_calendar_event(session)
        session["calendar_event_id"] = event_id
        session["confirmed_at"] = datetime.datetime.now(JST).isoformat()
        confirmed_reservations.append(session.copy())

        reply_flex(event.reply_token, "予約完了", build_complete_flex(session))
        notify_owner(session)

        del reservation_sessions[user_id]

    elif action == "cancel_reservation":
        if user_id in reservation_sessions:
            del reservation_sessions[user_id]
        reply_text(event.reply_token, "予約をキャンセルしました。\n「予約」と送信するといつでもやり直せます。")

    elif action == "check_reservation":
        check_user_reservations(event.reply_token, user_id)


def check_user_reservations(reply_token, user_id):
    now = datetime.datetime.now(JST)
    user_reservations = [
        r
        for r in confirmed_reservations
        if r.get("user_id") == user_id
        and datetime.datetime.strptime(f"{r['date']} {r['time']}", "%Y-%m-%d %H:%M").replace(
            tzinfo=JST
        )
        > now
    ]

    if not user_reservations:
        reply_text(reply_token, "現在、予約はありません。\n「予約」と送信すると予約できます。")
        return

    lines = ["📋 現在のご予約:\n"]
    for r in user_reservations:
        menu = next((m for m in MENU_ITEMS if m["id"] == r["menu"]), None)
        date_obj = datetime.datetime.strptime(r["date"], "%Y-%m-%d")
        weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]
        lines.append(
            f"・{date_obj.month}/{date_obj.day}（{weekday_jp[date_obj.weekday()]}）"
            f" {r['time']}〜\n"
            f"  {menu['name'] if menu else '不明'} / {r['guests']}名"
        )
    reply_text(reply_token, "\n".join(lines))


def notify_owner(session):
    if not OWNER_LINE_USER_ID or OWNER_LINE_USER_ID == "dummy":
        logger.warning("OWNER_LINE_USER_ID未設定: オーナー通知スキップ")
        return

    menu = next((m for m in MENU_ITEMS if m["id"] == session["menu"]), None)
    date_obj = datetime.datetime.strptime(session["date"], "%Y-%m-%d")
    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]

    message = (
        f"🔔 新しい予約が入りました！\n\n"
        f"📅 {date_obj.month}/{date_obj.day}（{weekday_jp[date_obj.weekday()]}）{session['time']}〜\n"
        f"👤 {session.get('name', '未設定')} 様\n"
        f"👥 {session['guests']}名\n"
        f"🍽️ {menu['name'] if menu else '不明'}\n"
        f"📱 {session.get('phone', '未設定')}\n"
        f"💰 ¥{menu['price'] * session['guests']:,}"
    )

    try:
        push_text(OWNER_LINE_USER_ID, message)
    except Exception as e:
        logger.error(f"オーナー通知エラー: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 前日リマインド
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def send_reminders():
    tomorrow = (datetime.datetime.now(JST) + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    for r in confirmed_reservations:
        if r["date"] == tomorrow and not r.get("reminded"):
            menu = next((m for m in MENU_ITEMS if m["id"] == r["menu"]), None)
            date_obj = datetime.datetime.strptime(r["date"], "%Y-%m-%d")
            weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]

            message = (
                f"⏰ 明日のご予約リマインド\n\n"
                f"{STORE_NAME}より\n\n"
                f"📅 {date_obj.month}/{date_obj.day}（{weekday_jp[date_obj.weekday()]}）"
                f" {r['time']}〜\n"
                f"🍽️ {menu['name'] if menu else ''}\n"
                f"👥 {r['guests']}名\n\n"
                f"ご来店をお待ちしております！\n"
                f"変更・キャンセルはお電話でご連絡ください。"
            )

            try:
                push_text(r["user_id"], message)
                r["reminded"] = True
                logger.info(f"リマインド送信: {r.get('name')} {r['date']}")
            except Exception as e:
                logger.error(f"リマインド送信エラー: {e}")


scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
scheduler.add_job(send_reminders, "cron", hour=18, minute=0)
scheduler.start()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 起動
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
