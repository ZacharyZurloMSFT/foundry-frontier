[CmdletBinding()]
param(
    [ValidateRange(1, 500)]
    [int]$Count = 20,
    [string]$Environment = "northwind-dev",
    [string]$AgentEndpoint = "",
    [string]$SubscriptionId = "",
    [string]$OutputPath = "agent-log-load-results.json"
)

$ErrorActionPreference = "Stop"
$env:AZURE_DEV_USER_AGENT = "microsoft_foundry_skill"

function Get-AzdValues {
    param([string]$EnvironmentName)

    $values = @{}
    azd env get-values -e $EnvironmentName | ForEach-Object {
        if ($_ -match '^([^=]+)=(.*)$') {
            $name = $Matches[1]
            $value = $Matches[2]
            if ($value.StartsWith('"') -and $value.EndsWith('"')) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $values[$name] = $value -replace '\\"', '"'
        }
    }
    return $values
}

$cases = @(
    @{ id = "delayed-espresso-01"; message = "Order NW-40021 for Priya Patel is past the delivery SLA and still in transit. Verify the order and policy, then draft the appropriate customer resolution." }
    @{ id = "damaged-skillet-01"; message = "Order NW-40022 arrived with a cracked cast-iron skillet. Check the order and return policy, then draft a resolution for Marcus Chen." }
    @{ id = "delayed-espresso-02"; message = "Customer C-8801 says the Cascade espresso machine on NW-40021 missed its promised date. Investigate and prepare the policy-backed resolution." }
    @{ id = "damaged-skillet-02"; message = "Marcus at marcus@example.com reports his skillet from NW-40022 was damaged on arrival. Look up the order, check policy, and draft the next action." }
    @{ id = "refund-request-01"; message = "Priya wants a refund for delayed order NW-40021. Confirm the shipment and applicable SLA remedy, then prepare a resolution draft." }
    @{ id = "return-request-01"; message = "Marcus wants to return the damaged kitchen item from NW-40022. Validate the order and return window, then draft the resolution." }
    @{ id = "shipment-stalled-01"; message = "Tracking NWF9922188 has not moved from Chicago for three days. Identify the order, apply the shipping policy, and draft a customer resolution." }
    @{ id = "goodwill-credit-01"; message = "Order NW-40021 is late for a gold-tier customer. Verify the record and policy, then draft a resolution including any allowed goodwill remedy." }
    @{ id = "replacement-request-01"; message = "The skillet in NW-40022 arrived cracked and the customer wants a replacement. Check the order and precedent, then draft the resolution." }
    @{ id = "late-delivery-01"; message = "Priya says NW-40021 was needed this week but remains in transit. Triage it, look up the order, check SLA policy, and prepare a resolution." }
    @{ id = "damaged-item-03"; message = "The delivered item on NW-40022 is unusable because it is cracked. Verify the customer and policy, then draft the best supported resolution." }
    @{ id = "espresso-complaint-01"; message = "Priya Patel is frustrated that her $189 espresso machine order NW-40021 is overdue. Investigate and draft a policy-compliant response." }
    @{ id = "skillet-complaint-01"; message = "Marcus Chen is angry that the skillet on NW-40022 arrived damaged. Look up the facts, consult policy, and draft the resolution." }
    @{ id = "sla-breach-01"; message = "Treat NW-40021 as a possible shipping SLA breach. Confirm the order state and customer tier, retrieve the governing policy, and prepare the resolution." }
    @{ id = "return-window-01"; message = "For NW-40022, determine whether the damaged kitchen item is within the return window and draft the appropriate resolution." }
    @{ id = "reship-request-01"; message = "Customer C-9102 requests a reship for the damaged item on NW-40022. Validate the order and policy before drafting the resolution." }
    @{ id = "delayed-order-04"; message = "Find the order associated with NWF9922188, assess the delivery delay under Northwind policy, and draft a resolution for the customer." }
    @{ id = "refund-approval-01"; message = "Draft the correct approval-ready remedy for Priya's delayed NW-40021 order after checking both the order data and shipping policy." }
    @{ id = "damaged-return-04"; message = "Prepare an approval-ready resolution for the cracked skillet on NW-40022 after validating the customer, order, and return policy." }
    @{ id = "end-to-end-01"; message = "Run the full Order Concierge workflow for overdue order NW-40021: triage the issue, look up the order, retrieve the relevant policy, and draft the final resolution." }
)

