# Restaurant Expense Tracker (LINE bot)

Forward a supplier receipt (photo or PDF) to the restaurant's LINE Official
Account. The bot archives the original in Google Drive, reads it with Google
Cloud Vision OCR, checks the numbers, categorizes the spend, logs it to
Google Sheets and replies in LINE:

```
฿1,240 · Makro · Food cost · 17/10 ✅      [OK] [Change category] [Flag]
```

Spec: v1.0, 17 Oct 2026. Cost: free at 60–80 receipts a month (Vision reads
1,000 pages a month free; Cloud Run stays in its free tier).

## How it works

```
LINE ──forward──▶ LINE OA ──webhook──▶ Cloud Run (FastAPI)
                                         │
       download ─▶ dedupe (SHA-256) ─▶ Drive /Receipts/YYYY/MM/ ─▶ Vision OCR
       ─▶ normalize (BE→CE dates, amounts) ─▶ validate ─▶ dedupe (vendor + invoice no.)
       ─▶ categorize (vendor memory) ─▶ rename in Drive ─▶ Sheets rows ─▶ LINE reply
```

| File | What it does |
|---|---|
| `app/main.py` | `POST /callback`: checks the LINE signature, returns 200, processes in the background |
| `app/pipeline.py` | The pipeline above, buttons, `summary`, `/retry`, `help` |
| `app/vision.py` | Vision OCR call with retries; the rule-based parser that turns its text into receipt fields |
| `app/normalize.py` | Thai dates/amounts, the validation rules |
| `app/categorize.py` | Categories and vendor matching (tax ID, then name/aliases) |
| `app/storage.py` | Sheets tabs and Drive archive |
| `app/dashboard.py` | Dashboard tab formulas |
| `scripts/try_ocr.py` | M0: run your own receipts through Vision |
| `scripts/setup.py` | M1: Drive token, Drive folder, Sheet tabs |
| `scripts/check_setup.py` | Pre-flight check of every integration |
| `scripts/backfill.py` | M4: import the Telegram archive |

## Bot commands

| You do | Bot does |
|---|---|
| Send/forward a photo or PDF | Archives, reads, logs and replies with a summary and buttons |
| **Change category** | Shows category chips; your choice updates the row and the vendor's memory |
| **Change supplier** | Shows the **Suppliers** tab as chips; your choice replaces the vendor, applies its category and remembers the misread name |
| **Change price** | Asks for the correct total; your next message (e.g. `1250`, `฿1,250.50`) sets `grand_total`. `cancel` stops; it gives up after 10 minutes |
| `daily` | Receipts logged today (by arrival time): amount, vendor, category, status, and the day's total |
| `summary` | Month-to-date spend by category |
| `/retry` | Re-runs OCR on `pending_ocr` receipts (e.g. after fixing Vision API access) |
| `help` | Usage |

Any other message (chat, stickers) gets no reply, so the bot can sit in a
group chat. In a group only allowlisted people are processed; turn on
"Allow bot to join group chats" in the LINE console to invite it.

The first receipt from a new vendor asks for a category. Every later receipt
from that vendor is categorized automatically. When the OCR finds no vendor,
the reply asks "Which shop is this?" with the same Suppliers chips.

The **Suppliers** tab is a fixed list you maintain: one name per row in the
`name` column, up to 13 shown. The **Vendors** tab is the bot's own memory of
categories, tax IDs and alternative spellings.

`review_status` values: `ok`, `needs_review` (a check failed; see `review_note`),
`pending_ocr`; `flagged` and `confirmed` can be typed in by hand.

## Setup

### M0: accuracy test (optional)
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
2. **Google Cloud**: create a project with billing on (Vision needs it even
   when it stays free; add a budget alert to be safe); enable Cloud Run,
   Sheets, Drive, Secret Manager and **Cloud Vision** APIs; create a service
   account and download its JSON key. The same key is used for Vision.
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
