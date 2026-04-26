"""
飲食店向け LINE予約Bot — Phase 2（オーナーダッシュボード付き）
- Supabaseデータベースで予約・顧客データを永続化
- キャンセル・変更機能
- 定休日・臨時休業対応
- Googleカレンダー連携
- オーナーLINE通知 / 前日リマインド
- Webダッシュボード（予約管理・定休日設定・顧客リスト）
"""

import os
import json
import datetime
import logging
from zoneinfo import ZoneInfo

from flask import Flask, request, abort, render_template, jsonify
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

import requests as http_requests

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from apscheduler.schedulers.background import BackgroundScheduler

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 設定
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# LINE API
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
OWNER_LINE_USER_ID = os.environ.get("OWNER_LINE_USER_ID", "")

# Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# Google Calendar
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

# 店舗設定
STORE_NAME = os.environ.get("STORE_NAME", "サンプルレストラン")
STORE_OPEN_HOUR = int(os.environ.get("STORE_OPEN_HOUR", "11"))
STORE_CLOSE_HOUR = int(os.environ.get("STORE_CLOSE_HOUR", "22"))
MAX_SEATS = int(os.environ.get("MAX_SEATS", "30"))
SLOT_INTERVAL_MINUTES = int(os.environ.get("SLOT_INTERVAL_MINUTES", "30"))
BOOKING_DEADLINE_HOURS = int(os.environ.get("BOOKING_DEADLINE_HOURS", "2"))  # 予約締切（何時間前）

# ダッシュボード
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "admin1234")

JST = ZoneInfo("Asia/Tokyo")

# SDK初期化
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Supabase REST APIヘルパー
SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

def supabase_get(table, params=None):
    """Supabase REST API GET"""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    res = http_requests.get(url, headers=SUPABASE_HEADERS, params=params or {})
    res.raise_for_status()
    return res.json()

def supabase_post(table, data):
    """Supabase REST API POST (INSERT)"""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    res = http_requests.post(url, headers=SUPABASE_HEADERS, json=data)
    res.raise_for_status()
    return res.json()

def supabase_patch(table, data, params):
    """Supabase REST API PATCH (UPDATE)"""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    res = http_requests.patch(url, headers=SUPABASE_HEADERS, json=data, params=params)
    res.raise_for_status()
    return res.json()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# メニュー設定
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MENU_ITEMS = [
    {"id": "seat_only", "name": "席のみ予約", "price": 0, "duration": 120, "emoji": "💺"},
    {"id": "lunch_a", "name": "ランチコースA", "price": 1500, "duration": 60, "emoji": "🍽️"},
    {"id": "lunch_b", "name": "ランチコースB", "price": 2500, "duration": 90, "emoji": "🥗"},
    {"id": "dinner_standard", "name": "ディナースタンダード", "price": 4000, "duration": 90, "emoji": "🍷"},
    {"id": "dinner_premium", "name": "ディナープレミアム", "price": 6000, "duration": 120, "emoji": "✨"},
    {"id": "party", "name": "パーティーコース", "price": 5000, "duration": 150, "emoji": "🎉"},
]

