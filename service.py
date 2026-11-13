"""Logika inti bot verifikasi — dipisah dari handler PTB supaya bisa dites
dengan bot stub & database stub tanpa jaringan / token asli.

Semua fungsi menerima `bot` (object dengan method async ala telegram.Bot)
dan `store` (db.Store).
"""

from __future__ import annotations

import html
import io
import logging
import re
import time
from datetime import datetime

from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from db import WIB, display_username, format_history_block, validate_contact

log = logging.getLogger("verifybot.service")

VERIFY_PROMPT = (
    "Halo {name} 👋\n\n"
    "Permintaan/akses kamu ke *{target}* lagi menunggu verifikasi.\n"
    "Biar bisa lanjut, kamu *wajib* membagikan nomor HP kamu sendiri lewat "
    "tombol *📱 Kirim Nomor HP* di bawah ini.\n\n"
    "⏰ Batas waktu verifikasi: *{deadline_str}* (30 detik per target yang menunggu).\n"
    "Kalau lewat dari batas itu, permintaan kamu ditolak / kamu dikeluarkan "
    "dari target — tapi tenang, kamu bisa minta join lagi dan verifikasi ulang.\n\n"
    "Nomor HP cuma dipakai buat catatan verifikasi admin, nggak dibagikan ke siapa pun."
)


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Kirim Nomor HP", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )


GATEWAY_PROMPT = (
    "Halo {name} 👋\n\n"
    "Kamu mau masuk ke *{target}*.\n"
    "Sebelum itu, verifikasi nomor HP dulu ya — tap tombol *📱 Kirim Nomor HP* "
    "di bawah dan bagikan nomor HP kamu sendiri.\n\n"
    "Sesudah verifikasi lolos, tombol masuk ke *{target}* langsung muncul di sini. "
    "Nomor HP cuma dipakai buat catatan verifikasi admin, nggak dibagikan ke siapa pun."
)

_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base36(n: int) -> str:
    if n == 0:
        return "0"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = _B36[r] + out
    return out


def gateway_code(chat_id: int) -> str:
    """Kode deep-link untuk satu target; deterministik dari chat_id."""
    return "g" + _base36(abs(int(chat_id)))


def gateway_link(bot_username: str, chat_id: int) -> str:
    return f"https://t.me/{bot_username}?start={gateway_code(chat_id)}"


def target_for_code(store, code: str):
    if not code:
        return None
    for target in store.list_targets():
        if gateway_code(target["_id"]) == code:
            return target
    return None


async def make_entry_link(bot, store, target: dict, user_id: int):
    """Link masuk untuk user yang sudah terverifikasi.

    Urutan: (1) link tipe Request Admin Approval yang diterbitkan bot per
    user (bernama "Verifikasi <user_id>", kedaluwarsa 1 jam) — link ini
    yang paling aman disebar ulang sekalipun: isinya cuma hak MEMINTA join,
    dan bot hanya menyetujui peminta yang sudah terverifikasi; (2) link
    langsung sekali pakai (member_limit=1); (3) link mentah yang
    didaftarkan owner; (4) link utama chat dari getChat.
    None bila semuanya gagal.
    """
    chat_id = target["_id"]
    name = f"Verifikasi {user_id}"[:32]
    try:
        link = await bot.create_chat_invite_link(
            chat_id=chat_id, name=name, creates_join_request=True,
            expire_date=int(time.time()) + 3600)
        url = getattr(link, "invite_link", None)
        if url:
            return url
    except Exception as exc:
        log.warning("Gagal terbitkan link acc untuk %s: %s", chat_id, exc)
    try:
        link = await bot.create_chat_invite_link(
            chat_id=chat_id, name=name, member_limit=1)
        url = getattr(link, "invite_link", None)
        if url:
            return url
    except Exception as exc:
        log.warning("Gagal terbitkan link sekali pakai untuk %s: %s", chat_id, exc)
    if target.get("raw_link"):
        return target["raw_link"]
    try:
        chat = await bot.get_chat(chat_id)
        return getattr(chat, "invite_link", None)
    except Exception as exc:
        log.warning("Gagal ambil link utama %s: %s", chat_id, exc)
        return None


async def _revoke_minted_link(bot, chat_id: int, url, name):
    """Cabut link invite terbitan bot ("Verifikasi <user_id>") sesudah dipakai
    join, supaya salinannya yang beredar ikut mati. Link lain tidak disentuh."""
    if not url or not name or not str(name).startswith("Verifikasi "):
        return
    try:
        await bot.revoke_chat_invite_link(chat_id=chat_id, invite_link=url)
    except Exception as exc:
        log.warning("Gagal cabut link terbitan untuk %s: %s", chat_id, exc)


async def send_gateway_entry(bot, store, user_id: int, target: dict):
    """Kirim tombol masuk ke target untuk user terverifikasi."""
    title = target.get("title") or str(target["_id"])
    url = await make_entry_link(bot, store, target, user_id)
    if not url:
        await _send_dm(bot, user_id,
                       f"✅ Kamu sudah terverifikasi, tapi bot gagal menyiapkan link masuk ke "
                       f"*{_md_escape(title)}*. Kabari admin ya biar dibantu masuk manual.",
                       parse_mode="Markdown")
        return False
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"🚀 Masuk ke {title}", url=url)]])
    await _send_dm(bot, user_id,
                   f"✅ Verifikasi kamu sudah lolos!\n"
                   f"Tap tombol di bawah buat membuka *{_md_escape(title)}*, lalu tap "
                   f"*Minta Bergabung* di sana — karena kamu sudah terverifikasi, bot "
                   f"langsung menyetujui permintaanmu dalam hitungan detik.",
                   parse_mode="Markdown", reply_markup=markup, protect_content=True)
    return True


async def ban_user(bot, store, user_id: int):
    """Ban user: blokir dari bot + ban dari SEMUA target tempat bot admin.

    Mengembalikan daftar (target_doc, ok, error_str) hasil perambatan.
    """
    store.set_banned(user_id, True)
    # Batalkan pending apa pun miliknya supaya tidak ada yang menyelesaikannya,
    # dan hapus pesan verifikasi grup milik pending itu sekalian.
    for cleared in store.clear_pendings(user_id) or []:
        await _delete_prompt_message(bot, cleared)
    results = []
    for target in store.list_targets():
        if target["_id"] == store.get_setting("log_group_id"):
            continue  # grup log bukan target member; jangan ban orang dari log
        try:
            await bot.ban_chat_member(chat_id=target["_id"], user_id=user_id)
            results.append((target, True, ""))
        except Exception as exc:
            results.append((target, False, str(exc)))
    return results


