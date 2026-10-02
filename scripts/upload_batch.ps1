# Upload a folder of JPEGs as one weekly batch.
# Usage: .\scripts\upload_batch.ps1 -Folder "D:\Photos\to-sell" [-Batch 2026-W40] [-Profile shuto]
param(
  [Parameter(Mandatory = $true)][string]$Folder,
  [string]$Batch = "",
  [string]$Profile = "default",
  [string]$Stack = "ShutoStack"
)
$ErrorActionPreference = "Stop"
if (-not $Batch) {
  $d = Get-Date
  $week = [System.Globalization.ISOWeek]::GetWeekOfYear($d)
  $year = [System.Globalization.ISOWeek]::GetYear($d)
  $Batch = "{0}-W{1:D2}" -f $year, $week
}
$bucket = aws cloudformation describe-stacks --stack-name $Stack --profile $Profile `
  --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" --output text
# notes.txt first, so the AI sees it when the photos are processed
$notes = Join-Path $Folder "notes.txt"
if (Test-Path $notes) { aws s3 cp $notes "s3://$bucket/incoming/$Batch/notes.txt" --profile $Profile }
Write-Host "Uploading $Folder -> s3://$bucket/incoming/$Batch/"
aws s3 sync $Folder "s3://$bucket/incoming/$Batch/" --profile $Profile `
  --exclude "*" --include "*.jpg" --include "*.jpeg" --include "*.JPG" --include "*.JPEG" --include "notes.txt"
