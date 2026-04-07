#!/usr/bin/env python3
"""
Vinted Telegram Bot — Mode Webhook pour Render (tier gratuit)
"""

import os
import json
import time
import logging
import asyncio
import sqlite3
import threading
import requests
from typing import Optional

from flask import Flask, request as flask_request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
RENDER_URL = os.environ["RENDER_EXTERNAL_URL"]
PORT = int(os.getenv("PORT", "10000"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
DB_PATH = os.getenv("DB_PATH", "vinted_bot.db")
VINTED_DOMAIN = os.getenv("VINTED_DOMAIN", "www.vinted.fr")
VINTED_BASE_URL = f"https://{VINTED_DOMAIN}/api/v2"
WEBHOOK_PATH = f"/webhook/{TOKEN}"
WEBHOOK_URL = f"{RENDER_URL}{WEBHOOK_PATH}"

(
    WAITING_KEYWORD,
    WAITING_BRAND,
    WAITING_SIZE,
    WAITING_CONDITION,
    WAITING_PRICE_MIN,
    WAITING_PRICE_MAX,
    WAITING_FILTER_NAME,
) = range(7)

CONDITIONS = {
    "6": "🏷 Neuf avec étiquettes",
    "1": "✨ Neuf sans étiquettes",
    "2": "💚 Très bon état",
    "3": "👍 Bon état",
    "4": "👌 Satisfaisant",
}

flask_app = Flask(__name__)


# ─── DATABASE ─────────────────────────────────────────────────────────────────

def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS filters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            keywords TEXT,
            brands TEXT,
            sizes TEXT,
            conditions TEXT,
            price_min REAL,
            price_max REAL,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS seen_items (
            item_id TEXT NOT NULL,
            filter_id INTEGER NOT NULL,
            seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (item_id, filter_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            notifications_enabled INTEGER DEFAULT 1,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def get_db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def register_user(user_id: int, username: str, first_name: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
            (user_id, username, first_name),
        )


def get_all_active_filters():
    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM filters WHERE active = 1").fetchall()


def get_user_filters(user_id: int):
    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM filters WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()


def create_filter(user_id, name, keywords, brands, sizes, conditions,
                  price_min, price_max):
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO filters
               (user_id, name, keywords, brands, sizes, conditions, price_min, price_max)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, name, keywords, brands, sizes, conditions, price_min, price_max),
        )
        return cur.lastrowid


def delete_filter(filter_id: int, user_id: int):
    with get_db() as conn:
        conn.execute(
            "DELETE FROM filters WHERE id = ? AND user_id = ?",
            (filter_id, user_id),
        )


def toggle_filter(filter_id: int, user_id: int, active: int):
    with get_db() as conn:
        conn.execute(
            "UPDATE filters SET active = ? WHERE id = ? AND user_id = ?",
            (active, filter_id, user_id),
        )


def is_item_seen(item_id: str, filter_id: int) -> bool:
    with get_db() as conn:
        return conn.execute(
            "SELECT 1 FROM seen_items WHERE item_id = ? AND filter_id = ?",
            (item_id, filter_id),
        ).fetchone() is not None


def mark_item_seen(item_id: str, filter_id: int):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_items (item_id, filter_id) VALUES (?, ?)",
            (item_id, filter_id),
        )


def cleanup_old_seen():
    with get_db() as conn:
        conn.execute(
            "DELETE FROM seen_items WHERE seen_at < datetime('now', '-30 days')"
        )


# ─── VINTED API ───────────────────────────────────────────────────────────────

vinted_session = requests.Session()
vinted_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Referer": f"https://{VINTED_DOMAIN}/",
    "Origin": f"https://{VINTED_DOMAIN}",
})


def refresh_vinted_cookie():
    try:
        vinted_session.get(f"https://{VINTED_DOMAIN}", timeout=10)
        logger.info("Cookie Vinted rafraîchi")
    except Exception as e:
        logger.warning(f"Erreur cookie Vinted: {e}")


