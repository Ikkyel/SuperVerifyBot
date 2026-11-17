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
            _dashboard_text(store), parse_mode="HTML",
            reply_markup=_dashboard_keyboard())
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

    targets = [t for t in store.list_targets()
               if t["_id"] != store.get_setting("log_group_id")]
    text = (
        "👋 <b>Halo, selamat datang!</b>\n\n"
        "Aku bot penjaga channel/grup di sini 🛡️\n"
        "Kalau kamu join ke channel/grup yang aku jaga, kamu <b>langsung "
        "diterima</b> — nggak ada verifikasi dan nggak perlu kirim apa-apa. "
        "Aku cuma mencatat data member buat admin.")
    if targets:
        text += ("\n\n🎯 <b>Mau masuk ke salah satu target?</b>\n"
                 "Pilih di bawah ini, nanti aku kasih tombol masuknya 👇")
        buttons = [[InlineKeyboardButton(f"🎯 {t.get('title') or t['_id']}",
                                         callback_data=f"gw:{t['_id']}")]
                   for t in targets]
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await update.message.reply_text(
            text + "\n\nSaat ini belum ada target yang bisa dimasuki lewat "
            "aku. Kalau kamu dikasih link masuk khusus, klik link itu aja ya 👍",
            parse_mode="HTML")


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
    store = _store(context)
    store.set_setting("log_group_id", chat.id)
    # Pengejar (meniru grup log bot fsub): topik klasifikasi ban yang belum
    # ada + topik untuk target lama yang belum punya, dibuatkan sekarang.
    ban_made = await service.ensure_ban_topics(context.bot, store)
    made = await service.ensure_all_target_topics(context.bot, store)
    extras = ""
    if ban_made:
        extras += f"\nBot juga langsung membuat {ban_made} topik ban (Log Ban / Log Ban Keluar / File Ban Global)."
    if made:
        extras += f"\nBot juga langsung membuat {made} topik untuk target yang belum punya topik."
    await update.message.reply_text(
        f"✅ Grup ini ditetapkan sebagai grup log (`{chat.id}`).\n"
        "Semua topik sekarang dibuat OTOMATIS oleh bot: topik per target tiap "
        "ada target baru, dan topik ban klasifikasinya sendiri-sendiri — kamu "
        "tidak perlu membuat topik & memasangkannya manual lagi (perintah "
        "pairing manual tetap tersedia sebagai cadangan)." + extras,
        parse_mode="Markdown")


