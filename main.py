import os
import re
import threading
import time
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import (
    JoinEvent,
    MessageAction,
    MessageEvent,
    QuickReply,
    QuickReplyButton,
    SourceGroup,
    SourceRoom,
    TextMessage,
    TextSendMessage,
)

import db
from ai_parser import analyze_message, guess_datetime_and_recurrence, make_chat_reply
from utility_tools import handle_utility_message


load_dotenv()
db.init_db()

app = Flask(__name__)
app.logger.setLevel("INFO")

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

WELCOME_MESSAGE = """嗨 😆

歡迎加入我的 AI 助手！

我不只是一般 LINE Bot，
而是可以陪你聊天、記住事情、幫你管理提醒的 AI 秘書 ✨

你可以直接自然聊天，例如：

🗣️「10分鐘後提醒我喝水」

🗣️「明天早上8點叫我起床」

🗣️「幫我記得月底繳費」

🗣️「我有哪些提醒？」

我會自己理解你的意思 😆

————————————

✨ 功能介紹

✅ AI 智能提醒
✅ 自然語言理解
✅ AI 聊天模式
✅ 長期記憶
✅ 每日提醒
✅ 提醒管理
✅ 智能刪除提醒

————————————

直接輸入訊息就可以開始使用啦 😆"""


def conversation_id(event):
    source = event.source
    if isinstance(source, SourceGroup):
        return source.group_id
    if isinstance(source, SourceRoom):
        return source.room_id
    return source.user_id


def actor_id(event):
    return getattr(event.source, "user_id", None) or conversation_id(event)


def user_scope_id(event):
    conv = conversation_id(event)
    user = actor_id(event)
    return user if conv == user else f"{conv}:{user}"


def reply_text(reply_token, text, quick_reply=None):
    if not line_bot_api:
        app.logger.error("LINE_CHANNEL_ACCESS_TOKEN is missing.")
        return
    line_bot_api.reply_message(reply_token, TextSendMessage(text=text, quick_reply=quick_reply))


def push_text(target_id, text):
    if not line_bot_api:
        app.logger.error("LINE_CHANNEL_ACCESS_TOKEN is missing.")
        return
    line_bot_api.push_message(target_id, TextSendMessage(text=text))


def quick_reply(labels):
    return QuickReply(items=[QuickReplyButton(action=MessageAction(label=label, text=label)) for label in labels])


def reminder_quick_reply():
    return quick_reply(["設定提醒", "取消"])


def repeat_quick_reply():
    return quick_reply(["要", "不要"])


def format_reminder_time(value):
    try:
        return datetime.fromisoformat(value).strftime("%Y/%m/%d %H:%M:%S")
    except ValueError:
        return value


def format_recurrence(value):
    if not value:
        return ""
    if value == "daily":
        return "（每天）"
    if value.startswith("weekly:"):
        names = ["一", "二", "三", "四", "五", "六", "日"]
        return f"（每週{names[int(value.split(':', 1)[1])]}）"
    if value.startswith("monthly:"):
        return f"（每月{value.split(':', 1)[1]}號）"
    if value.startswith("interval:"):
        return f"（每{value.split(':', 1)[1]}分鐘，直到你說好）"
    return ""


def format_reminder_list(reminders):
    if not reminders:
        return "目前沒有待提醒事項。"
    lines = ["你的提醒："]
    for reminder in reminders:
        lines.append(
            f"{reminder['id']}. {format_reminder_time(reminder['remind_at'])} "
            f"{format_recurrence(reminder.get('recurrence'))}- {reminder['title']}"
        )
    lines.append("\n刪除單筆：刪除提醒 3")
    lines.append("刪除範圍：刪除提醒 1~5")
    lines.append("刪除多筆：刪除提醒 1,3,5")
    return "\n".join(lines)


def parse_delete_command(text):
    match = re.fullmatch(r"(?:刪除|取消)提醒\s*(.+)", text)
    if not match:
        return None
    target = match.group(1).strip()
    range_match = re.fullmatch(r"(\d+)\s*(?:~|-|到|至)\s*(\d+)", target)
    if range_match:
        return ("range", int(range_match.group(1)), int(range_match.group(2)))
    ids = [int(value) for value in re.findall(r"\d+", target)]
    return ("ids", ids) if ids else None


def is_reminder_query(text):
    return bool(re.search(r"(我的提醒|查看提醒|提醒列表|有哪些提醒|有什麼提醒|今天還有什麼|待辦|還有什麼事)", text))


def is_memory_query(text):
    return bool(re.search(r"(你記得我什麼|你記得哪些|我的記憶|記憶列表)", text))


def extract_forget_keyword(text):
    match = re.fullmatch(r"(?:忘記|刪除記憶|不要記得)\s*(.+)", text)
    return match.group(1).strip() if match else None


