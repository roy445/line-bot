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

我可以陪你聊天、記住事情、管理提醒、查天氣、整理待辦。

你可以直接說：
「10分鐘後提醒我喝水」
「明天早上8點叫我起床」
「新增任務 數學作業」
「台北天氣」
「我有哪些提醒？」

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


def is_group_event(event):
    return isinstance(event.source, (SourceGroup, SourceRoom))


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
    for item in reminders:
        category = f" [{item['category']}]" if item.get("category") else ""
        lines.append(
            f"{item['id']}. {format_reminder_time(item['remind_at'])} "
            f"{format_recurrence(item.get('recurrence'))}{category} - {item['title']}"
        )
    lines.append("\n刪除：刪除提醒 3 / 刪除提醒 1~5 / 刪除提醒 1,3,5")
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
    return bool(re.search(r"我的提醒|查看提醒|提醒列表|有哪些提醒|有什麼提醒|今天還有什麼|待辦|還有什麼事", text))


def parse_repeat_minutes(text):
    match = re.search(r"(\d+)\s*(分鐘|分)", text)
    if match:
        return max(1, int(match.group(1)))
    if re.fullmatch(r"\d+", text):
        return max(1, int(text))
    return None


def add_reminder_and_ask_repeat(event, sid, target_id, title, remind_at, recurrence=None, category=None):
    reminder_id = db.add_reminder(sid, target_id, title, remind_at, recurrence, category)
    reply = (
        "提醒設定好了。\n"
        f"內容：{title}\n"
        f"時間：{format_reminder_time(remind_at)} {format_recurrence(recurrence)}"
    )
    if category:
        reply += f"\n分類：{category}"
    if recurrence:
        reply_text(event.reply_token, reply)
        return
    db.save_repeat_question(sid, reminder_id)
    reply_text(event.reply_token, reply + "\n\n要重複提醒嗎？", quick_reply=quick_reply(["要", "不要"]))


def handle_repeat_flow(event, sid, text):
    question = db.get_repeat_question(sid)
    if not question:
        return False
    if text in {"不要", "不用", "不用了", "否", "no", "No"}:
        db.clear_repeat_question(sid)
        reply_text(event.reply_token, "好，這個提醒只提醒一次。")
        return True
    if text in {"要", "需要", "yes", "Yes"}:
        reply_text(event.reply_token, "好，要每幾分鐘提醒一次？例如：5分鐘")
        return True
    minutes = parse_repeat_minutes(text)
    if minutes:
        ok = db.set_reminder_recurrence(sid, question["reminder_id"], f"interval:{minutes}")
        db.clear_repeat_question(sid)
        reply_text(event.reply_token, f"設定好了，我會每 {minutes} 分鐘提醒一次，直到你說「好」。" if ok else "找不到剛剛那個提醒。")
        return True
    return False


def maybe_complete_draft(event, sid, text):
    draft = db.get_reminder_draft(sid)
    if not draft:
        return False
    remind_at, recurrence = guess_datetime_and_recurrence(text, datetime.now().astimezone())
    if not remind_at:
        return False
    db.clear_reminder_draft(sid)
    add_reminder_and_ask_repeat(
        event,
        sid,
        draft["target_id"],
        draft["title"],
        remind_at.isoformat(timespec="seconds"),
        recurrence or draft.get("recurrence"),
    )
    return True


def handle_reminder_analysis(event, sid, target_id, text, analysis):
    if analysis["intent"] != "reminder" or analysis["confidence"] < 0.55:
        return False
    title = analysis.get("title") or "提醒事項"
    remind_at = analysis.get("remind_at")
    recurrence = analysis.get("recurrence")
    category = analysis.get("category")
    if not remind_at:
        db.save_reminder_draft(sid, target_id, title, text, recurrence)
        reply_text(event.reply_token, f"可以，{title} 要什麼時候提醒你？")
        return True
    if analysis["confidence"] >= 0.8:
        add_reminder_and_ask_repeat(event, sid, target_id, title, remind_at, recurrence, category)
        return True
    db.save_pending_reminder(sid, target_id, title, remind_at, text, recurrence, category)
    reply_text(
        event.reply_token,
        "要幫你設定提醒嗎？\n"
        f"內容：{title}\n"
        f"時間：{format_reminder_time(remind_at)} {format_recurrence(recurrence)}",
        quick_reply=quick_reply(["設定提醒", "取消"]),
    )
    return True


def handle_task_commands(event, sid, text):
    match = re.fullmatch(r"(?:新增任務|加入任務)\s+(.+)", text)
    if match:
        title = match.group(1).strip()
        category = None
        cat = re.search(r"分類\s*([^\s，,。]+)", title)
        if cat:
            category = cat.group(1)
            title = re.sub(r"分類\s*[^\s，,。]+", "", title).strip()
        task_id = db.add_task(sid, title, category)
        reply_text(event.reply_token, f"任務加好了：{task_id}. {title}")
        return True
    if text in {"我的任務", "任務列表"}:
        tasks = db.list_tasks(sid)
        if not tasks:
            reply_text(event.reply_token, "目前沒有未完成任務。")
        else:
            reply_text(event.reply_token, "你的任務：\n" + "\n".join(f"{t['id']}. {t['title']}" for t in tasks))
        return True
    match = re.fullmatch(r"(?:完成任務|任務完成)\s*(\d+)", text)
    if match:
        ok = db.complete_task(sid, int(match.group(1)))
        reply_text(event.reply_token, "完成了。" if ok else "找不到這個未完成任務。")
        return True
    return False


