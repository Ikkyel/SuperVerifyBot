"""Lapisan database MongoDB untuk bot verifikasi member.

Semua state disimpan di MongoDB (bukan memori) supaya tahan restart:
- users    : data user terverifikasi (nomor HP, status verified, snapshot profil)
- history  : riwayat nama & username per user_id
- pendings : verifikasi yang sedang menunggu (join request / join langsung) + deadline absolut
- targets  : chat target (channel/grup) yang dijaga bot + pemetaan topik log
- settings : konfigurasi runtime (LOG_GROUP_ID bila diatur lewat /setlog, dsb.)

Fungsi-fungsi murni (deadline, format riwayat, validasi contact) sengaja dibuat
tanpa dependensi jaringan supaya gampang dites.
"""

from __future__ import annotations

import html
import time
from datetime import datetime
from zoneinfo import ZoneInfo

WIB = ZoneInfo("Asia/Jakarta")
SECONDS_PER_TARGET = 30  # batas waktu verifikasi: 30 detik per target pending


# --------------------------------------------------------------------------- #
# Fungsi murni                                                                 #
# --------------------------------------------------------------------------- #

def deadline_for(anchor_ts: float, pending_count: int) -> float:
    """Deadline absolut = waktu pending pertama + 30 detik x jumlah pending."""
    return anchor_ts + SECONDS_PER_TARGET * max(1, pending_count)


def validate_contact(sender_id: int, contact_user_id) -> bool:
    """Nomor HP hanya sah kalau contact.user_id == id pengirimnya sendiri."""
    return contact_user_id is not None and int(contact_user_id) == int(sender_id)


def fmt_ts(ts: float) -> str:
    """Format epoch -> [DD/MM/YY HH:MM:SS] dalam WIB."""
    return datetime.fromtimestamp(ts, WIB).strftime("%d/%m/%y %H:%M:%S")


def display_username(username) -> str:
    if username:
        return f"@{username}"
    return "(kosong)"