async def buattopik_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buatkan SEMUA topik log yang belum ada (pengejar): topik klasifikasi
    ban + topik per target yang belum punya.

    Biasanya tidak perlu dijalankan: topik target dibuat otomatis saat bot
    jadi admin di target baru, dan topik ban saat /setlog. Perintah ini
    untuk memaksa pengecekan ulang kapan pun owner mau.
    """
    if not _is_admin(update, context):
        return
    store = _store(context)
    if not store.get_setting("log_group_id"):
        await update.message.reply_text(
            "Grup log belum diatur — jalankan /setlog di grup log dulu ya.")
        return
    ban_made = await service.ensure_ban_topics(context.bot, store)
    made = await service.ensure_all_target_topics(context.bot, store)
    if ban_made or made:
        parts = []
        if ban_made:
            parts.append(f"{ban_made} topik ban")
        if made:
            parts.append(f"{made} topik target")
        await update.message.reply_text(
            "✅ Dibuat & dipasangkan otomatis di grup log: "
            + " + ".join(parts) + ".")
    else:
        await update.message.reply_text(
            "Semua topik sudah lengkap (topik ban + topik per target) — tidak "
            "ada yang perlu dibuat. (Kalau ada yang gagal dibuat, biasanya "
            "izin Kelola Topik bot di grup log belum aktif; log-nya sementara "
            "ditampung jadi file.)")


# --------------------------------------------------------------------------- #
# /pasang (di dalam topik grup log)                                            #
# --------------------------------------------------------------------------- #

HELP_TEXT = (
    "🛡 *Bantuan — Bot Verifikasi Member*\n\n"
    "*Perintah owner (bisa dipakai di chat pribadi maupun di grup log):*\n"
    "• /help — tampilkan bantuan ini\n"
    "• /targets — daftar channel/grup yang dijaga bot + status pairing topik log-nya\n"
    "• /gerbang — daftar link gerbang semua target (link yang disebar ke user)\n"
    "• /daftarban — daftar user yang sedang di-ban global\n"
    "• /bankeluar — saklar *ban-keluar* (chat pribadi): ada DUA saklar terpisah, "
    "buat jalur biasa (/bankeluar on|off) dan jalur gerbang (/bankeluar gerbang "
    "on|off); posisi awal keduanya OFF (member keluar hanya dicatat, tidak "
    "di-ban). Saklar yang ON membuat member jalur itu yang keluar sendiri "
    "langsung di-ban dari target itu dan join lagi ditolak\n\n"
    "*Perintah yang dipakai di dalam grup log:*\n"
    "• /setlog — jalankan di grup log untuk menetapkan grup itu sebagai tujuan log "
    "(cukup sekali, di General)\n"
    "• /pasang — CADANGAN manual: jalankan *di dalam sebuah topik*, lalu tap target "
    "yang sesuai. Biasanya tidak perlu — topik per target dibuat & dipasangkan "
    "OTOMATIS oleh bot tiap ada target baru (dan lewat /buattopik untuk target lama)\n"
    "• Topik ban (Log Ban, Log Ban Keluar, File Ban Global) dibuat & dipasangkan "
    "OTOMATIS oleh bot saat /setlog atau /buattopik — perintah /setbanlog, "
    "/setlogkeluar & /setfilebanlog di bawah hanya untuk memindahkan topiknya manual\n"
    "• /setbanlog — jalankan *di dalam sebuah topik* untuk menjadikannya topik "
    "khusus log ban global (ban/unban global, sapuan daftar ban, upaya join user "
    "ter-ban global)\n"
    "• /buattopik — buatkan topik log otomatis untuk semua target yang belum "
    "punya topik (biasanya tidak perlu: topik dibuat bot sendiri tiap target baru)\n"
    "• /logfile — keluarkan kumpulan log verifikasi target yang belum punya topik "
    "sebagai satu file .txt ke General grup log (otomatis juga tiap 100 catatan tertampung)\n"
    "• /setfilebanlog — jalankan *di dalam sebuah topik* untuk menjadikannya topik khusus "
    "file data ban global (catatan sapuan ban global ke target baru, dikirim sebagai file "
    "bernama targetnya — bukan pesan chat)\n"
    "• /ekstrak — reply file .txt hasil bot (file ban global / file log) dengan perintah ini: "
    "bot membuat topik baru dan memposting isi file sebagai pesan chat di sana; "
    "kalau bot tidak bisa membuat topik, pakai /tempel di topik tujuan\n"
    "• /setlogkeluar — jalankan *di dalam sebuah topik* untuk menjadikannya topik khusus log "
    "keluar — SATU topik yang sama untuk SEMUA target (member yang keluar dari target mana "
    "pun dicatat di sini; member TIDAK di-ban, hanya dicatat keluarnya), terpisah dari log "
    "ban global\n\n"
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
    "• /backup — bot mengirim file JSON berisi DATA BAN GLOBAL saja\n"
    "• Pindah daftar ban ke bot lain: kirim file itu ke chat pribadi bot tujuan, lalu konfirmasi — "
    "daftar bannya digabung ke bot tujuan dan langsung diterapkan ke target-targetnya\n\n"
    "*Alur user biasa (mode campuran):*\n"
    "1. Lewat LINK GERBANG (link hasil bot): user yang belum pernah terverifikasi "
    "diminta kirim nomor HP-nya dulu — cukup SEKALI per user, sesudahnya masuk "
    "lewat link bot mana pun langsung dapat tombol masuk.\n"
    "2. Lewat link Request Admin Approval biasa / join langsung: user langsung "
    "diterima otomatis, tanpa verifikasi nomor HP dan tanpa batas waktu.\n"
    "3. Data membernya (nama, username, ID, foto profil, bio, status Premium, "
    "bahasa, riwayat nama/username — plus nomor HP bila ia pernah verifikasi "
    "gerbang) tercatat di topik log target masing-masing.\n"
    "4. Member yang keluar sendiri dari target cuma dicatat ke log keluar; "
    "ia baru di-ban dari target itu bila saklar /bankeluar JALURNYA (biasa / "
    "gerbang) lagi ON.\n"
    "5. User yang ter-ban global tetap ditolak/di-ban di semua jalur join.\n\n"
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
        "✅ Topik ini ditetapkan sebagai **topik khusus log ban global**. Catatan ban "
        "global, unban, sapuan daftar ban, dan upaya join user ter-ban global "
        "akan masuk ke sini. (Ban-keluar punya topiknya sendiri — /setlogkeluar; "
        "file sapuan juga topik sendiri — /setfilebanlog.)",
        parse_mode="Markdown")


async def setlogkeluar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update, context):
        return
    store = _store(context)
    chat, message = update.effective_chat, update.message
    if chat.id != store.get_setting("log_group_id"):
        await message.reply_text("/setlogkeluar dipakai di dalam grup log ya (di topik khusus log keluar).")
        return
    if not message.message_thread_id or message.is_topic_message is not True:
        await message.reply_text("/setlogkeluar harus dijalankan di DALAM sebuah topik, bukan di General.")
        return
    store.set_setting("leaveban_thread_id", message.message_thread_id)
    await message.reply_text(
        "✅ Topik ini ditetapkan sebagai **topik khusus log keluar** — SATU topik "
        "yang sama untuk SEMUA target. Catatan member dari target mana pun yang "
        "keluar sendiri semuanya masuk ke sini (bukan ke topik target "
        "masing-masing). Member yang keluar TIDAK di-ban — ini hanya catatan.",
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
)
    for entry in parsed["entries"]:
        await context.bot.send_message(chat_id=chat_id, message_thread_id=thread_id,
                                       text=entry[:4000])
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

def _toggle_status(on: bool) -> str:
    return ("🟢 *ON* — yang keluar sendiri langsung di-ban dari target itu, "
            "dan join lagi ke sana ditolak."
            if on else
            "🔴 *OFF* — yang keluar sendiri TIDAK di-ban; bot hanya mencatat "
            "keluarnya ke log keluar.")


def _bankeluar_status_text(store) -> str:
    return ("🚪 *Ban-Keluar* (keluar sendiri = di-ban dari target)\n\n"
            "Ada *dua saklar terpisah* — aturannya mengikuti jalur masuk "
            "membernya:\n\n"
            f"• *Jalur biasa* (link acc / join langsung): "
            f"{_toggle_status(service.leave_ban_enabled(store))}\n"
            f"• *Jalur gerbang* (masuk lewat link bot): "
            f"{_toggle_status(service.gateway_leave_ban_enabled(store))}\n\n"
            "Tiap saklar berlaku untuk SEMUA target di jalurnya dan "
            "langsung berlaku tanpa restart. Mematikan saklar (dari ON ke "
            "OFF) otomatis mencabut ban-keluar *di jalur itu saja* yang "
            "dipasang bot — user yang ter-ban global tidak ikut dilepas. "
            "Catatan ban-keluar yang terlanjur dicabut tidak kembali — "
            "saklar hanya berlaku untuk yang keluar setelah dinyalakan.")


def _bankeluar_keyboard(store) -> InlineKeyboardMarkup:
    if service.leave_ban_enabled(store):
        biasa = InlineKeyboardButton("🔴 Matikan ban-keluar BIASA",
                                     callback_data="bk:off")
    else:
        biasa = InlineKeyboardButton("🟢 Nyalakan ban-keluar BIASA",
                                     callback_data="bk:on")
    if service.gateway_leave_ban_enabled(store):
        gerbang = InlineKeyboardButton("🔴 Matikan ban-keluar GERBANG",
                                       callback_data="bg:off")
    else:
        gerbang = InlineKeyboardButton("🟢 Nyalakan ban-keluar GERBANG",
                                       callback_data="bg:on")
    return InlineKeyboardMarkup([
        [biasa],
        [gerbang],
        [InlineKeyboardButton("⬅️ Dasbor", callback_data="dash:home")],
    ])


async def _set_leave_ban_switch(context: ContextTypes.DEFAULT_TYPE, store,
                                on: bool, *, setting: str, enabled_fn,
                                origin: str, label: str):
    """Ubah satu saklar ban-keluar; kembalikan catatan hasil (bisa kosong).

    Perpindahan ON -> OFF sekalian MEMBERSIHKAN ban-keluar DI JALUR ITU
    SAJA: catatan dihapus + ban Telegram yang dipasang bot dilepas
    (permintaan owner 2026-11-15 malam) — kecuali user ter-ban global,
    yang tidak dilepas. Jalur satunya tidak disentuh sama sekali.
    """
    was_on = enabled_fn(store)
    store.set_setting(setting, bool(on))
    note = ""
    cleared = None
    if was_on and not on:
        try:
            cleared = await service.clear_leave_bans(context.bot, store,
                                                     origin=origin)
        except Exception:
            cleared = None
        if cleared and cleared.get("cleared"):
            note = (f"\n\n🧹 Sekalian dibersihkan: {cleared['cleared']} catatan "
                    f"ban-keluar dihapus, {cleared['unbanned']} ban Telegram "
                    "yang dipasang bot dilepas.")
    try:
        await service.send_ban_log(
            context.bot, store, _owner_id(context),
            (f"🟢 <b>Ban-keluar {label} DINYALAKAN</b> — mulai sekarang "
             f"member jalur itu yang keluar sendiri dari target akan "
             f"di-ban dari target itu."
             if on else
             f"🔴 <b>Ban-keluar {label} DIMATIKAN</b> — mulai sekarang "
             f"member jalur itu yang keluar sendiri dari target TIDAK "
             f"di-ban, hanya dicatat."
             + (f" Semua ban-keluar jalur itu yang dipasang bot dicabut "
                f"sekalian ({cleared['cleared']} catatan dihapus, "
                f"{cleared['unbanned']} ban dilepas)."
                if cleared and cleared.get("cleared") else "")),
            kind="keluar")
    except Exception:
        pass  # pencatatan saklar bersifat tambahan; jangan gagalkan perintah
    return note


async def _set_bankeluar(context: ContextTypes.DEFAULT_TYPE, store, on: bool):
    """Saklar ban-keluar JALUR BIASA."""
    return await _set_leave_ban_switch(
        context, store, on, setting="leave_ban_enabled",
        enabled_fn=service.leave_ban_enabled, origin="biasa",
        label="JALUR BIASA")


async def _set_bangerbang(context: ContextTypes.DEFAULT_TYPE, store, on: bool):
    """Saklar ban-keluar JALUR GERBANG (masuk lewat link bot)."""
    return await _set_leave_ban_switch(
        context, store, on, setting="gateway_leave_ban_enabled",
        enabled_fn=service.gateway_leave_ban_enabled, origin="gerbang",
        label="JALUR GERBANG")


async def bankeluar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update, context) or update.effective_chat.type != ChatType.PRIVATE:
        return
    store = _store(context)
    args = [a.lower() for a in (context.args or [])]
    note = ""
    if args and args[0] in ("on", "off"):
        note = await _set_bankeluar(context, store, args[0] == "on")
    elif len(args) >= 2 and args[0] == "gerbang" and args[1] in ("on", "off"):
        note = await _set_bangerbang(context, store, args[1] == "on")
    await update.message.reply_text(
        _bankeluar_status_text(store) + note, parse_mode="Markdown",
        reply_markup=_bankeluar_keyboard(store))


async def bankeluar_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _is_admin(update, context):
        await query.answer("Khusus admin bot.", show_alert=True)
        return
    store = _store(context)
    on = query.data == "bk:on"
    note = await _set_bankeluar(context, store, on)
    await query.answer("Ban-keluar biasa " + ("dinyalakan 🟢" if on else "dimatikan 🔴"))
    await query.edit_message_text(
        _bankeluar_status_text(store) + note, parse_mode="Markdown",
        reply_markup=_bankeluar_keyboard(store))


async def bangerbang_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _is_admin(update, context):
        await query.answer("Khusus admin bot.", show_alert=True)
        return
    store = _store(context)
    on = query.data == "bg:on"
    note = await _set_bangerbang(context, store, on)
    await query.answer("Ban-keluar gerbang " + ("dinyalakan 🟢" if on else "dimatikan 🔴"))
    await query.edit_message_text(
        _bankeluar_status_text(store) + note, parse_mode="Markdown",
        reply_markup=_bankeluar_keyboard(store))


# --------------------------------------------------------------------------- #
# Dasbor admin — menu inline di /start (khusus owner/admin, chat pribadi)      #
# --------------------------------------------------------------------------- #

def _dashboard_text(store) -> str:
    targets = [t for t in store.list_targets()
               if t["_id"] != store.get_setting("log_group_id")]
    log_status = ("terpasang ✅" if store.get_setting("log_group_id")
                  else "BELUM dipasang ⚠️")
    bk = ("biasa " + ("🟢 ON" if service.leave_ban_enabled(store) else "🔴 OFF")
          + " · gerbang "
          + ("🟢 ON" if service.gateway_leave_ban_enabled(store) else "🔴 OFF"))
    return (
        "🛡️ <b>Dasbor Bot Security</b>\n\n"
        "📊 <b>Ringkasan:</b>\n"
        f"🎯 Target dijaga: <b>{len(targets)}</b>\n"
        f"🚫 Ban global aktif: <b>{len(store.list_banned())}</b> user\n"
        f"👮 Admin bot: <b>{len(store.list_admins()) + 1}</b> (termasuk owner)\n"
        f"📋 Grup log: {log_status}\n"
        f"🚪 Ban-keluar: {bk}\n\n"
        "Pilih menu di bawah ini 👇")


def _dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Target & Gerbang", callback_data="dash:targets"),
         InlineKeyboardButton("🚫 Ban & Admin", callback_data="dash:ban")],
        [InlineKeyboardButton("📋 Log & Topik", callback_data="dash:log"),
         InlineKeyboardButton("💾 Backup & Bantuan", callback_data="dash:extra")],
    ])


def _back_row(callback_data: str = "dash:home"):
    return [InlineKeyboardButton("⬅️ Kembali", callback_data=callback_data)]


async def _dash_edit(query, text, keyboard, parse_mode="HTML"):
    try:
        await query.edit_message_text(text, parse_mode=parse_mode,
                                      reply_markup=keyboard)
    except Exception:
        pass  # isi tidak berubah / batas Telegram — abaikan


def _dash_targets_view(store, bot_username: str):
    targets = [t for t in store.list_targets()
               if t["_id"] != store.get_setting("log_group_id")]
    if not targets:
        text = ("🎯 <b>Target &amp; Gerbang</b>\n\nBelum ada target terdaftar. "
                "Target terdaftar otomatis saat bot dijadikan admin di "
                "channel/grup, atau saat ada member join lewat bot.")
        return text, InlineKeyboardMarkup([_back_row()])
    lines = ["🎯 <b>Target &amp; Gerbang</b>\n"]
    rows = []
    for t in targets:
        topik = (f"topik {t['thread_id']}" if t.get("thread_id")
                 else "topik belum dipasang")
        lines.append(f"• <b>{service._esc(t.get('title') or t['_id'])}</b> — {topik}")
        rows.append([InlineKeyboardButton(
            f"🔗 Link gerbang: {t.get('title') or t['_id']}",
            url=service.gateway_link(bot_username, t["_id"]))])
    lines.append("\nTap tombol link gerbang buat membuka/menyalin link yang "
                 "kamu sebar ke user.")
    rows.append(_back_row())
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _dash_ban_view(store):
    bk = ("biasa " + ("🟢 ON" if service.leave_ban_enabled(store) else "🔴 OFF")
          + " · gerbang "
          + ("🟢 ON" if service.gateway_leave_ban_enabled(store) else "🔴 OFF"))
    text = (
        "🚫 <b>Ban &amp; Admin</b>\n\n"
        f"🚪 Ban-keluar sekarang: <b>{bk}</b>\n"
        f"🚫 Ban global aktif: <b>{len(store.list_banned())}</b> user\n"
        f"👮 Admin bot: <b>{len(store.list_admins()) + 1}</b> (termasuk owner)\n\n"
        "Tap Ban/Unban buat mulai — bot akan minta ID user-nya, lalu "
        "konfirmasi dulu sebelum dieksekusi.")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Ban User", callback_data="dashact:ban"),
         InlineKeyboardButton("♻️ Unban User", callback_data="dashact:unban")],
        [InlineKeyboardButton("📋 Daftar Ban", callback_data="dash:banlist"),
         InlineKeyboardButton("🚪 Ban-Keluar", callback_data="dash:bankeluar")],
        [InlineKeyboardButton("👮 Kelola Admin", callback_data="dash:admins")],
        _back_row(),
    ])
    return text, kb


def _dash_banlist_view(store):
    banned = store.list_banned()
    if not banned:
        text = "🚫 <b>Daftar Ban Global</b>\n\nKosong — belum ada user yang diblokir."
    else:
        lines = [f"🚫 <b>Daftar Ban Global ({len(banned)} user):</b>\n"]
        for u in banned[:30]:
            uname = f" @{service._esc(u['username'])}" if u.get("username") else ""
            lines.append(
                f"• {service._profile_anchor(u['_id'], u.get('name') or '-')} "
                f"(<code>{u['_id']}</code>){uname}")
        if len(banned) > 30:
            lines.append(f"\n… dan {len(banned) - 30} user lainnya (lihat /daftarban).")
        text = "\n".join(lines)
    return text, InlineKeyboardMarkup([_back_row("dash:ban")])


def _dash_admins_view(store, owner_id: int):
    owner_doc = store.get_user(owner_id) or {}
    lines = ["👮 <b>Kelola Admin</b>\n",
             f"👑 Owner: {service._profile_anchor(owner_id, owner_doc.get('name') or 'Owner')} "
             f"(<code>{owner_id}</code>)"]
    admins = store.list_admins()
    if admins:
        lines.append("\n<b>Admin tambahan:</b>")
        for a in admins:
            doc = store.get_user(a["_id"]) or {}
            lines.append(
                f"• {service._profile_anchor(a['user_id'], doc.get('name') or '-')} "
                f"(<code>{a['user_id']}</code>)")
    else:
        lines.append("\nBelum ada admin tambahan.")
    lines.append("\nTambah/hapus admin khusus owner — bot akan minta ID "
                 "user-nya lalu konfirmasi.")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Tambah Admin", callback_data="dashact:addadmin"),
         InlineKeyboardButton("➖ Hapus Admin", callback_data="dashact:deladmin")],
        _back_row("dash:ban"),
    ])
    return "\n".join(lines), kb


def _dash_log_view(store):
    def _topic_label(setting_key):
        thread = store.get_setting(setting_key)
        return f"terpasang (topik {thread})" if thread else "belum dipasang"
    targets = [t for t in store.list_targets()
               if t["_id"] != store.get_setting("log_group_id")]
    paired = sum(1 for t in targets if t.get("thread_id"))
    log_group = store.get_setting("log_group_id")
    text = (
        "📋 <b>Log &amp; Topik</b>\n\n"
        f"🗂 Grup log: {f'<code>{log_group}</code>' if log_group else 'BELUM dipasang ⚠️ — jalankan /setlog di grup log'}\n"
        f"🚫 Topik Log Ban: {_topic_label('ban_thread_id')}\n"
        f"🚪 Topik Log Keluar: {_topic_label('leaveban_thread_id')}\n"
        f"📁 Topik File Ban Global: {_topic_label('fileban_thread_id')}\n"
        f"🎯 Topik target: {paired} dari {len(targets)} target terpasang\n\n"
        "Tombol di bawah membuatkan semua topik yang belum ada (target + "
        "ban) sekaligus. Memindahkan topik tetap lewat /setbanlog, "
        "/setlogkeluar, /setfilebanlog, atau /pasang di topik tujuan.")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("♻️ Buat Topik Otomatis", callback_data="dashact:topics")],
        _back_row(),
    ])
    return text, kb


def _dash_extra_view(store):
    text = (
        "💾 <b>Backup &amp; Bantuan</b>\n\n"
        "💾 <b>Backup Data Ban</b> — unduh file JSON berisi daftar ban global "
        "(khusus owner). File-nya bisa dikirim ke bot lain buat restore.\n\n"
        "❓ <b>Bantuan</b> — panduan lengkap semua perintah &amp; cara kerja bot.")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💾 Backup Data Ban", callback_data="dashact:backup")],
        [InlineKeyboardButton("❓ Bantuan", callback_data="dash:help")],
        _back_row(),
    ])
    return text, kb


_DASH_ACTIONS = {
    "ban": ("🚫 <b>Ban User (global)</b>", "user yang mau di-ban global"),
    "unban": ("♻️ <b>Unban User</b>", "user yang mau dicabut ban globalnya"),
    "addadmin": ("➕ <b>Tambah Admin</b>", "user yang mau diangkat jadi admin bot"),
    "deladmin": ("➖ <b>Hapus Admin</b>", "admin yang mau dicabut status adminnya"),
}


async def dashboard_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if not _is_admin(update, context):
        await query.answer("Khusus admin bot.", show_alert=True)
        return
    store = _store(context)
    data = query.data or ""

    if data == "dash:cancel":
        context.user_data.pop("dash_pending", None)
        await query.answer("Dibatalkan")
        try:
            await query.edit_message_text("❌ Dibatalkan.")
        except Exception:
            pass
        return

    if data.startswith("dashcf:"):
        await _dash_confirm_cb(update, context, data)
        return

    if data.startswith("dashact:"):
        await _dash_action_cb(update, context, data)
        return

    await query.answer()
    if data == "dash:home":
        await _dash_edit(query, _dashboard_text(store), _dashboard_keyboard())
    elif data == "dash:targets":
        text, kb = _dash_targets_view(store, context.bot.username or "")
        await _dash_edit(query, text, kb)
    elif data == "dash:ban":
        text, kb = _dash_ban_view(store)
        await _dash_edit(query, text, kb)
    elif data == "dash:bankeluar":
        await _dash_edit(query, _bankeluar_status_text(store),
                         _bankeluar_keyboard(store), parse_mode="Markdown")
    elif data == "dash:banlist":
        text, kb = _dash_banlist_view(store)
        await _dash_edit(query, text, kb)
    elif data == "dash:admins":
        text, kb = _dash_admins_view(store, _owner_id(context))
        await _dash_edit(query, text, kb)
    elif data == "dash:log":
        text, kb = _dash_log_view(store)
        await _dash_edit(query, text, kb)
    elif data == "dash:extra":
        text, kb = _dash_extra_view(store)
        await _dash_edit(query, text, kb)
    elif data == "dash:help":
        await _dash_edit(query, HELP_TEXT,
                         InlineKeyboardMarkup([_back_row("dash:extra")]),
                         parse_mode="Markdown")


async def _dash_action_cb(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          data: str):
    query = update.callback_query
    store = _store(context)
    action = data.split(":", 1)[1]

    if action == "topics":
        ban_made = await service.ensure_ban_topics(context.bot, store)
        made = await service.ensure_all_target_topics(context.bot, store)
        await query.answer(
            f"♻️ Selesai — topik baru dibuat: {ban_made} topik ban, "
            f"{made} topik target.", show_alert=True)
        text, kb = _dash_log_view(store)
        await _dash_edit(query, text, kb)
        return

    if action == "backup":
        if not _is_owner(update, context):
            await query.answer("Backup khusus owner.", show_alert=True)
            return
        payload_data = store.export_bans()
        summary = service.validate_backup(payload_data)
        payload = json.dumps(payload_data, ensure_ascii=False, indent=1).encode("utf-8")
        stamp = datetime.now(ZoneInfo("Asia/Jakarta")).strftime("%Y%m%d-%H%M%S")
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=InputFile(io.BytesIO(payload),
                               filename=f"verifybot-databan-{stamp}.json"),
            caption=(f"💾 Backup DATA BAN — {summary['banned']} user ter-ban "
                     "global beserta data membernya. Simpan baik-baik ya."),
            parse_mode="Markdown")
        await query.answer("💾 File backup dikirim di bawah dasbor ini.")
        return

    if action in ("addadmin", "deladmin") and not _is_owner(update, context):
        await query.answer("Kelola admin khusus owner.", show_alert=True)
        return
    if action not in _DASH_ACTIONS:
        await query.answer()
        return
    title, who = _DASH_ACTIONS[action]
    context.user_data["dash_pending"] = action
    await query.answer()
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=(f"{title}\n\nKirim <b>ID Telegram</b> {who} ke sini.\n"
              "ID bisa kamu lihat di log (baris 🆔 ID — tap buat menyalin)."),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Batal", callback_data="dash:cancel")]]))


async def _dash_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           data: str):
    query = update.callback_query
    store = _store(context)
    _, action, raw_id = data.split(":", 2)
    user_id = int(raw_id)
    owner_id = _owner_id(context)

    if action in ("addadmin", "deladmin") and not _is_owner(update, context):
        await query.answer("Kelola admin khusus owner.", show_alert=True)
        return

    if action == "ban":
        await service.ban_user(context.bot, store, user_id)
        result = (f"✅ Selesai — user <code>{user_id}</code> sudah di-<b>ban "
                  "global</b>: diblokir dari bot + di-ban dari semua target. "
                  "Laporan lengkapnya ada di topik Log Ban.")
    elif action == "unban":
        await service.unban_user(context.bot, store, user_id)
        result = (f"✅ Selesai — ban global user <code>{user_id}</code> sudah "
                  "dicabut di bot dan di semua target.")
    elif action == "addadmin":
        store.add_admin(user_id, added_by=owner_id)
        result = (f"✅ Selesai — user <code>{user_id}</code> sekarang admin "
                  "bot. Dia bisa memakai perintah operasional, tapi tidak "
                  "bisa mengelola admin dan tidak bisa di-ban.")
    elif action == "deladmin":
        store.remove_admin(user_id)
        result = (f"✅ Selesai — status admin user <code>{user_id}</code> "
                  "sudah dicabut.")
    else:
        await query.answer()
        return
    await query.answer("Berhasil ✅")
    await _dash_edit(query, result,
                     InlineKeyboardMarkup([_back_row("dash:ban")]))


async def contact_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pesan kontak di DM: balasan atas prompt verifikasi nomor HP gerbang."""
    store, user = _store(context), update.effective_user
    message = update.effective_message
    if not user or user.is_bot or not message or not message.contact:
        return
    store.record_encounter(user.id, service.full_name_of(user), user.username,
                           is_premium=user.is_premium,
                           language_code=user.language_code)
    await service.handle_gateway_contact(context.bot, store, _owner_id(context),
                                         user, message.contact)