def handle_settings(event, sid, text):
    match = re.fullmatch(r"我的城市是\s*(.+)", text)
    if match:
        db.set_user_setting(sid, "city", match.group(1).strip())
        reply_text(event.reply_token, f"好，我記住你的城市是 {match.group(1).strip()}。")
        return True
    match = re.fullmatch(r"語氣\s*(.+)", text)
    if match:
        db.set_user_setting(sid, "tone", match.group(1).strip())
        reply_text(event.reply_token, "好，我會照這個語氣跟你說話。")
        return True
    match = re.fullmatch(r"每日摘要\s*(\d{1,2})[:：](\d{2})", text)
    if match:
        value = f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
        db.set_user_setting(sid, "daily_summary_time", value)
        reply_text(event.reply_token, f"好，每天 {value} 我會整理提醒和任務給你。")
        return True
    return False


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
        abort(500)
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK", 200


def handle_text(event):
    conv_id = conversation_id(event)
    sid = user_scope_id(event)
    text = event.message.text.strip()

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
    if handle_task_commands(event, sid, text):
        return
    if handle_settings(event, sid, text):
        return

    if text in {"功能", "help", "Help"}:
        reply_text(
            event.reply_token,
            "可以用這些：\n"
            "聊天BOT / 關閉聊天BOT\n"
            "我的提醒 / 刪除提醒 1~5\n"
            "新增任務 數學作業 / 我的任務 / 完成任務 1\n"
            "每日摘要 08:00\n"
            "我的城市是台中 / 語氣可愛一點\n"
            "台北天氣 / 現在幾點 / 計算 12*3",
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
        count = db.delete_pending_reminder_range(sid, delete_command[1], delete_command[2]) if delete_command[0] == "range" else db.delete_pending_reminders(sid, delete_command[1])
        reply_text(event.reply_token, f"已取消 {count} 個提醒。" if count else "找不到符合的提醒。")
        return

    if text == "清空提醒":
        count = db.clear_pending_reminders(sid)
        reply_text(event.reply_token, f"已取消 {count} 個提醒。" if count else "目前沒有待提醒事項。")
        return

    if text == "設定提醒":
        pending = db.confirm_pending_reminder(sid)
        if not pending:
            reply_text(event.reply_token, "目前沒有等你確認的提醒。")
            return
        add_reminder_and_ask_repeat(event, sid, pending["target_id"] or conv_id, pending["title"], pending["remind_at"], pending.get("recurrence"), pending.get("category"))
        return
    if text == "取消":
        db.clear_pending_reminder(sid)
        db.clear_reminder_draft(sid)
        db.clear_repeat_question(sid)
        reply_text(event.reply_token, "好，先不設定。")
        return

    utility_reply = handle_utility_message(text)
    if utility_reply:
        reply_text(event.reply_token, utility_reply)
        return

    analysis = analyze_message(text)
    if handle_reminder_analysis(event, sid, conv_id, text, analysis):
        return
    if analysis.get("memory"):
        db.add_memory(sid, "auto", analysis["memory"])

    should_chat = db.is_chat_mode(conv_id) or (analysis["intent"] == "chat" and analysis["confidence"] >= 0.58 and not is_group_event(event))
    if should_chat:
        settings = db.get_user_settings(sid)
        db.add_message(sid, "user", text)
        reply = make_chat_reply(text, db.get_memories(sid), db.recent_messages(sid), settings)
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
                    body = f"提醒你：{reminder['title']} {suffix}".strip()
                    if "天氣" in reminder["title"]:
                        body += "\n\n" + (handle_utility_message("天氣") or "")
                    push_text(reminder["target_id"] or reminder["source_id"], body)
                    db.mark_reminder_done(reminder["id"], reminder["remind_at"], reminder.get("recurrence"))
                except LineBotApiError:
                    app.logger.exception("Failed to push reminder.")
            send_due_summaries()
        finally:
            time.sleep(5)


def send_due_summaries():
    now = datetime.now().astimezone()
    current = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    for target in db.summary_targets():
        if target["daily_summary_time"] != current or db.summary_already_sent(target["source_id"], today):
            continue
        reminders = db.list_pending_reminders(target["source_id"], limit=10)
        tasks = db.list_tasks(target["source_id"], limit=10)
        lines = ["今天摘要："]
        lines.append(format_reminder_list(reminders))
        lines.append("\n任務：")
        lines.extend([f"{task['id']}. {task['title']}" for task in tasks] or ["目前沒有未完成任務。"])
        push_text(target["source_id"].split(":", 1)[0], "\n".join(lines))
        db.mark_summary_sent(target["source_id"], today)


threading.Thread(target=reminder_worker, daemon=True).start()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
