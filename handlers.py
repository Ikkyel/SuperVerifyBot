"""Handler PTB yang meneruskan update Telegram ke service.py."""

from __future__ import annotations

import io
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import ContextTypes

import service

log = logging.getLogger("verifybot.handlers")

OWNER_HELP = (
    "Perintah admin:\n"
    "/targets — daftar channel/grup target yang dijaga bot + status topik log-nya\n"
    "/gerbang — daftar link gerbang semua target (buat disebar ke user)\n"
    "/setlog — jalankan di dalam grup log (forum) untuk menetapkan grup itu sebagai tujuan log\n"
    "/pasang — jalankan di dalam topik grup log, lalu pilih target untuk topik itu\n"
    "/help — bantuan lengkap (daftar perintah + keterangan + alur)\n"
    "/start — info bot\n"
    "/setbanlog — jalankan di dalam topik grup log untuk menjadikannya topik khusus log ban\n"
    "/logfile — keluarkan kumpulan log target tanpa topik sebagai file .txt ke grup log\n"
    "/setfilebanlog — jalankan di dalam topik grup log untuk menjadikannya topik khusus file data ban global\n"
    "/ekstrak — reply file .txt hasil bot dengan perintah ini: isinya diekstrak jadi pesan chat di topik baru\n"
    "/setlogkeluar — jalankan di dalam topik grup log untuk menjadikannya topik khusus log ban-keluar\n"
    "/ban <id_user> — blokir user dari bot + ban dari semua channel/grup target\n"
    "/unban <id_user> — cabut blokir + unban di semua target\n"
    "/daftarban — daftar user yang sedang diblokir\n"
    "/tambahadmin <id_user> — angkat admin baru (khusus owner)\n"
    "/hapusadmin <id_user> — cabut admin (khusus owner)\n"
    "/daftaradmin — daftar owner + admin bot (khusus owner)\n"
    "/backup — unduh file backup DATA BAN saja (JSON) untuk dipindah ke bot lain (khusus owner)\n\n"
    "Daftarkan link invite mentah sebuah target: cukup kirim link-nya ke sini "
    "(chat pribadi dengan bot), bot akan menyimpannya dan membalas link gerbangnya."
)