# 予約フローのセッション（インメモリ — フロー中のみ使用）
reservation_sessions = {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Supabase データ操作
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def db_get_or_create_customer(line_user_id, display_name=None):
    """顧客を取得、なければ新規作成"""
    try:
        rows = supabase_get("customers", {"line_user_id": f"eq.{line_user_id}", "select": "*"})
        if rows:
            return rows[0]
        new = supabase_post("customers", {
            "line_user_id": line_user_id,
            "display_name": display_name,
        })
        return new[0] if new else None
    except Exception as e:
        logger.error(f"顧客DB操作エラー: {e}")
        return None


def db_save_reservation(reservation_data):
    """予約をDBに保存"""
    try:
        res = supabase_post("reservations", reservation_data)
        return res[0] if res else None
    except Exception as e:
        logger.error(f"予約保存エラー: {e}")
        return None


def db_get_user_reservations(line_user_id):
    """ユーザーの未来の有効な予約を取得"""
    today = datetime.datetime.now(JST).strftime("%Y-%m-%d")
    try:
        rows = supabase_get("reservations", {
            "select": "*",
            "line_user_id": f"eq.{line_user_id}",
            "status": "eq.confirmed",
            "reservation_date": f"gte.{today}",
            "order": "reservation_date.asc,reservation_time.asc",
        })
        return rows or []
    except Exception as e:
        logger.error(f"予約取得エラー: {e}")
        return []


def db_cancel_reservation(reservation_id, line_user_id):
    """予約をキャンセル"""
    try:
        res = supabase_patch("reservations", {"status": "cancelled"}, {
            "id": f"eq.{reservation_id}",
            "line_user_id": f"eq.{line_user_id}",
        })
        return bool(res)
    except Exception as e:
        logger.error(f"キャンセルエラー: {e}")
        return False


def db_get_reservations_by_date(date_str):
    """指定日の有効な予約を全件取得"""
    try:
        rows = supabase_get("reservations", {
            "select": "*",
            "reservation_date": f"eq.{date_str}",
            "status": "eq.confirmed",
        })
        return rows or []
    except Exception as e:
        logger.error(f"日付別予約取得エラー: {e}")
        return []


def db_get_tomorrow_reminders():
    """翌日の未リマインド予約を取得"""
    tomorrow = (datetime.datetime.now(JST) + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        rows = supabase_get("reservations", {
            "select": "*",
            "reservation_date": f"eq.{tomorrow}",
            "status": "eq.confirmed",
            "reminded": "eq.false",
        })
        return rows or []
    except Exception as e:
        logger.error(f"リマインド取得エラー: {e}")
        return []


def db_mark_reminded(reservation_id):
    """リマインド送信済みにマーク"""
    try:
        supabase_patch("reservations", {"reminded": True}, {"id": f"eq.{reservation_id}"})
    except Exception as e:
        logger.error(f"リマインド更新エラー: {e}")


def db_update_customer_visit(line_user_id, name=None, phone=None):
    """顧客の来店回数・情報を更新"""
    try:
        customer = db_get_or_create_customer(line_user_id)
        if customer:
            update_data = {"visit_count": customer.get("visit_count", 0) + 1}
            if name:
                update_data["display_name"] = name
            if phone:
                update_data["phone"] = phone
            supabase_patch("customers", update_data, {"line_user_id": f"eq.{line_user_id}"})
    except Exception as e:
        logger.error(f"顧客更新エラー: {e}")


def db_is_closed_day(date_str):
    """指定日が定休日・臨時休業かチェック"""
    try:
        date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        day_of_week = date_obj.weekday()  # 0=月〜6=日

        # 特定日の休業チェック
        rows1 = supabase_get("closed_days", {
            "select": "*",
            "closed_date": f"eq.{date_str}",
        })
        if rows1:
            return True

        # 毎週の定休日チェック
        rows2 = supabase_get("closed_days", {
            "select": "*",
            "day_of_week": f"eq.{day_of_week}",
            "is_recurring": "eq.true",
        })
        if rows2:
            return True

        return False
    except Exception as e:
        logger.error(f"定休日チェックエラー: {e}")
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Google Calendar連携
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_calendar_service():
    try:
        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(
            creds_dict, scopes=["https://www.googleapis.com/auth/calendar"]
        )
        return build("calendar", "v3", credentials=creds)
    except Exception as e:
        logger.error(f"Google Calendar接続エラー: {e}")
        return None


def create_calendar_event(reservation):
    service = get_calendar_service()
    if not service:
        return None

    menu = next((m for m in MENU_ITEMS if m["id"] == reservation["menu_id"]), None)
    if not menu:
        return None

    start_dt = datetime.datetime.strptime(
        f"{reservation['reservation_date']} {reservation['reservation_time']}",
        "%Y-%m-%d %H:%M",
    ).replace(tzinfo=JST)
    end_dt = start_dt + datetime.timedelta(minutes=reservation["duration_minutes"])

    event = {
        "summary": f"【予約】{reservation['guest_name']} {reservation['guests']}名",
        "description": (
            f"コース: {reservation['menu_name']}\n"
            f"人数: {reservation['guests']}名\n"
            f"お名前: {reservation['guest_name']}\n"
            f"電話番号: {reservation.get('phone', '未設定')}\n"
            f"予約金額: ¥{reservation.get('total_price', 0):,}"
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


def delete_calendar_event(event_id):
    """Googleカレンダーのイベントを削除"""
    service = get_calendar_service()
    if not service or not event_id:
        return
    try:
        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
    except Exception as e:
        logger.error(f"カレンダー削除エラー: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 店舗設定（DB）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def db_get_setting(key, default=None):
    """設定値をDBから取得"""
    try:
        rows = supabase_get("store_settings", {"select": "value", "key": f"eq.{key}"})
        if rows:
            return rows[0]["value"]
        return default
    except Exception as e:
        logger.error(f"設定取得エラー: {e}")
        return default


def db_set_setting(key, value):
    """設定値をDBに保存（なければ作成、あれば更新）"""
    try:
        rows = supabase_get("store_settings", {"select": "key", "key": f"eq.{key}"})
        if rows:
            supabase_patch("store_settings", {"value": str(value)}, {"key": f"eq.{key}"})
        else:
            supabase_post("store_settings", {"key": key, "value": str(value)})
    except Exception as e:
        logger.error(f"設定保存エラー: {e}")


def get_booking_deadline_hours():
    """現在の予約締切時間を取得（時間単位）"""
    val = db_get_setting("booking_deadline_hours")
    if val is not None:
        try:
            return int(val)
        except ValueError:
            pass
    return BOOKING_DEADLINE_HOURS


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 空き枠計算（DB版）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_available_slots(date_str, guests, duration_minutes):
    now = datetime.datetime.now(JST)
    target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=JST)

    # 定休日チェック
    if db_is_closed_day(date_str):
        return []

    # DBから当日の予約を1回で取得
    day_reservations = db_get_reservations_by_date(date_str)

    slots = []
    hour = STORE_OPEN_HOUR
    minute = 0

    while hour < STORE_CLOSE_HOUR:
        time_str = f"{hour:02d}:{minute:02d}"
        slot_time = target_date.replace(hour=hour, minute=minute)

        # 予約締切チェック（現在時刻 + 締切時間 以内の枠は除外）
        deadline_hours = get_booking_deadline_hours()
        deadline = now + datetime.timedelta(hours=deadline_hours)
        if slot_time <= deadline:
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

        # 予約済み席数をメモリ内で計算
        reserved_seats = 0
        for r in day_reservations:
            r_start = datetime.datetime.strptime(
                f"{r['reservation_date']} {r['reservation_time']}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=JST)
            r_end = r_start + datetime.timedelta(minutes=r.get("duration_minutes", 60))
            if r_start <= slot_time < r_end:
                reserved_seats += r["guests"]

        available = MAX_SEATS - reserved_seats
        if available >= guests:
            slots.append({"time": time_str, "available": available})

        minute += SLOT_INTERVAL_MINUTES
        if minute >= 60:
            hour += 1
            minute = 0

    return slots


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
                {"type": "text", "text": STORE_NAME, "weight": "bold", "size": "xl",
                 "color": "#FFFFFF", "align": "center"},
                {"type": "text", "text": "LINE予約システム", "size": "sm",
                 "color": "#FFFFFFCC", "align": "center", "margin": "sm"},
            ],
            "backgroundColor": "#E05241",
            "paddingAll": "20px",
        },
        "body": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "text", "text": "ご来店ありがとうございます！", "weight": "bold", "size": "md"},
                {"type": "text", "text": "下のボタンからかんたんに予約できます。",
                 "size": "sm", "color": "#666666", "margin": "md", "wrap": True},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "button", "action": {"type": "postback", "label": "📅 予約する",
                 "data": "action=start_reservation"}, "style": "primary", "color": "#E05241"},
                {"type": "button", "action": {"type": "postback", "label": "📋 予約を確認する",
                 "data": "action=check_reservation"}, "style": "secondary", "margin": "sm"},
                {"type": "button", "action": {"type": "postback", "label": "❌ 予約をキャンセル",
                 "data": "action=list_cancel"}, "style": "secondary", "margin": "sm"},
            ],
        },
    }


