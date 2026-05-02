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
import re
import calendar as cal_module
from zoneinfo import ZoneInfo

from flask import Flask, request, abort, render_template, jsonify, session as flask_session, redirect, url_for
from markupsafe import escape as html_escape
from functools import wraps
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
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(32).hex())
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# LINE API
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
OWNER_LINE_USER_ID = os.environ.get("OWNER_LINE_USER_ID", "")

# Supabase（service_role keyを使用 — RLSをバイパスしてサーバーから全操作可能）
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", os.environ.get("SUPABASE_KEY", ""))

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

# ダッシュボード認証
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
if not DASHBOARD_PASSWORD:
    logger.warning("⚠️ DASHBOARD_PASSWORD が未設定です。必ず環境変数で設定してください。")

BRAND_COLOR = "#E05241"  # 飲食店版（赤系）

JST = ZoneInfo("Asia/Tokyo")


def dashboard_auth_required(f):
    """ダッシュボード用の認証デコレータ（セッションCookie方式）"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not flask_session.get("dashboard_authenticated"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("dashboard_login"))
        return f(*args, **kwargs)
    return decorated

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 入力バリデーション・サニタイズ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def sanitize_text(text, max_length=100):
    """テキスト入力のサニタイズ（XSS対策 + 長さ制限）"""
    if not isinstance(text, str):
        return ""
    text = str(html_escape(text.strip()))
    return text[:max_length]


def validate_phone(phone):
    """電話番号バリデーション（日本の電話番号形式）"""
    if not phone:
        return True, ""  # 空は許容（任意入力の場合）
    cleaned = re.sub(r"[\s\-‐‑‒–—―ー－]", "", phone)
    if re.match(r"^(0\d{9,10}|\+81\d{9,10})$", cleaned):
        return True, cleaned
    return False, ""


def validate_name(name):
    """名前バリデーション"""
    if not name or not name.strip():
        return False, ""
    name = name.strip()
    if len(name) > 50:
        return False, ""
    # HTMLタグを除去
    sanitized = re.sub(r"<[^>]+>", "", name)
    return True, sanitized


def validate_positive_int(value, max_val=999):
    """正の整数バリデーション"""
    try:
        n = int(value)
        return 1 <= n <= max_val, n
    except (ValueError, TypeError):
        return False, 0


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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 予約セッション管理（Supabase永続化）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SESSION_TTL_MINUTES = 30


def session_get(uid):
    """ユーザーの予約セッションを取得。期限切れなら削除してNone。"""
    try:
        rows = supabase_get("reservation_sessions", {
            "select": "*",
            "line_user_id": f"eq.{uid}",
        })
        if not rows:
            return None
        row = rows[0]
        expires = datetime.datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        if expires < datetime.datetime.now(datetime.timezone.utc):
            session_delete(uid)
            return None
        return row["session_data"]
    except Exception as e:
        logger.error(f"session_get error: {e}")
        return None


def session_set(uid, data):
    """ユーザーの予約セッションを作成または上書き。"""
    try:
        expires = (datetime.datetime.now(datetime.timezone.utc)
                   + datetime.timedelta(minutes=SESSION_TTL_MINUTES)).isoformat()
        payload = {
            "line_user_id": uid,
            "session_data": data,
            "expires_at": expires,
        }
        headers = {**SUPABASE_HEADERS, "Prefer": "return=representation,resolution=merge-duplicates"}
        url = f"{SUPABASE_URL}/rest/v1/reservation_sessions"
        res = http_requests.post(url, headers=headers, json=payload)
        res.raise_for_status()
    except Exception as e:
        logger.error(f"session_set error: {e}")


def session_update(uid, updates):
    """既存セッションの一部フィールドを更新。"""
    current = session_get(uid)
    if current is None:
        return None
    current.update(updates)
    session_set(uid, current)
    return current


def session_delete(uid):
    """ユーザーの予約セッションを削除。"""
    try:
        supabase_delete("reservation_sessions", {"line_user_id": f"eq.{uid}"})
    except Exception:
        pass


def cleanup_expired_sessions():
    """期限切れセッションを一括削除。"""
    try:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        supabase_delete("reservation_sessions", {"expires_at": f"lt.{now}"})
    except Exception as e:
        logger.error(f"Session cleanup error: {e}")


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
    """カレンダー型の日付選択（3ヶ月分カルーセル）"""
    today = datetime.datetime.now(JST)
    today_date = today.date()

    # 定休日データを一括取得（APIコール削減）
    try:
        all_closed = supabase_get("closed_days", {"select": "*"})
    except Exception:
        all_closed = []

    recurring_dows = set()
    specific_dates = set()
    for c in all_closed:
        if c.get("is_recurring") and c.get("day_of_week") is not None:
            recurring_dows.add(c["day_of_week"])
        if c.get("closed_date"):
            specific_dates.add(c["closed_date"])

    def is_closed(d):
        if d.weekday() in recurring_dows:
            return True
        return d.strftime("%Y-%m-%d") in specific_dates

    weekday_labels = ["月", "火", "水", "木", "金", "土", "日"]
    bubbles = []

    for month_offset in range(3):
        year = today.year
        month = today.month + month_offset
        while month > 12:
            year += 1
            month -= 12

        weeks = cal_module.monthcalendar(year, month)

        # 曜日ヘッダー
        header_row = {
            "type": "box", "layout": "horizontal", "margin": "md",
            "contents": [
                {"type": "text", "text": wd, "size": "xxs", "align": "center", "flex": 1,
                 "color": "#E05241" if i == 6 else "#1565c0" if i == 5 else "#888888"}
                for i, wd in enumerate(weekday_labels)
            ],
        }

        rows = [
            {"type": "text", "text": f"{year}年{month}月", "weight": "bold",
             "size": "md", "align": "center"},
            {"type": "separator", "margin": "sm"},
            header_row,
        ]

        for week in weeks:
            row_contents = []
            for i, day in enumerate(week):
                if day == 0:
                    row_contents.append({
                        "type": "box", "layout": "vertical", "flex": 1,
                        "contents": [{"type": "text", "text": " ", "size": "sm", "align": "center"}],
                    })
                else:
                    d = datetime.date(year, month, day)
                    date_str = d.strftime("%Y-%m-%d")
                    is_past = d < today_date
                    closed = is_closed(d)

                    if is_past:
                        row_contents.append({
                            "type": "box", "layout": "vertical", "flex": 1,
                            "contents": [{"type": "text", "text": str(day), "size": "sm",
                                          "align": "center", "color": "#DDDDDD"}],
                        })
                    elif closed:
                        row_contents.append({
                            "type": "box", "layout": "vertical", "flex": 1,
                            "action": {"type": "postback", "label": "定休日",
                                       "data": "action=closed_day"},
                            "contents": [{"type": "text", "text": str(day), "size": "sm",
                                          "align": "center", "color": "#CCCCCC",
                                          "decoration": "line-through"}],
                        })
                    elif d == today_date:
                        row_contents.append({
                            "type": "box", "layout": "vertical", "flex": 1,
                            "action": {"type": "postback", "label": str(day),
                                       "data": f"action=select_date&date={date_str}"},
                            "contents": [{"type": "text", "text": str(day), "size": "sm",
                                          "align": "center", "color": "#FFFFFF",
                                          "weight": "bold"}],
                            "backgroundColor": "#E05241", "cornerRadius": "14px",
                        })
                    else:
                        color = "#E05241" if i == 6 else "#1565c0" if i == 5 else "#333333"
                        row_contents.append({
                            "type": "box", "layout": "vertical", "flex": 1,
                            "action": {"type": "postback", "label": str(day),
                                       "data": f"action=select_date&date={date_str}"},
                            "contents": [{"type": "text", "text": str(day), "size": "sm",
                                          "align": "center", "color": color}],
                        })

            rows.append({
                "type": "box", "layout": "horizontal",
                "contents": row_contents, "margin": "sm",
            })

        bubbles.append({
            "type": "bubble", "size": "kilo",
            "body": {
                "type": "box", "layout": "vertical",
                "contents": rows, "paddingAll": "12px",
            },
        })

    return {"type": "carousel", "contents": bubbles}


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
# プライバシーポリシー・利用規約
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@app.route("/privacy")
def privacy_policy():
    return render_template("privacy.html", store_name=STORE_NAME, updated_date=datetime.datetime.now(JST).strftime("%Y年%m月%d日"))


@app.route("/terms")
def terms_of_service():
    return render_template("terms.html", store_name=STORE_NAME, updated_date=datetime.datetime.now(JST).strftime("%Y年%m月%d日"))


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
    """ヘルスチェック（Render監視用）— DB接続確認付き"""
    status = {"app": "ok", "database": "ok"}
    try:
        supabase_get("store_settings", {"select": "key", "limit": "1"})
    except Exception as e:
        status["database"] = f"error: {str(e)[:100]}"
        return jsonify(status), 503
    return jsonify(status), 200


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

    session = session_get(user_id)
    if session:
        if session["step"] == "name":
            valid, sanitized_name = validate_name(text)
            if not valid:
                reply_text(event.reply_token, "⚠️ お名前を正しく入力してください（50文字以内）")
                return
            session_update(user_id, {"name": sanitized_name, "step": "phone"})
            reply_text(event.reply_token, "📱 お電話番号を入力してください（例: 090-1234-5678）")
            return
        elif session["step"] == "phone":
            valid, cleaned_phone = validate_phone(text)
            if not valid:
                reply_text(event.reply_token, "⚠️ 電話番号の形式が正しくありません。\n例: 090-1234-5678")
                return
            session_update(user_id, {"phone": cleaned_phone or text, "step": "confirm"})
            session["phone"] = cleaned_phone or text
            session["step"] = "confirm"
            reply_flex(event.reply_token, "予約内容の確認", build_confirm_flex(session))
            return

    if text in ["予約", "予約する", "予約したい"]:
        reply_flex(event.reply_token, f"{STORE_NAME} LINE予約", build_welcome_flex())
    elif text in ["メニュー", "コース"]:
        session_set(user_id, {"step": "menu"})
        reply_flex(event.reply_token, "メニュー選択", build_menu_flex())
    elif text in ["確認", "予約確認"]:
        handle_check_reservation(event.reply_token, user_id)
    elif text in ["キャンセル", "取消"]:
        session_delete(user_id)
        handle_list_cancel(event.reply_token, user_id)
    else:
        #reply_flex(event.reply_token, f"{STORE_NAME} LINE予約", build_welcome_flex())
        return


@handler.add(PostbackEvent)
def handle_postback(event):
    user_id = event.source.user_id
    data = dict(x.split("=", 1) for x in event.postback.data.split("&"))
    action = data.get("action")

    if action == "start_reservation":
        session_set(user_id, {"step": "menu", "user_id": user_id})
        reply_flex(event.reply_token, "メニュー選択", build_menu_flex())

    elif action == "select_menu":
        session = session_get(user_id) or {"user_id": user_id}
        session["menu"] = data["menu_id"]
        session["step"] = "guests"
        session_set(user_id, session)
        reply_flex(event.reply_token, "人数選択", build_guests_flex())

    elif action == "select_guests":
        valid, guest_count = validate_positive_int(data.get("guests", 0), max_val=MAX_SEATS)
        if not valid:
            reply_text(event.reply_token, "⚠️ 人数が正しくありません。もう一度お試しください。")
            return
        session = session_get(user_id) or {}
        session["guests"] = guest_count
        session["step"] = "date"
        session_set(user_id, session)
        reply_text(event.reply_token, "📅 カレンダーを準備しています...\n少々お待ちください")
        push_flex(user_id, "日付選択", build_date_flex())

    elif action == "closed_day":
        reply_text(event.reply_token, "🚫 その日は定休日です。別の日をお選びください。")

    elif action == "select_date" or action == "reselect_date":
        session = session_get(user_id) or {}

        if action == "reselect_date":
            reply_flex(event.reply_token, "日付選択", build_date_flex())
            return

        # 日付を取得
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
        session_set(user_id, session)

        reply_text(event.reply_token, "🔍 空き状況を確認しています...\n少々お待ちください")
        menu = next((m for m in MENU_ITEMS if m["id"] == session.get("menu")), None)
        duration = menu["duration"] if menu else 60
        slots = get_available_slots(session["date"], session["guests"], duration)
        push_flex(user_id, "時間選択", build_time_flex(slots))

    elif action == "select_time":
        session = session_get(user_id) or {}
        session["time"] = data["time"]
        session["step"] = "name"
        session_set(user_id, session)
        reply_text(event.reply_token, "✏️ ご予約のお名前を入力してください")

    elif action == "confirm_reservation":
        session = session_get(user_id)
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
        session_delete(user_id)

    elif action == "cancel_flow":
        session_delete(user_id)
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
scheduler.add_job(cleanup_expired_sessions, "interval", minutes=10)
scheduler.start()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# オーナーダッシュボード
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ログイン</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,sans-serif;background:#f5f5f5;display:flex;align-items:center;justify-content:center;min-height:100vh}
.login{background:#fff;border-radius:16px;padding:40px 32px;width:90%%;max-width:380px;box-shadow:0 2px 12px rgba(0,0,0,.1);text-align:center}
.login h1{font-size:20px;margin-bottom:8px;color:#333}
.login small{color:#888;font-size:13px}
.login input{width:100%%;padding:12px;border:1px solid #ddd;border-radius:8px;font-size:16px;margin-top:20px}
.login input:focus{outline:none;border-color:#E05241}
.login button{width:100%%;padding:12px;background:#E05241;color:#fff;border:none;border-radius:8px;font-size:16px;font-weight:600;margin-top:12px;cursor:pointer}
.login button:hover{background:#c0392b}
.error{color:#c62828;font-size:13px;margin-top:12px}
</style></head><body>
<div class="login">
<h1>🍽️ %s</h1><small>オーナーダッシュボード</small>
<form method="POST"><input type="password" name="password" placeholder="パスワードを入力" autofocus>
<button type="submit">ログイン</button></form>
%s</div></body></html>"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# オーナーダッシュボード（セキュリティ強化版：ブルートフォース対策込）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ログイン試行の記録用（メモリ保持のため再起動でリセットされます）
LOGIN_ATTEMPTS = {}  # 形式: { "ip": {"count": 0, "lock_until": datetime} }
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION = datetime.timedelta(minutes=15)

def get_client_ip():
    """Render等のプロキシ環境下で正しい接続元IPを取得"""
    if request.headers.get('X-Forwarded-For'):
        # プロキシ経由の場合、一番左のIPがクライアントのIP
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr

@app.route("/dashboard/login", methods=["GET", "POST"])
def dashboard_login():
    ip = get_client_ip()
    now = datetime.datetime.now(JST)

    # 1. IPごとのロック状態をチェック
    if ip in LOGIN_ATTEMPTS:
        lock_until = LOGIN_ATTEMPTS[ip].get("lock_until")
        if lock_until and now < lock_until:
            wait_min = int((lock_until - now).total_seconds() / 60) + 1
            error_html = f'<div class="error">セキュリティロック中：あと{wait_min}分試行できません</div>'
            return LOGIN_PAGE % (STORE_NAME, error_html), 403

    if request.method == "POST":
        pw = request.form.get("password", "")
        
        if pw and pw == DASHBOARD_PASSWORD:
            # ログイン成功：当該IPの試行記録をクリア
            if ip in LOGIN_ATTEMPTS:
                del LOGIN_ATTEMPTS[ip]
            flask_session["dashboard_authenticated"] = True
            return redirect(url_for("dashboard"))
        
        # ログイン失敗：試行回数を記録
        if ip not in LOGIN_ATTEMPTS:
            LOGIN_ATTEMPTS[ip] = {"count": 0, "lock_until": None}
        
        LOGIN_ATTEMPTS[ip]["count"] += 1
        remaining = MAX_LOGIN_ATTEMPTS - LOGIN_ATTEMPTS[ip]["count"]

        if LOGIN_ATTEMPTS[ip]["count"] >= MAX_LOGIN_ATTEMPTS:
            # 上限に達したためロック時間を設定
            LOGIN_ATTEMPTS[ip]["lock_until"] = now + LOCKOUT_DURATION
            error_html = '<div class="error">失敗回数が上限に達しました。15分間ロックされます。</div>'
            logger.warning(f"🚨 セキュリティ警告：IP {ip} からのログイン試行が上限に達し、ロックされました。")
        else:
            error_html = f'<div class="error">パスワードが違います（あと{remaining}回でロック）</div>'
        
        return LOGIN_PAGE % (STORE_NAME, error_html), 401

    return LOGIN_PAGE % (STORE_NAME, "")


@app.route("/dashboard/logout")
def dashboard_logout():
    flask_session.clear()
    return redirect(url_for("dashboard_login"))


@app.route("/dashboard")
@dashboard_auth_required
def dashboard():
    return render_template("dashboard.html", store_name=STORE_NAME)


# --- 予約API ---
@app.route("/api/reservations")
@dashboard_auth_required
def api_get_reservations():
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
@dashboard_auth_required
def api_cancel_reservation(rid):
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
@dashboard_auth_required
def api_get_booking_deadline():
    try:
        hours = get_booking_deadline_hours()
        return jsonify({"value": hours})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/booking-deadline", methods=["POST"])
@dashboard_auth_required
def api_set_booking_deadline():
    body = request.get_json()
    hours = body.get("hours", 2)
    valid, hours_int = validate_positive_int(hours, max_val=24)
    if not valid and hours != 0:
        return jsonify({"error": "無効な値です（0〜24）"}), 400
    hours_int = int(hours) if hours == 0 else hours_int
    try:
        db_set_setting("booking_deadline_hours", hours_int)
        return jsonify({"success": True, "value": hours_int})
    except Exception as e:
        logger.error(f"API設定更新エラー: {e}")
        return jsonify({"error": str(e)}), 500


# --- 定休日API ---
@app.route("/api/closed-days")
@dashboard_auth_required
def api_get_closed_days():
    try:
        rows = supabase_get("closed_days", {"select": "*", "order": "day_of_week.asc,closed_date.asc"})
        return jsonify({"data": rows})
    except Exception as e:
        logger.error(f"API定休日取得エラー: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/closed-days", methods=["POST"])
@dashboard_auth_required
def api_add_closed_day():
    body = request.get_json()
    if not body:
        return jsonify({"error": "リクエストボディが空です"}), 400
    # サニタイズ
    safe_body = {}
    if "day_of_week" in body:
        valid, dow = validate_positive_int(body["day_of_week"], max_val=6)
        if body["day_of_week"] == 0:
            dow = 0
            valid = True
        if not valid:
            return jsonify({"error": "曜日の値が不正です（0〜6）"}), 400
        safe_body["day_of_week"] = dow
    if "closed_date" in body:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(body["closed_date"])):
            return jsonify({"error": "日付の形式が不正です（YYYY-MM-DD）"}), 400
        safe_body["closed_date"] = body["closed_date"]
    safe_body["reason"] = sanitize_text(body.get("reason", "定休日"), max_length=100)
    safe_body["is_recurring"] = bool(body.get("is_recurring", False))
    try:
        res = supabase_post("closed_days", safe_body)
        return jsonify({"data": res})
    except Exception as e:
        logger.error(f"API定休日追加エラー: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/closed-days/<int:cid>", methods=["DELETE"])
@dashboard_auth_required
def api_delete_closed_day(cid):
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
@dashboard_auth_required
def api_get_customers():
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
