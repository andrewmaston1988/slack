#!/usr/bin/env pwsh
# Post a notification message to a Slack channel.
#
# Simple usage:
#   notify.ps1 -Title "Done" -Message "One-liner"
#
# Rich usage (multi-line body from file):
#   notify.ps1 -Title "Job finished" -MessageFile "C:/tmp/output.txt"

param(
    [string]$Title       = "Claude Agent",
    [string]$Message     = "",
    [string]$MessageFile = "",
    [string]$Channel     = "",
    [string]$Priority    = "default",
    [string]$Tags        = "robot",
    [string]$Topic       = "claude_agents"
)

# Resolve message body
if ($MessageFile -and (Test-Path $MessageFile)) {
    $body = Get-Content $MessageFile -Raw -Encoding utf8
} elseif ($Message) {
    $body = $Message
} else {
    $body = "Session ended"
}

# Convert markdown to Slack mrkdwn using shared formatter.
# $OutputEncoding controls what PS sends TO python; [Console]::OutputEncoding
# controls what PS reads FROM python. Both must be UTF-8 to avoid mojibake.
# -join "`n" is required because PS captures subprocess output as a string[]
# and implicit array->string coercion joins with spaces, losing line breaks.
$mdScript = Join-Path $PSScriptRoot "md_to_slack.py"
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$body = ($body | python $mdScript) -join "`n"

$botToken = $env:SLACK_BOT_TOKEN
$channel  = if ($Channel) { $Channel } else { $env:SLACK_NOTIFY_CHANNEL }

if (-not $botToken) {
    Write-Error "SLACK_BOT_TOKEN not set"
    exit 1
}

$text = "*$Title*`n$body"

$payload = @{
    channel = $channel
    text    = $text
} | ConvertTo-Json -Compress

try {
    $response = Invoke-RestMethod `
        -Uri     "https://slack.com/api/chat.postMessage" `
        -Method  Post `
        -Headers @{ Authorization = "Bearer $botToken" } `
        -ContentType "application/json; charset=utf-8" `
        -Body    $payload `
        -ErrorAction Stop

    if (-not $response.ok) {
        Write-Error "Slack notification failed: $($response.error)"
        exit 1
    }
} catch {
    Write-Error "Slack notification failed: $_"
    exit 1
}