def parse_repeat_minutes(text):
    match = re.search(r"(\d+)\s*(分鐘|分)", text)
    if match:
        return max(1, int(match.group(1)))
    if re.fullmatch(r"\d+", text):
        return max(1, int(text))
    return None


def ask_repeat(event, sid, reminder_id):
    db.save_repeat_question(sid, reminder_id)
    reply_text(
        event.reply_token,
        "要重複提醒嗎？\n如果要，我會每隔幾分鐘提醒一次，直到你說「好」才結束。",
        quick_reply=repeat_quick_reply(),
    )


def add_reminder_and_ask_repeat(event, sid, target_id, title, remind_at, recurrence=None):
    reminder_id = db.add_reminder(sid, target_id, title, remind_at, recurrence)
    reply = (
        "提醒設定好了。\n"
        f"內容：{title}\n"
        f"時間：{format_reminder_time(remind_at)} {format_recurrence(recurrence)}"
    )
    if recurrence:
        reply_text(event.reply_token, reply)
    else:
        db.save_repeat_question(sid, reminder_id)
        reply_text(
            event.reply_token,
            reply + "\n\n要重複提醒嗎？",
            quick_reply=repeat_quick_reply(),
        )


def handle_repeat_flow(event, sid, text):
    question = db.get_repeat_question(sid)
    if not question:
        return False

    if text in {"不要", "不用", "不用了", "否", "no", "No"}:
        db.clear_repeat_question(sid)
        reply_text(event.reply_token, "好，這個提醒只提醒一次。")
        return True

    if text in {"要", "好", "需要", "yes", "Yes"}:
        reply_text(event.reply_token, "好，要每幾分鐘提醒一次？例如：5分鐘")
        return True

    minutes = parse_repeat_minutes(text)
    if minutes:
        ok = db.set_reminder_recurrence(sid, question["reminder_id"], f"interval:{minutes}")
        db.clear_repeat_question(sid)
        reply_text(
            event.reply_token,
            f"設定好了，我會每 {minutes} 分鐘提醒一次，直到你說「好」才結束。"
            if ok else "找不到剛剛那個提醒，可能已經被取消了。",
        )
        return True

    return False


def maybe_complete_draft(event, sid, text):
    draft = db.get_reminder_draft(sid)
    if not draft:
        return False
    remind_at, recurrence = guess_datetime_and_recurrence(text, datetime.now().astimezone())
    if not remind_at:
        return False
    recurrence = recurrence or draft.get("recurrence")
    db.clear_reminder_draft(sid)
    add_reminder_and_ask_repeat(
        event,
        sid,
        draft["target_id"],
        draft["title"],
        remind_at.isoformat(timespec="seconds"),
        recurrence,
    )
    return True


def handle_reminder_analysis(event, sid, target_id, text, analysis):
    if analysis["intent"] != "reminder" or analysis["confidence"] < 0.55:
        return False
    title = analysis.get("title") or "提醒事項"
    remind_at = analysis.get("remind_at")
    recurrence = analysis.get("recurrence")
    if not remind_at:
        db.save_reminder_draft(sid, target_id, title, text, recurrence)
        reply_text(event.reply_token, f"可以，{title} 要什麼時候提醒你？")
        return True
    if analysis["confidence"] >= 0.8:
        add_reminder_and_ask_repeat(event, sid, target_id, title, remind_at, recurrence)
        return True
    db.save_pending_reminder(sid, target_id, title, remind_at, text, recurrence)
    reply_text(
        event.reply_token,
        "要幫你設定提醒嗎？\n"
        f"內容：{title}\n"
        f"時間：{format_reminder_time(remind_at)} {format_recurrence(recurrence)}",
        quick_reply=reminder_quick_reply(),
    )
    return True


@app.route("/", methods=["GET"])
def home():
    missing = []
    if not LINE_CHANNEL_ACCESS_TOKEN:
        missing.append("LINE_CHANNEL_ACCESS_TOKEN")
    if not LINE_CHANNEL_SECRET:
        missing.append("LINE_CHANNEL_SECRET")
    if missing:
        return "Bot server is running, but missing: " + ", ".join(missing), 200
    return "LINE reminder/chat bot is running.", 200


@app.route("/callback", methods=["POST"])
def callback():
    if not handler:
        app.logger.error("LINE_CHANNEL_SECRET is missing.")
        abort(500)
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    app.logger.info("Webhook received: %s", body[:500])
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK", 200


