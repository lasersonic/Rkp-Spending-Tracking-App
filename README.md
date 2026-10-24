# Restaurant Expense Tracker (LINE bot)

Send a supplier receipt (photo or PDF) to the restaurant's LINE Official
Account. The bot saves the original in Google Drive, reads the supplier and
price with Google Cloud Vision for you to confirm (or you type them), takes
the category from the supplier, logs it to Google Sheets and replies in LINE:

```
📁 Saved (R261024-a1b2c3).
I read: ฿100 · ร้านหมูอนามัย BETAGRO · 17/10
Is this right?                      [✅ Confirm] [✏️ Enter manually]
→ Confirm
Saved: ฿100 · ร้านหมูอนามัย BETAGRO · Food cost · 17/10 ✅
   [Change price] [Change category] [Change supplier]
```

**Enter manually** (or a photo Vision can't read at all) asks instead:

```
Which supplier? Pick one, or tap ✏️ to type the name.
   [ร้านผักสมบัติโน๊ต] [หมูอนามัย BETAGRO] … [✏️ Type name]
→ tap a supplier
Type the total price for ร้านหมูอนามัย BETAGRO, e.g. 1250.
→ type 1250
Saved: ฿1,250 · ร้านหมูอนามัย BETAGRO · Food cost · 24/10 ✅
```

When Vision reads only one of the two (handwritten bills usually give a price
but no supplier), **Confirm** keeps that one and asks only for the other.

Spec: v1.0, 17 Oct 2026. Cost: free (Vision reads 1,000 pages a month free;
Cloud Run stays in its free tier).

## How it works

```
LINE ──send──▶ LINE OA ──webhook──▶ Cloud Run (FastAPI)
                                      │
   download ─▶ dedupe (SHA-256) ─▶ Drive /Receipts/YYYY/MM/ ─▶ Vision OCR
   ─▶ match supplier (Vendors memory, Suppliers tab) ─▶ Sheets row (unconfirmed)
   ─▶ "I read …" [Confirm] [Enter manually]
   ─▶ ask what's missing (supplier chips or typed, price) ─▶ Sheets row (ok)
   ─▶ rename in Drive ─▶ result + buttons
```

The backfill (`scripts/backfill.py`) has no one to confirm, so it runs:
download ─▶ dedupe ─▶ Drive ─▶ Vision OCR ─▶ validate ─▶ dedupe (vendor + invoice no.)
─▶ categorize ─▶ rename in Drive ─▶ Sheets rows.

| File | What it does |
|---|---|
| `app/main.py` | `POST /callback`: checks the LINE signature, returns 200, processes in the background |
| `app/pipeline.py` | The flow above, the questions and buttons, `daily`, `summary`, `help`; the backfill's OCR flow |
| `app/vision.py` | Vision OCR call with retries and the rule-based receipt parser |
| `app/normalize.py` | Thai dates/amounts, the validation rules |
| `app/categorize.py` | Categories and vendor matching (tax ID, then name/aliases) |
| `app/storage.py` | Sheets tabs and Drive archive |
| `app/dashboard.py` | Dashboard tab formulas |
| `scripts/try_ocr.py` | Run receipts through Vision and show what it reads |
| `scripts/setup.py` | M1: Drive token, Drive folder, Sheet tabs |
| `scripts/check_setup.py` | Pre-flight check of every integration |
| `scripts/backfill.py` | M4: import the Telegram archive |

## Bot commands

| You do | Bot does |
|---|---|
| Send/forward a photo or PDF | Saves it to Drive, reads it and shows **I read: price · supplier · date** |
| **✅ Confirm** | Keeps what was read; asks only for what's missing, then shows the result |
| **✏️ Enter manually** | Drops what was read and asks **which supplier**, then **the price** |
| Tap a supplier, or **✏️ Type name** and type it | Saves the supplier (and its remembered category), then asks **the price** |
| Type the price (e.g. `1250`, `฿1,250.50`, `๑๒๕๐ บาท`) | Saves it and shows the result with **Change price**, **Change category**, **Change supplier** |
| **Change price** | Asks for the correct total; your next message replaces it |
| **Change category** | Shows category chips; your choice updates the row and the supplier's memory |
| **Change supplier** | Shows the supplier chips again; the price is kept |
| `cancel` | Stops the current question; the buttons on the message still work later |
| `daily` | Receipts logged today (by arrival time): amount, supplier, category, status, and the day's total |
| `summary` | Month-to-date spend by category |
| `help` | Usage |

The bot takes your next message as the answer for 30 minutes after it asks;
after that, tap the button on the message again. Answers only count from the
person who sent the photo, in the same chat. In a 1:1 chat you can type the
supplier name straight away; in a group chat tap **✏️ Type name** first, so
ordinary chat isn't taken as a name.

Any other message (chat, stickers) gets no reply, so the bot can sit in a
group chat. In a group only allowlisted people are processed; turn on
"Allow bot to join group chats" in the LINE console to invite it.

The first receipt from a new supplier asks for a category. Every later receipt
from that supplier is categorized automatically. A typed name matches a known
supplier regardless of spacing and case.

The **Suppliers** tab is a fixed list you maintain: one name per row in the
`name` column, up to 12 shown (plus "Type name"). Typed names that aren't on
it are remembered on the **Vendors** tab, the bot's own memory of categories,
tax IDs and alternative spellings; add them to Suppliers to get a chip.

`review_status` values: `ok`, `unconfirmed` (read by OCR, not confirmed yet),
`incomplete` (supplier or price not given yet), `needs_review` (a check failed,
e.g. the same invoice was logged before; see `review_note`), `pending_ocr`
(backfill: OCR failed); `flagged` and `confirmed` can be typed in by hand.

The receipt date comes from the OCR when it's plausible (not in the future,
under a year old), otherwise it's the day the photo was sent. **Enter
manually** always uses the day it was sent.