async def unban_user(bot, store, user_id: int):
    """Cabut ban user: buka blokir bot + unban di semua target
    (termasuk ban-karena-keluar per target)."""
    store.set_banned(user_id, False)
    store.clear_left_bans(user_id)
    results = []
    for target in store.list_targets():
        if target["_id"] == store.get_setting("log_group_id"):
            continue
        try:
            await bot.unban_chat_member(chat_id=target["_id"], user_id=user_id,
                                        only_if_banned=True)
            results.append((target, True, ""))
        except Exception as exc:
            results.append((target, False, str(exc)))
    return results


def validate_backup(data) -> dict:
    """Periksa struktur file backup DATA BAN; kembalikan ringkasan isinya.

    Melempar ValueError dengan pesan yang bisa ditampilkan ke owner kalau
    file-nya bukan backup data ban bot ini atau rusak.
    """
    if not isinstance(data, dict) or data.get("format") != "verifybot-bans-backup":
        raise ValueError("File ini bukan backup data ban bot verifikasi "
                         "(penanda format tidak cocok).")
    banned_users = data.get("banned_users")
    if banned_users is None:
        banned_users = []
    if not isinstance(banned_users, list) or any(
            not isinstance(d, dict) or "_id" not in d for d in banned_users):
        raise ValueError("Isi backup rusak: daftar user ter-ban tidak valid.")
    leave_bans = data.get("leave_bans")
    if leave_bans is None:
        leave_bans = []
    if not isinstance(leave_bans, list):
        raise ValueError("Isi backup rusak: daftar ban-karena-keluar bukan daftar.")
    return {"banned": len(banned_users), "leave_bans": len(leave_bans)}


def is_admin_id(store, owner_id: int, user_id: int) -> bool:
    """Owner (dari env) selalu admin tertinggi; selebihnya dari daftar admin DB."""
    return int(user_id) == int(owner_id) or store.is_admin(user_id)


async def apply_blacklist(bot, store, chat_id: int):
    """Terapkan SELURUH daftar ban ke satu chat (dipakai saat bot baru
    dijadikan admin di sana): member lama yang terdaftar langsung di-ban,
    dan yang belum pernah join ikut terblokir karena ban Telegram berlaku
    juga untuk non-member. Mengembalikan dict berisi daftar ID yang
    berhasil & yang gagal."""
    applied_ids, failed_ids = [], []
    for user_doc in store.list_banned():
        try:
            await bot.ban_chat_member(chat_id=chat_id, user_id=user_doc["_id"])
            applied_ids.append(user_doc["_id"])
        except Exception as exc:
            failed_ids.append(user_doc["_id"])
            log.warning("Sapu daftar ban: gagal ban %s di %s: %s",
                        user_doc["_id"], chat_id, exc)
    return {"applied": applied_ids, "failed": failed_ids}


def _safe_filename_part(title: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\n\r]+', " ", str(title)).strip()
    return cleaned[:60] or "target"


async def send_sweep_file(bot, store, owner_id: int, chat_id: int,
                          chat_title: str, sweep: dict, ts: float = None):
    """Kirim catatan sapuan daftar ban global ke sebuah target baru sebagai
    SATU FILE .txt — bukan pesan chat — ke topik khusus file ban global
    (settings fileban_thread_id, dipasang dengan /setfilebanlog; beda dari
    topik log ban biasa). Belum dipasang -> General grup log. Nama file:
    'File data ban global (<nama channel/grup>)'. Kalau pengiriman file
    gagal, jatuh ke catatan teks biasa di topik ban supaya tidak hilang."""
    ts = time.time() if ts is None else ts
    log_group_id = store.get_setting("log_group_id")
    if not log_group_id:
        return False
    applied_ids = sweep.get("applied", [])
    failed_ids = sweep.get("failed", [])
    lines = [
        "DATA BAN GLOBAL — diterapkan ke target baru",
        f"Target: {chat_title} (ID {chat_id})",
        f"Waktu: {_now_str(ts)}",
        f"Hasil sapuan: {len(applied_ids)} user ter-ban diterapkan, "
        f"{len(failed_ids)} gagal.",
        "",
        f"DAFTAR USER BAN GLOBAL ({len(applied_ids) + len(failed_ids)} user):",
        "",
    ]
    for idx, user_id in enumerate(applied_ids + failed_ids, 1):
        doc = store.get_user(user_id) or {}
        mark = "✅ diterapkan" if user_id in applied_ids else "❌ GAGAL"
        lines.append(f"{idx}. {doc.get('name') or '-'}")
        lines.append(f"    Username: {display_username(doc.get('username'))}")
        lines.append(f"    ID: {user_id}")
        lines.append(f"    Nomor HP: {doc.get('phone') or '-'}")
        lines.append(f"    Status di target ini: {mark}")
        lines.append("")
    content = "\n".join(lines)
    filename = f"File data ban global ({_safe_filename_part(chat_title)}).txt"
    kwargs = {"chat_id": log_group_id,
              "document": io.BytesIO(content.encode("utf-8")),
              "filename": filename,
              "caption": (f"🧹 File data ban global untuk {chat_title} — "
                          f"{len(applied_ids)} user ter-ban diterapkan di sana."),
              "protect_content": True}
    thread_id = store.get_setting("fileban_thread_id")
    if thread_id:
        kwargs["message_thread_id"] = thread_id
    try:
        await bot.send_document(**kwargs)
        return True
    except Exception as exc:
        log.warning("Gagal kirim file data ban global untuk %s: %s", chat_id, exc)
        return await send_ban_log(
            bot, store, owner_id,
            "🧹 <b>Sapu daftar ban ke target baru</b> (bot baru jadi admin)\n\n"
            f"🎯 Target: {_esc(chat_title)}\n"
            f"✅ Diterapkan: {len(applied_ids)} user\n"
            f"❌ Gagal: {len(failed_ids)} user\n"
            "(Pengiriman versi file-nya gagal, jadi dicatat sebagai teks.)")


def ban_thread_id_for(store, kind: str):
    """Topik tujuan catatan ban menurut jenisnya, biar tidak kecampur:
    'global' -> topik log ban global (/setbanlog); 'keluar' -> topik log
    ban-keluar (/setlogkeluar), jatuh ke topik ban global bila belum
    dipasang, lalu ke General bila dua-duanya belum ada."""
    if kind == "keluar":
        return (store.get_setting("leaveban_thread_id")
                or store.get_setting("ban_thread_id"))
    return store.get_setting("ban_thread_id")