def build_menu_flex():
    bubbles = []
    for item in MENU_ITEMS:
        bubble = {
            "type": "bubble", "size": "micro",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": item["emoji"], "size": "3xl", "align": "center"},
                    {"type": "text", "text": item["name"], "weight": "bold", "size": "sm",
                     "align": "center", "margin": "md", "wrap": True},
                    {"type": "text", "text": f"¥{item['price']:,}/人", "size": "lg",
                     "color": "#E05241", "align": "center", "weight": "bold", "margin": "sm"},
                    {"type": "text", "text": f"所要時間: 約{item['duration']}分", "size": "xs",
                     "color": "#999999", "align": "center", "margin": "sm"},
                ],
                "paddingAll": "12px",
            },
            "footer": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "button", "action": {"type": "postback", "label": "選択",
                     "data": f"action=select_menu&menu_id={item['id']}"},
                     "style": "primary", "color": "#E05241", "height": "sm"}
                ],
            },
        }
        bubbles.append(bubble)
    return {"type": "carousel", "contents": bubbles}


def build_guests_flex():
    buttons = []
    for n in range(1, 9):
        buttons.append({
            "type": "button",
            "action": {"type": "postback", "label": f"{n}名",
                       "data": f"action=select_guests&guests={n}"},
            "style": "secondary", "height": "sm", "margin": "sm",
        })
    rows = []
    for i in range(0, len(buttons), 2):
        rows.append({"type": "box", "layout": "horizontal",
                      "contents": buttons[i:i+2], "spacing": "sm"})
    return {
        "type": "bubble",
        "body": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "text", "text": "👥 人数を選択", "weight": "bold", "size": "lg"},
                {"type": "text", "text": "ご来店人数をお選びください",
                 "size": "sm", "color": "#666666", "margin": "md"},
                {"type": "separator", "margin": "lg"},
                *rows,
            ],
        },
    }