## Setup

### M0: OCR test (optional)
Vision only returns text and word positions; `app/vision.py` finds the fields
itself (labels such as "Grand Total"/"รวมทั้งหมด", tax IDs with a valid checksum,
dates, table rows). To see what it makes of your receipts:
```bash
pip install -r requirements.txt
GOOGLE_SA_JSON=sa.json python -m scripts.try_ocr receipt1.jpg receipt2.pdf --rows
```
It prints the text rows, what the bot would log, and saves each raw response as
`<file>.vision.json`. If a field comes out wrong, share that JSON file.

What to expect: printed receipts and tax invoices read well. Handwritten bills
get their total and usually their date, but no vendor or reliable items; low
OCR confidence marks them `needs_review` so you can check them with the
Change price / Change supplier buttons. HEIC files are archived but not read
(send iPhone photos as photos, not files).

### M1: accounts
1. **LINE**: create a LINE Official Account and enable the Messaging API
   (LINE Developers console). Note the *Channel secret* and issue a
   *Channel access token*.
2. **Google Cloud**: create a project; enable Cloud Run, Sheets, Drive and
   Secret Manager APIs; create a service account and download its JSON key.
   Also enable the **Cloud Vision** API (it
   needs billing on even while it stays free; add a budget alert to be safe).
   The same key is used for Vision.
3. **Sheet**: create an empty Google Sheet and share it (Editor) with the
   service account's email. `SHEET_ID` is the long ID in its URL. Then:
   ```bash
   GOOGLE_SA_JSON=sa.json SHEET_ID=... python -m scripts.setup init-sheet
   ```
4. **Drive**: on a personal Google account a service account has no storage
   quota, so it can't upload into your Drive. The bot uploads as you instead:
   1. APIs & Services → OAuth consent screen: External, add yourself, then
      **Publish app** ("In production"). In "Testing" the token expires after 7 days.
   2. Credentials → Create OAuth client ID → *Desktop app* → download JSON.
   3. Run:
      ```bash
      pip install -r requirements.txt
      python -m scripts.setup drive-token --client-secrets client_secret.json > drive_token.json
      GOOGLE_DRIVE_TOKEN_JSON=drive_token.json python -m scripts.setup create-folder
      ```
      The second command creates `Receipts` in your Drive and prints `DRIVE_FOLDER_ID`.
      The bot only gets the `drive.file` scope: it sees files it created, nothing else.

   (With Google Workspace you can use a Shared Drive shared with the service
   account instead and skip the token.)

### Secrets