async def send_ban_log(bot, store, owner_id: int, html_text: str, kind: str = "global"):
    """Kirim catatan peristiwa ban ke topiknya di grup log (menurut `kind`:
    'global' atau 'keluar'). Belum dipasang -> jatuh ke General grup log.
    Grup log belum diatur -> dilewati (owner tetap dapat laporan langsung
    dari perintahnya). Isi dilindungi protect_content.
    """
    log_group_id = store.get_setting("log_group_id")
    if not log_group_id:
        return False
    base = {"chat_id": log_group_id, "protect_content": True}
    thread_id = ban_thread_id_for(store, kind)
    if thread_id:
        base["message_thread_id"] = thread_id
    try:
        await bot.send_message(text=html_text, parse_mode="HTML", **base)
        return True
    except Exception as exc:
        log.warning("Kirim log ban HTML gagal: %s — coba teks polos.", exc)
        try:
            plain = re.sub(r"</?[a-z]+>", "", html_text)
            await bot.send_message(text=plain, parse_mode=None, **base)
            return True
        except Exception as exc2:
            log.warning("Gagal kirim log ban: %s", exc2)
            return False


def _user_block(store, user_id: int) -> str:
    """Blok data member lengkap untuk log ban."""
    user = store.get_user(user_id) or {}
    lines = [
        "📋 <b>Data member:</b>",
        f"👤 Nama: {_esc(user.get('name') or '-')}",
        f"🔗 Username: {_esc(display_username(user.get('username')))}",
        f"🆔 ID: <code>{user_id}</code>",
        f"📱 Nomor HP: {_esc(user.get('phone') or '-')}",
        f"⭐ Premium: {'Ya' if user.get('is_premium') else 'Tidak'}",
        f"🌐 Bahasa aplikasi: {_esc(user.get('language_code') or '-')}",
        f"✅ Status verifikasi: {'Terverifikasi' if user.get('verified') else 'Belum terverifikasi'}",
        f"🚫 Ban global di bot: {'Ya' if user.get('banned') else 'Tidak'}",
    ]
    history = ""
    try:
        history = store.history_block(user_id)
    except Exception:
        history = ""
    if history:
        lines += ["", history]
    return "\n".join(lines)


_MEMBER_STATUS_LABELS = {
    "creator": "👑 Pemilik",
    "administrator": "🛡 Admin",
    "member": "👥 Member",
    "left": "➖ Bukan member",
    "kicked": "🚫 TER-BAN di sini",
}


async def membership_lines(bot, store, user_id: int):
    """Status user itu di SEMUA target (dicek langsung via getChatMember),
    supaya jelas dia member di mana saja dan ter-ban di mana saja."""
    lines = ["🎯 <b>Status di semua target:</b>"]
    found = False
    for target in store.list_targets():
        if target["_id"] == store.get_setting("log_group_id"):
            continue
        found = True
        label = "❔ tidak bisa dicek"
        try:
            member = await bot.get_chat_member(chat_id=target["_id"], user_id=user_id)
            status = getattr(getattr(member, "status", ""), "value",
                             getattr(member, "status", ""))
            if status == "restricted":
                label = ("👥 Member (dibatasi)" if getattr(member, "is_member", False)
                         else "🚪 Sudah keluar")
            else:
                label = _MEMBER_STATUS_LABELS.get(str(status), str(status))
        except Exception as exc:
            log.debug("Cek status %s di %s gagal: %s", user_id, target["_id"], exc)
        lines.append(f"• {_esc(target.get('title') or target['_id'])}: {label}")
    if not found:
        lines.append("• (belum ada target)")
    return lines


async def send_ban_report(bot, store, owner_id: int, title: str, user_id: int,
                          extra: str = "", ts: float = None, kind: str = "global"):
    """Laporan ban lengkap per user ke topik ban sesuai jenisnya (`kind`:
    'global' / 'keluar'): foto profil (bila ada) + data member lengkap +
    status di semua target + keterangan peristiwa."""
    ts = time.time() if ts is None else ts
    log_group_id = store.get_setting("log_group_id")
    if not log_group_id:
        return False
    thread_id = ban_thread_id_for(store, kind)
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos and getattr(photos, "total_count", 0) and photos.photos:
            photo_kwargs = {"chat_id": log_group_id,
                            "photo": photos.photos[0][0].file_id,
                            "protect_content": True}
            if thread_id:
                photo_kwargs["message_thread_id"] = thread_id
            await bot.send_photo(**photo_kwargs)
    except Exception as exc:
        log.debug("Kirim foto profil log ban %s gagal: %s", user_id, exc)
    lines = [title, "", _user_block(store, user_id), ""]
    lines += await membership_lines(bot, store, user_id)
    if extra:
        lines += ["", extra]
    lines.append(f"\n🕒 Waktu: {_now_str(ts)}")
    return await send_ban_log(bot, store, owner_id, "\n".join(lines), kind=kind)


async def handle_member_left(bot, store, owner_id: int, chat, user, ts: float = None):
    """Member keluar sendiri dari target -> otomatis di-ban dari target itu
    (tidak bisa join lagi). Per target saja, bukan ban global bot.

    Dilewati: grup log, owner, bot lain, user yang sudah ter-ban global.
    """
    ts = time.time() if ts is None else ts
    if chat.id == store.get_setting("log_group_id"):
        return "skip_log_group"
    if is_admin_id(store, owner_id, user.id) or getattr(user, "is_bot", False):
        return "skip"
    if store.is_banned(user.id):
        return "skip_banned"
    store.record_encounter(user.id, full_name_of(user), getattr(user, "username", None),
                           ts=ts, is_premium=getattr(user, "is_premium", None),
                           language_code=getattr(user, "language_code", None))
    # Kalau dia keluar di tengah verifikasi grup, pesan verifikasinya di
    # grup itu sudah tidak berguna: hapus, dan pending-nya ditutup (dia
    # sudah di-ban dari target ini, tidak ada lagi yang perlu ditunggu).
    try:
        for pending in store.list_pendings(user.id):
            if pending.get("chat_id") == chat.id:
                await _delete_prompt_message(bot, pending)
                store.remove_pending(user.id, chat.id)
    except Exception as exc:
        log.debug("Bersihkan pending saat %s keluar dari %s gagal: %s",
                  user.id, chat.id, exc)
    store.mark_left_ban(user.id, chat.id, ts)
    try:
        await bot.ban_chat_member(chat_id=chat.id, user_id=user.id)
    except Exception as exc:
        log.warning("Gagal auto-ban %s sesudah keluar dari %s: %s", user.id, chat.id, exc)
    target = store.get_target(chat.id) or {"_id": chat.id, "title": str(chat.id)}
    await send_ban_report(
        bot, store, owner_id,
        "🚪 <b>Keluar → otomatis di-ban dari target</b>",
        user.id,
        extra=(f"🎯 Target asal: {_esc(target.get('title') or chat.id)}\n"
               "User ini tidak bisa join lagi ke target tersebut "
               "(sampai owner menjalankan /unban)."),
        ts=ts, kind="keluar")
    return "left_banned"