def build_date_flex():
    today = datetime.datetime.now(JST)
    tomorrow = today + datetime.timedelta(days=1)
    max_date = today + datetime.timedelta(days=62)  # 約2ヶ月先まで
    return {
        "type": "bubble",
        "body": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "text", "text": "📅 日付を選択", "weight": "bold", "size": "lg"},
                {"type": "text", "text": "カレンダーからご希望の日をお選びください",
                 "size": "sm", "color": "#666666", "margin": "md", "wrap": True},
                {"type": "text", "text": f"※ {tomorrow.month}/{tomorrow.day} 〜 {max_date.month}/{max_date.day} の範囲で選択できます",
                 "size": "xs", "color": "#999999", "margin": "sm", "wrap": True},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "action": {
                        "type": "datetimepicker",
                        "label": "📅 カレンダーを開く",
                        "data": "action=select_date",
                        "mode": "date",
                        "initial": tomorrow.strftime("%Y-%m-%d"),
                        "min": tomorrow.strftime("%Y-%m-%d"),
                        "max": max_date.strftime("%Y-%m-%d"),
                    },
                    "style": "primary",
                    "color": "#E05241",
                },
            ],
        },
    }


def build_time_flex(available_slots):
    if not available_slots:
        return {
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "😢 空き枠なし", "weight": "bold", "size": "lg"},
                    {"type": "text", "text": "この日は満席です。別の日をお選びください。",
                     "size": "sm", "color": "#666666", "margin": "md", "wrap": True},
                ],
            },
            "footer": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "button", "action": {"type": "postback", "label": "別の日を選ぶ",
                     "data": "action=reselect_date"}, "style": "primary", "color": "#E05241"}
                ],
            },
        }
    buttons = []
    for slot in available_slots[:12]:
        buttons.append({
            "type": "button",
            "action": {"type": "postback",
                       "label": f"{slot['time']}〜（残{slot['available']}席）",
                       "data": f"action=select_time&time={slot['time']}"},
            "style": "secondary", "height": "sm", "margin": "sm",
        })
    return {
        "type": "bubble",
        "body": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "text", "text": "🕐 時間を選択", "weight": "bold", "size": "lg"},
                {"type": "text", "text": "ご希望の時間帯をお選びください",
                 "size": "sm", "color": "#666666", "margin": "md"},
                {"type": "separator", "margin": "lg"},
                *buttons,
            ],
        },
    }


