# Bot Security / Pencatat Member Telegram

Bot ini menjaga channel & grup private kamu dengan **mode tanpa verifikasi** (keputusan owner 2026-11-15): member baru **langsung diterima otomatis** — tidak ada verifikasi nomor HP, tidak ada pesan verifikasi, tidak ada batas waktu. Bot mencatat data lengkap member (nama, username, ID, bio, foto profil, status Premium, bahasa, waktu join, dan link sumber kedatangannya — **tanpa nomor HP**), menyimpan **riwayat perubahan nama & username**, lalu mengirim catatan itu ke **topik khusus per target** di sebuah grup log forum.

Ada dua jalur masuk:

- **Jalur A — Join Request (link tipe "Request Admin Approval")**: user minta join → **langsung di-approve otomatis** oleh bot, datanya tercatat ke log.
- **Jalur B — Join Langsung (link langsung, misalnya link otomatis dari bot fsub/file sharing)**: user langsung jadi member penuh; bot hanya mencatat & mengirim log-nya.

Yang tetap ditegakkan penuh: **ban global** (user diblokir senyap dari bot + di-ban dari semua target, dan ditolak lagi di semua jalur join). Member yang keluar sendiri dari target **default-nya tidak di-ban** — ia bebas join lagi kapan pun; bot hanya mencatat peristiwa keluarnya ke topik Log Keluar. Perilaku itu bisa diganti owner lewat saklar **`/bankeluar`** (lihat bagian Ban di bawah): posisi ON mengembalikan aturan lama (keluar sendiri = di-ban dari target itu).

---

## Panduan Setting (langkah demi langkah)