| Name | Value |
|---|---|
| `LINE_CHANNEL_SECRET` | LINE channel secret |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE channel access token |
| `GOOGLE_SA_JSON` | Service account key JSON (Sheets and Vision) |
| `GOOGLE_DRIVE_TOKEN_JSON` | Output of `setup drive-token` (Drive) |
| `SHEET_ID`, `DRIVE_FOLDER_ID` | Targets |
| `ALLOWED_USER_IDS` | Comma-separated LINE user IDs allowed to submit |
| `OWNER_USER_ID` | Gets alerts (defaults to the first allowed ID) |
| `TZ` | `Asia/Bangkok` |

To find your LINE user ID, deploy first and message the bot: it replies to
anyone not on the allowlist with their own user ID.

### Deploy (M2)

```bash
PROJECT=your-project
for s in LINE_CHANNEL_SECRET LINE_CHANNEL_ACCESS_TOKEN ALLOWED_USER_IDS; do
  printf '%s' "<value>" | gcloud secrets create $s --data-file=- --project $PROJECT
done
gcloud secrets create GOOGLE_SA_JSON --data-file=sa.json --project $PROJECT
gcloud secrets create GOOGLE_DRIVE_TOKEN_JSON --data-file=drive_token.json --project $PROJECT
# Let the Cloud Run service account read them:
#   roles/secretmanager.secretAccessor

gcloud run deploy expense-bot --source . --project $PROJECT \
  --region asia-southeast1 --allow-unauthenticated \
  --min-instances 0 --max-instances 1 --no-cpu-throttling --memory 512Mi --timeout 300 \
  --set-env-vars TZ=Asia/Bangkok,SHEET_ID=...,DRIVE_FOLDER_ID=... \
  --set-secrets LINE_CHANNEL_SECRET=LINE_CHANNEL_SECRET:latest,LINE_CHANNEL_ACCESS_TOKEN=LINE_CHANNEL_ACCESS_TOKEN:latest,GOOGLE_SA_JSON=GOOGLE_SA_JSON:latest,GOOGLE_DRIVE_TOKEN_JSON=GOOGLE_DRIVE_TOKEN_JSON:latest,ALLOWED_USER_IDS=ALLOWED_USER_IDS:latest
```

Then in the LINE Developers console: webhook URL `<service url>/callback`,
**Use webhook** on, **Verify**; in LINE Official Account Manager turn
**auto-reply messages** off.

Why these flags:
- `--no-cpu-throttling`: the bot answers LINE immediately and does the work
  after the response. With throttled CPU that background work would stall.
  At this volume it stays inside the free tier.
- `--max-instances 1`: one instance processes receipts one at a time, so
  duplicate checks can't race.
- `--allow-unauthenticated`: LINE must reach the webhook. Requests are
  authenticated by the `X-Line-Signature` check and the user allowlist.

### Check the setup
Once the secrets exist, run the pre-flight check (locally, with the same env vars):
```bash
python -m scripts.check_setup                                   # config, LINE, Vision, Sheet, Drive
python -m scripts.check_setup --service-url https://<service>.run.app   # + deployed webhook
python -m scripts.check_setup --ocr-sample receipt.jpg          # + one real OCR call
```
It checks: every setting is present and well-formed; the LINE token works, the
OA is in Bot mode, the webhook URL ends in `/callback`, is switched on and
passes LINE's test call; the Vision API is enabled for the key; the service account can open the
Sheet and the tabs are right; the Drive folder is visible and a test file can be
uploaded (catches the service-account storage problem); the deployed service is
up and rejects unsigned requests.

### M4: Telegram backfill
```bash
python -m scripts.backfill import ./ChatExport_2026-10-17 --dry-run   # count pages (Vision reads 1,000/month free)
python -m scripts.backfill import ./ChatExport_2026-10-17 --limit 5   # trial
python -m scripts.backfill import ./ChatExport_2026-10-17
# fill in the empty categories on the Vendors tab, then:
python -m scripts.backfill apply-categories
```
Uses the same environment variables as the bot. Already-logged files are skipped.

### M5: dashboard
`init-sheet` creates the **Dashboard** tab: monthly spend by category, food
cost trend, input VAT by month (for ภ.พ.30), top vendors, and item unit cost
by month. It's all formulas over Receipts/LineItems, so it always matches them.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```
Tests use in-memory fakes for LINE, Vision, Sheets and Drive; no keys needed.