def _detail_row(label, value, bold=False):
    return {
        "type": "box", "layout": "horizontal",
        "contents": [
            {"type": "text", "text": label, "size": "sm", "color": "#999999", "flex": 2},
            {"type": "text", "text": value, "size": "sm",
             "weight": "bold" if bold else "regular",
             "color": "#E05241" if bold else "#333333", "flex": 3, "wrap": True},
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
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "text", "text": "📋 予約内容の確認", "weight": "bold", "size": "lg"},
                {"type": "separator", "margin": "lg"},
                {"type": "box", "layout": "vertical", "margin": "lg", "spacing": "md",
                 "contents": [
                    _detail_row("コース", f"{menu['emoji']} {menu['name']}"),
                    _detail_row("人数", f"{session['guests']}名"),
                    _detail_row("日付", date_display),
                    _detail_row("時間", f"{session['time']}〜"),
                    _detail_row("お名前", session.get("name", "未設定")),
                    _detail_row("電話番号", session.get("phone", "未設定")),
                    {"type": "separator", "margin": "lg"},
                    _detail_row("合計金額", f"¥{total:,}", bold=True),
                ]},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "button", "action": {"type": "postback", "label": "✅ 予約を確定する",
                 "data": "action=confirm_reservation"}, "style": "primary", "color": "#E05241"},
                {"type": "button", "action": {"type": "postback", "label": "❌ キャンセル",
                 "data": "action=cancel_flow"}, "style": "secondary", "margin": "sm"},
            ],
        },
    }


def build_complete_flex(reservation):
    menu = next((m for m in MENU_ITEMS if m["id"] == reservation["menu_id"]), None)
    date_obj = datetime.datetime.strptime(reservation["reservation_date"], "%Y-%m-%d")
    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]
    date_display = f"{date_obj.month}/{date_obj.day}（{weekday_jp[date_obj.weekday()]}）"
    return {
        "type": "bubble",
        "body": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "text", "text": "🎉", "size": "3xl", "align": "center"},
                {"type": "text", "text": "予約が完了しました！", "weight": "bold",
                 "size": "lg", "align": "center", "margin": "md"},
                {"type": "separator", "margin": "lg"},
                {"type": "box", "layout": "vertical", "margin": "lg", "spacing": "md",
                 "contents": [
                    _detail_row("コース", menu["name"] if menu else ""),
                    _detail_row("日時", f"{date_display} {reservation['reservation_time']}〜"),
                    _detail_row("人数", f"{reservation['guests']}名"),
                    _detail_row("予約番号", f"#{reservation['id']}"),
                ]},
                {"type": "text", "text": "前日にリマインドをお送りします。\nご来店をお待ちしております！",
                 "size": "xs", "color": "#999999", "margin": "xl", "wrap": True, "align": "center"},
            ],
        },
    }


def build_cancel_list_flex(reservations):
    """キャンセル対象の予約一覧"""
    if not reservations:
        return {
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "現在、予約はありません。",
                     "size": "sm", "color": "#666666", "wrap": True},
                ],
            },
        }
    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]
    buttons = []
    for r in reservations[:5]:
        date_obj = datetime.datetime.strptime(r["reservation_date"], "%Y-%m-%d")
        wd = weekday_jp[date_obj.weekday()]
        label = f"{date_obj.month}/{date_obj.day}({wd}) {r['reservation_time']}〜 {r['menu_name']}"
        # ラベルは最大40文字
        if len(label) > 40:
            label = label[:37] + "..."
        buttons.append({
            "type": "button",
            "action": {"type": "postback", "label": label,
                       "data": f"action=cancel_confirm&rid={r['id']}"},
            "style": "secondary", "height": "sm", "margin": "sm",
        })
    return {
        "type": "bubble",
        "body": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "text", "text": "❌ キャンセルする予約を選択",
                 "weight": "bold", "size": "md"},
                {"type": "separator", "margin": "lg"},
                *buttons,
            ],
        },
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LINE送信ヘルパー
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def reply_flex(reply_token, alt_text, flex_content):
    line_bot_api.reply_message(reply_token,
        FlexSendMessage(alt_text=alt_text, contents=flex_content))

def reply_text(reply_token, text):
    line_bot_api.reply_message(reply_token, TextSendMessage(text=text))

def push_text(user_id, text):
    line_bot_api.push_message(user_id, TextSendMessage(text=text))