def search_vinted(keywords="", brands="", sizes="", conditions="",
                  price_min=None, price_max=None, per_page=20):
    search_text = keywords or ""
    if brands:
        brand_list = [b.strip() for b in brands.split(",") if b.strip()]
        search_text = f"{search_text} {' '.join(brand_list)}".strip()

    params = {
        "search_text": search_text,
        "order": "newest_first",
        "per_page": per_page,
    }
    if sizes:
        params["size_ids[]"] = [s.strip() for s in sizes.split(",") if s.strip()]
    if conditions:
        params["status_ids[]"] = [c.strip() for c in conditions.split(",") if c.strip()]
    if price_min is not None:
        params["price_from"] = price_min
    if price_max is not None:
        params["price_to"] = price_max

    try:
        resp = vinted_session.get(
            f"{VINTED_BASE_URL}/catalog/items", params=params, timeout=15
        )
        if resp.status_code == 401:
            refresh_vinted_cookie()
            resp = vinted_session.get(
                f"{VINTED_BASE_URL}/catalog/items", params=params, timeout=15
            )
        resp.raise_for_status()
        return resp.json().get("items", [])
    except Exception as e:
        logger.error(f"Erreur Vinted: {e}")
        return []


def format_item_message(item: dict, filter_name: str) -> str:
    title = item.get("title", "Sans titre")
    price = item.get("total_item_price", {})
    price_str = (
        f"{price.get('amount', '?')} {price.get('currency_code', '€')}"
        if isinstance(price, dict) else f"{price} €"
    )
    brand = item.get("brand_title", "")
    size = item.get("size_title", "")
    condition = CONDITIONS.get(str(item.get("status", "")), "")
    url = item.get("url", "")
    if url and not url.startswith("http"):
        url = f"https://{VINTED_DOMAIN}{url}"
    seller = item.get("user", {}).get("login", "")
    location = item.get("city") or item.get("country_title") or ""

    lines = [f"🛍 *{title}*", f"💰 *{price_str}*"]
    if brand:
        lines.append(f"🏷 Marque : {brand}")
    if size:
        lines.append(f"📐 Taille : {size}")
    if condition:
        lines.append(f"✅ État : {condition}")
    if location:
        lines.append(f"📍 {location}")
    if seller:
        lines.append(f"👤 {seller}")
    lines.append(f"\n🔔 _{filter_name}_")
    if url:
        lines.append(f"[👉 Voir l'annonce]({url})")
    return "\n".join(lines)


