# Test maker bot

Telegram bot: test savollarini (fayl, rasm yoki matn) qabul qiladi va **Blooket** yoki **Kahoot** uchun
import qilinadigan faylni (Blooket: `.csv`, Kahoot: `.xlsx`) rasmiy shablonlar asosida yaratib beradi.

## O'rnatish

```bash
python -m pip install -r requirements.txt
cp .env.example .env   # so'ng BOT_TOKEN va (ixtiyoriy) GEMINI_API_KEY ni yozing
python -m bot
```

Lokal ishga tushirishda bot **polling** rejimida ishlaydi. Agar bot Render'da ishlab turgan bo'lsa,
lokal nusxa ishga tushmaydi va sababini aytadi: ikkita nusxa bir-biriga xalaqit bermaydi.

## Render'ga joylash

1. Kodni GitHub'ga yuklang. `.env` `.gitignore` da, shuning uchun token va kalitlar yuklanmaydi.
2. [dashboard.render.com](https://dashboard.render.com) da **New → Blueprint** ni bosib, repozitoriyni tanlang.
   `render.yaml` dagi sozlamalar avtomatik olinadi.
3. Render so'raganda `BOT_TOKEN` va `GEMINI_API_KEY` ni kiriting. `WEBHOOK_SECRET` avtomatik yaratiladi.
4. Deploy tugagach, loglarda `Webhook set: https://<nom>.onrender.com/webhook` qatori chiqishi kerak.

Render'da bot **webhook** rejimida ishlaydi, chunki Render `RENDER_EXTERNAL_URL` ni o'zi beradi.
Bepul tarifdagi servis 15 daqiqa ishlatilmasa uxlab qoladi. Shundan keyin kelgan birinchi xabarga
javob 30–60 soniya kechikadi, lekin xabar yo'qolmaydi: Telegram uni qayta yuboradi. Servis uxlaganda
yuklangan, lekin hali eksport qilinmagan savollar o'chadi.

Botni yana kompyuterda ishga tushirish kerak bo'lsa, avval Render'dagi servisni **Suspend** qiling,
so'ng `FORCE_POLLING=1` bilan ishga tushiring. Bu webhook'ni o'chiradi.

## Qanday ishlaydi

1. **Ajratish** (`bot/extract.py`): docx (Word avto-raqamlash, jadvallar, qalin/rangli/bo'yalgan matn bilan),
   pdf, xlsx/xls/csv, pptx, txt/html/rtf. Rasm va skanerlangan PDF Gemini'ga yuboriladi.
2. **Tahlil** (`bot/parser.py`): tekin qoidaviy parser — raqamli/harfli savollar, bir qatordagi variantlar,
   `*`/`+`/`#`/`(to'g'ri)` belgilari, `Javob: B`, oxiridagi kalit (`1-B 2-A`), HEMIS (`++++`/`====`/`#`),
   `?`/`+`/`-` formati, Moodle GIFT, jadvallar (shu jumladan Blooket/Kahoot shablonlarining o'zi).
   ChatGPT / Gemini / Claude'dan nusxalangan matn ham tushunadi: `**qalin**`, `###` sarlavha, `* A)` ro'yxat,
   `---`, Markdown jadval, `✅ Javob: B`, `To'g'ri javob: B) Toshkent`, `Izoh:` qatorlari.
3. **AI zaxira** (`bot/ai.py`, Gemini): qoidaviy natija yomon bo'lsa yoki fayl rasm bo'lsa ishga tushadi.
   AI faqat manbada belgilangan javoblarni oladi. To'g'ri javobni o'zi topishi faqat
   "🤖 AI aniqlasin" tugmasi bosilganda ishlaydi va natija belgilab ko'rsatiladi.
4. **Eksport** (`bot/export.py`): `templates/` dagi asl shablon ochiladi va faqat ma'lumot kataklari
   to'ldiriladi (Blooket: 3-qatordan, Kahoot: 9-qatordan, B–H ustunlar). Kahoot `.xlsx` da logo, validatsiya va
   formatlash saqlanadi; Blooket faqat CSV qabul qiladi, shuning uchun shablon CSV ko'rinishida eksport qilinadi.

## AI bilan test tuzish (`/generate`, `bot/generate.py`)

Foydalanuvchi **mavzu** (300 belgigacha) yoki **material** yuboradi: matn, Word, PDF, PowerPoint, TXT yoki rasm.
So'ng savollar sonini (5–30), qiyinlikni va tilni tanlaydi, Gemini testni tuzadi va test oddiy eksport oqimiga
tushadi. Savollar Kahoot chegaralariga moslab tuziladi: 4 ta variant, bitta to'g'ri javob, savol ≤ 110,
variant ≤ 70 belgi. To'g'ri javobning o'rni aralashtiriladi.

| Cheklov | Qiymat |
|---|---|
| Savollar soni | ≤ 30 |
| Material fayli | ≤ 5 MB |
| Material matni | ≤ 20 000 belgi (≈10 bet) |
| Kunlik limit | 3 ta test / foydalanuvchi (`GEN_DAILY_LIMIT`), Toshkent vaqti bilan 00:00 da yangilanadi |

Limit faqat test muvaffaqiyatli tuzilganda sarflanadi. Hisoblagich xotirada saqlanadi, shuning uchun servis
qayta ishga tushsa (Render bepul tarifida servis uxlab qolganda ham) u nolga tushadi.

## Platforma cheklovlari

| | Blooket | Kahoot |
|---|---|---|
| Variantlar | 2–4 | 2–4 |
| Savol / javob uzunligi | cheklanmagan | 120 / 75 belgi (qisqartiriladi) |
| Vaqt | ≤ 300 s | 5, 10, 20, 30, 60, 90, 120, 240 s |
| To'g'ri javob | raqam(lar): `2` yoki `1,4` | raqam(lar): `2` yoki `1,4` |
| Bir faylga | 100 savol | 100 savol |