def parse_export_file(text: str, file_name: str = "") -> dict:
    """Uraikan isi file teks hasil bot (file data ban global / file log tanpa
    topik) jadi {title, intro, entries} untuk diekstrak kembali ke chat.

    Judul diambil dari nama file 'File data ban global (<judul>)' bila cocok,
    selain itu dari baris 'Target: ...' di isi, selain itu nama file.
    Entri dipecah per blok bernomor ('1. ', '2. ', ...) atau per pemisah
    '====' pada file log; kalau dua-duanya tidak ketemu, isi dipecah per
    ~3500 karakter.
    """
    title = ""
    match = re.search(r"\((.+)\)\.txt$", file_name or "")
    if match:
        title = match.group(1)
    if not title:
        match = re.search(r"^Target:\s*(.+?)\s*\(ID ", text, re.M)
        if match:
            title = match.group(1)
    if not title:
        title = (file_name or "Ekstrak file").rsplit(".", 1)[0]

    lines = text.splitlines()
    entries, current, intro_lines = [], None, []
    for line in lines:
        if re.match(r"^\d+\.\s", line):
            if current is not None:
                entries.append("\n".join(current).strip())
            current = [line]
        elif current is not None:
            current.append(line)
        else:
            intro_lines.append(line)
    if current is not None:
        entries.append("\n".join(current).strip())
    if not entries:
        blocks = [b.strip() for b in re.split(r"={10,}", text) if b.strip()]
        if len(blocks) > 1:
            intro_lines = blocks[0].splitlines()
            entries = blocks[1:]
    intro = "\n".join(intro_lines).strip()
    if not entries:
        entries = [text[i:i + 3500] for i in range(0, len(text), 3500)] or [text]
        intro = ""
    return {"title": title, "intro": intro, "entries": entries}


async def _gateway_for_target(bot, store, user, target: dict):
    if store.is_banned(user.id):
        return "banned"  # senyap: tanpa balasan apa pun (permintaan owner)
    store.set_user_gateway(user.id, target["_id"])
    if store.is_verified(user.id):
        await send_gateway_entry(bot, store, user.id, target)
        return "entry_sent"
    text = GATEWAY_PROMPT.format(name=_md_escape(full_name_of(user)),
                                 target=_md_escape(target.get("title") or "target"))
    await _send_dm(bot, user.id, text, parse_mode="Markdown",
                   reply_markup=contact_keyboard())
    return "prompted"


async def handle_gateway_start(bot, store, owner_id: int, user, code: str,
                               bot_username: str = ""):
    """User membuka bot lewat link gerbang (?start=<kode>)."""
    target = target_for_code(store, code or "")
    if target is None:
        return "unknown"
    return await _gateway_for_target(bot, store, user, target)


async def handle_gateway_pick(bot, store, owner_id: int, user, chat_id: int,
                              bot_username: str = ""):
    """User memilih target dari daftar di /start biasa."""
    target = store.get_target(chat_id)
    if target is None:
        return "unknown"
    return await _gateway_for_target(bot, store, user, target)


async def register_gateway_link(bot, store, owner_id: int, link: str,
                                bot_username: str = ""):
    """Owner mengirim link invite mentah sebuah target (di DM).

    Bot mencocokkannya dengan link utama tiap target terdaftar; bila cocok,
    link disimpan & link gerbang dibalas. Bila tidak cocok, owner diminta
    memilih targetnya lewat tombol (link disimpan sementara di settings).
    """
    link = (link or "").strip()
    for target in store.list_targets():
        if target.get("raw_link") == link:
            if store.get_setting("pending_gateway_link") == link:
                store.set_setting("pending_gateway_link", None)
            await _send_dm(
                bot, owner_id,
                f"ℹ️ Link itu sudah terdaftar untuk *{target.get('title') or target['_id']}*.\n\n"
                f"Link gerbangnya:\n{gateway_link(bot_username, target['_id'])}",
                parse_mode="Markdown", disable_web_page_preview=True)
            return "already"
    for target in store.list_targets():
        try:
            chat = await bot.get_chat(target["_id"])
        except Exception:
            continue
        if getattr(chat, "invite_link", None) == link:
            store.set_target_raw_link(target["_id"], link)
            await _send_dm(
                bot, owner_id,
                f"✅ Link itu cocok dengan *{target.get('title') or target['_id']}* "
                f"dan sudah disimpan.\n\nLink gerbangnya (yang kamu sebar ke user):\n"
                f"{gateway_link(bot_username, target['_id'])}",
                parse_mode="Markdown")
            return "matched"
    store.set_setting("pending_gateway_link", link)
    targets = store.list_targets()
    if not targets:
        await _send_dm(bot, owner_id,
                       "Belum ada target terdaftar, jadi link itu belum bisa disimpan.")
        return "no_targets"
    buttons = [[InlineKeyboardButton(t.get("title") or str(t["_id"]),
                                     callback_data=f"glink:{t['_id']}")]
               for t in targets]
    await _send_dm(bot, owner_id,
                   "Link itu nggak cocok otomatis dengan link utama target mana pun "
                   "(mungkin link invite tambahan). Link itu buat target yang mana? Tap salah satu:",
                   reply_markup=InlineKeyboardMarkup(buttons))
    return "choose"


async def assign_gateway_link(bot, store, owner_id: int, chat_id: int,
                              bot_username: str = ""):
    """Pasangkan link mentah yang menunggu ke target pilihan owner."""
    link = store.get_setting("pending_gateway_link")
    if not link:
        return None
    target = store.get_target(chat_id)
    if target is None:
        return None
    store.set_target_raw_link(chat_id, link)
    store.set_setting("pending_gateway_link", None)
    return gateway_link(bot_username, chat_id)