def _store(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["store"]


def _owner_id(context: ContextTypes.DEFAULT_TYPE) -> int:
    return context.application.bot_data["owner_id"]


def _is_owner(update: Update, context) -> bool:
    user = update.effective_user
    return bool(user and user.id == _owner_id(context))


def _is_admin(update: Update, context) -> bool:
    """Owner + admin tambahan (daftar admin di DB)."""
    user = update.effective_user
    if not user:
        return False
    if user.id == _owner_id(context):
        return True
    store = context.bot_data.get("store")
    return bool(store) and store.is_admin(user.id)


# --------------------------------------------------------------------------- #
# /start, /targets, /setlog                                                    #
# --------------------------------------------------------------------------- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store, user = _store(context), update.effective_user
    if not user or update.effective_chat.type != ChatType.PRIVATE:
        return
    store.record_encounter(user.id, service.full_name_of(user), user.username,
                           is_premium=user.is_premium, language_code=user.language_code)

    if _is_admin(update, context):
        await update.message.reply_text(
            "Halo Owner 👋 Bot verifikasi member aktif.\n\n" + OWNER_HELP)
        return

    if store.is_banned(user.id):
        return  # senyap: user ter-ban tidak dibalas apa pun (permintaan owner)

    args = context.args or []
    if args and args[0] != "verify":
        handled = await service.handle_gateway_start(
            context.bot, store, _owner_id(context), user, args[0],
            bot_username=(context.bot.username or ""))
        if handled != "unknown":
            return

    if store.list_pendings(user.id):
        await update.message.reply_text(
            "Kamu punya akses yang lagi menunggu verifikasi ⏳\n"
            "Kirim nomor HP kamu sendiri lewat tombol di bawah ini buat menyelesaikannya ya.",
            reply_markup=service.contact_keyboard())
        return

    targets = [t for t in store.list_targets()
               if t["_id"] != store.get_setting("log_group_id")]
    text = (
        "Halo 👋 Aku bot verifikasi member.\n"
        "Kalau kamu minta join ke channel/grup yang aku jaga, aku akan minta kamu "
        "membagikan nomor HP di sini buat verifikasi — sesudah itu permintaanmu "
        "otomatis disetujui.")
    if targets:
        text += "\n\nAtau pilih tujuanmu di bawah ini buat mulai verifikasi sekarang:"
        buttons = [[InlineKeyboardButton(f"🎯 {t.get('title') or t['_id']}",
                                         callback_data=f"gw:{t['_id']}")]
                   for t in targets]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.message.reply_text(text + " Nggak ada yang perlu dilakukan sekarang kok.")


async def targets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update, context) or update.effective_chat.type != ChatType.PRIVATE:
        return
    store = _store(context)
    targets = store.list_targets()
    if not targets:
        await update.message.reply_text(
            "Belum ada target terdaftar. Target otomatis terdaftar saat bot menerima "
            "event (join request / member join / bot dijadikan admin) dari channel/grupnya.")
        return
    lines = ["🎯 *Daftar target yang dijaga bot:*"]
    for t in targets:
        topik = f"topik {t['thread_id']}" if t.get("thread_id") else "topik belum dipasang"
        lines.append(f"• {t.get('title') or t['_id']} (`{t['_id']}`, {t.get('type') or '-'}) — {topik}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def setlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update, context):
        return
    chat = update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("/setlog dipakai di dalam grup log ya.")
        return
    if not getattr(chat, "is_forum", False):
        await update.message.reply_text(
            "Grup ini belum mengaktifkan Topics. Aktifkan dulu Topics di pengaturan grup, "
            "lalu jalankan /setlog lagi.")
        return
    _store(context).set_setting("log_group_id", chat.id)
    await update.message.reply_text(
        f"✅ Grup ini ditetapkan sebagai grup log verifikasi (`{chat.id}`).\n"
        "Sekarang buat topik per target, lalu jalankan /pasang di dalam tiap topik.",
        parse_mode="Markdown")


# --------------------------------------------------------------------------- #
# /pasang (di dalam topik grup log)                                            #
# --------------------------------------------------------------------------- #

HELP_TEXT = (
    "🛡 *Bantuan — Bot Verifikasi Member*\n\n"
    "*Perintah owner (bisa dipakai di chat pribadi maupun di grup log):*\n"
    "• /help — tampilkan bantuan ini\n"
    "• /targets — daftar channel/grup yang dijaga bot + status pairing topik log-nya\n"
    "• /gerbang — daftar link gerbang semua target (link yang disebar ke user)\n"
    "• /daftarban — daftar user yang sedang di-ban global\n\n"
    "*Perintah yang dipakai di dalam grup log:*\n"
    "• /setlog — jalankan di grup log untuk menetapkan grup itu sebagai tujuan log "
    "(cukup sekali, di General)\n"
    "• /pasang — jalankan *di dalam sebuah topik*, lalu tap target yang sesuai: "
    "topik itu jadi tempat log verifikasi target tersebut\n"
    "• /setbanlog — jalankan *di dalam sebuah topik* untuk menjadikannya topik "
    "khusus log ban (ban/unban, sapuan daftar ban, member keluar yang di-ban)\n"
    "• /logfile — keluarkan kumpulan log verifikasi target yang belum punya topik "
    "sebagai satu file .txt ke General grup log (otomatis juga tiap 100 catatan tertampung)\n"
    "• /setfilebanlog — jalankan *di dalam sebuah topik* untuk menjadikannya topik khusus "
    "file data ban global (catatan sapuan ban global ke target baru, dikirim sebagai file "
    "bernama targetnya — bukan pesan chat)\n"
    "• /ekstrak — reply file .txt hasil bot (file ban global / file log) dengan perintah ini: "
    "bot membuat topik baru dan memposting isi file sebagai pesan chat di sana; "
    "kalau bot tidak bisa membuat topik, pakai /tempel di topik tujuan\n"
    "• /setlogkeluar — jalankan *di dalam sebuah topik* untuk menjadikannya topik khusus log "
    "ban-keluar (member keluar yang otomatis di-ban + upaya joinnya yang digagalkan), "
    "terpisah dari log ban global\n\n"
    "*Kelola admin (khusus owner, chat pribadi):*\n"
    "• /tambahadmin <id_user> — angkat user jadi admin bot\n"
    "• /hapusadmin <id_user> — cabut status admin user\n"
    "• /daftaradmin — daftar owner + admin bot\n"
    "Admin bisa memakai semua perintah operasional, tapi hanya owner yang bisa "
    "menambah/menghapus admin.\n\n"
    "*Perintah ban (chat pribadi):*\n"
    "• /ban <id_user> — blokir user dari bot secara senyap + ban dari semua target; "
    "daftar ban otomatis diterapkan juga ke target baru saat bot jadi admin di sana\n"
    "• /unban <id_user> — cabut semua ban user itu (termasuk ban-karena-keluar); "
    "user bergabung lagi sendiri sesudahnya\n\n"
    "*Backup & restore (khusus owner, chat pribadi):*\n"
    "• /backup — bot mengirim file JSON berisi DATA BAN saja (daftar ban global + ban-karena-keluar)\n"
    "• Pindah daftar ban ke bot lain: kirim file itu ke chat pribadi bot tujuan, lalu konfirmasi — "
    "daftar bannya digabung ke bot tujuan dan langsung diterapkan ke target-targetnya\n\n"
    "*Alur user biasa:*\n"
    "1. User membuka link gerbang target / link Request Admin Approval / join langsung.\n"
    "2. Bot meminta nomor HP lewat tombol *📱 Kirim Nomor HP* (wajib nomor milik sendiri).\n"
    "3. Nomor valid → otomatis di-approve/dibuka di semua target yang menunggu, "
    "dan datanya tercatat di topik log target masing-masing.\n"
    "4. Batas waktu verifikasi: 30 detik per target. Lewat batas → request ditolak / "
    "member dikeluarkan.\n"
    "5. Member yang keluar sendiri dari target otomatis di-ban dari target itu.\n"
    "6. Pesan verifikasi yang bot kirim di dalam grup otomatis terhapus begitu "
    "verifikasinya selesai atau waktunya habis, biar grup tidak penuh.\n\n"
    "_Daftarkan link invite mentah sebuah target: kirim link-nya ke chat pribadi bot, "
    "bot akan membalas link gerbangnya._"
)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update, context):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def pasang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update, context):
        return
    store = _store(context)
    chat, message = update.effective_chat, update.message
    if chat.id != store.get_setting("log_group_id"):
        await message.reply_text("/pasang dipakai di dalam grup log ya (di topik yang mau dipasangkan).")
        return
    if not message.message_thread_id or message.is_topic_message is not True:
        await message.reply_text("/pasang harus dijalankan di DALAM sebuah topik, bukan di General.")
        return
    targets = store.list_targets()
    if not targets:
        await message.reply_text("Belum ada target terdaftar, jadi belum ada yang bisa dipasang.")
        return
    buttons = [[InlineKeyboardButton(t.get("title") or str(t["_id"]),
                                     callback_data=f"pasang:{t['_id']}")]
               for t in targets]
    await message.reply_text(
        "Topik ini mau dipasangkan ke target yang mana? Tap salah satu:",
        reply_markup=InlineKeyboardMarkup(buttons))