$azdValues = Get-AzdValues -EnvironmentName $Environment
if (-not $AgentEndpoint) {
    $AgentEndpoint = $azdValues["AGENT_ORCHESTRATOR_RESPONSES_ENDPOINT"]
}
if (-not $SubscriptionId) {
    $SubscriptionId = $azdValues["AZURE_SUBSCRIPTION_ID"]
}
if (-not $AgentEndpoint) {
    throw "AGENT_ORCHESTRATOR_RESPONSES_ENDPOINT is missing. Pass -AgentEndpoint or deploy the orchestrator in azd environment '$Environment'."
}
if (-not $SubscriptionId) {
    throw "AZURE_SUBSCRIPTION_ID is missing. Pass -SubscriptionId or select azd environment '$Environment'."
}

$accessToken = az account get-access-token `
    --subscription $SubscriptionId `
    --resource "https://ai.azure.com" `
    --query accessToken `
    --output tsv
if (-not $accessToken) {
    throw "Could not acquire an Azure AI access token."
}

$results = [System.Collections.Generic.List[object]]::new()
$startedAt = [DateTimeOffset]::UtcNow

for ($i = 0; $i -lt $Count; $i++) {
    $case = $cases[$i % $cases.Count]
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $responseId = $null
    $errors = [System.Collections.Generic.List[string]]::new()
    $completed = $false

    try {
        # No previous_response_id or conversation is supplied, so every call
        # starts an isolated Responses chain in the hosted agent runtime.
        $body = @{ input = $case.message } | ConvertTo-Json -Compress
        $response = Invoke-RestMethod `
            -Method Post `
            -Uri $AgentEndpoint `
            -Headers @{ Authorization = "Bearer $accessToken" } `
            -ContentType "application/json" `
            -Body $body `
            -TimeoutSec 180

        $responseId = $response.id
        $completed = $response.status -eq "completed"
        if ($response.error) {
            $errors.Add(($response.error | ConvertTo-Json -Compress -Depth 4))
        }
    }
    catch {
        $errors.Add($_.Exception.Message)
    }
    finally {
        $stopwatch.Stop()
    }

    $status = if ($errors.Count -gt 0) { "failed" } elseif ($completed) { "completed" } else { "incomplete" }
    $result = [pscustomobject]@{
        run = $i + 1
        case_id = $case.id
        response_id = $responseId
        status = $status
        elapsed_ms = $stopwatch.ElapsedMilliseconds
        errors = @($errors)
    }
    $results.Add($result)
    Write-Host ("[{0}/{1}] {2}: {3} ({4} ms)" -f ($i + 1), $Count, $case.id, $status, $stopwatch.ElapsedMilliseconds)
}

$finishedAt = [DateTimeOffset]::UtcNow
$report = [ordered]@{
    environment = $Environment
    agent_endpoint = $AgentEndpoint
    started_at = $startedAt.ToString("o")
    finished_at = $finishedAt.ToString("o")
    requested_runs = $Count
    completed_runs = @($results | Where-Object status -eq "completed").Count
    failed_runs = @($results | Where-Object status -eq "failed").Count
    incomplete_runs = @($results | Where-Object status -eq "incomplete").Count
    results = $results
}

$outputFullPath = [System.IO.Path]::GetFullPath($OutputPath)
$outputDirectory = Split-Path -Parent $outputFullPath
if ($outputDirectory -and -not (Test-Path $outputDirectory)) {
    New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
}
$report | ConvertTo-Json -Depth 6 | Set-Content -Path $outputFullPath -Encoding utf8

Write-Host "Wrote metadata-only run summary to $outputFullPath"
Write-Host ("Completed: {0}; failed: {1}; incomplete: {2}" -f $report.completed_runs, $report.failed_runs, $report.incomplete_runs)

if ($report.failed_runs -gt 0 -or $report.incomplete_runs -gt 0) {
    exit 1
}