def full_name_of(user) -> str:
    name = getattr(user, "full_name", None)
    if name:
        return name
    parts = [getattr(user, "first_name", "") or "", getattr(user, "last_name", "") or ""]
    return " ".join(p for p in parts if p).strip()


def _now_str(ts: float) -> str:
    return datetime.fromtimestamp(ts, WIB).strftime("%d/%m/%Y %H:%M:%S WIB")


def _md_escape(text) -> str:
    """Escape nilai dinamis (nama user, judul target) untuk parse_mode Markdown.

    Nama seperti '-_-' atau username ber-underscore membuat Telegram menolak
    pesan ("Can't parse entities") bila disisipkan mentah ke template Markdown.
    """
    out = str(text)
    for ch in ("_", "*", "`", "["):
        out = out.replace(ch, "\\" + ch)
    return out


def _plain_markdown(text: str) -> str:
    """Bersihkan format Markdown dari sebuah teks untuk pengiriman darurat
    sebagai teks polos (tanpa parse_mode).

    Dipakai bila Telegram menolak pesan Markdown ("Can't parse entities")
    karena nama/judul user mengandung karakter yang lolos dari _md_escape —
    pesannya tetap sampai (beserta tombolnya), hanya tanpa huruf tebal."""
    out = str(text)
    for esc in ("\\_", "\\*", "\\`", "\\["):
        out = out.replace(esc, esc[1])
    return out.replace("*", "")


def _is_parse_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "parse" in msg or "entities" in msg


async def _send_dm(bot, chat_id, text, **kwargs):
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return True
    except Exception as exc:  # user memblokir bot, dsb. — jangan jatuhkan handler
        log.warning("Gagal kirim DM ke %s: %s", chat_id, exc)
        if _is_parse_error(exc) and kwargs.get("parse_mode"):
            # Gagal format, bukan gagal kirim: coba lagi sebagai teks polos
            # supaya prompt verifikasi tetap sampai (tombol ikut terkirim).
            try:
                plain_kwargs = {k: v for k, v in kwargs.items()
                                if k != "parse_mode"}
                await bot.send_message(chat_id=chat_id,
                                       text=_plain_markdown(text),
                                       **plain_kwargs)
                log.info("DM ke %s terkirim sebagai teks polos "
                         "(Markdown ditolak Telegram).", chat_id)
                return True
            except Exception as exc2:
                log.warning("Gagal kirim DM teks polos ke %s: %s", chat_id, exc2)
        return False


