#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RDP Telegram Bot (Clean + Anti-spam)
- Chỉ còn chức năng tạo Windows RDP qua GitHub Actions + xem phiên gần nhất.
- Bỏ hoàn toàn Chat AI.
- Chống spam: 1 repo chỉ chạy 1 phiên RDP tại 1 thời điểm (global lock), lưu trạng thái vào repo.
- UI nghiêm túc, chữ đơn giản, không dùng "font lạ" gây lỗi.

ENV bắt buộc:
- TELEGRAM_BOT_TOKEN
- GH_PAT  (PAT có quyền repo: actions:read, actions:write, contents:read, contents:write)
- GITHUB_REPOSITORY (GitHub Actions tự set, dạng owner/repo)

ENV tuỳ chọn:
- WORKFLOW_FILE (default: WindowsRDP.yml)
- STATE_PATH (default: rdp_state.json)
- COOLDOWN_SECONDS (default: 60)
"""

import os
import time
import json
import base64
import logging
from typing import Optional, Dict, Any

import requests
import telebot
from telebot import types

# ---------------------- CONFIG ----------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
GH_PAT = os.environ.get("GH_PAT", "").strip()
REPO = os.environ.get("GITHUB_REPOSITORY", "").strip()   # owner/repo
WORKFLOW_FILE = os.environ.get("WORKFLOW_FILE", "WindowsRDP.yml").strip()
STATE_PATH = os.environ.get("STATE_PATH", "rdp_state.json").strip()
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "60").strip() or "60")

API = "https://api.github.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("rdp-bot")

if not BOT_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN")
if not GH_PAT:
    raise SystemExit("Missing GH_PAT")
if not REPO:
    raise SystemExit("Missing GITHUB_REPOSITORY (owner/repo)")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", disable_web_page_preview=True)

# in-memory cooldown per chat (anti spam bấm liên tục)
_last_click: Dict[int, float] = {}

# ---------------------- GITHUB HELPERS ----------------------

def gh_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "rdp-telegram-bot"
    }

def gh_get_json(url: str, params: Optional[dict] = None) -> Any:
    r = requests.get(url, headers=gh_headers(), params=params, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub GET failed {r.status_code}: {r.text[:300]}")
    return r.json()

def gh_post_json(url: str, payload: dict) -> Any:
    r = requests.post(url, headers=gh_headers(), json=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub POST failed {r.status_code}: {r.text[:300]}")
    return r.json()

def gh_put_json(url: str, payload: dict) -> Any:
    r = requests.put(url, headers=gh_headers(), json=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub PUT failed {r.status_code}: {r.text[:300]}")
    return r.json()

def read_repo_file(path: str) -> Optional[dict]:
    """Read JSON file from repo (default branch). Return dict or None if not found."""
    url = f"{API}/repos/{REPO}/contents/{path}"
    r = requests.get(url, headers=gh_headers(), timeout=30)
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise RuntimeError(f"Read file failed {r.status_code}: {r.text[:300]}")
    data = r.json()
    content_b64 = data.get("content", "")
    sha = data.get("sha", "")
    if not content_b64:
        return {"_sha": sha}
    raw = base64.b64decode(content_b64).decode("utf-8", errors="replace")
    obj = json.loads(raw) if raw.strip() else {}
    if isinstance(obj, dict):
        obj["_sha"] = sha
    return obj

def write_repo_file(path: str, obj: dict, message: str) -> None:
    """Write JSON file to repo using Contents API."""
    existing = read_repo_file(path)
    sha = existing.get("_sha") if isinstance(existing, dict) else None
    clean_obj = dict(obj)
    clean_obj.pop("_sha", None)

    payload = {
        "message": message,
        "content": base64.b64encode((json.dumps(clean_obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")).decode("utf-8"),
    }
    if sha:
        payload["sha"] = sha

    url = f"{API}/repos/{REPO}/contents/{path}"
    gh_put_json(url, payload)

def is_any_workflow_running() -> bool:
    """Check if there is any in-progress run for the workflow."""
    # list workflow runs by file name
    url = f"{API}/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/runs"
    data = gh_get_json(url, params={"per_page": 10})
    runs = data.get("workflow_runs", []) or []
    for run in runs:
        status = run.get("status")  # queued, in_progress, completed
        if status in ("queued", "in_progress"):
            return True
    return False

def dispatch_windows_rdp(chat_id: int, requested_by: int) -> None:
    url = f"{API}/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    payload = {
        "ref": "main",
        "inputs": {
            "chat_id": str(chat_id),
            "requested_by": str(requested_by),
        }
    }
    gh_post_json(url, payload)

# ---------------------- UI ----------------------

def main_menu() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🖥️ Tạo Windows RDP", callback_data="create_rdp"),
        types.InlineKeyboardButton("📌 Phiên gần nhất", callback_data="last_session"),
    )
    kb.add(
        types.InlineKeyboardButton("🧹 Reset trạng thái", callback_data="reset_state"),
        types.InlineKeyboardButton("❓ Hướng dẫn", callback_data="help"),
    )
    return kb

def send_home(chat_id: int) -> None:
    text = (
        "<b>RDP Bot</b>\n"
        "• Tạo Windows RDP bằng GitHub Actions\n"
        "• 1 phiên chạy tại 1 thời điểm (chống spam)\n\n"
        "Chọn chức năng bên dưới:"
    )
    bot.send_message(chat_id, text, reply_markup=main_menu())

def pretty_session(state: dict) -> str:
    # state format produced by workflow/bot
    status = (state or {}).get("status", "unknown")
    owner = (state or {}).get("owner_chat_id")
    started = (state or {}).get("started_at")
    endpoint = (state or {}).get("endpoint")
    username = (state or {}).get("username")
    password = (state or {}).get("password")
    web = (state or {}).get("web")

    lines = ["<b>Phiên gần nhất</b>"]
    lines.append(f"Trạng thái: <b>{status}</b>")
    if owner:
        lines.append(f"Owner chat_id: <code>{owner}</code>")
    if started:
        lines.append(f"Start: <code>{started}</code>")
    if endpoint:
        lines.append(f"RDP: <code>{endpoint}</code>")
    if username:
        lines.append(f"User: <code>{username}</code>")
    if password:
        lines.append(f"Pass: <code>{password}</code>")
    if web:
        lines.append(f"Web: <code>{web}</code>")
    return "\n".join(lines)

def cooldown_ok(chat_id: int) -> bool:
    now = time.time()
    last = _last_click.get(chat_id, 0)
    if now - last < COOLDOWN_SECONDS:
        return False
    _last_click[chat_id] = now
    return True

# ---------------------- HANDLERS ----------------------

@bot.message_handler(commands=["start", "menu"])
def on_start(msg):
    send_home(msg.chat.id)

@bot.callback_query_handler(func=lambda call: True)
def on_callback(call):
    chat_id = call.message.chat.id
    data = call.data

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    if data == "help":
        bot.send_message(
            chat_id,
            "<b>Hướng dẫn</b>\n"
            "1) Bấm <b>🖥️ Tạo Windows RDP</b>\n"
            "2) Đợi workflow chạy xong, hệ thống sẽ gửi thông tin RDP về Telegram\n"
            "3) Nếu bạn đã tắt phiên rồi, bấm <b>🧹 Reset trạng thái</b> để tạo phiên mới\n\n"
            "<i>Lưu ý:</i> Repo này chỉ cho chạy <b>1 phiên</b> tại 1 thời điểm."
        )
        return

    if data == "last_session":
        state = read_repo_file(STATE_PATH) or {}
        bot.send_message(chat_id, pretty_session(state))
        return

    if data == "reset_state":
        state = read_repo_file(STATE_PATH) or {}
        # chỉ owner mới reset để tránh phá của người khác
        owner = str(state.get("owner_chat_id", ""))
        if state.get("status") in ("running", "queued") and owner and owner != str(chat_id):
            bot.send_message(chat_id, "⛔ Phiên đang thuộc người khác. Bạn không thể reset.")
            return
        new_state = {
            "status": "stopped",
            "owner_chat_id": owner or str(chat_id),
            "updated_at": int(time.time()),
            "note": "reset by telegram"
        }
        write_repo_file(STATE_PATH, new_state, "Reset RDP state")
        bot.send_message(chat_id, "✅ Đã reset trạng thái. Bây giờ bạn có thể tạo phiên mới.")
        return

    if data == "create_rdp":
        if not cooldown_ok(chat_id):
            bot.send_message(chat_id, f"⏳ Bạn bấm nhanh quá. Đợi {COOLDOWN_SECONDS}s rồi thử lại.")
            return

        state = read_repo_file(STATE_PATH) or {}
        status = state.get("status", "stopped")

        # khóa theo file trạng thái
        if status in ("running", "queued"):
            owner = state.get("owner_chat_id", "")
            bot.send_message(
                chat_id,
                "⛔ Hiện đang có 1 phiên RDP đang chạy.\n"
                f"Owner: <code>{owner}</code>\n"
                "Hãy đợi phiên đó tắt xong hoặc bấm <b>🧹 Reset trạng thái</b> (nếu bạn là owner)."
            )
            return

        # khóa theo workflow in_progress (double-check)
        if is_any_workflow_running():
            bot.send_message(chat_id, "⛔ Workflow đang chạy/đang chờ. Đợi xong rồi tạo lại.")
            return

        # set state queued trước để chống spam
        queued_state = {
            "status": "queued",
            "owner_chat_id": str(chat_id),
            "started_at": int(time.time()),
            "updated_at": int(time.time()),
        }
        try:
            write_repo_file(STATE_PATH, queued_state, "Queue RDP session")
        except Exception as e:
            log.exception("Failed to write state before dispatch")
            bot.send_message(chat_id, f"⚠️ Không ghi được trạng thái vào repo: {e}")
            return

        # dispatch workflow
        try:
            dispatch_windows_rdp(chat_id=chat_id, requested_by=chat_id)
        except Exception as e:
            # rollback state
            try:
                write_repo_file(STATE_PATH, {"status": "stopped", "updated_at": int(time.time()), "note": "dispatch failed"}, "Stop RDP state (dispatch failed)")
            except Exception:
                pass
            bot.send_message(chat_id, f"❌ Tạo RDP thất bại: {e}")
            return

        bot.send_message(
            chat_id,
            "✅ Đã gửi yêu cầu tạo Windows RDP.\n"
            "⏳ Đợi workflow chạy xong, thông tin RDP sẽ được gửi về đây."
        )
        return

    # fallback
    send_home(chat_id)


def main():
    log.info("Bot started")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)

if __name__ == "__main__":
    main()