def push_flex(user_id, alt_text, flex_content):
    line_bot_api.push_message(user_id,
        FlexSendMessage(alt_text=alt_text, contents=flex_content))


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
    user_id = event.source.user_id
    db_get_or_create_customer(user_id)
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
        handle_check_reservation(event.reply_token, user_id)
    elif text in ["キャンセル", "取消"]:
        if user_id in reservation_sessions:
            del reservation_sessions[user_id]
        handle_list_cancel(event.reply_token, user_id)
    else:
        reply_flex(event.reply_token, f"{STORE_NAME} LINE予約", build_welcome_flex())


@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    data = dict(x.split("=", 1) for x in event.postback.data.split("&"))
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

    elif action == "closed_day":
        reply_text(event.reply_token, "🚫 その日は定休日です。別の日をお選びください。")

    elif action == "select_date" or action == "reselect_date":
        session = reservation_sessions.get(user_id, {})

        if action == "reselect_date":
            reply_flex(event.reply_token, "日付選択", build_date_flex())
            return

        # カレンダーピッカーから日付を取得
        selected_date = None
        if hasattr(event.postback, 'params') and event.postback.params:
            selected_date = event.postback.params.get('date')
        if not selected_date:
            selected_date = data.get("date")
        if not selected_date:
            reply_text(event.reply_token, "日付の取得に失敗しました。もう一度お試しください。")
            return

        # 定休日チェック（カレンダー選択後に判定）
        if db_is_closed_day(selected_date):
            reply_text(event.reply_token, "🚫 申し訳ありません、その日は定休日です。\n別の日をお選びください。")
            push_flex(user_id, "日付選択", build_date_flex())
            return

        session["date"] = selected_date
        session["step"] = "time"
        reservation_sessions[user_id] = session

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

        menu = next((m for m in MENU_ITEMS if m["id"] == session["menu"]), None)
        if not menu:
            reply_text(event.reply_token, "メニュー情報の取得に失敗しました。やり直してください。")
            return

        # DB保存用データ
        reservation_data = {
            "line_user_id": user_id,
            "guest_name": session.get("name", "未設定"),
            "phone": session.get("phone", ""),
            "menu_id": session["menu"],
            "menu_name": menu["name"],
            "guests": session["guests"],
            "reservation_date": session["date"],
            "reservation_time": session["time"],
            "duration_minutes": menu["duration"],
            "total_price": menu["price"] * session["guests"],
            "status": "confirmed",
        }

        # Googleカレンダーに登録
        cal_event_id = create_calendar_event(reservation_data)
        reservation_data["calendar_event_id"] = cal_event_id

        # DBに保存
        saved = db_save_reservation(reservation_data)
        if not saved:
            reply_text(event.reply_token, "予約の保存に失敗しました。もう一度お試しください。")
            return

        # 顧客情報を更新
        db_update_customer_visit(user_id, session.get("name"), session.get("phone"))

        # 完了メッセージ
        reply_flex(event.reply_token, "予約完了", build_complete_flex(saved))

        # オーナー通知
        notify_owner(saved)

        # セッションクリア
        del reservation_sessions[user_id]

    elif action == "cancel_flow":
        if user_id in reservation_sessions:
            del reservation_sessions[user_id]
        reply_text(event.reply_token, "予約フローをキャンセルしました。\n「予約」と送信するとやり直せます。")

    elif action == "check_reservation":
        handle_check_reservation(event.reply_token, user_id)

    elif action == "list_cancel":
        handle_list_cancel(event.reply_token, user_id)

    elif action == "cancel_confirm":
        rid = int(data["rid"])
        reply_flex(event.reply_token, "キャンセル確認", {
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "本当にキャンセルしますか？",
                     "weight": "bold", "size": "md", "wrap": True},
                    {"type": "text", "text": "キャンセルすると元に戻せません。",
                     "size": "sm", "color": "#999999", "margin": "md"},
                ],
            },
            "footer": {
                "type": "box", "layout": "horizontal", "spacing": "sm",
                "contents": [
                    {"type": "button", "action": {"type": "postback", "label": "はい、キャンセル",
                     "data": f"action=cancel_execute&rid={rid}"},
                     "style": "primary", "color": "#E05241", "height": "sm"},
                    {"type": "button", "action": {"type": "postback", "label": "戻る",
                     "data": "action=list_cancel"},
                     "style": "secondary", "height": "sm"},
                ],
            },
        })

    elif action == "cancel_execute":
        rid = int(data["rid"])

        # カレンダーからも削除
        try:
            rows = supabase_get("reservations", {"select": "*", "id": f"eq.{rid}"})
            if rows:
                delete_calendar_event(rows[0].get("calendar_event_id"))
        except Exception as e:
            logger.error(f"キャンセル時カレンダー削除エラー: {e}")

        success = db_cancel_reservation(rid, user_id)
        if success:
            reply_text(event.reply_token, "✅ 予約をキャンセルしました。\n「予約」と送信すると再予約できます。")
            # オーナーにキャンセル通知
            if OWNER_LINE_USER_ID and OWNER_LINE_USER_ID != "dummy":
                try:
                    push_text(OWNER_LINE_USER_ID, f"⚠️ 予約キャンセルがありました（予約番号: #{rid}）")
                except Exception:
                    pass
        else:
            reply_text(event.reply_token, "キャンセルに失敗しました。もう一度お試しください。")