async def pasang_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not _is_admin(update, context):
        return
    await query.answer()
    store = _store(context)
    chat_id = int(query.data.split(":", 1)[1])
    thread_id = query.message.message_thread_id
    if not thread_id:
        await query.edit_message_text("Pesan ini tidak berada di dalam topik; pemasangan dibatalkan.")
        return
    target = store.get_target(chat_id)
    if not target:
        await query.edit_message_text("Target itu sudah tidak terdaftar.")
        return
    store.set_target_thread(chat_id, thread_id)
    await query.edit_message_text(
        f"✅ Topik ini sekarang dipasangkan ke *{target.get('title') or chat_id}*. "
        "Semua log verifikasi target itu akan masuk ke sini.",
        parse_mode="Markdown")


# --------------------------------------------------------------------------- #
# Link gerbang                                                                 #
# --------------------------------------------------------------------------- #

_INVITE_LINK_RE = r"(https?://)?(t\.me|telegram\.me)/(joinchat/|\+)[A-Za-z0-9_-]+"


async def gerbang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update, context) or update.effective_chat.type != ChatType.PRIVATE:
        return
    store = _store(context)
    username = context.bot.username or ""
    targets = store.list_targets()
    if not targets:
        await update.message.reply_text("Belum ada target terdaftar.")
        return
    lines = ["🚪 *Link gerbang per target* (sebar link ini ke user, bukan link mentahnya):"]
    for t in targets:
        raw = "link mentah tersimpan" if t.get("raw_link") else "link mentah belum didaftarkan"
        lines.append(f"• *{t.get('title') or t['_id']}* — {raw}")
    lines.append("\nTiap link dikirim sebagai pesan terpisah sesudah ini, "
                 "satu pesan = satu link utuh, biar gampang disalin/diteruskan persis.\n"
                 "Daftarkan link mentah: kirim link invite targetnya ke chat ini.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown",
                                    disable_web_page_preview=True)
    for t in targets:
        await update.message.reply_text(service.gateway_link(username, t['_id']),
                                        disable_web_page_preview=True)


