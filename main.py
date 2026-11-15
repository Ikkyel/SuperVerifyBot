"""Bot Security / Pencatat Member Telegram — titik masuk.

MODE TANPA VERIFIKASI (keputusan owner 2026-11-15): satu bot menjaga banyak
channel/grup private, dan:
- Jalur A (join request): langsung di-approve otomatis.
- Jalur B (join langsung, mis. dari bot fsub): langsung diterima penuh.
- Bot hanya MENCATAT data member (nama, username, ID, foto, bio, Premium,
  bahasa, riwayat nama/username — TANPA nomor HP) ke topik grup forum per
  target; target tanpa topik ditampung lalu dikeluarkan sebagai file .txt.
- Penegakan yang tetap jalan: ban global (blokir senyap + ban semua target)
  dan ban-keluar (member keluar sendiri otomatis di-ban dari target itu).

Env: BOT_TOKEN, OWNER_ID, MONGODB_URI, LOG_GROUP_ID (opsional — bisa /setlog).
"""

from __future__ import annotations

import logging
import os

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import db as dbmod
import handlers

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s: %(message)s")
log = logging.getLogger("verifybot")

ALLOWED_UPDATES = ["message", "callback_query", "chat_join_request",
                   "chat_member", "my_chat_member"]


async def _post_init(app: Application):
    store = app.bot_data["store"]
    # Pending verifikasi adalah sisa sistem lama (gerbang nomor HP). Di mode
    # tanpa verifikasi tidak ada pending baru; sisa lama cukup dibersihkan
    # catatannya — member terkait TIDAK dikeluarkan (tidak ada lagi deadline).
    stale = store.list_pendings()
    for pending in stale:
        store.remove_pending(pending["user_id"], pending["chat_id"])
    if stale:
        log.info("Startup: %s catatan pending sisa sistem lama dibersihkan.",
                 len(stale))
    log.info("Bot jalan (mode tanpa verifikasi). Target terdaftar: %s",
             len(store.list_targets()))


def build_app() -> Application:
    token = os.environ["BOT_TOKEN"]
    owner_id = int(os.environ.get("OWNER_ID", "0") or 0)
    mongo_uri = os.environ["MONGODB_URI"]
    store = dbmod.connect(mongo_uri, os.environ.get("MONGODB_DB", "verifybot"))

    env_log_group = os.environ.get("LOG_GROUP_ID")
    if env_log_group:
        store.set_setting("log_group_id", int(env_log_group))

    app = Application.builder().token(token).post_init(_post_init).build()
    app.bot_data["store"] = store
    app.bot_data["owner_id"] = owner_id

    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("help", handlers.help_cmd))
    app.add_handler(CommandHandler("targets", handlers.targets_cmd))
    app.add_handler(CommandHandler("gerbang", handlers.gerbang_cmd))
    app.add_handler(CommandHandler("ban", handlers.ban_cmd))
    app.add_handler(CommandHandler("unban", handlers.unban_cmd))
    app.add_handler(CommandHandler("daftarban", handlers.daftarban_cmd))
    app.add_handler(CommandHandler("tambahadmin", handlers.tambahadmin_cmd))
    app.add_handler(CommandHandler("hapusadmin", handlers.hapusadmin_cmd))
    app.add_handler(CommandHandler("daftaradmin", handlers.daftaradmin_cmd))
    app.add_handler(CommandHandler("backup", handlers.backup_cmd))
    app.add_handler(CommandHandler("logfile", handlers.logfile_cmd))
    app.add_handler(CommandHandler("ekstrak", handlers.ekstrak_cmd))
    app.add_handler(CommandHandler("tempel", handlers.tempel_cmd))
    app.add_handler(CallbackQueryHandler(handlers.restore_cb, pattern=r"^restore:"))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.Document.ALL, handlers.restore_document))
    app.add_handler(CommandHandler("setlog", handlers.setlog_cmd))
    app.add_handler(CommandHandler("buattopik", handlers.buattopik_cmd))
    app.add_handler(CommandHandler("pasang", handlers.pasang_cmd))
    app.add_handler(CommandHandler("setbanlog", handlers.setbanlog_cmd))
    app.add_handler(CommandHandler("setfilebanlog", handlers.setfilebanlog_cmd))
    app.add_handler(CommandHandler("setlogkeluar", handlers.setlogkeluar_cmd))
    app.add_handler(CallbackQueryHandler(handlers.pasang_callback, pattern=r"^pasang:"))
    app.add_handler(CallbackQueryHandler(handlers.glink_callback, pattern=r"^glink:"))
    app.add_handler(CallbackQueryHandler(handlers.gateway_pick_callback, pattern=r"^gw:"))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE,
                                   handlers.owner_link_message))
    app.add_handler(ChatJoinRequestHandler(handlers.join_request))
    app.add_handler(ChatMemberHandler(handlers.chat_member_update,
                                      ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(handlers.chat_member_update,
                                      ChatMemberHandler.MY_CHAT_MEMBER))
    return app


def main():
    app = build_app()
    log.info("Mulai polling…")
    app.run_polling(allowed_updates=ALLOWED_UPDATES)


if __name__ == "__main__":
    main()