# ─── HANDLERS ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username or "", user.first_name or "")
    kb = [
        [InlineKeyboardButton("➕ Créer un filtre", callback_data="go_newfilter")],
        [InlineKeyboardButton("📋 Mes filtres", callback_data="go_filters")],
        [InlineKeyboardButton("❓ Aide", callback_data="go_help")],
    ]
    await update.message.reply_text(
        f"👋 *Bonjour {user.first_name}!*\n\n"
        "Je surveille Vinted en temps réel et t'envoie les nouvelles annonces selon tes filtres.\n\n"
        "*Commandes :*\n"
        "/newfilter — Créer un filtre\n"
        "/filters — Mes filtres actifs\n"
        "/deletefilter `<id>` — Supprimer\n"
        "/pausefilter `<id>` — Mettre en pause\n"
        "/resumefilter `<id>` — Réactiver\n"
        "/stop — Couper les notifs\n"
        "/resume — Réactiver les notifs",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Bot Vinted*\n\n"
        "/newfilter — Créer un filtre guidé\n"
        "/filters — Voir tes filtres\n"
        "/deletefilter `<id>` — Supprimer\n"
        "/pausefilter `<id>` — Mettre en pause\n"
        "/resumefilter `<id>` — Réactiver\n"
        "/stop — Désactiver toutes les notifs\n"
        "/resume — Réactiver toutes les notifs\n\n"
        "_Vérification toutes les 60 secondes._",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_new_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    ctx.user_data["filter"] = {}
    await update.message.reply_text(
        "🔍 *Nouveau filtre — 1/7*\n\nDonne un *nom* à ce filtre :\n_(ex: Nike pas cher, Zara taille M)_",
        parse_mode=ParseMode.MARKDOWN,
    )
    return WAITING_FILTER_NAME


async def recv_filter_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["filter"]["name"] = update.message.text.strip()
    await update.message.reply_text(
        "🔑 *2/7 — Mots-clés*\n\nMots-clés à rechercher _(ex: air force 1)_\nOu /skip pour ignorer.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return WAITING_KEYWORD


async def recv_keyword(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    ctx.user_data["filter"]["keywords"] = "" if t == "/skip" else t
    await update.message.reply_text(
        "🏷 *3/7 — Marque(s)*\n\nMarques séparées par des virgules _(ex: Nike, Adidas)_\nOu /skip.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return WAITING_BRAND


async def recv_brand(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    ctx.user_data["filter"]["brands"] = "" if t == "/skip" else t
    ctx.user_data["filter"]["sizes_selected"] = []
    kb = [
        [
            InlineKeyboardButton("XS", callback_data="sz_XS"),
            InlineKeyboardButton("S", callback_data="sz_S"),
            InlineKeyboardButton("M", callback_data="sz_M"),
            InlineKeyboardButton("L", callback_data="sz_L"),
        ],
        [
            InlineKeyboardButton("XL", callback_data="sz_XL"),
            InlineKeyboardButton("XXL", callback_data="sz_XXL"),
            InlineKeyboardButton("34", callback_data="sz_34"),
            InlineKeyboardButton("36", callback_data="sz_36"),
        ],
        [
            InlineKeyboardButton("38", callback_data="sz_38"),
            InlineKeyboardButton("40", callback_data="sz_40"),
            InlineKeyboardButton("42", callback_data="sz_42"),
            InlineKeyboardButton("44", callback_data="sz_44"),
        ],
        [
            InlineKeyboardButton("✅ Valider", callback_data="sz_done"),
            InlineKeyboardButton("⏭ Ignorer", callback_data="sz_skip"),
        ],
    ]
    await update.message.reply_text(
        "📐 *4/7 — Taille(s)*\n\nSélectionne une ou plusieurs tailles :",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return WAITING_SIZE


async def recv_size_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "sz_skip":
        ctx.user_data["filter"]["sizes"] = ""
    elif data == "sz_done":
        ctx.user_data["filter"]["sizes"] = ",".join(
            ctx.user_data["filter"].get("sizes_selected", [])
        )
    else:
        sz = data.replace("sz_", "")
        sel = ctx.user_data["filter"].setdefault("sizes_selected", [])
        if sz in sel:
            sel.remove(sz)
        else:
            sel.append(sz)
        label = ", ".join(sel) if sel else "aucune"
        await q.edit_message_text(
            f"📐 *4/7 — Taille(s)*\n\nSélectionnées : *{label}*\n\nContinue ou valide :",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=q.message.reply_markup,
        )
        return WAITING_SIZE

    ctx.user_data["filter"]["conds_selected"] = []
    kb = [
        [
            InlineKeyboardButton("🏷 Neuf+étiquettes", callback_data="cd_6"),
            InlineKeyboardButton("✨ Neuf", callback_data="cd_1"),
        ],
        [
            InlineKeyboardButton("💚 Très bon état", callback_data="cd_2"),
            InlineKeyboardButton("👍 Bon état", callback_data="cd_3"),
        ],
        [InlineKeyboardButton("👌 Satisfaisant", callback_data="cd_4")],
        [
            InlineKeyboardButton("✅ Valider", callback_data="cd_done"),
            InlineKeyboardButton("⏭ Tous les états", callback_data="cd_skip"),
        ],
    ]
    await q.edit_message_text(
        "✅ *5/7 — État(s)*\n\nSélectionne les états acceptés :",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return WAITING_CONDITION


async def recv_cond_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "cd_skip":
        ctx.user_data["filter"]["conditions"] = ""
    elif data == "cd_done":
        ctx.user_data["filter"]["conditions"] = ",".join(
            ctx.user_data["filter"].get("conds_selected", [])
        )
    else:
        cid = data.replace("cd_", "")
        sel = ctx.user_data["filter"].setdefault("conds_selected", [])
        if cid in sel:
            sel.remove(cid)
        else:
            sel.append(cid)
        labels = [CONDITIONS.get(c, c) for c in sel]
        label = ", ".join(labels) if labels else "aucun"
        await q.edit_message_text(
            f"✅ *5/7 — État(s)*\n\nSélectionnés : *{label}*\n\nContinue ou valide :",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=q.message.reply_markup,
        )
        return WAITING_CONDITION

    await q.edit_message_text(
        "💶 *6/7 — Prix minimum*\n\nPrix min en € _(ex: 5)_\nOu /skip.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return WAITING_PRICE_MIN


async def recv_price_min(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "/skip":
        ctx.user_data["filter"]["price_min"] = None
    else:
        try:
            ctx.user_data["filter"]["price_min"] = float(t.replace(",", "."))
        except ValueError:
            await update.message.reply_text("❌ Nombre invalide. Réessaie ou /skip")
            return WAITING_PRICE_MIN
    await update.message.reply_text(
        "💶 *7/7 — Prix maximum*\n\nPrix max en € _(ex: 50)_\nOu /skip.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return WAITING_PRICE_MAX


async def recv_price_max(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if t == "/skip":
        ctx.user_data["filter"]["price_max"] = None
    else:
        try:
            ctx.user_data["filter"]["price_max"] = float(t.replace(",", "."))
        except ValueError:
            await update.message.reply_text("❌ Nombre invalide. Réessaie ou /skip")
            return WAITING_PRICE_MAX

    f = ctx.user_data["filter"]
    user = update.effective_user
    fid = create_filter(
        user.id, f.get("name", "Filtre"),
        f.get("keywords", ""), f.get("brands", ""),
        f.get("sizes", ""), f.get("conditions", ""),
        f.get("price_min"), f.get("price_max"),
    )

    conds = [CONDITIONS.get(c, c) for c in (f.get("conditions") or "").split(",") if c]
    summary = f"✅ *Filtre créé !* (ID: {fid})\n\n📌 *{f.get('name')}*\n"
    if f.get("keywords"):
        summary += f"🔑 {f['keywords']}\n"
    if f.get("brands"):
        summary += f"🏷 {f['brands']}\n"
    if f.get("sizes"):
        summary += f"📐 {f['sizes']}\n"
    if conds:
        summary += f"✅ {', '.join(conds)}\n"
    if f.get("price_min") is not None:
        summary += f"💶 Min : {f['price_min']} €\n"
    if f.get("price_max") is not None:
        summary += f"💶 Max : {f['price_max']} €\n"
    summary += "\n_Je surveille Vinted et t'envoie les annonces !_ 🚀"
    await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)
    ctx.user_data.clear()
    return ConversationHandler.END


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("❌ Annulé.")
    return ConversationHandler.END


async def cmd_filters(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = get_user_filters(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Aucun filtre. Utilise /newfilter pour en créer un.")
        return
    text = "📋 *Tes filtres :*\n\n"
    for f in rows:
        ico = "🟢" if f["active"] else "🔴"
        text += f"{ico} *[{f['id']}]* {f['name']}\n"
        if f["keywords"]:
            text += f"   🔑 {f['keywords']}\n"
        if f["brands"]:
            text += f"   🏷 {f['brands']}\n"
        if f["sizes"]:
            text += f"   📐 {f['sizes']}\n"
        if f["price_max"]:
            text += f"   💶 Max {f['price_max']} €\n"
        text += "\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_delete_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage : /deletefilter `<id>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        delete_filter(int(ctx.args[0]), update.effective_user.id)
        await update.message.reply_text(f"🗑 Filtre #{ctx.args[0]} supprimé.")
    except ValueError:
        await update.message.reply_text("❌ ID invalide.")


async def cmd_pause_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage : /pausefilter `<id>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        toggle_filter(int(ctx.args[0]), update.effective_user.id, 0)
        await update.message.reply_text(f"⏸ Filtre #{ctx.args[0]} mis en pause.")
    except ValueError:
        await update.message.reply_text("❌ ID invalide.")


async def cmd_resume_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage : /resumefilter `<id>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        toggle_filter(int(ctx.args[0]), update.effective_user.id, 1)
        await update.message.reply_text(f"▶️ Filtre #{ctx.args[0]} réactivé.")
    except ValueError:
        await update.message.reply_text("❌ ID invalide.")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user.id, update.effective_user.username or "", update.effective_user.first_name or "")
    with get_db() as conn:
        conn.execute("UPDATE users SET notifications_enabled=0 WHERE user_id=?", (update.effective_user.id,))
    await update.message.reply_text("🔕 Notifications désactivées. /resume pour réactiver.")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user.id, update.effective_user.username or "", update.effective_user.first_name or "")
    with get_db() as conn:
        conn.execute("UPDATE users SET notifications_enabled=1 WHERE user_id=?", (update.effective_user.id,))
    await update.message.reply_text("🔔 Notifications réactivées !")


async def btn_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "go_newfilter":
        await q.message.reply_text("Tape /newfilter pour créer un filtre.")
    elif q.data == "go_filters":
        rows = get_user_filters(q.from_user.id)
        if not rows:
            await q.message.reply_text("Aucun filtre. Utilise /newfilter.")
        else:
            text = "📋 *Tes filtres :*\n\n"
            for f in rows:
                ico = "🟢" if f["active"] else "🔴"
                text += f"{ico} *[{f['id']}]* {f['name']}\n"
            await q.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    elif q.data == "go_help":
        await q.message.reply_text(
            "/newfilter — Créer un filtre\n/filters — Voir mes filtres\n/stop — Couper les notifs",
        )


# ─── VINTED POLLING THREAD ────────────────────────────────────────────────────

_bot_app: Application = None


def vinted_poll_loop():
    import asyncio

    async def run():
        global _bot_app
        refresh_vinted_cookie()
        cleanup_ctr = 0
        while True:
            try:
                active_filters = get_all_active_filters()
                with get_db() as conn:
                    enabled = {
                        r[0] for r in conn.execute(
                            "SELECT user_id FROM users WHERE notifications_enabled=1"
                        ).fetchall()
                    }

                for f in active_filters:
                    if f["user_id"] not in enabled:
                        continue
                    items = search_vinted(
                        keywords=f["keywords"] or "",
                        brands=f["brands"] or "",
                        sizes=f["sizes"] or "",
                        conditions=f["conditions"] or "",
                        price_min=f["price_min"],
                        price_max=f["price_max"],
                    )
                    new_items = [
                        it for it in items
                        if str(it.get("id")) and not is_item_seen(str(it["id"]), f["id"])
                    ]
                    for it in new_items[:5]:
                        iid = str(it["id"])
                        mark_item_seen(iid, f["id"])
                        msg = format_item_message(it, f["name"])
                        photos = it.get("photos", [])
                        photo_url = None
                        if photos:
                            ph = photos[0]
                            photo_url = (
                                ph.get("full_size_url")
                                or ph.get("url")
                                or (ph.get("thumbnails") or [{}])[-1].get("url")
                            )
                        try:
                            if photo_url:
                                await _bot_app.bot.send_photo(
                                    chat_id=f["user_id"],
                                    photo=photo_url,
                                    caption=msg,
                                    parse_mode=ParseMode.MARKDOWN,
                                )
                            else:
                                await _bot_app.bot.send_message(
                                    chat_id=f["user_id"],
                                    text=msg,
                                    parse_mode=ParseMode.MARKDOWN,
                                )
                        except Exception as e:
                            logger.error(f"Envoi échoué: {e}")
                        await asyncio.sleep(1)
                    await asyncio.sleep(2)

            except Exception as e:
                logger.error(f"Erreur polling: {e}")

            cleanup_ctr += 1
            if cleanup_ctr >= 100:
                cleanup_old_seen()
                cleanup_ctr = 0

            await asyncio.sleep(POLL_INTERVAL)

    asyncio.run(run())


# ─── FLASK + WEBHOOK ──────────────────────────────────────────────────────────

@flask_app.get("/")
def health():
    return jsonify({"status": "ok", "bot": "Vinted Bot"})


@flask_app.post(WEBHOOK_PATH)
def webhook():
    update = Update.de_json(flask_request.get_json(force=True), _bot_app.bot)
    asyncio.run(_bot_app.process_update(update))
    return "ok"


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    global _bot_app
    init_db()

    _bot_app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("newfilter", cmd_new_filter)],
        states={
            WAITING_FILTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_filter_name)],
            WAITING_KEYWORD:     [MessageHandler(filters.TEXT, recv_keyword)],
            WAITING_BRAND:       [MessageHandler(filters.TEXT, recv_brand)],
            WAITING_SIZE:        [CallbackQueryHandler(recv_size_cb, pattern="^sz_")],
            WAITING_CONDITION:   [CallbackQueryHandler(recv_cond_cb, pattern="^cd_")],
            WAITING_PRICE_MIN:   [MessageHandler(filters.TEXT, recv_price_min)],
            WAITING_PRICE_MAX:   [MessageHandler(filters.TEXT, recv_price_max)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    _bot_app.add_handler(CommandHandler("start", cmd_start))
    _bot_app.add_handler(CommandHandler("help", cmd_help))
    _bot_app.add_handler(conv)
    _bot_app.add_handler(CommandHandler("filters", cmd_filters))
    _bot_app.add_handler(CommandHandler("deletefilter", cmd_delete_filter))
    _bot_app.add_handler(CommandHandler("pausefilter", cmd_pause_filter))
    _bot_app.add_handler(CommandHandler("resumefilter", cmd_resume_filter))
    _bot_app.add_handler(CommandHandler("stop", cmd_stop))
    _bot_app.add_handler(CommandHandler("resume", cmd_resume))
    _bot_app.add_handler(CallbackQueryHandler(btn_callback))

    asyncio.run(_bot_app.initialize())

    # Register webhook with Telegram
    asyncio.run(_bot_app.bot.set_webhook(url=WEBHOOK_URL))
    logger.info(f"Webhook enregistré : {WEBHOOK_URL}")

    # Start Vinted polling in background thread
    t = threading.Thread(target=vinted_poll_loop, daemon=True)
    t.start()

    logger.info(f"Serveur Flask sur port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
