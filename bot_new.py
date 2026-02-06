#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChatGPT RDP Bot (Clean Edition)
- Telegram bot: tạo Windows RDP bằng GitHub Actions + chat bằng ChatGPT (OpenAI).
- Không còn hệ thống điểm/tiền/referral nữa (free flow).

ENV cần có:
- TELEGRAM_BOT_TOKEN
- GH_PAT
- (optional) OPENAI_API_KEY  -> bật ChatGPT mode
- (optional) OPENAI_MODEL    -> default: gpt-5-mini
- (optional) GITHUB_REPOSITORY (GitHub Actions tự set)
- (optional) WORKFLOW_FILE   -> default: WindowsRDP.yml
- (optional) DEFAULT_LANG    -> "Tiếng Việt" / "English" (default: Tiếng Việt)
"""

import os
import time
import json
import logging
import random
import string
from dataclasses import dataclass, asdict
from typing import Dict, Optional

import requests
import telebot
from telebot import types

# OpenAI (ChatGPT)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # pip chưa cài hoặc user không dùng


# ---------------------- CONFIG ----------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GH_PAT = os.environ.get("GH_PAT", "").strip()
REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()  # ex: owner/repo
WORKFLOW_FILE = os.environ.get("WORKFLOW_FILE", "WindowsRDP.yml").strip()
DEFAULT_LANG = os.environ.get("DEFAULT_LANG", "Tiếng Việt").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini").strip()

if not BOT_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN")
if not GH_PAT:
    raise SystemExit("Missing GH_PAT")
if not REPO:
    # fallback cho trường hợp chạy local
    REPO = os.environ.get("REPO_FALLBACK", "YOUR_GH_USER/YOUR_REPO").strip()

GITHUB_API = "https://api.github.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("ChatGPTRDPBot")


# ---------------------- STATE (in-memory) ----------------------
# Vì bot chạy trên GitHub Actions (6h reset), state này sẽ reset theo mỗi lần job restart.

@dataclass
class LastRequest:
    os_version: str
    num_machines: str
    username: str
    password: str
    language: str
    requested_at: float

@dataclass
class UserState:
    step: str = "idle"  # idle | pick_os | pick_count
    temp_os: Optional[str] = None
    temp_lang: str = DEFAULT_LANG
    chat_mode: bool = False
    last_request: Optional[LastRequest] = None
    last_response_id: Optional[str] = None  # OpenAI conversation via previous_response_id

USERS: Dict[int, UserState] = {}


def st(uid: int) -> UserState:
    if uid not in USERS:
        USERS[uid] = UserState()
    return USERS[uid]


# ---------------------- UI ----------------------

OS_OPTIONS = [
    ("Windows Server 2025", "Windows Server 2025 (Docker - 4vCPU | 8GB RAM)"),
    ("Windows Server 2022", "Windows Server 2022 (Docker - 4vCPU | 8GB RAM)"),
    ("Windows Server 2019", "Windows Server 2019 (Docker - 4vCPU | 8GB RAM)"),
    ("Windows Server 2012", "Windows Server 2012 (Docker - 4vCPU | 8GB RAM)"),
    ("Windows 11 Pro", "Windows 11 Professional (Docker - 4vCPU | 8GB RAM)"),
    ("Windows 10 Pro", "Windows 10 Professional (Docker - 4vCPU | 8GB RAM)"),
]

COUNT_OPTIONS = ["1", "2", "3", "4", "5"]

MAIN_BTNS = [
    "🖥️ Tạo Windows RDP",
    "📌 Phiên gần nhất",
    "💬 ChatGPT",
    "🧹 Xoá chat",
    "❓ Hướng dẫn",
]

CHAT_BTNS = [
    "⬅️ Menu",
    "🧹 Xoá chat",
]

def main_kb():
    kb = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    kb.add(*(types.KeyboardButton(x) for x in MAIN_BTNS))
    return kb

def chat_kb():
    kb = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    kb.add(*(types.KeyboardButton(x) for x in CHAT_BTNS))
    return kb

def os_inline_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    for short, full in OS_OPTIONS:
        kb.add(types.InlineKeyboardButton(short, callback_data=f"os|{full}"))
    kb.add(types.InlineKeyboardButton("🌐 Đổi ngôn ngữ", callback_data="lang|toggle"))
    return kb

def count_inline_kb():
    kb = types.InlineKeyboardMarkup(row_width=5)
    for c in COUNT_OPTIONS:
        kb.add(types.InlineKeyboardButton(c, callback_data=f"count|{c}"))
    kb.add(types.InlineKeyboardButton("⬅️ Quay lại", callback_data="nav|back_to_os"))
    return kb

def esc_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------- GitHub Actions dispatch ----------------------

def dispatch_windows_rdp(
    chat_id: int,
    os_version: str,
    num_machines: str,
    username: str,
    password: str,
    language: str,
) -> None:
    url = f"{GITHUB_API}/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
    }
    payload = {
        "ref": "main",
        "inputs": {
            "os_version": os_version,
            "num_machines": num_machines,
            "language": language,
            "chat_id": str(chat_id),
            "username": username,
            "password": password,
        }
    }
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
    if r.status_code != 204:
        raise RuntimeError(f"GitHub dispatch failed: {r.status_code} {r.text[:500]}")


def gen_password() -> str:
    # Windows-friendly, đủ mạnh, tránh ký tự "lạ" dễ lỗi
    core = "".join(random.choices(string.ascii_letters + string.digits, k=10))
    tail = random.choice("!@#") + random.choice(string.digits)
    return f"Win-{core}{tail}"


# ---------------------- OpenAI ChatGPT ----------------------

def openai_client() -> Optional["OpenAI"]:
    if not OPENAI_API_KEY or OpenAI is None:
        return None
    # OpenAI() tự đọc OPENAI_API_KEY từ env theo docs
    return OpenAI()

def chatgpt_reply(uid: int, user_text: str) -> str:
    client = openai_client()
    if client is None:
        return (
            "⚠️ <b>ChatGPT chưa bật</b>\n\n"
            "Bạn cần set secret <code>OPENAI_API_KEY</code> trong GitHub repo.\n"
            "Xong rồi bấm lại <b>💬 ChatGPT</b> nha."
        )

    u = st(uid)

    instructions = (
        "Bạn là ChatGPT. Trả lời tự nhiên, dễ hiểu, vui vẻ. "
        "Ưu tiên tiếng Việt. Nếu user hỏi mơ hồ thì hỏi lại 1 câu ngắn gọn."
    )

    kwargs = dict(
        model=OPENAI_MODEL,
        instructions=instructions,
        input=user_text,
        max_output_tokens=700,
    )
    if u.last_response_id:
        kwargs["previous_response_id"] = u.last_response_id

    resp = client.responses.create(**kwargs)
    u.last_response_id = getattr(resp, "id", None)

    out = getattr(resp, "output_text", None) or ""
    out = out.strip()

    if not out:
        out = "🤖 Mình bị trống output mất rồi. Bạn nói lại câu đó được không?"
    return esc_html(out)


# ---------------------- Telegram bot ----------------------

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")


def send_welcome(chat_id: int, name: str):
    msg = (
        f"👋 Hey <b>{esc_html(name)}</b>!\n\n"
        "Mình là <b>ChatGPT RDP Bot</b>.\n"
        "Bạn muốn làm gì nè?\n\n"
        "🖥️ <b>Tạo Windows RDP</b> (Public IP)\n"
        "💬 <b>ChatGPT</b> (hỏi gì cũng được)\n\n"
        "Chọn nút bên dưới nha 👇"
    )
    bot.send_message(chat_id, msg, reply_markup=main_kb())


@bot.message_handler(commands=["start", "menu"])
def cmd_start(m):
    u = st(m.chat.id)
    u.step = "idle"
    u.chat_mode = False
    send_welcome(m.chat.id, m.from_user.first_name or "bạn")


@bot.message_handler(func=lambda m: m.text == "🖥️ Tạo Windows RDP")
def cmd_create_rdp(m):
    uid = m.chat.id
    u = st(uid)
    u.step = "pick_os"
    u.chat_mode = False

    msg = (
        "🖥️ <b>Tạo Windows RDP</b>\n\n"
        "1) Chọn bản Windows bạn muốn\n"
        "2) Chọn số lượng máy\n"
        "3) Bot sẽ bắn workflow và <b>workflow sẽ nhắn bạn IP public</b> khi xong.\n\n"
        "👉 Chọn Windows nè:"
    )
    bot.send_message(uid, msg, reply_markup=types.ReplyKeyboardRemove())
    bot.send_message(uid, "🧩 <b>Chọn hệ điều hành:</b>", reply_markup=os_inline_kb())


@bot.message_handler(func=lambda m: m.text == "📌 Phiên gần nhất")
def cmd_last_session(m):
    uid = m.chat.id
    u = st(uid)
    if not u.last_request:
        bot.send_message(uid, "📭 Chưa có phiên nào hết. Bấm <b>🖥️ Tạo Windows RDP</b> để tạo nha.", reply_markup=main_kb())
        return

    lr = u.last_request
    when = time.strftime("%H:%M:%S %d/%m/%Y", time.localtime(lr.requested_at))
    msg = (
        "📌 <b>Phiên gần nhất</b>\n\n"
        f"🪟 OS: <b>{esc_html(lr.os_version)}</b>\n"
        f"🧱 Số máy: <b>{esc_html(lr.num_machines)}</b>\n"
        f"👤 User: <code>{esc_html(lr.username)}</code>\n"
        f"🔐 Pass: <code>{esc_html(lr.password)}</code>\n"
        f"🌐 Ngôn ngữ: <b>{esc_html(lr.language)}</b>\n"
        f"🕒 Lúc: <i>{when}</i>\n\n"
        "ℹ️ IP public sẽ nằm trong tin nhắn do workflow gửi. "
        "Nếu bạn lỡ trôi tin nhắn thì vào <b>Actions</b> trong GitHub để xem log."
    )
    bot.send_message(uid, msg, reply_markup=main_kb())


@bot.message_handler(func=lambda m: m.text == "🧹 Xoá chat")
def cmd_reset_chat(m):
    uid = m.chat.id
    u = st(uid)
    u.last_response_id = None
    bot.send_message(uid, "🧹 OK! Đã reset hội thoại ChatGPT.", reply_markup=(chat_kb() if u.chat_mode else main_kb()))


@bot.message_handler(func=lambda m: m.text == "💬 ChatGPT")
def cmd_chatgpt(m):
    uid = m.chat.id
    u = st(uid)
    u.chat_mode = True
    u.step = "idle"

    msg = (
        "💬 <b>ChatGPT mode: ON</b>\n\n"
        "Giờ bạn cứ nhắn như chat bình thường, mình trả lời.\n"
        "Muốn thoát thì bấm <b>⬅️ Menu</b>."
    )
    bot.send_message(uid, msg, reply_markup=chat_kb())


@bot.message_handler(func=lambda m: m.text == "⬅️ Menu")
def cmd_back_menu(m):
    uid = m.chat.id
    u = st(uid)
    u.chat_mode = False
    u.step = "idle"
    send_welcome(uid, m.from_user.first_name or "bạn")


@bot.message_handler(func=lambda m: m.text == "❓ Hướng dẫn")
def cmd_help(m):
    uid = m.chat.id
    msg = (
        "❓ <b>Hướng dẫn nhanh</b>\n\n"
        "🖥️ <b>Tạo Windows RDP</b>\n"
        "• Chọn OS + số máy\n"
        "• Bot bắn workflow\n"
        "• Workflow gửi lại IP public + port + web viewer\n\n"
        "💬 <b>ChatGPT</b>\n"
        "• Bấm <b>💬 ChatGPT</b> rồi nhắn câu hỏi\n"
        "• Nếu báo chưa bật: thêm secret <code>OPENAI_API_KEY</code>\n\n"
        "Tips: Không share IP/pass cho người lạ nha 😄"
    )
    bot.send_message(uid, msg, reply_markup=main_kb())


@bot.callback_query_handler(func=lambda c: True)
def cb_handler(c):
    uid = c.message.chat.id
    u = st(uid)

    try:
        if c.data.startswith("lang|"):
            # Toggle language
            u.temp_lang = "English" if u.temp_lang == "Tiếng Việt" else "Tiếng Việt"
            bot.answer_callback_query(c.id, f"Language: {u.temp_lang}")
            # Refresh OS picker message
            bot.edit_message_reply_markup(uid, c.message.message_id, reply_markup=os_inline_kb())
            return

        if c.data.startswith("os|"):
            os_full = c.data.split("|", 1)[1]
            u.temp_os = os_full
            u.step = "pick_count"
            bot.answer_callback_query(c.id, "OK, chọn số máy 👇")
            bot.send_message(uid, f"✅ OS đã chọn: <b>{esc_html(os_full)}</b>\n\nGiờ chọn <b>số lượng máy</b>:", reply_markup=count_inline_kb())
            return

        if c.data.startswith("nav|back_to_os"):
            u.step = "pick_os"
            bot.answer_callback_query(c.id)
            bot.send_message(uid, "⬅️ Quay lại: Chọn hệ điều hành:", reply_markup=os_inline_kb())
            return

        if c.data.startswith("count|"):
            if not u.temp_os:
                bot.answer_callback_query(c.id, "Bạn chọn OS trước nha!")
                return

            count = c.data.split("|", 1)[1]
            bot.answer_callback_query(c.id, "Đang bắn workflow...")

            username = "Admin"
            password = gen_password()
            language = u.temp_lang or DEFAULT_LANG

            # Dispatch
            dispatch_windows_rdp(
                chat_id=uid,
                os_version=u.temp_os,
                num_machines=count,
                username=username,
                password=password,
                language=language,
            )

            u.last_request = LastRequest(
                os_version=u.temp_os,
                num_machines=count,
                username=username,
                password=password,
                language=language,
                requested_at=time.time(),
            )
            u.step = "idle"

            msg = (
                "🚀 <b>Đã gửi yêu cầu tạo RDP!</b>\n\n"
                f"🪟 OS: <b>{esc_html(u.temp_os)}</b>\n"
                f"🧱 Số máy: <b>{esc_html(count)}</b>\n"
                f"👤 User: <code>{esc_html(username)}</code>\n"
                f"🔐 Pass: <code>{esc_html(password)}</code>\n"
                f"🌐 Lang: <b>{esc_html(language)}</b>\n\n"
                "⏳ Chờ vài phút nhé. Khi IP public sẵn sàng, <b>workflow sẽ nhắn thẳng cho bạn</b>.\n"
                "Nếu không thấy tin nhắn: vào tab <b>Actions</b> của repo để xem log."
            )
            bot.send_message(uid, msg, reply_markup=main_kb())
            return

    except Exception as e:
        log.exception("Callback error")
        bot.answer_callback_query(c.id, "❌ Lỗi rồi, thử lại nha!")
        bot.send_message(uid, f"❌ <b>Lỗi:</b> <code>{esc_html(str(e))}</code>", reply_markup=main_kb())


@bot.message_handler(func=lambda m: True, content_types=["text"])
def fallback_text(m):
    uid = m.chat.id
    u = st(uid)

    # Nếu đang chat mode -> gửi qua ChatGPT
    if u.chat_mode:
        try:
            bot.send_chat_action(uid, "typing")
            reply = chatgpt_reply(uid, m.text)
            bot.send_message(uid, reply, reply_markup=chat_kb())
        except Exception as e:
            log.exception("ChatGPT error")
            bot.send_message(uid, f"❌ Lỗi ChatGPT: <code>{esc_html(str(e))}</code>", reply_markup=chat_kb())
        return

    # Ngoài chat mode: gợi menu
    bot.send_message(uid, "👀 Mình chưa hiểu lệnh đó. Bấm nút menu nha 👇", reply_markup=main_kb())


if __name__ == "__main__":
    log.info("Bot starting...")
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=10)
        except Exception as e:
            log.error(f"Polling error: {e}")
            time.sleep(5)