async def dash_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pesan teks DM: kalau sedang ada alur dasbor yang menunggu ID, proses
    sebagai ID; selain itu teruskan ke handler link gerbang seperti biasa."""
    pending = context.user_data.get("dash_pending") if context.user_data else None
    if not pending or not _is_admin(update, context):
        await owner_link_message(update, context)
        return
    store = _store(context)
    message = update.message
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.reply_text(
            "ID harus berupa angka ya. Kirim ID Telegram-nya, atau tap "
            "❌ Batal di pesan tadi kalau berubah pikiran.")
        return
    user_id = int(raw)
    context.user_data.pop("dash_pending", None)
    owner_id = _owner_id(context)
    title, _who = _DASH_ACTIONS[pending]

    problem = None
    if pending == "ban":
        if user_id == owner_id:
            problem = "Itu ID owner — owner tidak bisa di-ban 😄"
        elif store.is_admin(user_id):
            problem = ("User itu admin bot — cabut dulu status adminnya "
                       "lewat Kelola Admin, baru bisa di-ban.")
        elif store.is_banned(user_id):
            problem = f"User <code>{user_id}</code> sudah ter-ban global kok."
    elif pending == "unban":
        if not store.is_banned(user_id):
            problem = f"User <code>{user_id}</code> tidak sedang ter-ban global."
    elif pending == "addadmin":
        if user_id == owner_id:
            problem = "Owner sudah admin tertinggi, nggak perlu ditambahkan 😄"
        elif store.is_admin(user_id):
            problem = f"User <code>{user_id}</code> sudah jadi admin bot."
    elif pending == "deladmin":
        if user_id == owner_id:
            problem = "Owner tidak bisa dicabut statusnya 😄"
        elif not store.is_admin(user_id) or user_id == owner_id:
            problem = f"User <code>{user_id}</code> bukan admin tambahan."
    if problem:
        await message.reply_text(problem, parse_mode="HTML")
        return

    doc = store.get_user(user_id) or {}
    name_line = ""
    if doc.get("name"):
        name_line = (f"\n👤 Dari catatan bot: "
                     f"{service._profile_anchor(user_id, doc['name'])}"
                     + (f" (@{service._esc(doc['username'])})"
                        if doc.get("username") else ""))
    await message.reply_text(
        f"{title}\n\n🆔 ID: <code>{user_id}</code>{name_line}\n\n"
        "Yakin mau lanjut?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Ya, lanjutkan",
                                  callback_data=f"dashcf:{pending}:{user_id}")],
            [InlineKeyboardButton("❌ Batal", callback_data="dash:cancel")],
        ]))


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
            f"Isi: {summary['banned']} user ter-ban global (beserta data membernya).\n\n"
            "Simpan file ini baik-baik — isinya termasuk data member yang ter-ban. "
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
        f"• {summary['banned']} user ter-ban global (beserta data membernya)\n\n"
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
        # Tegakkan langsung: ban global ke semua target bot ini.
        for banned_id in result["banned_ids"]:
            await service.ban_user(context.bot, store, banned_id)
        await query.answer("Daftar ban tergabung ✅")
        await query.edit_message_text(
            "♻️ *Restore data ban selesai.* Yang digabungkan ke bot ini:\n"
            f"• {result['banned_users']} user ter-ban global — sudah langsung di-ban "
            "dari semua target bot ini\n\n"
            "Data lain (target, admin, settings, riwayat) tidak berubah. "
            "(Catatan ban-keluar di backup lama diabaikan — fitur itu sudah dicabut.)",
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
        if became_admin and chat.id != store.get_setting("log_group_id"):
            # Target baru: buatkan topik log-nya di grup log + pasangkan
            # otomatis (meniru grup log bot fsub) — owner tidak perlu lagi
            # membuat topik manual & /pasang.
            await service.ensure_target_topic(
                context.bot, store, chat.id, getattr(chat, "title", "") or "")
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