async def _delete_prompt_message(bot, pending):
    """Hapus pesan verifikasi yang bot kirim di grup sebuah target (jalur B).

    Best-effort: pesan memang cuma 'sampah' sesudah verifikasinya beres —
    gagal hapus (pesan sudah hilang, bot kehilangan hak hapus, bot stub
    tanpa delete_message) tidak boleh menggagalkan proses utamanya.
    """
    if not pending:
        return
    message_id = pending.get("prompt_message_id")
    chat_id = pending.get("chat_id")
    if not message_id or not chat_id:
        return
    delete = getattr(bot, "delete_message", None)
    if delete is None:
        return
    try:
        await delete(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        log.debug("Gagal hapus pesan verifikasi grup %s/%s: %s",
                  chat_id, message_id, exc)


async def _remember_prompt_message(store, user_id: int, chat_id: int, message):
    """Catat message_id pesan verifikasi grup ke pending-nya di DB."""
    message_id = getattr(message, "message_id", None) if message else None
    if not message_id:
        return
    setter = getattr(store, "set_pending_prompt", None)
    if setter is None:
        return
    try:
        setter(user_id, chat_id, message_id)
    except Exception as exc:
        log.debug("Gagal catat message_id prompt %s/%s: %s",
                  user_id, chat_id, exc)


# --------------------------------------------------------------------------- #
# Log                                                                          #
# --------------------------------------------------------------------------- #

def _esc(value) -> str:
    """Escape teks dinamis (dari user) untuk parse_mode HTML."""
    return html.escape(str(value), quote=False)


def build_log_text(store, user_id: int, target: dict, event_label: str,
                   source: str, ts: float) -> str:
    # HTML, BUKAN Markdown: format riwayat memakai "[DD/MM/YY ...]" yang oleh
    # parser Markdown dibaca sebagai awal link dan membuat pengiriman gagal
    # ("Can't parse entities"). Semua nilai dari user di-escape.
    user = store.get_user(user_id) or {}
    lines = [
        f"✅ <b>{_esc(event_label)}</b>",
        "",
        f"👤 Nama: {_esc(user.get('name') or '-')}",
        f"🔗 Username: {_esc(display_username(user.get('username')))}",
        f"🆔 ID: <code>{user_id}</code>",
        f"📱 Nomor HP: {_esc(user.get('phone') or '(belum ada)')}",
        f"💎 Premium: {'Ya' if user.get('is_premium') else 'Tidak'}",
        f"🗣 Bahasa: {_esc(user.get('language_code') or '-')}",
    ]
    if user.get("bio"):
        lines.append(f"📝 Bio: {_esc(user['bio'])}")
    lines += [
        f"🎯 Target: {_esc(target.get('title') or target.get('_id'))} ({_esc(target.get('type') or '-')})",
        f"🔖 Sumber: {_esc(source or '-')}",
        f"🕒 Waktu: {_now_str(ts)}",
        "",
        format_history_block(user_id, store.get_history(user_id)),
    ]
    return "\n".join(lines)


def _source_of(pending_or_link_name, pending_or_link_url) -> str:
    name, url = pending_or_link_name, pending_or_link_url
    if name and url:
        return f'Link "{name}" ({url})'
    if url:
        return f"Link {url}"
    if name:
        return f'Link "{name}"'
    return "Join langsung / tidak diketahui"


async def send_log(bot, store, owner_id: int, user_id: int, target: dict,
                   event_label: str, source: str, ts: float = None):
    """Kirim catatan verifikasi ke topik grup log sesuai pemetaan target."""
    ts = time.time() if ts is None else ts
    log_group_id = store.get_setting("log_group_id")
    text = build_log_text(store, user_id, target, event_label, source, ts)
    if not log_group_id:
        log.warning("LOG_GROUP_ID belum diatur; log untuk %s dilewati.", user_id)
        return False

    thread_id = target.get("thread_id")
    if not thread_id:
        # Target BELUM punya topik sendiri: log-nya DITAMPUNG, bukan dikirim
        # sebagai pesan — nanti dikeluarkan sebagai satu file .txt ke General
        # grup log (perintah /logfile, atau otomatis tiap 100 catatan).
        plain = re.sub(r"</?(b|code)>", "", text)
        store.add_unpaired_log({
            "ts": ts, "user_id": user_id, "target_id": target.get("_id"),
            "target_title": target.get("title") or str(target.get("_id")),
            "event": event_label, "source": source, "text": plain,
        })
        notified = store.get_setting("notified_missing_topic", []) or []
        key = str(target.get("_id"))
        if key not in notified:
            notified.append(key)
            store.set_setting("notified_missing_topic", notified)
            try:
                await bot.send_message(
                    chat_id=log_group_id,
                    text=(f"ℹ️ Target {target.get('title') or key} belum dipasangkan ke "
                          "topik log, jadi log-nya ditampung dan akan dikirim sebagai "
                          "file ke sini. Pakai /logfile untuk mengeluarkannya sekarang, "
                          "atau pasangkan topiknya lewat /pasang."),
                    parse_mode=None)
            except Exception as exc:
                log.debug("Gagal kirim pemberitahuan topik ke grup log: %s", exc)
        if store.count_unpaired_logs() >= 100:
            await flush_unpaired_logs(bot, store)
        return True

    # protect_content: log berisi nomor HP — cegah diteruskan/disalin keluar
    # dari grup log oleh siapa pun yang bisa melihatnya.
    base = {"chat_id": log_group_id, "protect_content": True}
    if thread_id:
        base["message_thread_id"] = thread_id

    # Foto profil sebagai sampul log bila tersedia.
    photo_id = None
    try:
        photos = await bot.get_user_profile_photos(user_id=user_id, limit=1)
        if photos and photos.photos:
            photo_id = photos.photos[0][-1].file_id
    except Exception as exc:
        log.debug("get_user_profile_photos %s gagal: %s", user_id, exc)

    async def _do_send(body: str, parse_mode):
        if photo_id and len(body) <= 1024:
            await bot.send_photo(photo=photo_id, caption=body,
                                 parse_mode=parse_mode, **base)
        elif photo_id:
            await bot.send_photo(photo=photo_id, **base)
            await bot.send_message(text=body, parse_mode=parse_mode, **base)
        else:
            await bot.send_message(text=body, parse_mode=parse_mode, **base)

    sent = False
    try:
        await _do_send(text, "HTML")
        sent = True
    except Exception as exc:
        # Jangan sampai log hilang gara-gara format: coba ulang sebagai teks polos.
        log.warning("Kirim log HTML gagal untuk %s: %s — coba teks polos.", user_id, exc)
        try:
            plain = re.sub(r"</?(b|code)>", "", text)
            await _do_send(plain, None)
            sent = True
        except Exception as exc2:
            log.warning("Gagal kirim log untuk %s: %s", user_id, exc2)

    return sent


async def flush_unpaired_logs(bot, store) -> int:
    """Keluarkan tampungan log target tanpa topik sebagai satu file .txt ke
    General grup log, lalu kosongkan tampungannya. Mengembalikan jumlah
    catatan yang terkirim (0 kalau tampungan kosong / kirim gagal)."""
    log_group_id = store.get_setting("log_group_id")
    entries = store.list_unpaired_logs()
    if not log_group_id or not entries:
        return 0
    lines = [
        "LOG VERIFIKASI — TARGET TANPA TOPIK LOG SENDIRI",
        f"Dibuat: {_now_str(time.time())}",
        f"Jumlah catatan: {len(entries)}",
        "",
        "(Foto profil member tidak disertakan dalam file ini; semua data "
        "teksnya lengkap di bawah.)",
        "",
    ]
    for entry in entries:
        lines.append("=" * 60)
        lines.append(f"[{_now_str(entry.get('ts'))}] "
                     f"{entry.get('target_title')} — {entry.get('event')}")
        lines.append("-" * 60)
        lines.append(entry.get("text") or "")
        lines.append("")
    content = "\n".join(lines)
    stamp = datetime.now(WIB).strftime("%Y%m%d-%H%M%S")
    try:
        await bot.send_document(
            chat_id=log_group_id,
            document=io.BytesIO(content.encode("utf-8")),
            filename=f"log-tanpa-topik-{stamp}.txt",
            caption=(f"📄 Kumpulan log verifikasi dari target yang belum punya "
                     f"topik sendiri — {len(entries)} catatan."),
            protect_content=True)
    except Exception as exc:
        log.warning("Gagal kirim file log tanpa topik: %s", exc)
        return 0
    store.clear_unpaired_logs()
    return len(entries)


# --------------------------------------------------------------------------- #
# Jalur A — join request                                                       #
# --------------------------------------------------------------------------- #

async def handle_join_request(bot, store, owner_id: int, chat, user,
                              invite_link=None, user_chat_id=None, bio=None,
                              ts: float = None):
    """Proses satu ChatJoinRequest. `invite_link` object ChatInviteLink / None."""
    ts = time.time() if ts is None else ts
    store.register_target(chat.id, getattr(chat, "title", ""), getattr(chat, "type", ""))
    store.record_encounter(user.id, full_name_of(user), getattr(user, "username", None),
                           ts=ts, is_premium=getattr(user, "is_premium", None),
                           language_code=getattr(user, "language_code", None), bio=bio)
    target = store.get_target(chat.id)
    link_url = getattr(invite_link, "invite_link", None) if invite_link else None
    link_name = getattr(invite_link, "name", None) if invite_link else None
    source = _source_of(link_name, link_url)

    if store.is_banned(user.id):
        try:
            await bot.decline_chat_join_request(chat_id=chat.id, user_id=user.id)
        except Exception as exc:
            log.warning("Gagal decline join request user ter-ban %s: %s", user.id, exc)
        await send_ban_report(
            bot, store, owner_id,
            "🛡 <b>Upaya join user ter-ban digagalkan</b> (join request ditolak)",
            user.id,
            extra=f"🎯 Target upaya join: {_esc(target.get('title') or chat.id)}",
            ts=ts)
        return "banned"

    if store.has_left_ban(user.id, chat.id):
        try:
            await bot.decline_chat_join_request(chat_id=chat.id, user_id=user.id)
        except Exception as exc:
            log.warning("Gagal decline join request user ban-keluar %s: %s", user.id, exc)
        await send_ban_report(
            bot, store, owner_id,
            "🛡 <b>Upaya join user ban-keluar digagalkan</b> (pernah keluar dari target ini)",
            user.id,
            extra=f"🎯 Target upaya join: {_esc(target.get('title') or chat.id)}",
            ts=ts, kind="keluar")
        return "left_banned"

    if store.is_verified(user.id):
        await bot.approve_chat_join_request(chat_id=chat.id, user_id=user.id)
        await send_log(bot, store, owner_id, user.id, target,
                       "Member baru — sudah terverifikasi (join request langsung di-approve)",
                       source, ts)
        await _revoke_minted_link(bot, chat.id, link_url, link_name)
        return "approved_verified"

    deadline = store.add_pending(user.id, chat.id, kind="join_request", ts=ts,
                                 invite_link=link_url, invite_name=link_name,
                                 user_chat_id=user_chat_id)
    dm_target = user_chat_id or user.id
    text = VERIFY_PROMPT.format(name=_md_escape(full_name_of(user)),
                                target=_md_escape(target.get("title") or "target"),
                                deadline_str=_now_str(deadline))
    await _send_dm(bot, dm_target, text, parse_mode="Markdown",
                   reply_markup=contact_keyboard())
    return "pending"


# --------------------------------------------------------------------------- #
# Jalur B — join langsung                                                      #
# --------------------------------------------------------------------------- #

async def _restrict(bot, chat_id, user_id, allow: bool):
    if allow:
        perms = None
        try:
            chat = await bot.get_chat(chat_id)
            perms = getattr(chat, "permissions", None)
        except Exception:
            perms = None
        if perms is None:
            perms = ChatPermissions(can_send_messages=True, can_send_audios=True,
                                    can_send_documents=True, can_send_photos=True,
                                    can_send_videos=True, can_send_video_notes=True,
                                    can_send_voice_notes=True, can_send_polls=True,
                                    can_send_other_messages=True, can_add_web_page_previews=True,
                                    can_invite_users=True)
        await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=perms)
    else:
        perms = ChatPermissions(can_send_messages=False, can_send_audios=False,
                                can_send_documents=False, can_send_photos=False,
                                can_send_videos=False, can_send_video_notes=False,
                                can_send_voice_notes=False, can_send_polls=False,
                                can_send_other_messages=False, can_add_web_page_previews=False,
                                can_invite_users=False)
        await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=perms)


