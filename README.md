# Shuto

Weekly photo batches → AI-generated metadata (Claude on Amazon Bedrock) → Shutterstock.

```
you ──upload_batch──▶ S3 incoming/<batch>/*.jpg
                         │ ObjectCreated
                         ▼
                      SQS (retries, DLQ)
                         ▼
                Lambda: ProcessPhotoFn ──▶ Bedrock (Claude, vision)
                         │                   title / keywords / categories / editorial flag
                         ├──▶ S3 metadata/<batch>/<photo>.json
                         ├──▶ DynamoDB (status per photo)
                         └──▶ FTPS upload to Shutterstock   (only when upload_enabled=true)

EventBridge (weekly) ──▶ Lambda: ExportCsvFn ──▶ S3 exports/<batch>/shutterstock-*.csv ──▶ SNS email
```

**Why a CSV step?** Shutterstock has no public contributor-upload API. Photos go in over FTPS
(or the web uploader), and metadata is applied by uploading a CSV with the **CSV** button on
[submit.shutterstock.com](https://submit.shutterstock.com). A CSV sent over FTPS is ignored.
So the weekly flow is: photos land in your Submit queue → you get an email → upload the CSV →
review → submit.

## Repo layout

| Path | What |
|---|---|
| `app.py`, `cdk.json` | CDK app entry + config (model ID, schedule, flags) |
| `infra/shuto_stack.py` | All AWS resources |
| `infra/bundling.py` | Builds the Lambda package with pip (no Docker needed) |
| `src/shuto/` | Lambda code: `handlers.py`, `metadata.py` (Bedrock), `ftps.py`, `csv_export.py`, `categories.py` |
| `scripts/upload_batch.{ps1,sh}` | Upload a local folder as a batch |
| `tests/` | Unit tests + CDK assertions |

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

# 4. Deploy (dry-run mode: generates metadata, does NOT upload to Shutterstock)
cdk deploy -c notify_email=you@example.com
```

Then:

1. **Bedrock model access**: in the Bedrock console (Tokyo), make sure Anthropic Claude models are
   enabled for your account. Default model is `jp.anthropic.claude-sonnet-4-6` (JP cross-region
   profile: inference stays in Tokyo/Osaka). Change it in `cdk.json` → `bedrock_model_id`,
   e.g. `global.anthropic.claude-sonnet-5-5`.
2. **Confirm the SNS email subscription** from your inbox.
3. **Shutterstock FTPS credentials**: put your contributor FTPS login into the secret named in
   the `FtpsSecretName` output:
   ```powershell
   aws secretsmanager put-secret-value --secret-id <FtpsSecretName> `
     --secret-string '{\"host\":\"ftps.shutterstock.com\",\"username\":\"...\",\"password\":\"...\"}'
   ```
4. When metadata quality looks good, turn on real uploads:
   `cdk deploy -c upload_enabled=true` (or set it in `cdk.json`).

## Weekly use

```powershell
# optional: D:\Photos\week40\notes.txt  e.g. "Kyoto, autumn foliage, Tofuku-ji, Nov 2026"
.\scripts\upload_batch.ps1 -Folder D:\Photos\week40            # batch defaults to ISO week, e.g. 2026-W40
```

- Each photo is described by Claude; results go to `metadata/<batch>/<photo>.json` (edit freely).
- Every Monday 09:00 JST (`export_schedule_cron`, UTC) the exporter writes a CSV per pending batch
  and emails you. Export one batch on demand:
  ```powershell
  aws lambda invoke --function-name <ExportFunctionName> --payload '{\"batch\":\"2026-W40\"}' `
    --cli-binary-format raw-in-base64-out out.json
  ```
- Download the CSV, upload it on the Submit page, review, submit.

Photo status in DynamoDB: `described` → `uploaded` (FTPS done) → `exported` (in a CSV).
Failed photos retry 3× and then land in the DLQ (`DeadLetterQueueUrl` output).

## Config (`cdk.json` → `context`, or `-c key=value`)

| Key | Default | Meaning |
|---|---|---|
| `bedrock_model_id` | `jp.anthropic.claude-sonnet-4-6` | Bedrock model / inference profile |
| `upload_enabled` | `false` | FTPS-upload originals to Shutterstock |
| `notify_email` | `""` | Email for "CSV ready" notifications |
| `export_schedule_cron` | `cron(0 0 ? * MON *)` | Weekly export (UTC) |

## Dev

```powershell
pytest -q
ruff check . ; ruff format .
cdk synth            # cdk diff before deploy
```

## Notes / Shutterstock rules baked in

- JPEG only (S3 notifications filter `.jpg/.jpeg`); Shutterstock minimum is 4 MP.
- 7–50 keywords, most relevant first; 1–2 categories from the fixed list in `categories.py`.
- Claude flags `editorial` when recognizable people (no model release), logos or brands appear —
  double-check these before submitting.
- Filenames are case-sensitive and must match between FTPS upload and CSV.

## Ideas / next

- Japan-specific keyword enrichment (place names in romaji, seasonal terms like *koyo*, *sakura*).
- Reject-reason feedback loop: store Shutterstock rejection reasons and feed them to the prompt.
- Cost tracking per batch (Bedrock tokens).
