param(
    [Parameter(Mandatory = $true)]
    [string]$BaseUrl,
    [string]$AgentUrl = "",
    [string]$AgentToken = ""
)

$ErrorActionPreference = "Stop"
$base = $BaseUrl.TrimEnd("/")

function Assert-Status([string]$Url, [hashtable]$Headers = @{}) {
    $response = Invoke-WebRequest -Uri $Url -Headers $Headers -Method Get -UseBasicParsing
    if ($response.StatusCode -ne 200) {
        throw "GET $Url returned HTTP $($response.StatusCode)"
    }
    Write-Host "OK $($response.StatusCode) $Url"
}

Assert-Status "$base/healthz"
Assert-Status "$base/api/v1/health/live"
Assert-Status "$base/api/v1/health/ready"

if ($AgentUrl) {
    Assert-Status "$($AgentUrl.TrimEnd('/'))/v1/health"
    if (-not $AgentToken) {
        throw "AgentUrl was provided but AgentToken is empty"
    }
    Assert-Status "$($AgentUrl.TrimEnd('/'))/v1/aba/status" @{ Authorization = "Bearer $AgentToken" }
}

Write-Host "Read-only server verification passed."