async def handle_direct_join(bot, store, owner_id: int, chat, user,
                             invite_link=None, ts: float = None, bot_username: str = ""):
    """Proses member yang langsung jadi member (grup: restrict; channel: pending saja)."""
    ts = time.time() if ts is None else ts
    chat_type = getattr(chat, "type", "")
    store.register_target(chat.id, getattr(chat, "title", ""), chat_type)
    store.record_encounter(user.id, full_name_of(user), getattr(user, "username", None),
                           ts=ts, is_premium=getattr(user, "is_premium", None),
                           language_code=getattr(user, "language_code", None))
    target = store.get_target(chat.id)
    link_url = getattr(invite_link, "invite_link", None) if invite_link else None
    link_name = getattr(invite_link, "name", None) if invite_link else None
    source = _source_of(link_name, link_url)
    is_group = chat_type in ("group", "supergroup")

    if store.is_banned(user.id):
        # User ter-ban yang masih bisa nyelonong masuk (mis. link lama):
        # langsung ban lagi dari target ini, tanpa unban.
        try:
            await bot.ban_chat_member(chat_id=chat.id, user_id=user.id)
        except Exception as exc:
            log.warning("Gagal ban ulang user ter-ban %s di %s: %s", user.id, chat.id, exc)
        await send_ban_report(
            bot, store, owner_id,
            "🛡 <b>Upaya join user ter-ban digagalkan</b> (langsung di-ban lagi)",
            user.id,
            extra=f"🎯 Target upaya join: {_esc(target.get('title') or chat.id)}",
            ts=ts)
        return "banned"

    if store.has_left_ban(user.id, chat.id):
        try:
            await bot.ban_chat_member(chat_id=chat.id, user_id=user.id)
        except Exception as exc:
            log.warning("Gagal ban ulang user ban-keluar %s di %s: %s", user.id, chat.id, exc)
        await send_ban_report(
            bot, store, owner_id,
            "🛡 <b>Upaya join user ban-keluar digagalkan</b> (pernah keluar dari target ini)",
            user.id,
            extra=f"🎯 Target upaya join: {_esc(target.get('title') or chat.id)}",
            ts=ts, kind="keluar")
        return "left_banned"

    if store.is_verified(user.id):
        await send_log(bot, store, owner_id, user.id, target,
                       "Member baru (sudah terverifikasi)", source, ts)
        await _revoke_minted_link(bot, chat.id, link_url, link_name)
        return "logged_verified"

    kind = "direct_group" if is_group else "direct_channel"
    deadline = store.add_pending(user.id, chat.id, kind=kind, ts=ts,
                                 invite_link=link_url, invite_name=link_name)
    if is_group:
        try:
            await _restrict(bot, chat.id, user.id, allow=False)
        except Exception as exc:
            log.warning("Gagal restrict %s di %s: %s", user.id, chat.id, exc)
        deep = f"https://t.me/{bot_username}?start=verify" if bot_username else None
        markup = (InlineKeyboardMarkup([[InlineKeyboardButton("✅ Verifikasi Sekarang", url=deep)]])
                  if deep else None)
        mention = _md_escape(f"@{user.username}" if getattr(user, "username", None) else full_name_of(user))
        group_text = (f"Halo {mention} 👋 Kamu baru bergabung dan sementara *dibatasi* dulu.\n"
                      f"Selesaikan verifikasi nomor HP lewat tombol di bawah sebelum "
                      f"*{_now_str(deadline)}* — kalau lewat, kamu akan dikeluarkan dari grup ini.")
        sent_msg = None
        try:
            sent_msg = await bot.send_message(
                chat_id=chat.id,
                text=group_text,
                parse_mode="Markdown", reply_markup=markup,
            )
        except Exception as exc:
            log.warning("Gagal kirim pesan verifikasi grup %s: %s", chat.id, exc)
            if _is_parse_error(exc):
                # Gagal format, bukan gagal kirim: coba lagi sebagai teks polos.
                try:
                    sent_msg = await bot.send_message(
                        chat_id=chat.id,
                        text=_plain_markdown(group_text),
                        reply_markup=markup,
                    )
                    log.info("Pesan verifikasi grup %s terkirim sebagai teks polos "
                             "(Markdown ditolak Telegram).", chat.id)
                except Exception as exc2:
                    log.warning("Gagal kirim pesan verifikasi grup teks polos %s: %s",
                                chat.id, exc2)
        # Catat message_id-nya: pesan ini dihapus otomatis begitu verifikasi
        # user ini selesai / kedaluwarsa / dibatalkan (lihat
        # complete_verification & expire_pendings), biar grup tidak penuh.
        await _remember_prompt_message(store, user.id, chat.id, sent_msg)
    else:
        # Channel tidak punya pesan publik per-user. Coba DM prompt verifikasi:
        # terkirim hanya bila user pernah Start bot (mis. diarahkan dari
        # postingan pin channel); kalau belum, _send_dm menelan error 403-nya.
        text = VERIFY_PROMPT.format(name=_md_escape(full_name_of(user)),
                                    target=_md_escape(target.get("title") or "target"),
                                    deadline_str=_now_str(deadline))
        await _send_dm(bot, user.id, text, parse_mode="Markdown",
                       reply_markup=contact_keyboard())
    return "pending"