async def owner_link_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner mengirim link invite mentah di DM -> daftarkan sebagai link gerbang."""
    if not _is_admin(update, context) or not update.message or not update.message.text:
        return
    import re
    match = re.search(_INVITE_LINK_RE, update.message.text)
    if not match:
        return
    link = match.group(0)
    if not link.startswith("http"):
        link = "https://" + link
    await service.register_gateway_link(context.bot, _store(context),
                                        _owner_id(context), link,
                                        bot_username=(context.bot.username or ""))


async def glink_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not _is_admin(update, context):
        return
    await query.answer()
    store = _store(context)
    chat_id = int(query.data.split(":", 1)[1])
    link = await service.assign_gateway_link(context.bot, store, _owner_id(context),
                                             chat_id,
                                             bot_username=(context.bot.username or ""))
    if link:
        target = store.get_target(chat_id)
        await query.edit_message_text(
            f"✅ Link mentah tersimpan untuk *{target.get('title') or chat_id}*.\n\n"
            f"Link gerbangnya (yang kamu sebar ke user):\n{link}",
            parse_mode="Markdown", disable_web_page_preview=True)
    else:
        await query.edit_message_text("Link yang menunggu sudah tidak ada; kirim ulang link-nya ya.")


async def gateway_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    store, user = _store(context), query.from_user
    store.record_encounter(user.id, service.full_name_of(user), user.username,
                           is_premium=user.is_premium, language_code=user.language_code)
    chat_id = int(query.data.split(":", 1)[1])
    handled = await service.handle_gateway_pick(
        context.bot, store, _owner_id(context), user, chat_id,
        bot_username=(context.bot.username or ""))
    if handled == "unknown":
        await query.edit_message_text("Target itu sudah tidak terdaftar.")
    else:
        target = store.get_target(chat_id)
        await query.edit_message_text(
            f"Kamu memilih *{target.get('title') or chat_id}* — lanjutkan di bawah ya 👇",
            parse_mode="Markdown")


# --------------------------------------------------------------------------- #
# Topik log ban                                                                #
# --------------------------------------------------------------------------- #

async def setbanlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update, context):
        return
    store = _store(context)
    chat, message = update.effective_chat, update.message
    if chat.id != store.get_setting("log_group_id"):
        await message.reply_text("/setbanlog dipakai di dalam grup log ya (di topik khusus ban).")
        return
    if not message.message_thread_id or message.is_topic_message is not True:
        await message.reply_text("/setbanlog harus dijalankan di DALAM sebuah topik, bukan di General.")
        return
    store.set_setting("ban_thread_id", message.message_thread_id)
    await message.reply_text(
        "✅ Topik ini ditetapkan sebagai **topik khusus log ban**. Semua catatan ban, "
        "unban, sapuan daftar ban, upaya join user ter-ban, dan member keluar yang "
        "otomatis di-ban akan masuk ke sini, terpisah dari log verifikasi.",
        parse_mode="Markdown")


async def setlogkeluar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update, context):
        return
    store = _store(context)
    chat, message = update.effective_chat, update.message
    if chat.id != store.get_setting("log_group_id"):
        await message.reply_text("/setlogkeluar dipakai di dalam grup log ya (di topik khusus log ban-keluar).")
        return
    if not message.message_thread_id or message.is_topic_message is not True:
        await message.reply_text("/setlogkeluar harus dijalankan di DALAM sebuah topik, bukan di General.")
        return
    store.set_setting("leaveban_thread_id", message.message_thread_id)
    await message.reply_text(
        "✅ Topik ini ditetapkan sebagai **topik khusus log ban-keluar**. Catatan "
        "member yang keluar sendiri lalu otomatis di-ban, dan upaya join user "
        "ban-keluar yang digagalkan, akan masuk ke sini — terpisah dari log ban "
        "global supaya tidak kecampur.",
        parse_mode="Markdown")


async def setfilebanlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update, context):
        return
    store = _store(context)
    chat, message = update.effective_chat, update.message
    if chat.id != store.get_setting("log_group_id"):
        await message.reply_text("/setfilebanlog dipakai di dalam grup log ya (di topik khusus file ban global).")
        return
    if not message.message_thread_id or message.is_topic_message is not True:
        await message.reply_text("/setfilebanlog harus dijalankan di DALAM sebuah topik, bukan di General.")
        return
    store.set_setting("fileban_thread_id", message.message_thread_id)
    await message.reply_text(
        "✅ Topik ini ditetapkan sebagai **topik khusus file data ban global**. "
        "Setiap bot jadi admin di channel/grup baru, catatan sapuan daftar ban "
        "global ke target itu akan dikirim ke sini sebagai satu file bernama "
        "*File data ban global (nama channel/grup)* — terpisah dari topik log ban biasa.",
        parse_mode="Markdown")


# --------------------------------------------------------------------------- #
# Ban / unban (owner)                                                          #
# --------------------------------------------------------------------------- #

def _parse_user_id(update: Update, context) -> int | None:
    args = context.args or []
    if not args:
        return None
    try:
        return int(args[0])
    except ValueError:
        return None


def _ban_summary(verb: str, user_id: int, user_doc, results) -> str:
    label = user_id
    if user_doc:
        label = f"{user_doc.get('name') or user_id} (`{user_id}`)"
        if user_doc.get("username"):
            label += f" @{user_doc['username']}"
    lines = [f"{verb} *{label}* selesai.", "Hasil per target:"]
    if not results:
        lines.append("• (belum ada target terdaftar)")
    for target, ok, err in results:
        name = target.get("title") or target["_id"]
        lines.append(f"• {name}: {'✅' if ok else '❌ ' + err[:60]}")
    return "\n".join(lines)


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update, context) or update.effective_chat.type != ChatType.PRIVATE:
        return
    user_id = _parse_user_id(update, context)
    if user_id is None:
        await update.message.reply_text(
            "Pakainya: `/ban <id_user>` — contoh: `/ban 8572691319`.\n"
            "ID user bisa kamu lihat di log verifikasinya (baris 🆔 ID).",
            parse_mode="Markdown")
        return
    if user_id == _owner_id(context):
        await update.message.reply_text("Itu ID kamu sendiri — nggak bisa nge-ban owner 😄")
        return
    store = _store(context)
    if user_id == _owner_id(context):
        await update.message.reply_text("Owner bot tidak bisa di-ban. 😄")
        return
    if store.is_admin(user_id):
        await update.message.reply_text(
            "User itu adalah admin bot ini — hapus dulu dari daftar admin "
            "pakai /hapusadmin, baru bisa di-ban.")
        return
    results = await service.ban_user(context.bot, store, user_id)
    await update.message.reply_text(
        _ban_summary("🚫 Ban", user_id, store.get_user(user_id), results),
        parse_mode="Markdown")
    result_lines = "\n".join(
        f"{'✅' if ok else '❌'} {service._esc(t.get('title') or t['_id'])}"
        for t, ok, _ in results) or "—"
    await service.send_ban_report(
        context.bot, store, _owner_id(context),
        "🚫 <b>Ban manual (perintah owner)</b>",
        user_id,
        extra=f"🎯 Hasil ban per target:\n{result_lines}")


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update, context) or update.effective_chat.type != ChatType.PRIVATE:
        return
    user_id = _parse_user_id(update, context)
    if user_id is None:
        await update.message.reply_text(
            "Pakainya: `/unban <id_user>` — contoh: `/unban 8572691319`.",
            parse_mode="Markdown")
        return
    store = _store(context)
    results = await service.unban_user(context.bot, store, user_id)
    await update.message.reply_text(
        _ban_summary("✅ Unban", user_id, store.get_user(user_id), results),
        parse_mode="Markdown")
    result_lines = "\n".join(
        f"{'✅' if ok else '❌'} {service._esc(t.get('title') or t['_id'])}"
        for t, ok, _ in results) or "—"
    await service.send_ban_report(
        context.bot, store, _owner_id(context),
        "✅ <b>Unban (perintah owner)</b> — termasuk ban-karena-keluar ikut dicabut",
        user_id,
        extra=f"🎯 Hasil unban per target:\n{result_lines}")


async def daftarban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update, context) or update.effective_chat.type != ChatType.PRIVATE:
        return
    banned = _store(context).list_banned()
    if not banned:
        await update.message.reply_text("Daftar ban kosong — belum ada user yang diblokir.")
        return
    lines = [f"🚫 *User terblokir ({len(banned)}):*"]
    for u in banned:
        uname = f" @{u['username']}" if u.get("username") else ""
        # Nama dibuat tautan sebutan berbasis ID: bisa di-tap menuju
        # profilnya, termasuk user yang tidak punya username.
        label = service._md_escape(u.get('name') or '-')
        lines.append(f"• [{label}](tg://user?id={u['_id']}) (`{u['_id']}`){uname}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --------------------------------------------------------------------------- #
# File log target tanpa topik                                                  #
# --------------------------------------------------------------------------- #

async def logfile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update, context):
        return
    store = _store(context)
    pending = store.count_unpaired_logs()
    if not pending:
        await update.message.reply_text(
            "📭 Belum ada log tertampung dari target tanpa topik — tampungannya kosong.")
        return
    sent = await service.flush_unpaired_logs(context.bot, store)
    if sent:
        await update.message.reply_text(
            f"📄 File log terkirim ke General grup log ({sent} catatan). "
            "Tampungannya sudah dikosongkan.")
    else:
        await update.message.reply_text(
            "❌ Gagal mengirim file log-nya — tampungannya masih utuh, coba lagi nanti.")


# --------------------------------------------------------------------------- #
# Ekstrak file kembali ke chat                                                 #
# --------------------------------------------------------------------------- #

async def _post_extract(context, chat_id: int, thread_id: int, parsed: dict):
    header = f"📄 *Ekstrak file: {service._md_escape(parsed['title'])}*"
    if parsed.get("intro"):
        header += "\n\n" + service._md_escape(parsed["intro"])
    await context.bot.send_message(chat_id=chat_id, message_thread_id=thread_id,
                                   text=header[:4000], parse_mode="Markdown",
                                   protect_content=True)
    for entry in parsed["entries"]:
        await context.bot.send_message(chat_id=chat_id, message_thread_id=thread_id,
                                       text=entry[:4000], protect_content=True)
    await context.bot.send_message(
        chat_id=chat_id, message_thread_id=thread_id,
        text=f"— Selesai: {len(parsed['entries'])} entri diekstrak dari file "
             f"{parsed['title']}.")


async def ekstrak_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply sebuah file .txt hasil bot dengan /ekstrak -> isinya diposting
    sebagai pesan chat di topik BARU yang bot buat sendiri di grup log."""
    if not _is_admin(update, context):
        return
    store = _store(context)
    chat, message = update.effective_chat, update.message
    if chat.id != store.get_setting("log_group_id"):
        await message.reply_text("/ekstrak dipakai di dalam grup log ya.")
        return
    reply = message.reply_to_message
    document = reply.document if reply else None
    if document is None:
        await message.reply_text(
            "Caranya: *reply* (balas) pesan file-nya, lalu kirim /ekstrak pada balasan itu.",
            parse_mode="Markdown")
        return
    try:
        file = await context.bot.get_file(document.file_id)
        raw = bytes(await file.download_as_bytearray())
        text = raw.decode("utf-8")
    except Exception as exc:
        await message.reply_text(f"❌ File-nya tidak bisa kubaca: {exc}")
        return
    parsed = service.parse_export_file(text, document.file_name or "")
    topic_name = f"📄 Ekstrak — {parsed['title']}"[:128]
    try:
        topic = await context.bot.create_forum_topic(chat_id=chat.id, name=topic_name)
    except Exception:
        context.bot_data[f"extract:{update.effective_user.id}"] = parsed
        await message.reply_text(
            "Aku tidak bisa membuat topik baru sendiri (izin Kelola Topik kurang). "
            "Jalan cadangan: buka/buat topik tujuanmu di grup log ini, lalu kirim "
            "/tempel di dalam topik itu — isinya langsung kuposting di sana.")
        return
    await _post_extract(context, chat.id, topic.message_thread_id, parsed)
    await message.reply_text(
        f"✅ Isi file *{service._md_escape(parsed['title'])}* sudah diekstrak ke "
        f"topik baru *{service._md_escape(topic_name)}* di grup log ini "
        f"({len(parsed['entries'])} entri).",
        parse_mode="Markdown")


