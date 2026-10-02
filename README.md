# Shuto

Weekly photo batches → AI-generated metadata (Claude on Amazon Bedrock) → Shutterstock.

```
you ──upload_batch──▶ S3 incoming/<batch>/*.jpg
                         │ ObjectCreated
                         ▼
                      SQS (3 retries, then DLQ ──▶ alarm email)
                         ▼
                Lambda: ProcessPhotoFn
                   ├─ < 4 MP? ──▶ skipped, reported in the weekly email
                   ├─ Bedrock (Claude, vision) ──▶ S3 metadata/<batch>/<photo>.json  (source of truth)
                   └─ upload_enabled: write title/keywords into the JPEG (IPTC + XMP)
                                      ──▶ FTPS to Shutterstock ("Not submitted" in your portal)

EventBridge (weekly) ──▶ Lambda: ExportCsvFn ──▶ S3 exports/<batch>/shutterstock[-editorial]-<ts>.csv
                                              └─▶ SNS email (CSV paths + anything needing attention)
```

## How it works with Shutterstock

Shutterstock has **no API for contributor uploads**: its public API is for buyers. The only
official ways in are the web uploader and FTPS. So:

| Step | Who | What |
|---|---|---|
| 1 | you (1 min) | `upload_batch.ps1` → photos into S3 |
| 2 | automatic | Claude writes title, keywords, categories and flags. The photo is uploaded over FTPS with title and keywords **embedded** (IPTC/XMP), so it already shows them in the portal |
| 3 | automatic | Monday email: "CSV ready" |
| 4 | you (~5 min) | submit.shutterstock.com → **CSV** button → upload the CSV (adds categories + editorial/mature flags, matched by filename) → check → **Submit** |

The final Submit stays manual on purpose. It's your quality gate. Don't automate the portal
with scraped cookies or browser bots; it's captcha-protected and risks your account.

## Repo layout

| Path | What |
|---|---|
| `app.py`, `cdk.json` | CDK app entry + config (model ID, schedule, flags) |
| `infra/shuto_stack.py` | All AWS resources |
| `infra/bundling.py` | Builds the Lambda package with pip (no Docker needed) |
| `src/shuto/handlers.py` | Lambda entry points (process + export) |
| `src/shuto/metadata.py` | Bedrock prompt, validation, image checks |
| `src/shuto/embed.py` | Lossless IPTC/XMP writer for JPEGs |
| `src/shuto/ftps.py` | FTPS upload (explicit TLS, session reuse) |
| `src/shuto/csv_export.py`, `categories.py` | Shutterstock CSV + category list |
| `scripts/upload_batch.{ps1,sh}` | Upload a local folder as a batch |
| `scripts/check_ftps.py` | Test your Shutterstock FTPS login locally |
| `tests/` | Unit tests, handler tests (moto), CDK assertions |

## One-time setup

Prerequisites: Python 3.12+, Node 20+, AWS CLI v2, an AWS account.

```powershell
# 1. Tooling
npm install -g aws-cdk
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt

# 2. AWS credentials (IAM Identity Center recommended)
aws configure sso --profile shuto
$env:AWS_PROFILE = "shuto"       # bash: export AWS_PROFILE=shuto

# 3. Bootstrap CDK once per account/region
cdk bootstrap aws://<ACCOUNT_ID>/ap-northeast-1

# 4. Deploy in dry-run mode (generates metadata, does NOT upload to Shutterstock)
cdk deploy -c notify_email=you@example.com
```

Then:

1. **Confirm the SNS subscription email** (and the AWS Budgets email, if you get one).
2. **Bedrock model access**: in the Bedrock console (Tokyo), make sure Anthropic Claude models
   are enabled (first use asks for a short use-case form). Default model:
   `jp.anthropic.claude-sonnet-4-6` (JP cross-region profile: inference stays in Tokyo/Osaka).
   Change `bedrock_model_id` in `cdk.json`, e.g. `global.anthropic.claude-sonnet-5-5`
   (Sonnet 5.5 is only available in Tokyo via the global profile).