def format_history_block(user_id: int, history_doc) -> str:
    """Blok riwayat persis format yang diminta owner; entri terbaru di atas."""
    names = (history_doc or {}).get("names", [])
    usernames = (history_doc or {}).get("usernames", [])
    lines = [f"👤 Riwayat untuk {user_id}", "Nama:"]
    for entry in reversed(names):
        lines.append(f"[{fmt_ts(entry['ts'])}] {html.escape(str(entry['name']))}")
    lines.append("Nama Pengguna:")
    for entry in reversed(usernames):
        lines.append(f"[{fmt_ts(entry['ts'])}] {html.escape(display_username(entry['username']))}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Store                                                                        #
# --------------------------------------------------------------------------- #

class Store:
    """Bungkus koleksi MongoDB. `db` adalah object database pymongo/mongomock."""

    def __init__(self, db):
        self.db = db
        self.users = db["users"]
        self.history = db["history"]
        self.pendings = db["pendings"]
        self.targets = db["targets"]
        self.settings = db["settings"]
        self.leave_bans = db["leave_bans"]
        self.admins = db["admins"]
        self.unpaired_logs = db["unpaired_logs"]

    # ---------------- settings ---------------- #
    def get_setting(self, key, default=None):
        doc = self.settings.find_one({"_id": "config"}) or {}
        return doc.get(key, default)

    def set_setting(self, key, value):
        self.settings.update_one({"_id": "config"}, {"$set": {key: value}}, upsert=True)

    # ---------------- targets ---------------- #
    def register_target(self, chat_id: int, title: str, chat_type: str):
        self.targets.update_one(
            {"_id": int(chat_id)},
            {"$set": {"title": title or "", "type": chat_type or ""},
             "$setOnInsert": {"thread_id": None}},
            upsert=True,
        )

    def get_target(self, chat_id: int):
        return self.targets.find_one({"_id": int(chat_id)})

    def list_targets(self):
        return list(self.targets.find({}).sort("title", 1))

    def set_target_thread(self, chat_id: int, thread_id: int):
        self.targets.update_one({"_id": int(chat_id)},
                                {"$set": {"thread_id": int(thread_id)}})

    def set_target_raw_link(self, chat_id: int, link: str):
        """Link invite mentah (langsung masuk) milik target, didaftarkan owner
        untuk fitur link gerbang."""
        self.targets.update_one({"_id": int(chat_id)},
                                {"$set": {"raw_link": link}})

    # ---------------- users & riwayat ---------------- #
    def record_encounter(self, user_id: int, full_name: str, username,
                         ts: float = None, **snapshot):
        """Catat pertemuan dengan user: snapshot profil + riwayat nama/username.

        Entri riwayat baru hanya ditambah bila nilai berubah dari catatan
        terakhir. Username kosong disimpan sebagai None (ditampilkan "(kosong)").
        """
        ts = time.time() if ts is None else ts
        user_id = int(user_id)
        uname = username or None

        set_fields = {"name": full_name or "", "username": uname, "last_seen": ts}
        for key in ("is_premium", "language_code", "bio"):
            if snapshot.get(key) is not None:
                set_fields[key] = snapshot[key]
        self.users.update_one(
            {"_id": user_id},
            {"$set": set_fields,
             "$setOnInsert": {"phone": None, "verified": False}},
            upsert=True,
        )

        hist = self.history.find_one({"_id": user_id}) or {"names": [], "usernames": []}
        names = hist.get("names", [])
        usernames = hist.get("usernames", [])
        changed = False
        if not names or names[-1]["name"] != (full_name or ""):
            names.append({"ts": ts, "name": full_name or ""})
            changed = True
        if not usernames or usernames[-1]["username"] != uname:
            usernames.append({"ts": ts, "username": uname})
            changed = True
        if changed:
            self.history.update_one(
                {"_id": user_id},
                {"$set": {"names": names, "usernames": usernames}},
                upsert=True,
            )
        return self.users.find_one({"_id": user_id})

    def get_user(self, user_id: int):
        return self.users.find_one({"_id": int(user_id)})

    def get_history(self, user_id: int):
        return self.history.find_one({"_id": int(user_id)})

    def is_verified(self, user_id: int) -> bool:
        doc = self.get_user(user_id)
        return bool(doc and doc.get("verified") and doc.get("phone"))

    def mark_verified(self, user_id: int, phone: str):
        self.users.update_one({"_id": int(user_id)},
                              {"$set": {"phone": phone, "verified": True}})

    # ---------------- link gerbang ---------------- #
    def set_user_gateway(self, user_id: int, chat_id: int):
        """Catat target tujuan user yang datang lewat link gerbang."""
        self.users.update_one({"_id": int(user_id)},
                              {"$set": {"gateway_target": int(chat_id)}})

    def get_user_gateway(self, user_id: int):
        doc = self.get_user(user_id)
        return doc.get("gateway_target") if doc else None

    def clear_user_gateway(self, user_id: int):
        self.users.update_one({"_id": int(user_id)},
                              {"$set": {"gateway_target": None}})

    # ---------------- ban ---------------- #
    def set_banned(self, user_id: int, banned: bool, ts: float = None):
        ts = time.time() if ts is None else ts
        self.users.update_one(
            {"_id": int(user_id)},
            {"$set": {"banned": bool(banned), "banned_at": ts if banned else None},
             "$setOnInsert": {"phone": None, "verified": False,
                              "name": "", "username": None}},
            upsert=True,
        )

    def is_banned(self, user_id: int) -> bool:
        doc = self.get_user(user_id)
        return bool(doc and doc.get("banned"))

    # ---------------- admin tambahan ---------------- #
    def add_admin(self, user_id: int, added_by: int, ts: float = None):
        ts = time.time() if ts is None else ts
        self.admins.update_one(
            {"_id": int(user_id)},
            {"$set": {"added_by": int(added_by), "ts": ts}},
            upsert=True,
        )

    def remove_admin(self, user_id: int) -> bool:
        return self.admins.delete_one({"_id": int(user_id)}).deleted_count > 0

    def is_admin(self, user_id: int) -> bool:
        return self.admins.find_one({"_id": int(user_id)}) is not None

    def list_admins(self):
        return list(self.admins.find().sort("_id", 1))

    # ---------------- log target tanpa topik (ditampung -> file) ---------------- #
    def add_unpaired_log(self, entry: dict):
        """Tampung satu catatan log verifikasi milik target yang belum punya
        topik sendiri; nanti dikeluarkan sebagai file .txt."""
        self.unpaired_logs.insert_one(dict(entry))

    def list_unpaired_logs(self):
        return list(self.unpaired_logs.find().sort("ts", 1))

    def count_unpaired_logs(self) -> int:
        return self.unpaired_logs.count_documents({})

    def clear_unpaired_logs(self):
        self.unpaired_logs.delete_many({})

    # ---------------- backup / restore DATA BAN ---------------- #
    def export_bans(self) -> dict:
        """Backup KHUSUS data ban (permintaan owner): daftar ban global —
        beserta data member yang ter-ban supaya bot tujuan mengenali
        orangnya — dan daftar ban-karena-keluar per target. Data lain
        (target, admin, settings, riwayat) TIDAK ikut."""
        return {
            "format": "verifybot-bans-backup",
            "version": 1,
            "exported_at": time.time(),
            "banned_users": list(self.users.find({"banned": True})),
            "leave_bans": list(self.leave_bans.find()),
        }

    def import_bans(self, data: dict) -> dict:
        """Gabungkan data ban dari backup ke database bot ini (BUKAN menimpa
        data lain): user ter-ban ditandai banned + datanya diperbarui,
        catatan ban-karena-keluar ditambahkan. Mengembalikan ringkasan +
        daftar yang harus ditegakkan ke target."""
        banned_users = data.get("banned_users") or []
        for doc in banned_users:
            doc = dict(doc)
            doc["banned"] = True
            self.users.update_one({"_id": doc["_id"]}, {"$set": doc}, upsert=True)
        leave_bans = data.get("leave_bans") or []
        for doc in leave_bans:
            self.leave_bans.update_one({"_id": doc["_id"]}, {"$set": doc},
                                       upsert=True)
        return {
            "banned_users": len(banned_users),
            "leave_bans": len(leave_bans),
            "banned_ids": [d["_id"] for d in banned_users],
            "leave_ban_pairs": [(d["user_id"], d["chat_id"]) for d in leave_bans
                                if "user_id" in d and "chat_id" in d],
        }

    def list_banned(self):
        return list(self.users.find({"banned": True}).sort("_id", 1))

    # ---------------- ban karena keluar (per target) ---------------- #
    def mark_left_ban(self, user_id: int, chat_id: int, ts: float = None):
        """Catat user di-ban dari SATU target karena keluar dari target itu."""
        ts = time.time() if ts is None else ts
        self.leave_bans.update_one(
            {"_id": f"{int(user_id)}:{int(chat_id)}"},
            {"$set": {"user_id": int(user_id), "chat_id": int(chat_id), "ts": ts}},
            upsert=True,
        )

    def has_left_ban(self, user_id: int, chat_id: int) -> bool:
        return self.leave_bans.find_one(
            {"_id": f"{int(user_id)}:{int(chat_id)}"}) is not None

    def clear_left_bans(self, user_id: int):
        self.leave_bans.delete_many({"user_id": int(user_id)})

    # ---------------- pendings ---------------- #
    def add_pending(self, user_id: int, chat_id: int, kind: str, ts: float = None,
                    invite_link: str = None, invite_name: str = None,
                    user_chat_id: int = None):
        """Tambah pending & hitung ulang deadline kumulatif semua pending user.

        Deadline ditambatkan ke pending paling awal: anchor + 30dtk x jumlah.
        Mengembalikan deadline absolut baru.
        """
        ts = time.time() if ts is None else ts
        user_id, chat_id = int(user_id), int(chat_id)
        pid = f"{user_id}:{chat_id}"
        existing = self.pendings.find_one({"_id": pid})
        created = existing["created"] if existing else ts
        self.pendings.update_one(
            {"_id": pid},
            {"$set": {"user_id": user_id, "chat_id": chat_id, "kind": kind,
                      "invite_link": invite_link, "invite_name": invite_name,
                      "user_chat_id": user_chat_id, "created": created}},
            upsert=True,
        )
        return self.refresh_deadlines(user_id)

    def refresh_deadlines(self, user_id: int):
        docs = list(self.pendings.find({"user_id": int(user_id)}))
        if not docs:
            return None
        anchor = min(d["created"] for d in docs)
        deadline = deadline_for(anchor, len(docs))
        self.pendings.update_many({"user_id": int(user_id)},
                                  {"$set": {"deadline": deadline}})
        return deadline

    def set_pending_prompt(self, user_id: int, chat_id: int, message_id: int):
        """Simpan message_id pesan verifikasi yang bot kirim DI GRUP target
        (jalur B grup), supaya pesan itu bisa dihapus otomatis begitu
        verifikasinya selesai / kedaluwarsa — grup tidak penuh dengan
        pesan-pesan verifikasi sisa. Disimpan di DB (bukan memori) supaya
        tetap bisa dihapus walau bot sempat restart di tengah jalan."""
        if not message_id:
            return
        self.pendings.update_one(
            {"_id": f"{int(user_id)}:{int(chat_id)}"},
            {"$set": {"prompt_message_id": int(message_id)}})

    def list_pendings(self, user_id: int = None):
        query = {} if user_id is None else {"user_id": int(user_id)}
        return list(self.pendings.find(query))

    def remove_pending(self, user_id: int, chat_id: int):
        self.pendings.delete_one({"_id": f"{int(user_id)}:{int(chat_id)}"})
        self.refresh_deadlines(user_id)

    def clear_pendings(self, user_id: int):
        docs = self.list_pendings(user_id)
        self.pendings.delete_many({"user_id": int(user_id)})
        return docs

    def expired_pendings(self, now: float = None):
        now = time.time() if now is None else now
        return [d for d in self.pendings.find({}) if d.get("deadline", 0) <= now]


def connect(mongo_uri: str, db_name: str = "verifybot"):
    """Buat Store dari URI MongoDB asli."""
    from pymongo import MongoClient

    client = MongoClient(mongo_uri)
    return Store(client[db_name])