def handle_check_reservation(reply_token, user_id):
    reservations = db_get_user_reservations(user_id)
    if not reservations:
        reply_text(reply_token, "現在、予約はありません。\n「予約」と送信すると予約できます。")
        return
    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]
    lines = ["📋 現在のご予約:\n"]
    for r in reservations:
        date_obj = datetime.datetime.strptime(r["reservation_date"], "%Y-%m-%d")
        lines.append(
            f"・{date_obj.month}/{date_obj.day}（{weekday_jp[date_obj.weekday()]}）"
            f" {r['reservation_time']}〜\n"
            f"  {r['menu_name']} / {r['guests']}名（#{r['id']}）"
        )
    reply_text(reply_token, "\n".join(lines))


def handle_list_cancel(reply_token, user_id):
    reservations = db_get_user_reservations(user_id)
    reply_flex(reply_token, "キャンセル選択", build_cancel_list_flex(reservations))


def notify_owner(reservation):
    if not OWNER_LINE_USER_ID or OWNER_LINE_USER_ID == "dummy":
        return
    date_obj = datetime.datetime.strptime(reservation["reservation_date"], "%Y-%m-%d")
    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]
    message = (
        f"🔔 新しい予約が入りました！\n\n"
        f"📅 {date_obj.month}/{date_obj.day}（{weekday_jp[date_obj.weekday()]}）"
        f"{reservation['reservation_time']}〜\n"
        f"👤 {reservation['guest_name']} 様\n"
        f"👥 {reservation['guests']}名\n"
        f"🍽️ {reservation['menu_name']}\n"
        f"📱 {reservation.get('phone', '未設定')}\n"
        f"💰 ¥{reservation.get('total_price', 0):,}\n"
        f"🔖 予約番号: #{reservation['id']}"
    )
    try:
        push_text(OWNER_LINE_USER_ID, message)
    except Exception as e:
        logger.error(f"オーナー通知エラー: {e}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 前日リマインド（DB版）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def send_reminders():
    reminders = db_get_tomorrow_reminders()
    weekday_jp = ["月", "火", "水", "木", "金", "土", "日"]
    for r in reminders:
        date_obj = datetime.datetime.strptime(r["reservation_date"], "%Y-%m-%d")
        message = (
            f"⏰ 明日のご予約リマインド\n\n"
            f"{STORE_NAME}より\n\n"
            f"📅 {date_obj.month}/{date_obj.day}（{weekday_jp[date_obj.weekday()]}）"
            f" {r['reservation_time']}〜\n"
            f"🍽️ {r['menu_name']}\n"
            f"👥 {r['guests']}名\n\n"
            f"ご来店をお待ちしております！\n"
            f"キャンセルはLINEトーク画面から可能です。"
        )
        try:
            push_text(r["line_user_id"], message)
            db_mark_reminded(r["id"])
            logger.info(f"リマインド送信: {r['guest_name']} {r['reservation_date']}")
        except Exception as e:
            logger.error(f"リマインド送信エラー: {e}")


scheduler = BackgroundScheduler(timezone="Asia/Tokyo")
scheduler.add_job(send_reminders, "cron", hour=18, minute=0)
scheduler.start()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# オーナーダッシュボード
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def check_dashboard_auth():
    """ダッシュボード認証チェック"""
    token = request.args.get("token") or request.headers.get("X-Dashboard-Token")
    if not token or token != DASHBOARD_PASSWORD:
        abort(401)


@app.route("/dashboard")
def dashboard_page():
    """ダッシュボード画面"""
    check_dashboard_auth()
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("{{ store_name }}", STORE_NAME)
    html = html.replace("{{ token }}", request.args.get("token", ""))
    return html


# --- 予約API ---
@app.route("/api/reservations")
def api_get_reservations():
    check_dashboard_auth()
    date_from = request.args.get("date_from", datetime.datetime.now(JST).strftime("%Y-%m-%d"))
    status_filter = request.args.get("status", "")
    params = {
        "select": "*",
        "reservation_date": f"gte.{date_from}",
        "order": "reservation_date.asc,reservation_time.asc",
    }
    if status_filter:
        params["status"] = f"eq.{status_filter}"
    try:
        rows = supabase_get("reservations", params)
        return jsonify({"data": rows})
    except Exception as e:
        logger.error(f"API予約取得エラー: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/reservations/<int:rid>/cancel", methods=["POST"])
def api_cancel_reservation(rid):
    check_dashboard_auth()
    try:
        # カレンダーからも削除
        rows = supabase_get("reservations", {"select": "*", "id": f"eq.{rid}"})
        if rows:
            delete_calendar_event(rows[0].get("calendar_event_id"))
            line_user_id = rows[0].get("line_user_id")

        res = supabase_patch("reservations", {"status": "cancelled"}, {"id": f"eq.{rid}"})

        # お客様にキャンセル通知
        if rows and line_user_id:
            try:
                r = rows[0]
                push_text(line_user_id,
                    f"⚠️ ご予約がキャンセルされました\n\n"
                    f"📅 {r['reservation_date']} {r['reservation_time']}〜\n"
                    f"🍽️ {r['menu_name']}\n"
                    f"👥 {r['guests']}名\n\n"
                    f"ご不明な点がございましたらお問い合わせください。")
            except Exception:
                pass

        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"APIキャンセルエラー: {e}")
        return jsonify({"error": str(e)}), 500


