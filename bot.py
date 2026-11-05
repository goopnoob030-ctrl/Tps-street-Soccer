import os
import re
import json
import time
import sqlite3
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

TOKEN = os.getenv("BOT_TOKEN")
ROOT_ADMIN = int(os.getenv("ADMIN_ID", "8695966053"))
DB_FILE = os.getenv("DB_FILE", "tps_street_soccer.db")

CLUBS = [
    "🇪🇸 Реал Мадрид", "🇪🇸 Барселона", "🏴 Манчестер Сити",
    "🏴 Ливерпуль", "🇫🇷 ПСЖ", "🇩🇪 Бавария", "🇮🇹 Интер",
    "🇮🇹 Милан", "🏴 Арсенал", "🇪🇸 Атлетико Мадрид",
    "🏴 Манчестер Юнайтед", "🏴 Челси", "🇩🇪 Боруссия Дортмунд",
    "🇮🇹 Ювентус", "🇪🇸 Севилья", "🇫🇷 Марсель"
]
POSITIONS = ["Вратарь", "Защитник", "Полузащитник", "Нападающий"]

db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row
lock = threading.RLock()

def init_db():
    with lock:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS players(
            user_id INTEGER PRIMARY KEY, username TEXT, roblox_username TEXT,
            roblox_id INTEGER, registration_status TEXT DEFAULT 'unregistered',
            club TEXT, position TEXT, career_status TEXT DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS clubs(
            name TEXT PRIMARY KEY, owner_id INTEGER, closed INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT,
            payload TEXT, status TEXT DEFAULT 'pending', created_at REAL
        );
        CREATE TABLE IF NOT EXISTS transfers(
            id INTEGER PRIMARY KEY AUTOINCREMENT, player_id INTEGER,
            old_club TEXT, new_club TEXT, status TEXT DEFAULT 'invited'
        );
        CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        """)
        db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (ROOT_ADMIN,))
        for name in CLUBS:
            db.execute("INSERT OR IGNORE INTO clubs(name,owner_id,closed) VALUES(?,NULL,0)", (name,))
        db.commit()

def setting(key):
    with lock:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

def set_setting(key, value):
    with lock:
        db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value))
        )
        db.commit()

def is_admin(uid):
    with lock:
        return db.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone() is not None

def ensure_player(user):
    with lock:
        db.execute(
            "INSERT OR IGNORE INTO players(user_id,username) VALUES(?,?)",
            (user.id, user.username)
        )
        db.execute("UPDATE players SET username=? WHERE user_id=?", (user.username, user.id))
        db.commit()

def player(uid):
    with lock:
        return db.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()

def registered(uid):
    return is_admin(uid) or bool(player(uid) and player(uid)["registration_status"] == "approved")

def username(user):
    return "@" + user.username if user.username else f"ID {user.id}"

def owner_club(uid):
    with lock:
        return db.execute("SELECT * FROM clubs WHERE owner_id=?", (uid,)).fetchone()

def menu(uid):
    rows = [
        [InlineKeyboardButton("⚽ Взять клуб", callback_data="clubs")],
        [InlineKeyboardButton("🔎 Искать клуб", callback_data="search")],
        [InlineKeyboardButton("📢 Объявления", callback_data="ads")],
        [InlineKeyboardButton("👤 Профиль", callback_data="profile")],
    ]
    if owner_club(uid):
        rows.insert(2, [InlineKeyboardButton("⚙️ Управление клубом", callback_data="management")])
    if is_admin(uid):
        rows.append([InlineKeyboardButton("👑 Админ-панель", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

async def send_group(context, text, keyboard):
    group_id = setting("admin_group_id")
    if not group_id:
        return False
    try:
        await context.bot.send_message(int(group_id), text, reply_markup=keyboard)
        return True
    except Exception:
        return False

async def create_request(context, uid, kind, payload, description):
    with lock:
        cur = db.execute(
            "INSERT INTO requests(user_id,kind,payload,status,created_at) VALUES(?,?,?,'pending',?)",
            (uid, kind, json.dumps(payload, ensure_ascii=False), time.time())
        )
        db.commit()
        rid = cur.lastrowid
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Принять", callback_data=f"mod_yes_{rid}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"mod_no_{rid}")
    ]])
    ok = await send_group(context, f"📥 ЗАЯВКА #{rid}\n\n{description}\n\nID: {uid}", kb)
    if not ok:
        with lock:
            db.execute("UPDATE requests SET status='no_group' WHERE id=?", (rid,))
            db.commit()
        return None
    return rid

def lookup_roblox(name):
    body = json.dumps({"usernames": [name], "excludeBannedUsers": False}).encode()
    req = urllib.request.Request(
        "https://users.roblox.com/v1/usernames/users", data=body,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            data = json.loads(response.read().decode())
        return data.get("data", [None])[0]
    except Exception:
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_player(user)
    if not registered(user.id):
        p = player(user.id)
        if p["registration_status"] == "pending":
            await update.message.reply_text("⏳ Твоя регистрация уже отправлена администраторам. Дождись решения.")
        else:
            context.user_data["flow"] = "registration"
            await update.message.reply_text(
                "📝 РЕГИСТРАЦИЯ TPS STREET SOCCER\n\n"
                "Отправь точный Roblox username (не Display Name)."
            )
        return
    await update.message.reply_text(
        "⚽ TPS STREET SOCCER\nДобро пожаловать!\nВыбери действие:",
        reply_markup=menu(user.id)
    )

async def setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text("Добавь бота в админскую группу и отправь /setgroup там.")
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return
    set_setting("admin_group_id", update.effective_chat.id)
    await update.message.reply_text("✅ Группа заявок подключена.")

async def setchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /setchannel @username_канала")
        return
    channel = context.args[0].strip()
    set_setting("tm_channel", channel)
    await update.message.reply_text(f"✅ Канал ТМ установлен: {channel}")

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid, data = q.from_user.id, q.data
    ensure_player(q.from_user)

    if data.startswith(("mod_yes_", "mod_no_")):
        if not is_admin(uid):
            await q.answer("Нет доступа", show_alert=True)
            return
        await moderate(q, context, int(data.rsplit("_", 1)[1]), data.startswith("mod_yes_"))
        return

    if not registered(uid):
        await q.edit_message_text("⛔ Сначала пройди регистрацию через /start.")
        return

    p = player(uid)
    if data == "home":
        await q.edit_message_text("⚽ Главное меню", reply_markup=menu(uid))
    elif data == "clubs":
        with lock:
            clubs = db.execute("SELECT * FROM clubs ORDER BY rowid").fetchall()
        text = "⚽ СПИСОК КЛУБОВ\n\n"
        rows = []
        for c in clubs:
            text += f"{'🔒' if c['owner_id'] or c['closed'] else '🟢'} {c['name']}\n"
            if not c["owner_id"] and not c["closed"]:
                rows.append([InlineKeyboardButton(c["name"], callback_data="take_" + c["name"])])
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="home")])
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith("take_"):
        club = data[5:]
        if p["club"]:
            await q.edit_message_text("❌ Ты уже состоишь в клубе.")
            return
        with lock:
            c = db.execute("SELECT * FROM clubs WHERE name=?", (club,)).fetchone()
        if not c or c["owner_id"] or c["closed"]:
            await q.edit_message_text("❌ Этот клуб уже занят или закрыт.")
            return
        rid = await create_request(
            context, uid, "take_club", {"club": club},
            f"⚽ ЗАЯВКА НА КЛУБ\nКлуб: {club}\nRoblox: {p['roblox_username']}\nTelegram: {username(q.from_user)}"
        )
        await q.edit_message_text("✅ Заявка отправлена в админскую группу." if rid else "❌ Не удалось отправить. Проверь /setgroup.")
    elif data == "search":
        context.user_data["flow"] = "free_agent"
        await q.edit_message_text("🔎 ИСКАТЬ КЛУБ\nНапиши короткое объявление о себе. Оно уйдёт администраторам на проверку.")
    elif data == "ads":
        await q.edit_message_text(
            "📢 ОБЪЯВЛЕНИЯ",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔎 Поиск игроков", callback_data="player_ad")],
                [InlineKeyboardButton("📣 Реклама канала", callback_data="channel_ad")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="home")]
            ])
        )
    elif data in ("player_ad", "channel_ad"):
        if data == "player_ad" and not owner_club(uid):
            await q.edit_message_text("⛔ Искать игроков могут только владельцы клубов.")
            return
        context.user_data["flow"] = data
        await q.edit_message_text("Отправь текст объявления. Оно попадёт в админскую группу на проверку.")
    elif data == "management":
        c = owner_club(uid)
        if not c:
            await q.edit_message_text("⛔ Управление доступно только владельцам клубов.")
            return
        await q.edit_message_text(
            f"⚙️ УПРАВЛЕНИЕ КЛУБОМ\n{c['name']}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 Состав", callback_data="squad")],
                [InlineKeyboardButton("🔄 Предложить трансфер", callback_data="transfer")],
                [InlineKeyboardButton("🚪 Исключить игрока", callback_data="kick")],
                [InlineKeyboardButton("👑 Передать клуб", callback_data="owner_change")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="home")]
            ])
        )
    elif data == "squad":
        c = owner_club(uid)
        with lock:
            members = db.execute("SELECT roblox_username,position,user_id FROM players WHERE club=?", (c["name"],)).fetchall() if c else []
        text = f"👥 СОСТАВ {c['name'] if c else ''}\n\n"
        text += "\n".join(f"• {m['roblox_username']} — {m['position'] or 'позиция не указана'}" for m in members) or "Состав пуст."
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="management")]]))
    elif data in ("transfer", "kick", "owner_change"):
        context.user_data["flow"] = data + "_target"
        prompts = {
            "transfer": "Отправь Roblox username или Telegram ID игрока для трансфера.",
            "kick": "Отправь Roblox username или Telegram ID игрока для исключения.",
            "owner_change": "Отправь Roblox username или Telegram ID нового владельца."
        }
        await q.edit_message_text(prompts[data])
    elif data == "profile":
        career = "Завершена" if p["career_status"] == "retired" else "Активна"
        kb = [[InlineKeyboardButton("🏁 Завершить карьеру" if career == "Активна" else "↩️ Вернуться в карьеру",
                                    callback_data="career_retire" if career == "Активна" else "career_return")],
              [InlineKeyboardButton("⚙️ Позиция", callback_data="position")],
              [InlineKeyboardButton("⬅️ Назад", callback_data="home")]]
        await q.edit_message_text(
            f"👤 ПРОФИЛЬ\n\nRoblox: {p['roblox_username']}\n"
            f"Telegram: {username(q.from_user)}\nКлуб: {p['club'] or 'Ищет клуб'}\n"
            f"Позиция: {p['position'] or 'Не указана'}\nКарьера: {career}",
            reply_markup=InlineKeyboardMarkup(kb)
        )
    elif data in ("career_retire", "career_return"):
        kind = "career_retire" if data == "career_retire" else "career_return"
        label = "завершение карьеры" if kind == "career_retire" else "возвращение в карьеру"
        rid = await create_request(context, uid, kind, {}, f"🏁 ЗАЯВКА: {label}\nRoblox: {p['roblox_username']}")
        await q.edit_message_text("✅ Заявка отправлена администраторам." if rid else "❌ Не удалось отправить заявку.")
    elif data == "position":
        rows = [[InlineKeyboardButton(pos, callback_data="pos_" + pos)] for pos in POSITIONS]
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="profile")])
        await q.edit_message_text("Выбери футбольную позицию:", reply_markup=InlineKeyboardMarkup(rows))
    elif data.startswith("pos_"):
        pos = data[4:]
        if pos not in POSITIONS:
            return
        rid = await create_request(context, uid, "position", {"position": pos}, f"⚽ СМЕНА ПОЗИЦИИ\nRoblox: {p['roblox_username']}\nНовая позиция: {pos}")
        await q.edit_message_text("✅ Заявка на смену позиции отправлена." if rid else "❌ Не удалось отправить заявку.")
    elif data.startswith("transfer_yes_") or data.startswith("transfer_no_"):
        tid = int(data.rsplit("_", 1)[1])
        with lock:
            tr = db.execute("SELECT * FROM transfers WHERE id=?", (tid,)).fetchone()
            if not tr or tr["player_id"] != uid or tr["status"] != "invited":
                await q.edit_message_text("Предложение недействительно.")
                return
            db.execute("UPDATE transfers SET status=? WHERE id=?", ("accepted" if data.startswith("transfer_yes_") else "rejected", tid))
            db.commit()
        if data.startswith("transfer_no_"):
            await q.edit_message_text("Ты отклонил предложение.")
        else:
            rid = await create_request(context, uid, "transfer", {"transfer_id": tid},
                                       f"🔄 СОГЛАСИЕ НА ТРАНСФЕР\nRoblox: {p['roblox_username']}\n{tr['old_club'] or 'Свободный агент'} ➜ {tr['new_club']}")
            await q.edit_message_text("✅ Согласие отправлено администраторам." if rid else "❌ Не удалось отправить заявку.")
    elif data == "admin":
        if not is_admin(uid):
            await q.edit_message_text("⛔ Нет доступа.")
            return
        await q.edit_message_text("👑 АДМИН-ПАНЕЛЬ\nКоманды: /setgroup, /setchannel @канал, /addadmin ID")

async def moderate(q, context, rid, approve):
    with lock:
        req = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not req or req["status"] != "pending":
            await q.edit_message_text("Заявка уже обработана.")
            return
        payload = json.loads(req["payload"])
        uid, kind = req["user_id"], req["kind"]
        p = db.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()
        publish = None
        if approve:
            if kind == "registration":
                db.execute("UPDATE players SET registration_status='approved',roblox_username=?,roblox_id=? WHERE user_id=?",
                           (payload["roblox_username"], payload["roblox_id"], uid))
            elif kind == "take_club":
                club = payload["club"]
                c = db.execute("SELECT * FROM clubs WHERE name=?", (club,)).fetchone()
                if not c or c["owner_id"] or c["closed"] or p["club"]:
                    approve = False
                else:
                    db.execute("UPDATE clubs SET owner_id=? WHERE name=?", (uid, club))
                    db.execute("UPDATE players SET club=? WHERE user_id=?", (club, uid))
                    publish = f"✅ Клуб «{club}» взят игроком {('@'+p['username']) if p['username'] else 'ID '+str(uid)}"
            elif kind == "free_agent":
                publish = (f"📬 Свободный агент\n\nRoblox ник: {p['roblox_username']}\n"
                           f"Статус: Ищет клуб!\n📩: {'@'+p['username'] if p['username'] else 'ID '+str(uid)}\n\n{payload['text']}")
            elif kind in ("player_ad", "channel_ad"):
                publish = payload["text"]
            elif kind == "career_retire":
                db.execute("UPDATE players SET career_status='retired' WHERE user_id=?", (uid,))
            elif kind == "career_return":
                db.execute("UPDATE players SET career_status='active' WHERE user_id=?", (uid,))
            elif kind == "position":
                db.execute("UPDATE players SET position=? WHERE user_id=?", (payload["position"], uid))
            elif kind == "kick":
                db.execute("UPDATE players SET club=NULL WHERE user_id=? AND club=?", (payload["target_id"], payload["club"]))
            elif kind == "owner_change":
                club = payload["club"]
                db.execute("UPDATE clubs SET owner_id=? WHERE name=? AND owner_id=?", (payload["new_owner"], club, payload["old_owner"]))
                db.execute("UPDATE players SET club=NULL WHERE user_id=?", (payload["old_owner"],))
                db.execute("UPDATE players SET club=? WHERE user_id=?", (club, payload["new_owner"]))
            elif kind == "transfer":
                tr = db.execute("SELECT * FROM transfers WHERE id=?", (payload["transfer_id"],)).fetchone()
                if tr and tr["status"] == "accepted":
                    db.execute("UPDATE players SET club=? WHERE user_id=?", (tr["new_club"], uid))
                    db.execute("UPDATE transfers SET status='approved' WHERE id=?", (tr["id"],))
                    publish = f"⚽ Переход в клуб:\n\nRoblox ник — {p['roblox_username']}\n{tr['old_club'] or 'Свободный агент'} ➜ {tr['new_club']}"
        status = "approved" if approve else "rejected"
        db.execute("UPDATE requests SET status=? WHERE id=?", (status, rid))
        db.commit()

    if approve and publish:
        channel = setting("tm_channel")
        if channel:
            try:
                await context.bot.send_message(channel, publish)
            except Exception:
                pass
    messages = {
        "registration": "Регистрация подтверждена!" if approve else "Регистрация отклонена. Попробуй снова через /start.",
        "take_club": "Клуб закреплён за тобой!" if approve else "Заявка на клуб отклонена.",
        "free_agent": "Анкета опубликована!" if approve else "Анкета отклонена.",
        "player_ad": "Объявление опубликовано!" if approve else "Объявление отклонено.",
        "channel_ad": "Объявление опубликовано!" if approve else "Объявление отклонено.",
        "career_retire": "Завершение карьеры подтверждено." if approve else "Заявка отклонена.",
        "career_return": "Возвращение в карьеру подтверждено." if approve else "Заявка отклонена.",
        "position": "Позиция обновлена." if approve else "Смена позиции отклонена.",
        "transfer": "Трансфер одобрен." if approve else "Трансфер отклонён.",
        "kick": "Игрок исключён." if approve else "Исключение отклонено.",
        "owner_change": "Владелец клуба изменён." if approve else "Передача клуба отклонена."
    }
    try:
        await context.bot.send_message(uid, messages.get(kind, "Заявка обработана."))
    except Exception:
        pass
    await q.edit_message_text(f"{'✅ Принята' if approve else '❌ Отклонена'} заявка #{rid}")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flow = context.user_data.get("flow")
    if not flow:
        return
    txt = (update.message.text or "").strip()
    uid = update.effective_user.id
    ensure_player(update.effective_user)
    p = player(uid)

    if flow == "registration":
        if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", txt):
            await update.message.reply_text("Roblox username должен быть 3–20 символов: латиница, цифры и _.")
            return
        await update.message.reply_text("🔎 Проверяю Roblox username...")
        roblox = lookup_roblox(txt)
        if not roblox:
            await update.message.reply_text("Не нашёл аккаунт. Проверь точный Roblox username и отправь снова.")
            return
        rid = await create_request(context, uid, "registration",
            {"roblox_username": roblox["name"], "roblox_id": roblox["id"]},
            f"📝 РЕГИСТРАЦИЯ\nRoblox: {roblox['name']}\nRoblox ID: {roblox['id']}\nTelegram: {username(update.effective_user)}")
        if rid:
            with lock:
                db.execute("UPDATE players SET registration_status='pending' WHERE user_id=?", (uid,))
                db.commit()
            context.user_data.pop("flow", None)
            await update.message.reply_text("✅ Заявка отправлена в админскую группу.")
        else:
            await update.message.reply_text("❌ Не подключена группа. Администратор должен выполнить /setgroup.")
    elif flow in ("free_agent", "player_ad", "channel_ad"):
        context.user_data.pop("flow", None)
        if len(txt) > 900:
            await update.message.reply_text("Максимум 900 символов.")
            return
        if flow == "player_ad":
            c = owner_club(uid)
            if not c:
                await update.message.reply_text("Только владельцы клубов могут искать игроков.")
                return
            txt = f"⚽ {c['name']}\n\n{txt}\n\n📩 {username(update.effective_user)}"
        if flow == "channel_ad" and not re.search(r"t\.me/", txt):
            await update.message.reply_text("Добавь ссылку на Telegram-канал.")
            return
        kind = flow
        rid = await create_request(context, uid, kind, {"text": txt}, f"📢 {kind.upper()}\nRoblox: {p['roblox_username']}\n\n{txt}")
        await update.message.reply_text("✅ Отправлено на модерацию." if rid else "❌ Не удалось отправить. Проверь подключение группы.")
    elif flow in ("transfer_target", "kick_target", "owner_change_target"):
        context.user_data.pop("flow", None)
        c = owner_club(uid)
        if not c:
            await update.message.reply_text("Только владелец клуба может это делать.")
            return
        raw = txt.lstrip("@")
        with lock:
            target = db.execute(
                "SELECT * FROM players WHERE CAST(user_id AS TEXT)=? OR LOWER(roblox_username)=LOWER(?) OR LOWER(username)=LOWER(?)",
                (raw, raw, raw)
            ).fetchone()
        if not target or target["user_id"] == uid:
            await update.message.reply_text("Игрок не найден. Он должен зарегистрироваться в боте.")
            return
        if flow == "transfer_target":
            if target["club"] == c["name"]:
                await update.message.reply_text("Игрок уже в твоём клубе.")
                return
            with lock:
                cur = db.execute("INSERT INTO transfers(player_id,old_club,new_club,status) VALUES(?,?,?,'invited')",
                                 (target["user_id"], target["club"], c["name"]))
                db.commit()
                tid = cur.lastrowid
            try:
                await context.bot.send_message(target["user_id"],
                    f"⚽ ТРАНСФЕРНОЕ ПРЕДЛОЖЕНИЕ\nКлуб: {c['name']}\nСогласен перейти?",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Принять", callback_data=f"transfer_yes_{tid}"),
                        InlineKeyboardButton("❌ Отклонить", callback_data=f"transfer_no_{tid}")
                    ]]))
                await update.message.reply_text("Предложение отправлено игроку.")
            except Exception:
                await update.message.reply_text("Игрок должен сначала открыть бота.")
        elif flow == "kick_target":
            rid = await create_request(context, uid, "kick", {"club": c["name"], "target_id": target["user_id"]},
                f"🚪 ИСКЛЮЧЕНИЕ\nКлуб: {c['name']}\nИгрок: {target['roblox_username']}")
            await update.message.reply_text("Заявка отправлена." if rid else "Не удалось отправить заявку.")
        else:
            rid = await create_request(context, uid, "owner_change",
                {"club": c["name"], "old_owner": uid, "new_owner": target["user_id"]},
                f"👑 ПЕРЕДАЧА КЛУБА\nКлуб: {c['name']}\nСтарый владелец: {p['roblox_username']}\nНовый владелец: {target['roblox_username']}")
            await update.message.reply_text("Заявка отправлена администраторам." if rid else "Не удалось отправить заявку.")

async def addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ROOT_ADMIN:
        await update.message.reply_text("Только главный администратор.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /addadmin TELEGRAM_ID")
        return
    with lock:
        db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (int(context.args[0]),))
        db.commit()
    await update.message.reply_text("Администратор добавлен.")

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"TPS Street Soccer bot is running")
    def log_message(self, *_):
        pass

def health_server():
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "10000"))), HealthHandler).serve_forever()

def main():
    if not TOKEN:
        raise RuntimeError("Set BOT_TOKEN in Render Environment Variables")
    init_db()
    threading.Thread(target=health_server, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setgroup", setgroup))
    app.add_handler(CommandHandler("setchannel", setchannel))
    app.add_handler(CommandHandler("addadmin", addadmin))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print("TPS Street Soccer bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