async def tempel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tempel hasil /ekstrak yang tertunda ke topik tempat perintah dikirim."""
    if not _is_admin(update, context):
        return
    store = _store(context)
    chat, message = update.effective_chat, update.message
    if chat.id != store.get_setting("log_group_id"):
        return
    if not message.message_thread_id or message.is_topic_message is not True:
        await message.reply_text("/tempel dipakai di dalam topik tujuan ya.")
        return
    parsed = context.bot_data.pop(f"extract:{update.effective_user.id}", None)
    if parsed is None:
        await message.reply_text(
            "Belum ada ekstrak yang menunggu. Reply file-nya dengan /ekstrak dulu ya.")
        return
    await _post_extract(context, chat.id, message.message_thread_id, parsed)
    await message.reply_text(
        f"✅ Ekstrak *{service._md_escape(parsed['title'])}* sudah kuposting di topik ini.",
        parse_mode="Markdown")


# --------------------------------------------------------------------------- #
# Backup / restore (KHUSUS owner)                                              #
# --------------------------------------------------------------------------- #

async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update, context) or update.effective_chat.type != ChatType.PRIVATE:
        return
    store = _store(context)
    data = store.export_bans()
    summary = service.validate_backup(data)
    payload = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    stamp = datetime.now(ZoneInfo("Asia/Jakarta")).strftime("%Y%m%d-%H%M%S")
    await update.message.reply_document(
        document=InputFile(io.BytesIO(payload), filename=f"verifybot-databan-{stamp}.json"),
        caption=(
            "💾 *Backup DATA BAN* selesai (cuma data ban, bukan seluruh data bot).\n"
            f"Isi: {summary['banned']} user ter-ban global (beserta data membernya) "
            f"dan {summary['leave_bans']} catatan ban-karena-keluar.\n\n"
            "Simpan file ini baik-baik — isinya termasuk nomor HP member yang ter-ban. "
            "Untuk memindahkan daftar ban ke bot lain: kirim file ini ke chat pribadi "
            "bot tujuan dan konfirmasi — daftar bannya digabung ke sana dan langsung "
            "diterapkan ke target-target bot tujuan."),
        parse_mode="Markdown")


async def restore_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner mengirim file backup (.json) ke DM bot -> tawarkan restore."""
    if not _is_owner(update, context) or update.effective_chat.type != ChatType.PRIVATE:
        return
    document = update.message.document if update.message else None
    if document is None:
        return
    name = (document.file_name or "").lower()
    if not (name.endswith(".json") or document.mime_type == "application/json"):
        return
    try:
        file = await context.bot.get_file(document.file_id)
        raw = bytes(await file.download_as_bytearray())
        data = json.loads(raw.decode("utf-8"))
        summary = service.validate_backup(data)
    except (ValueError, UnicodeDecodeError) as exc:
        await update.message.reply_text(f"❌ File backup tidak valid: {exc}")
        return
    except Exception as exc:
        await update.message.reply_text(f"❌ Gagal membaca file backup: {exc}")
        return
    context.bot_data["pending_restore"] = data
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Ya, gabungkan daftar bannya", callback_data="restore:yes")],
        [InlineKeyboardButton("❌ Batal", callback_data="restore:no")],
    ])
    await update.message.reply_text(
        "♻️ *File backup data ban terbaca.* Isinya:\n"
        f"• {summary['banned']} user ter-ban global (beserta data membernya)\n"
        f"• {summary['leave_bans']} catatan ban-karena-keluar\n\n"
        "Daftar ban ini akan *DIGABUNG* ke bot ini (data lain tidak berubah) dan "
        "langsung diterapkan ke target-target bot ini. Lanjutkan?",
        reply_markup=keyboard, parse_mode="Markdown")