# --- 設定API ---
@app.route("/api/settings/booking-deadline")
def api_get_booking_deadline():
    check_dashboard_auth()
    try:
        hours = get_booking_deadline_hours()
        return jsonify({"value": hours})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/booking-deadline", methods=["POST"])
def api_set_booking_deadline():
    check_dashboard_auth()
    body = request.get_json()
    hours = body.get("hours", 2)
    try:
        db_set_setting("booking_deadline_hours", int(hours))
        return jsonify({"success": True, "value": int(hours)})
    except Exception as e:
        logger.error(f"API設定更新エラー: {e}")
        return jsonify({"error": str(e)}), 500


# --- 定休日API ---
@app.route("/api/closed-days")
def api_get_closed_days():
    check_dashboard_auth()
    try:
        rows = supabase_get("closed_days", {"select": "*", "order": "day_of_week.asc,closed_date.asc"})
        return jsonify({"data": rows})
    except Exception as e:
        logger.error(f"API定休日取得エラー: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/closed-days", methods=["POST"])
def api_add_closed_day():
    check_dashboard_auth()
    body = request.get_json()
    try:
        res = supabase_post("closed_days", body)
        return jsonify({"data": res})
    except Exception as e:
        logger.error(f"API定休日追加エラー: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/closed-days/<int:cid>", methods=["DELETE"])
def api_delete_closed_day(cid):
    check_dashboard_auth()
    try:
        url = f"{SUPABASE_URL}/rest/v1/closed_days"
        res = http_requests.delete(url, headers=SUPABASE_HEADERS, params={"id": f"eq.{cid}"})
        res.raise_for_status()
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"API定休日削除エラー: {e}")
        return jsonify({"error": str(e)}), 500


# --- 顧客API ---
@app.route("/api/customers")
def api_get_customers():
    check_dashboard_auth()
    try:
        rows = supabase_get("customers", {
            "select": "*",
            "order": "visit_count.desc,created_at.desc",
        })
        return jsonify({"data": rows})
    except Exception as e:
        logger.error(f"API顧客取得エラー: {e}")
        return jsonify({"error": str(e)}), 500


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 起動
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