def handle_text(event):
    conv_id = conversation_id(event)
    sid = user_scope_id(event)
    text = event.message.text.strip()
    app.logger.info("Text message from %s: %s", sid, text)

    if text in {"好", "好了", "完成", "結束任務", "不用提醒了"}:
        count = db.complete_interval_reminders(sid)
        if count:
            db.clear_repeat_question(sid)
            reply_text(event.reply_token, f"好，已結束 {count} 個重複提醒任務。")
            return

    if handle_repeat_flow(event, sid, text):
        return

    if maybe_complete_draft(event, sid, text):
        return

    if text in {"功能", "help", "Help"}:
        reply_text(
            event.reply_token,
            "可以用這些：\n"
            "聊天BOT：開啟聊天\n"
            "關閉聊天BOT：停止聊天\n"
            "我的提醒 / 我有哪些提醒：查看提醒\n"
            "刪除提醒 1~5：取消一段範圍\n"
            "你記得我什麼：查看記憶\n"
            "台北天氣 / 現在幾點 / 計算 12*3：實用工具",
        )
        return

    if text == "聊天BOT":
        db.set_chat_mode(conv_id, True)
        reply_text(event.reply_token, "好，我在。你可以直接跟我聊。")
        return

    if text in {"關閉聊天BOT", "停止聊天BOT", "退出聊天BOT"}:
        db.set_chat_mode(conv_id, False)
        reply_text(event.reply_token, "好，我先安靜一點。有事再叫我。")
        return

    if is_reminder_query(text):
        reply_text(event.reply_token, format_reminder_list(db.list_pending_reminders(sid)))
        return

    delete_command = parse_delete_command(text)
    if delete_command:
        count = (
            db.delete_pending_reminder_range(sid, delete_command[1], delete_command[2])
            if delete_command[0] == "range"
            else db.delete_pending_reminders(sid, delete_command[1])
        )
        reply_text(event.reply_token, f"已取消 {count} 個提醒。" if count else "找不到符合的待提醒編號。")
        return

    if text == "清空提醒":
        count = db.clear_pending_reminders(sid)
        reply_text(event.reply_token, f"已取消 {count} 個提醒。" if count else "目前沒有待提醒事項。")
        return

    if is_memory_query(text):
        memories = db.get_memories(sid, limit=20)
        reply_text(
            event.reply_token,
            "我目前還沒有記住你的特別資訊。"
            if not memories else "我記得這些：\n" + "\n".join(f"- {item['content']}" for item in memories),
        )
        return

    forget_keyword = extract_forget_keyword(text)
    if forget_keyword:
        count = db.delete_memories_like(sid, forget_keyword)
        reply_text(event.reply_token, f"已刪除 {count} 筆相關記憶。")
        return

    if text == "清除記憶":
        count = db.clear_memories(sid)
        reply_text(event.reply_token, f"已清除 {count} 筆記憶。")
        return

    if text == "設定提醒":
        pending = db.confirm_pending_reminder(sid)
        if not pending:
            reply_text(event.reply_token, "目前沒有等你確認的提醒。")
            return
        add_reminder_and_ask_repeat(
            event,
            sid,
            pending["target_id"] or conv_id,
            pending["title"],
            pending["remind_at"],
            pending.get("recurrence"),
        )
        return

    if text == "取消":
        pending = db.get_pending_reminder(sid)
        db.clear_pending_reminder(sid)
        db.clear_reminder_draft(sid)
        db.clear_repeat_question(sid)
        reply_text(event.reply_token, "好，先不設定。" if pending else "目前沒有待確認的提醒。")
        return

    utility_reply = handle_utility_message(text)
    if utility_reply:
        reply_text(event.reply_token, utility_reply)
        return

    analysis = analyze_message(text)
    app.logger.info("Analysis: %s", analysis)
    if handle_reminder_analysis(event, sid, conv_id, text, analysis):
        return

    if analysis.get("memory"):
        db.add_memory(sid, "auto", analysis["memory"])

    if db.is_chat_mode(conv_id) or (analysis["intent"] == "chat" and analysis["confidence"] >= 0.58):
        db.add_message(sid, "user", text)
        reply = make_chat_reply(text, db.get_memories(sid), db.recent_messages(sid))
        db.add_message(sid, "assistant", reply)
        reply_text(event.reply_token, reply)
        return

    if text in {"測試", "test", "Test"}:
        reply_text(event.reply_token, "我有收到。現在 webhook 是通的。")


def handle_join(event):
    reply_text(event.reply_token, WELCOME_MESSAGE)


if handler:
    handler.add(MessageEvent, message=TextMessage)(handle_text)
    handler.add(JoinEvent)(handle_join)


def reminder_worker():
    while True:
        try:
            for reminder in db.due_reminders():
                try:
                    suffix = format_recurrence(reminder.get("recurrence"))
                    push_text(reminder["target_id"] or reminder["source_id"], f"提醒你：{reminder['title']} {suffix}".strip())
                    db.mark_reminder_done(reminder["id"], reminder["remind_at"], reminder.get("recurrence"))
                except LineBotApiError:
                    app.logger.exception("Failed to push reminder.")
        finally:
            time.sleep(5)


threading.Thread(target=reminder_worker, daemon=True).start()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