async def restore_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not _is_owner(update, context):
        return
    action = (query.data or "").split(":", 1)[-1]
    data = context.bot_data.pop("pending_restore", None)
    if action == "yes" and data is not None:
        store = _store(context)
        result = store.import_bans(data)
        # Tegakkan langsung: ban global ke semua target bot ini, ban-keluar
        # ke target asal masing-masing.
        for banned_id in result["banned_ids"]:
            await service.ban_user(context.bot, store, banned_id)
        for left_user_id, left_chat_id in result["leave_ban_pairs"]:
            try:
                await context.bot.ban_chat_member(chat_id=left_chat_id,
                                                  user_id=left_user_id)
            except Exception:
                pass  # bot mungkin belum admin di target itu; sapuan saat jadi admin menutupinya sebagian (khusus ban global)
        await query.answer("Daftar ban tergabung ✅")
        await query.edit_message_text(
            "♻️ *Restore data ban selesai.* Yang digabungkan ke bot ini:\n"
            f"• {result['banned_users']} user ter-ban global — sudah langsung di-ban "
            "dari semua target bot ini\n"
            f"• {result['leave_bans']} catatan ban-karena-keluar — sudah diterapkan "
            "ke target asalnya\n\n"
            "Data lain (target, admin, settings, riwayat) tidak berubah.",
            parse_mode="Markdown")
    else:
        await query.answer("Dibatalkan")
        await query.edit_message_text("♻️ Restore dibatalkan — tidak ada data yang berubah.")