# --------------------------------------------------------------------------- #
# Contact & penyelesaian verifikasi                                            #
# --------------------------------------------------------------------------- #

async def handle_contact(bot, store, owner_id: int, user, contact, ts: float = None):
    """Tangani pesan contact. Mengembalikan status string."""
    ts = time.time() if ts is None else ts
    store.record_encounter(user.id, full_name_of(user), getattr(user, "username", None), ts=ts)
    if store.is_banned(user.id):
        return "banned"  # senyap: tanpa balasan apa pun (permintaan owner)
    if not validate_contact(user.id, getattr(contact, "user_id", None)):
        await _send_dm(bot, user.id,
                       "⚠️ Nomor itu bukan nomor HP akun kamu sendiri. "
                       "Kirim ulang ya pakai tombol *📱 Kirim Nomor HP* dengan nomor milikmu.",
                       parse_mode="Markdown", reply_markup=contact_keyboard())
        return "rejected_foreign_number"

    phone = getattr(contact, "phone_number", "") or ""
    store.mark_verified(user.id, phone)
    await complete_verification(bot, store, owner_id, user, ts)
    await _send_dm(bot, user.id,
                   "✅ Verifikasi berhasil! Nomor kamu sudah tercatat dan akses kamu "
                   "ke semua target yang menunggu sudah dibuka. Selamat bergabung 🎉")
    return "verified"


async def complete_verification(bot, store, owner_id: int, user, ts: float = None):
    """Selesaikan SEMUA pending user: approve join request, buka restrict, kirim log."""
    ts = time.time() if ts is None else ts
    done = []
    for pending in store.list_pendings(user.id):
        chat_id = pending["chat_id"]
        target = store.get_target(chat_id) or {"_id": chat_id, "title": str(chat_id)}
        source = _source_of(pending.get("invite_name"), pending.get("invite_link"))
        try:
            if pending["kind"] == "join_request":
                await bot.approve_chat_join_request(chat_id=chat_id, user_id=user.id)
                label = "Member baru terverifikasi (join request di-approve)"
            elif pending["kind"] == "direct_group":
                await _restrict(bot, chat_id, user.id, allow=True)
                label = "Member baru terverifikasi (pembatasan grup dibuka)"
            else:
                label = "Member baru terverifikasi"
            await send_log(bot, store, owner_id, user.id, target, label, source, ts)
            await _revoke_minted_link(bot, chat_id, pending.get("invite_link"),
                                      pending.get("invite_name"))
            done.append(chat_id)
        except Exception as exc:
            log.warning("Gagal selesaikan pending %s/%s: %s", user.id, chat_id, exc)
        # Verifikasinya beres untuk target ini: pesan verifikasi di grupnya
        # (kalau ada) dihapus otomatis supaya grup bersih.
        await _delete_prompt_message(bot, pending)
        store.remove_pending(user.id, chat_id)
    # Tujuan dari link gerbang: sesudah verifikasi lolos, langsung kirim
    # tombol masuk ke target yang dia tuju.
    gw_chat = store.get_user_gateway(user.id)
    if gw_chat:
        gw_target = store.get_target(gw_chat)
        if gw_target is not None:
            await send_gateway_entry(bot, store, user.id, gw_target)
        store.clear_user_gateway(user.id)
    return done


# --------------------------------------------------------------------------- #
# Kedaluwarsa                                                                  #
# --------------------------------------------------------------------------- #

async def expire_pendings(bot, store, owner_id: int, now: float = None):
    """Proses semua pending yang deadline-nya sudah lewat.

    Join request -> decline. Grup/channel join langsung -> ban+unban (kick).
    Mengembalikan daftar (user_id, chat_id, kind) yang diproses.
    """
    now = time.time() if now is None else now
    processed = []
    by_user = {}
    for pending in store.expired_pendings(now):
        by_user.setdefault(pending["user_id"], []).append(pending)

    for user_id, pendings in by_user.items():
        any_done = False
        for pending in pendings:
            chat_id, kind = pending["chat_id"], pending["kind"]
            try:
                if kind == "join_request":
                    await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
                else:
                    await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
                    # Ban-karena-keluar bersifat permanen: jangan di-unban di
                    # sini, supaya status ter-bannya dari target itu utuh.
                    if not store.has_left_ban(user_id, chat_id):
                        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id,
                                                    only_if_banned=True)
            except Exception as exc:
                # PENTING: pending JANGAN dihapus — biarkan pemeriksa deadline
                # mencoba lagi di siklus berikutnya sampai penegakannya berhasil.
                # (Kasus nyata 2026-11-12: banChatMember timeout sekali dan
                # member yang belum verifikasi lolos tetap di dalam channel
                # karena pending-nya terlanjur terhapus.)
                log.warning("Gagal expire pending %s/%s (akan dicoba lagi): %s",
                            user_id, chat_id, exc)
                continue
            processed.append((user_id, chat_id, kind))
            # Waktunya habis & user sudah ditendang/ditolak: pesan verifikasi
            # di grupnya dihapus otomatis juga, jangan jadi sampah di grup.
            await _delete_prompt_message(bot, pending)
            store.remove_pending(user_id, chat_id)
            any_done = True
        if any_done:
            await _send_dm(bot, user_id,
                           "⏰ Batas waktu verifikasi kamu sudah habis, jadi permintaan/akses "
                           "yang menunggu sudah dibatalkan. Kamu bisa minta join lagi kapan pun "
                           "dan selesaikan verifikasinya dalam batas waktu ya.")
    return processed