### 1. Buat bot di @BotFather
1. Chat [@BotFather](https://t.me/BotFather) → `/newbot` → ikuti langkahnya (nama + username bot).
2. Simpan **token** bot-nya baik-baik. Token ini rahasia — taruh langsung di Config Vars Heroku (langkah 2), **jangan dikirim ke siapa pun lewat chat**.
3. (Opsional) Atur foto & deskripsi bot lewat BotFather biar tampilannya meyakinkan.

### 2. Env vars & deploy Heroku (ringkas)
Project ini sudah berisi `Procfile` (`worker: python main.py`) dan `requirements.txt`, jadi tinggal:

1. Buat app baru di Heroku, hubungkan ke repo/folder ini, deploy.
2. Di **Settings → Config Vars**, isi:
   - `BOT_TOKEN` — token dari BotFather
   - `OWNER_ID` — ID Telegram kamu (angka). Contoh punya owner project ini: `9270334972`
   - `MONGODB_URI` — URI MongoDB Atlas (buat cluster gratis di mongodb.com/atlas, lalu ambil connection string-nya)
   - `LOG_GROUP_ID` — ID grup log forum (boleh dikosongkan dulu; bisa diatur belakangan lewat `/setlog`, lihat langkah 4)
   - `MONGODB_DB` — opsional, default `verifybot`
3. Di tab **Resources**, pastikan dyno **worker** ON (dan web dyno OFF — bot ini tidak pakai web).
4. Cek tab **View logs**: harusnya muncul tulisan bot mulai polling.

> Semua state (data member, riwayat, daftar ban, daftar target, pemetaan topik) disimpan di **MongoDB**, bukan memori — jadi restart dyno Heroku tidak menghilangkan catatan apa pun.

### 3. Jadikan bot admin di tiap channel/grup target
Untuk **setiap** channel/grup yang mau dijaga:

- **Channel**: tambah bot sebagai **Administrator** dan aktifkan izin **Invite Users / Add Subscribers** (perlu buat approve/tolak join request & mengeluarkan subscriber). Izin lain boleh dimatikan.
- **Grup/supergroup**: tambah bot sebagai **Administrator** dan aktifkan izin:
  - **Ban users / mengeluarkan member** (perlu buat menegakkan ban)
  - **Invite Users via Link** (perlu buat approve join request)
- Target akan **terdaftar otomatis** di bot begitu bot menerima event dari chat itu (bot dijadikan admin, ada join request, atau ada member join). Cek daftarnya lewat perintah `/targets` di DM bot (khusus owner).

### 4. Buat grup log (forum) & atur LOG_GROUP_ID
1. Buat **grup baru** khusus log (isinya cukup kamu + bot ini).
2. Di pengaturan grup, aktifkan **Topics** (grup otomatis jadi supergroup forum).
3. Tambahkan bot sebagai **admin** di grup log ini — **wajib dengan izin Kelola Topik (Manage Topics)** supaya bot bisa membuat topik log otomatis per target (lihat langkah 5).
4. Tetapkan grup ini sebagai tujuan log, salah satu cara:
   - Isi `LOG_GROUP_ID` di Config Vars dengan ID grupnya (formatnya seperti `-100xxxxxxxxxx`), **atau**
   - Di dalam grup log, kirim perintah **`/setlog`** dari akun owner — bot akan menyimpan ID grup itu ke database.
5. Kalau pakai `/setlog`, pastikan Topics sudah aktif dulu — bot akan mengingatkan kalau belum.

### 5. Topik per target — OTOMATIS dibuat bot
Bot **membuat & memasangkan topiknya sendiri** di grup log, meniru grup log bot fsub: begitu bot dijadikan admin di target baru, target itu terdaftar dan bot langsung membuat topik bernama target itu di grup log (plus ikon emoji dari set resmi Telegram bila tersedia) — semua log member target itu masuk ke topiknya sendiri tanpa kamu perlu melakukan apa pun.

- Target lama yang belum punya topik dibuatkan saat kamu menjalankan `/setlog` lagi, atau kapan pun lewat perintah `/buattopik`.
- Topik yang sudah terpasang tidak pernah didobel/diutak-atik.
- Kalau pembuatan topik gagal (biasanya izin Kelola Topik bot di grup log belum aktif), log target itu sementara ditampung jadi file (lihat catatan di bawah) sampai topiknya ada.

### 6. Cadangan manual: pasangkan topik dengan /pasang
Hanya bila kamu mau menata sendiri (atau pembuatan otomatis gagal):
1. Buat topik sendiri di grup log, beri nama sesuai targetnya.
2. Buka topik itu, kirim **`/pasang`** *di dalam topik itu* (bukan di General).
3. Bot membalas dengan tombol berisi daftar target — **tap target yang sesuai**.
4. Selesai: bot menyimpan ID topik itu untuk target tersebut dan mengonfirmasi.

> Kalau ada target yang belum dipasangkan ke topik, log verifikasinya **ditampung** oleh bot (tidak dikirim sebagai pesan) dan dikeluarkan sebagai **satu file .txt** ke General grup log — keluarkan kapan saja dengan perintah `/logfile`, atau otomatis setiap 100 catatan tertampung. Bot memberi tahu sekali di grup log bahwa target itu belum punya topik. (Dalam file .txt, foto profil member tidak disertakan; semua data teksnya lengkap.)

### 6c. Topik khusus file data ban global (dibuat otomatis; manual = cadangan)
Topik ini **dibuat & dipasangkan otomatis oleh bot** saat `/setlog` atau `/buattopik`, sama seperti dua topik ban lainnya (6b & 6e). Cara manual hanya cadangan bila kamu mau memindahkannya ke topik lain: buat topik sendiri (mis. "File Ban Global"), buka topiknya, lalu kirim **`/setfilebanlog`** di dalam topik itu. Sesudah itu, setiap bot dijadikan admin di channel/grup baru, catatan sapuan daftar ban global ke target tersebut dikirim ke topik ini sebagai **satu file** bernama `File data ban global (<nama channel/grup>).txt` — berisi daftar user ban global yang diterapkan ke target itu (nama, username, ID, status berhasil/gagal per user). Ini terpisah dari topik log ban biasa (yang isinya laporan peristiwa ban per user). Belum dipasang → file-nya jatuh ke General grup log.

### 6d. Membaca isi file sebagai chat lagi (/ekstrak)
File hasil bot (file data ban global maupun file log tanpa topik) bisa diekstrak kembali jadi pesan chat: **reply (balas) pesan file-nya** di grup log, lalu kirim **`/ekstrak`** pada balasan itu. Bot membaca file-nya, **membuat topik baru sendiri** bernama "📄 Ekstrak — <nama target>", dan memposting isinya di sana sebagai pesan-pesan chat (satu pesan per entri user/catatan) supaya detailnya bisa dibaca & dicari langsung. Kalau bot tidak punya izin membuat topik, bot akan meminta jalan cadangan: buka topik tujuanmu, lalu kirim `/tempel` di dalamnya.

### 6b. Topik khusus log ban global (dibuat otomatis; manual = cadangan)
Topik ini **dibuat & dipasangkan otomatis oleh bot** saat `/setlog` atau `/buattopik`. Cara manual hanya cadangan: buat topik sendiri di grup log (mis. "Log Ban"), buka topiknya, lalu kirim **`/setbanlog`** di dalam topik itu. Semua catatan **ban global** (ban manual, unban, upaya join user ter-ban global yang digagalkan) akan masuk ke topik ini, terpisah dari log verifikasi. Catatan member keluar **tidak** masuk ke sini — itu masuk topik "Log Keluar" (lihat 6e); file sapuan daftar ban juga **tidak** masuk ke sini — itu masuk topik "File Ban Global" (lihat 6c). Belum dipasang → catatan ban jatuh ke General grup log.

### 6e. Topik khusus log keluar (dibuat otomatis; manual = cadangan) — SATU topik untuk SEMUA target
Topik ini **dibuat & dipasangkan otomatis oleh bot** saat `/setlog` atau `/buattopik` dengan nama "Log Keluar". Cara manual hanya cadangan: buat topik sendiri, buka topiknya, lalu kirim **`/setlogkeluar`** di dalam topik itu. Catatan member keluar dari **target mana pun** semuanya masuk ke topik ini — satu topik bersama untuk semua target, **bukan** ke topik target masing-masing — dan **terpisah dari log ban global** supaya tidak kecampur. Member yang keluar **tidak di-ban**, hanya dicatat. Belum dipasang → catatan keluar ikut masuk ke topik log ban global (lalu ke General bila itu pun belum ada). (Topik lama bernama "Log Ban Keluar" yang sudah terpasang tetap dipakai apa adanya — routing-nya pakai ID topik; silakan rename manual bila mau.)

### 7. Atur link masuknya
**Jalur A (disarankan untuk link yang kamu sebar sendiri):**
1. Buka channel/grup target → **Invite Links** → **Create a New Link**.
2. Aktifkan **Request Admin Approval**, beri nama link sesuai sumbernya (mis. `FSub — File A`, `Bio IG`, dst). Nama link ini akan tercatat di log sebagai **sumber kedatangan** user — jadi buat satu link bernama per sumber penempatan biar ketahuan user datang dari mana.
3. Sebar link itu. User yang klik akan mengirim join request, bukan langsung masuk.

**Jalur B (kasus bot fsub/file sharing):**
Bot fsub biasanya **membuat link-nya sendiri secara otomatis** dan link itu tipe langsung masuk — tidak bisa diganti dari luar. Tidak apa-apa, tetap pakai link itu:
- Kalau targetnya **grup**: user masuk → langsung jadi member penuh (tidak dibatasi apa pun); bot mencatat datanya & mengirim log. Sesudah itu dia balik ke bot fsub dan cek join seperti biasa (status membernya sudah sah).
- Kalau targetnya **channel**: sama — join langsung diterima, bot mencatat & mengirim log-nya.

**Jalur Gerbang (link kombinasi bot — disarankan untuk link yang kamu sebar sendiri):**
Bot menerbitkan satu **link gerbang** per target, bentuknya link bot + kode target (`t.me/<usernamebot>?start=g<kode>`). Link gerbang inilah yang disebar ke user, bukan link mentah channel/grupnya:
1. Daftarkan link mentahnya sekali: kirim link invite biasa (langsung masuk) target itu ke DM bot sebagai owner. Bot mencocokkannya dengan target terdaftar (atau meminta owner memilih target lewat tombol) lalu membalas link gerbangnya. Lihat semua link gerbang kapan pun dengan `/gerbang`.
2. User klik link gerbang → bot terbuka & langsung tahu tujuannya → bot **langsung** menampilkan tombol **🚀 Masuk ke <nama target>** (tidak ada langkah verifikasi apa pun sebelumnya).
3. Tombol itu berisi link **tipe Request Admin Approval** yang diterbitkan bot khusus untuk user itu (bernama `Verifikasi <user_id>`, kedaluwarsa 1 jam; bila gagal diterbitkan, fallback: link langsung sekali pakai, lalu link mentah terdaftar). User tap tombol itu lalu tap **Minta Bergabung** — bot langsung menyetujui otomatis. Bot tidak bisa memasukkan user secara otomatis; bergabung tetap tindakan user sendiri. Satu-satunya yang ditolak di pintu adalah user yang ter-ban global.
4. Begitu link terbitan itu menghasilkan join/persetujuan, **bot langsung mencabutnya** (revoke) — salinan yang beredar ikut mati. Pesan tombol masuk dan pesan log juga dikirim dengan **protect_content** (tidak bisa diteruskan/disalin keluar). Catatan: membatasi simpan/teruskan/screenshot untuk **isi channel secara keseluruhan** adalah pengaturan pemilik channel (Edit channel → *Restrict saving content*), tidak bisa diubah bot lewat API; screenshot sendiri tidak pernah bisa diblokir 100% oleh Telegram.
5. Join-nya tercatat & terbit di log topik target itu.

User yang membuka bot tanpa link gerbang (`/start` biasa) ditawari daftar target untuk dipilih. Catatan: jalur gerbang tidak menggantikan tombol join bawaan bot fsub (link-nya dibuat bot fsub sendiri); jalur gerbang berlaku untuk semua tempat yang link-nya kamu pasang sendiri.

### 8. Mode tanpa verifikasi — ringkasan perilaku
- **Tidak ada gerbang sama sekali**: join request langsung di-approve, join langsung langsung diterima penuh. Tidak ada pesan verifikasi ke user, tidak ada batas waktu, tidak ada yang dikeluarkan karena "belum verifikasi".
- Bot bekerja di belakang layar: mencatat data member + mengirim log ke topik target masing-masing.
- Pengecualian satu-satunya: user **ter-ban global** ditolak/di-ban di semua jalur. Member yang pernah keluar dari target bebas masuk lagi seperti user baru.
- Nama member di pesan log, laporan ban, dan daftar `/daftarban` **bisa di-tap untuk membuka profilnya** — termasuk member yang **tidak punya username**, karena bot menautkan namanya berbasis ID (`tg://user?id=...`). Batasan Telegram: tautan profil ini dijamin jalan bila usernya pernah menghubungi bot atau sedang jadi member di chat tempat pesan itu tampil; untuk ID yang belum pernah berinteraksi sama sekali (mis. ID asing dari daftar ban pribadi owner), tautannya mungkin tidak terbuka — ID-nya tetap tercantum persis untuk dipakai perintah `/ban` / `/unban`.

### 9. Checklist tes pakai akun kedua
Sebelum dipakai beneran, tes dulu pakai akun Telegram kedua (bukan akun owner):

- [ ] Akun kedua klik link **Request Admin Approval** → join request **langsung ter-approve otomatis** (tanpa DM apa pun dari bot), log lengkap + blok riwayat muncul di **topik yang benar** di grup log (dengan foto profil bila ada), dan di log itu **tidak ada baris nomor HP**
- [ ] Akun kedua join ke **grup** target lewat link langsung → langsung jadi member penuh (bisa kirim pesan), **tidak ada pesan verifikasi** di grup, log-nya muncul di topik target
- [ ] Akun kedua klik **link gerbang** target → bot langsung menampilkan tombol **🚀 Masuk** tanpa meminta apa pun → tap tombol + Minta Bergabung → langsung di-approve
- [ ] Akun kedua **keluar sendiri** dari sebuah target → TIDAK ter-ban (saklar `/bankeluar` posisi awal OFF); catatan keluarnya muncul di topik Log Keluar (satu topik bersama untuk semua target); join lagi ke target yang sama → langsung diterima seperti biasa
- [ ] `/bankeluar` di DM → nyalakan saklarnya → akun kedua keluar sendiri → langsung ter-ban dari target itu (laporan masuk topik Log Keluar); join lagi → ditolak; matikan lagi saklarnya → perilaku balik normal + ban-keluar yang barusan terbentuk tercabut otomatis
- [ ] `/start` sebagai owner/admin → muncul Dasbor (ringkasan + 4 menu); tap 🚫 Ban & Admin → Ban User → kirim ID akun kedua → konfirmasi ✅ → akun itu ter-ban global; tap 📋 Log & Topik → status topik-topik tampil benar
- [ ] Ganti nama/username akun kedua, lalu join target lain → di log target itu, blok **👤 Riwayat** menampilkan nama/username lama *dan* baru, terbaru di atas
- [ ] Jalankan `/targets` dari akun owner → semua target muncul, yang sudah dipasang menunjukkan nomor topiknya
- [ ] Jadikan bot admin di satu target BARU → topik bernama target itu muncul sendiri di grup log & log member pertamanya masuk ke topik itu

---

## Perintah-perintah

| Perintah | Siapa | Di mana | Fungsi |
|---|---|---|---|
| `/start` | semua | DM bot | User biasa: sambutan + daftar target (tap target → tombol masuk langsung). Owner/admin: **Dasbor Bot Security** (menu inline, lihat di bawah) |
| `/targets` | owner | DM bot | Daftar target terdaftar + status topiknya |
| `/gerbang` | owner | DM bot | Daftar link gerbang semua target |
| `/ban <id_user>` | owner | DM bot | Blokir user dari bot + ban dari semua target |
| `/unban <id_user>` | owner | DM bot | Cabut blokir + unban di semua target |
| `/daftarban` | owner | DM bot | Daftar user yang sedang diblokir |
| `/bankeluar` | owner/admin | DM bot | Saklar ban-keluar: lihat status, nyalakan/matikan (atau `/bankeluar on` / `/bankeluar off`) |
| *(kirim link invite)* | owner | DM bot | Daftarkan link mentah sebuah target; bot membalas link gerbangnya |
| `/setlog` | owner | grup log | Tetapkan grup ini sebagai tujuan log (tersimpan di DB) |
| `/pasang` | owner | di dalam topik grup log | Pasangkan topik ini ke salah satu target (pilih lewat tombol) |

## Ban user
`/ban <id_user>` (ID terlihat di log verifikasi, baris 🆔 ID) melakukan tiga hal sekaligus: (1) user **diblokir dari bot secara senyap** — /start dan link gerbang tidak dibalas apa pun, bot seolah tidak ada; (2) user **di-ban dari semua channel/grup target** tempat bot admin (grup log dikecualikan); (3) penegakan berkelanjutan — kalau user itu tetap nyelonong masuk target (mis. link lama) atau mengirim join request, bot otomatis **men-ban/menolaknya lagi**. Daftar ban juga **diterapkan otomatis ke target baru**: begitu bot dijadikan admin di channel/grup mana pun, seluruh daftar ban disapu ke sana — member lama yang terdaftar langsung di-ban, dan yang belum pernah join ikut terblokir (ban Telegram berlaku juga untuk non-member, jadi mereka tidak akan bisa masuk sesudahnya); owner menerima ringkasan hasil sapuan lewat DM. `/unban <id_user>` membalikkan semuanya (unban tidak mengembalikan status member; user bergabung lagi sendiri — lewat link gerbang langsung dapat tombol masuk).

**Semua log hanya ke grup log:** log verifikasi, log ban, hasil sapuan daftar ban, dan peringatan (mis. target belum dipasangkan ke topik) semuanya masuk ke grup log — bot tidak mengirim log apa pun ke DM owner. Yang tetap muncul di chat pribadi hanyalah balasan atas perintah yang owner/admin ketik sendiri (mis. hasil `/ban`, isi `/backup`).

**Backup & restore DATA BAN saja:** perintah `/backup` (khusus owner, chat pribadi) membuat bot mengirim file JSON berisi **khusus data ban**: daftar ban global (beserta data member yang ter-ban, supaya bot tujuan mengenali orangnya) dan daftar ban-karena-keluar per target. Data lain (target, pairing topik, admin, settings, riwayat user lain) TIDAK ikut. Untuk **memindahkan daftar ban ke bot lain**: kirim file itu ke chat pribadi bot tujuan dan tap **✅ Ya, gabungkan daftar bannya** — daftar ban **digabung** (bukan menimpa data lain) lalu **langsung ditegakkan**: user ban global di-ban dari semua target bot tujuan, dan ban-karena-keluar diterapkan ke target asalnya. File backup berisi data member yang ter-ban — simpan baik-baik, jangan disebar.

**Dasbor admin (`/start` di DM, khusus owner/admin):** ringkasan langsung (jumlah target, ban global aktif, jumlah admin, status grup log, status saklar ban-keluar) + 4 menu inline — 🎯 Target & Gerbang (daftar target + tombol link gerbang tiap target), 🚫 Ban & Admin (Ban/Unban User lewat alur tap → kirim ID → konfirmasi, Daftar Ban, saklar Ban-Keluar, Kelola Admin tambah/hapus dengan alur yang sama), 📋 Log & Topik (status semua topik + tombol Buat Topik Otomatis), 💾 Backup & Bantuan (kirim file backup data ban, panduan lengkap). Semua perintah teks tetap jalan seperti biasa; dasbor hanya cara pakai alternatif yang lebih gampang dari HP.

**Admin tambahan:** owner bisa mengangkat admin lain dengan `/tambahadmin <id_user>`, mencabutnya dengan `/hapusadmin <id_user>`, dan melihat daftarnya dengan `/daftaradmin` (ketiganya khusus owner, dari chat pribadi). Admin tambahan bisa memakai semua perintah operasional (/targets, /gerbang, /ban, /unban, /daftarban, /pasang, /setlog, /setbanlog, /help, dan mendaftarkan link gerbang), tapi tidak bisa menambah/menghapus admin, tidak bisa di-ban selama masih admin, dan tidak kena auto-ban saat keluar dari target. Owner (dari `OWNER_ID`) selalu jadi admin tertinggi dan tidak masuk daftar.

**Member keluar: default hanya dicatat — bisa diganti pakai saklar `/bankeluar`:** member yang **keluar sendiri** dari sebuah channel/grup target **tidak di-ban** selama saklar ban-keluar OFF (posisi awal) — ia bebas join lagi kapan pun lewat jalur apa pun, dan bot hanya mencatat peristiwa keluarnya (data member + target + waktu) ke topik Log Keluar bersama (satu topik untuk semua target). Owner/admin bisa menyalakan saklarnya kapan pun dari DM bot dengan `/bankeluar` (tombol on/off, atau `/bankeluar on|off`): selama ON, member yang keluar sendiri langsung **di-ban dari target itu** dan join lagi ditolak di semua jalur (join request ditolak, join langsung di-ban lagi, link gerbang ditolak senyap) — persis perilaku lama. Saklar berlaku untuk semua target sekaligus, langsung berlaku tanpa restart, dan perubahan saklar dicatat ke topik Log Keluar. Mematikan saklar (dari ON ke OFF) otomatis **mencabut semua ban-keluar yang dipasang bot** (catatan dihapus + ban Telegram dilepas; user ter-ban global tidak ikut dilepas), dan bot melaporkan jumlahnya saat saklar dimatikan. Yang tidak dicatat/di-ban saat keluar: owner, admin, bot lain, user ter-ban global, dan grup log. Catatan ban-keluar LAMA dari sistem sebelum saklar ada dicabut otomatis SEKALI saat bot start pertama kali versi ini (catatan dihapus + ban Telegram yang dipasang bot dilepas; ban global tidak disentuh) — catatan baru yang dibuat saat saklar ON tidak ikut terhapus oleh restart. Peristiwa ban global dicatat ke topiknya masing-masing: ban global (manual, penolakan upaya join) ke topik Log Ban, dan sapuan daftar ban ke topik File Ban Global (lihat langkah setup no. 5). Khusus catatan per user (ban manual, unban, upaya join yang digagalkan), isinya **laporan lengkap**: foto profil member, data member (nama, username, ID, status Premium, bahasa, ban global, plus riwayat nama/username; nomor HP hanya tampil bila user itu pernah tercatat di sistem lama), dan **status dia di semua target** yang dicek langsung saat itu juga (member / bukan member / 🚫 TER-BAN di sini) — jadi langsung kelihatan dia ter-ban di mana saja. Catatan sapuan daftar ban tetap ringkas per target baru.

## Data yang dicatat di log
Nama lengkap, username (atau "(kosong)"), ID Telegram, bio (bila tersedia dari join request), status Premium, bahasa, waktu kejadian (WIB), nama target, sumber link (nama/URL invite link bila diketahui), foto profil (bila ada), dan blok riwayat nama & username sejak pertama kali bot menemui user itu. Nomor HP **tidak** dicatat (mode tanpa verifikasi).

## Keterbatasan (jujur)
- **Tanpa verifikasi = siapa pun bisa masuk.** Mode ini memang melepas saringan nomor HP (keputusan owner 2026-11-15): perlindungan yang tersisa adalah daftar ban — orang yang di-ban per ID akun tetap tertahan, tapi orang yang sama bisa kembali dengan akun Telegram baru. Bot tetap mencatat semua yang masuk, jadi pelacakan & ban manual selalu bisa dilakukan sesudahnya dari log.
- **Jalur langsung di channel bersifat best-effort.** Event `chat_member` untuk join langsung di channel bergantung pada perilaku Telegram (bot harus admin dan `chat_member` termasuk `allowed_updates` — sudah diatur di bot ini), tapi keandalannya di channel **perlu dibuktikan saat tes lapangan** dengan channel uji + akun kedua sebelum diandalkan. Untuk **grup/supergroup**, event member jauh lebih bisa diandalkan.
- **Riwayat nama/username hanya sejak bot berjalan.** Telegram tidak menyediakan API riwayat lama, jadi perubahan sebelum bot dipasang tidak tercatat; riwayat bertambah setiap bot menemui user itu lagi (join request, join, /start).