# --------------------------------------------------------------------------- #
# Kelola admin (KHUSUS owner)                                                  #
# --------------------------------------------------------------------------- #

def _parse_admin_id(context):
    args = getattr(context, "args", None) or []
    if len(args) != 1:
        return None
    try:
        return int(args[0])
    except (TypeError, ValueError):
        return None


async def tambahadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update, context) or update.effective_chat.type != ChatType.PRIVATE:
        return
    user_id = _parse_admin_id(context)
    if user_id is None:
        await update.message.reply_text(
            "Formatnya: `/tambahadmin <id_user>`\n"
            "ID user bisa dilihat di log verifikasi (baris 🆔 ID).",
            parse_mode="Markdown")
        return
    store = _store(context)
    if user_id == _owner_id(context):
        await update.message.reply_text("Kamu sendiri ownernya — sudah admin tertinggi. 👑")
        return
    if store.is_admin(user_id):
        await update.message.reply_text("User itu sudah jadi admin.")
        return
    store.add_admin(user_id, update.effective_user.id)
    doc = store.get_user(user_id) or {}
    label = f" — {doc.get('name')}" if doc.get("name") else ""
    label += f" ({service.display_username(doc.get('username'))})" if doc.get("username") else ""
    await update.message.reply_text(
        f"✅ Ditambahkan sebagai admin bot: `{user_id}`{label}\n"
        "Admin bisa memakai perintah operasional (/targets, /gerbang, /ban, /unban, "
        "/pasang, /setlog, /setbanlog, /help), tapi hanya owner yang bisa "
        "menambah/menghapus admin.", parse_mode="Markdown")
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=("👋 Kamu ditambahkan sebagai *admin* bot verifikasi ini oleh owner. "
                  "Ketik /help untuk melihat perintah yang bisa kamu pakai."),
            parse_mode="Markdown")
    except Exception:
        pass  # admin belum pernah Start bot; abaikan


async def hapusadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update, context) or update.effective_chat.type != ChatType.PRIVATE:
        return
    user_id = _parse_admin_id(context)
    if user_id is None:
        await update.message.reply_text(
            "Formatnya: `/hapusadmin <id_user>` — lihat daftarnya di /daftaradmin.",
            parse_mode="Markdown")
        return
    store = _store(context)
    if user_id == _owner_id(context):
        await update.message.reply_text("Owner tidak bisa dihapus dari daftar admin. 👑")
        return
    if store.remove_admin(user_id):
        await update.message.reply_text(
            f"✅ Admin `{user_id}` dihapus. Akses perintah adminnya langsung dicabut.",
            parse_mode="Markdown")
    else:
        await update.message.reply_text("User itu memang bukan admin.")


