"""Bot Verifikasi Member Telegram — titik masuk.

Satu bot menjaga banyak channel/grup private:
- Jalur A (join request): user wajib share nomor HP sendiri di DM -> auto-approve.
- Jalur B (join langsung, mis. dari bot fsub): grup -> restrict sampai verifikasi
  selesai atau deadline lewat (dikeluarkan); channel -> pending (best-effort).
- Verifikasi cukup sekali untuk semua target; deadline 30 detik per target pending.
- Log lengkap + riwayat nama/username dikirim ke topik grup forum per target.

Env: BOT_TOKEN, OWNER_ID, MONGODB_URI, LOG_GROUP_ID (opsional — bisa /setlog).
"""

from __future__ import annotations

import asyncio
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
import service

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s: %(message)s")
log = logging.getLogger("verifybot")

ALLOWED_UPDATES = ["message", "callback_query", "chat_join_request",
                   "chat_member", "my_chat_member"]


async def _check_expired(context):
    app = context.application
    await service.expire_pendings(app.bot, app.bot_data["store"],
                                  app.bot_data["owner_id"])


async def _deadline_loop(app: Application):
    """Cadangan pemeriksa deadline bila JobQueue (APScheduler) tidak tersedia."""
    while True:
        try:
            await _check_expired(_SimpleContext(app))
        except Exception:  # jangan sampai loop mati karena satu error
            log.exception("Pemeriksa deadline (loop cadangan) error.")
        await asyncio.sleep(5)


class _SimpleContext:
    def __init__(self, app):
        self.application = app


async def _post_init(app: Application):
    store = app.bot_data["store"]
    # Pending yang deadline-nya lewat selama bot mati -> proses segera.
    processed = await service.expire_pendings(app.bot, store, app.bot_data["owner_id"])
    if processed:
        log.info("Startup: %s pending kedaluwarsa diproses.", len(processed))
    pending_count = len(store.list_pendings())
    log.info("Bot jalan. Pending aktif dimuat ulang dari DB: %s", pending_count)
    # Pemeriksa deadline berkala: JobQueue bila ada, kalau tidak loop asyncio.
    if app.job_queue is not None:
        app.job_queue.run_repeating(_check_expired, interval=5, first=5,
                                    name="deadline_checker")
    else:
        log.warning("JobQueue tidak tersedia — pakai loop deadline cadangan.")
        app.bot_data["deadline_task"] = asyncio.create_task(_deadline_loop(app))


async def _post_shutdown(app: Application):
    task = app.bot_data.get("deadline_task")
    if task is not None:
        task.cancel()


def build_app() -> Application:
    token = os.environ["BOT_TOKEN"]
    owner_id = int(os.environ.get("OWNER_ID", "0") or 0)
    mongo_uri = os.environ["MONGODB_URI"]
    store = dbmod.connect(mongo_uri, os.environ.get("MONGODB_DB", "verifybot"))

    env_log_group = os.environ.get("LOG_GROUP_ID")
    if env_log_group:
        store.set_setting("log_group_id", int(env_log_group))

    app = (Application.builder().token(token).post_init(_post_init)
           .post_shutdown(_post_shutdown).build())
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
    app.add_handler(MessageHandler(filters.CONTACT & filters.ChatType.PRIVATE,
                                   handlers.contact_message))
    return app


def main():
    app = build_app()
    log.info("Mulai polling…")
    app.run_polling(allowed_updates=ALLOWED_UPDATES)


if __name__ == "__main__":
    main()