3. **Test your FTPS login locally** (uploads nothing):
   ```powershell
   python scripts\check_ftps.py --user you@example.com
   ```
4. **Store the FTPS login in AWS.** Shutterstock FTPS uses your **account** email + password.
   Write it to a file (keeps PowerShell quoting out of it), store it, delete the file:
   ```powershell
   '{"host":"ftps.shutterstock.com","username":"you@example.com","password":"..."}' |
     Set-Content -Encoding ascii ftps.json
   aws secretsmanager put-secret-value --secret-id <FtpsSecretName> --secret-string file://ftps.json
   Remove-Item ftps.json
   ```
   If you change your Shutterstock password, update the secret too (running Lambdas cache it
   until their next cold start).
5. When the metadata looks good, turn on real uploads: set `"upload_enabled": "true"` in
   `cdk.json` and `cdk deploy`.

## Weekly use

```powershell
# optional context for Claude: D:\Photos\week40\notes.txt
#   e.g. "Kyoto, Tofuku-ji temple, autumn foliage. Shot Nov 2026."
.\scripts\upload_batch.ps1 -Folder D:\Photos\week40      # batch = current ISO week, e.g. 2026-W40
```

- `metadata/<batch>/<photo>.json` is the **source of truth**. Edit it to fix a title or keyword
  and the next export uses your edit. Delete it and re-upload the photo to make Claude regenerate.
- Every Monday 09:00 JST (`export_schedule_cron`, in UTC) the exporter writes per batch:
  - `shutterstock-<ts>.csv`: commercial photos
  - `shutterstock-editorial-<ts>.csv`: editorial photos. Check the captions follow
    `CITY, COUNTRY - Month D, YYYY: …` before submitting.

  The email also lists anything needing attention: under 4 MP, too few keywords, broken JSON.
- Export one batch on demand:
  ```powershell
  '{"batch":"2026-W40"}' | Set-Content -Encoding ascii payload.json
  aws lambda invoke --function-name <ExportFunctionName> --payload file://payload.json `
    --cli-binary-format raw-in-base64-out out.json
  ```

In **dry-run** mode (`upload_enabled=false`) the CSVs are a preview. When you switch uploads on,
photos you upload again are sent over FTPS and show up in the next CSV.

Photo state (DynamoDB): `status` = `described` | `uploaded` | `too_small`, plus `uploaded_at`
and `exported_at`. Photos that fail 3 times go to the DLQ (`DeadLetterQueueUrl`) and you get
an alarm email.

## Config (`cdk.json` → `context`, or `-c key=value`)

| Key | Default | Meaning |
|---|---|---|
| `bedrock_model_id` | `jp.anthropic.claude-sonnet-4-6` | Bedrock model / inference profile |
| `upload_enabled` | `false` | FTPS-upload photos to Shutterstock |
| `notify_email` | `""` | Email for CSV-ready, DLQ alarm and budget alerts |
| `export_schedule_cron` | `cron(0 0 ? * MON *)` | Weekly export (UTC) |
| `monthly_budget_usd` | `20` | Account-wide AWS budget alert (80% actual / 100% forecast); `0` disables |

## Dev

```powershell
pytest -q
ruff check . ; ruff format .
cdk synth            # cdk diff before deploy
```

## Shutterstock rules baked in

- JPEG only, ≥ 4 MP (smaller photos are skipped and reported).
- 7–50 keywords, most relevant first; 1–2 categories from the list in `categories.py`.
- Editorial: Claude flags recognizable people (no model release), logos/brands and news events,
  and writes the caption in editorial format using the EXIF capture date + your `notes.txt`.
- Filenames are case-sensitive and must match between the FTPS upload and the CSV.
- Before the first real batch, compare the CSV headers with the sample CSV on the Submit page.

## Ideas / next

- Japan-specific keyword enrichment (place names in romaji, seasonal terms like *koyo*, *sakura*).
- Reject-reason feedback loop: store Shutterstock rejection reasons and feed them to the prompt.
- Cost tracking per batch (Bedrock tokens).
