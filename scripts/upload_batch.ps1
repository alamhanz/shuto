# Upload a folder of JPEGs as one weekly batch. Works in Windows PowerShell 5.1 and PowerShell 7.
# Usage: .\scripts\upload_batch.ps1 -Folder "D:\Photos\to-sell" [-Batch 2026-W40] [-AwsProfile shuto]
# Without -AwsProfile the AWS CLI uses $env:AWS_PROFILE (or your default profile).
param(
  [Parameter(Mandatory = $true)][string]$Folder,
  [string]$Batch = "",
  [string]$AwsProfile = "",
  [string]$Stack = "ShutoStack"
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path $Folder -PathType Container)) { throw "Folder not found: $Folder" }

if (-not $Batch) {
  # ISO-8601 week: the week (Mon-Sun) belongs to the year its Thursday falls in.
  $d = (Get-Date).Date
  $thursday = $d.AddDays(3 - (([int]$d.DayOfWeek + 6) % 7))
  $cal = [System.Globalization.CultureInfo]::InvariantCulture.Calendar
  $week = $cal.GetWeekOfYear($thursday, [System.Globalization.CalendarWeekRule]::FirstFourDayWeek, [DayOfWeek]::Monday)
  $Batch = "{0}-W{1:D2}" -f $thursday.Year, $week
}

$awsArgs = @()
if ($AwsProfile) { $awsArgs = @("--profile", $AwsProfile) }

$bucket = aws cloudformation describe-stacks --stack-name $Stack @awsArgs `
  --query "Stacks[0].Outputs[?OutputKey=='BucketName'].OutputValue" --output text
if ($LASTEXITCODE -ne 0 -or -not $bucket) { throw "Could not read BucketName from stack $Stack" }
$dest = "s3://$bucket/incoming/$Batch/"

# notes.txt first, so the AI sees it when the photos are processed
$notes = Join-Path $Folder "notes.txt"
if (Test-Path $notes) { aws s3 cp $notes "${dest}notes.txt" @awsArgs }

Write-Host "Uploading $Folder -> $dest"
aws s3 sync $Folder $dest @awsArgs `
  --exclude "*" --include "*.jpg" --include "*.jpeg" --include "*.JPG" --include "*.JPEG"
if ($LASTEXITCODE -ne 0) { throw "aws s3 sync failed" }
Write-Host "Done. Batch: $Batch"