async def daftaradmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update, context) or update.effective_chat.type != ChatType.PRIVATE:
        return
    store = _store(context)
    owner_id = _owner_id(context)
    owner_doc = store.get_user(owner_id) or {}
    lines = ["👑 *Daftar Admin Bot*\n",
             f"• Owner: `{owner_id}`"
             + (f" — {service._md_escape(owner_doc.get('name') or '')}" if owner_doc.get("name") else "")
             + (f" ({service.display_username(owner_doc.get('username'))})" if owner_doc.get("username") else "")]
    admins = store.list_admins()
    for doc in admins:
        user_doc = store.get_user(doc["_id"]) or {}
        lines.append(
            f"• Admin: `{doc['_id']}`"
            + (f" — {service._md_escape(user_doc.get('name') or '')}" if user_doc.get("name") else "")
            + (f" ({service.display_username(user_doc.get('username'))})" if user_doc.get("username") else ""))
    if not admins:
        lines.append("\nBelum ada admin tambahan. Tambah dengan /tambahadmin <id_user>.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# --------------------------------------------------------------------------- #
# Jalur A — join request                                                       #
# --------------------------------------------------------------------------- #

async def join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    store = _store(context)
    await service.handle_join_request(
        context.bot, store, _owner_id(context), req.chat, req.from_user,
        invite_link=req.invite_link, user_chat_id=req.user_chat_id,
        bio=getattr(req, "bio", None))


# --------------------------------------------------------------------------- #
# Jalur B — chat_member & my_chat_member                                       #
# --------------------------------------------------------------------------- #

def _became_member(old, new) -> bool:
    member_statuses = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
                       ChatMemberStatus.OWNER}
    was = old.status in member_statuses or (
        old.status == ChatMemberStatus.RESTRICTED and getattr(old, "is_member", False))
    now = new.status in member_statuses or (
        new.status == ChatMemberStatus.RESTRICTED and getattr(new, "is_member", False))
    return now and not was


def _became_left(old, new) -> bool:
    """Member -> keluar sendiri. Transisi dari status BANNED tidak dihitung:
    itu sisa proses ban+unban (kick) oleh bot/admin, bukan keluar sukarela."""
    if old.status == ChatMemberStatus.BANNED:
        return False
    was = old.status in (ChatMemberStatus.MEMBER,) or (
        old.status == ChatMemberStatus.RESTRICTED and getattr(old, "is_member", False))
    return was and new.status == ChatMemberStatus.LEFT


async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member or update.my_chat_member
    chat, user = cmu.chat, cmu.new_chat_member.user
    store = _store(context)
    # Setiap event dari chat target = daftarkan targetnya.
    store.register_target(chat.id, getattr(chat, "title", ""), getattr(chat, "type", ""))

    if update.my_chat_member:
        # Bot baru dijadikan admin di sebuah chat -> sapu daftar ban ke sana:
        # member lama yang ada di daftar ban langsung di-ban, yang belum
        # pernah join ikut terblokir (ban Telegram berlaku untuk non-member).
        new_member = cmu.new_chat_member
        became_admin = (
            user.id == context.bot.id
            and new_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
            and cmu.old_chat_member.status not in (ChatMemberStatus.ADMINISTRATOR,
                                                   ChatMemberStatus.OWNER)
        )
        if (became_admin and chat.id != store.get_setting("log_group_id")
                and store.list_banned()):
            sweep = await service.apply_blacklist(context.bot, store, chat.id)
            owner_id = _owner_id(context)
            if owner_id:
                raw_title = getattr(chat, "title", "") or str(chat.id)
                # Catatan sapuan = SATU FILE ke topik khusus file ban global
                # (bukan pesan chat, bukan DM owner).
                await service.send_sweep_file(
                    context.bot, store, owner_id, chat.id, raw_title, sweep)
        return  # event soal status bot sendiri; cukup untuk registrasi target

    if user.is_bot:
        return
    if _became_member(cmu.old_chat_member, cmu.new_chat_member):
        await service.handle_direct_join(
            context.bot, store, _owner_id(context), chat, user,
            invite_link=getattr(cmu, "invite_link", None),
            bot_username=(context.bot.username or ""))
    elif _became_left(cmu.old_chat_member, cmu.new_chat_member):
        await service.handle_member_left(
            context.bot, store, _owner_id(context), chat, user)


# --------------------------------------------------------------------------- #
# Contact                                                                      #
# --------------------------------------------------------------------------- #

async def contact_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE or not update.message.contact:
        return
    user = update.effective_user
    await service.handle_contact(context.bot, _store(context),
                                 _owner_id(context), user, update.message.contact)
