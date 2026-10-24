targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Environment name (also used as azd env name)')
param environmentName string

@minLength(1)
@description('Compute region — Container Apps, Cosmos, Storage, VNet, App Insights.')
param location string = 'canadacentral'

@minLength(1)
@description('AI region — Foundry AI Services (Voice Live, Agent Service, models) and Azure AI Search. Private endpoints for these still land in the compute VNet, so cross-region traffic stays on the Microsoft backbone.')
param aiLocation string = 'eastus2'

@description('Insurance claims project within the existing shared Foundry account. The default is the live project name; the original native project northwind-orders is still accepted by the API and the hosted-agent deploy script.')
param insuranceProjectName string = 'nw-insurance-claims'
@description('Use the independently hosted insurance agents after they have been deployed.')
@allowed(['true', 'false'])
param hostedAgentsEnabled string = 'true'

@description('Opt in to reference-only adoption. No foundational resources, RBAC, models, app settings, or network configuration are reconciled.')
@allowed(['true', 'false'])
param reuseExistingResources string = 'false'
param appsResourceGroupName string = ''
param dataResourceGroupName string = ''
param networkingResourceGroupName string = ''
param aiResourceGroupName string = ''
param foundryResourceGroupName string = ''
param observabilityResourceGroupName string = ''
@description('Independent workspace placement. When omitted in reuse mode, use Apps, where the existing workspace now resides.')
param logAnalyticsResourceGroupName string = ''
@description('Deprecated optional compatibility label only. Never used to select or require an existing resource group.')
param legacyResourceGroupName string = ''
@description('Existing managed environment placement. Defaults to Apps for the replacement stack; set an explicit override for transitional deployments. Selection does not migrate app attachments.')
param containerAppsEnvironmentResourceGroupName string = ''
@description('Existing VNet placement. Defaults to Networking for the replacement stack; set an explicit override for transitional deployments.')
param vnetResourceGroupName string = ''
@description('Existing Cosmos private endpoint in Networking. Defaults to the approved replacement; set an explicit override for a transitional endpoint. Selection does not move its subnet.')
@minLength(1)
param cosmosPrivateEndpointName string = 'pe-zz-cosmos-northwind'
@description('Operator-confirmed deployment readiness, never inferred from packaged files.')
@allowed(['true', 'false'])
param insuranceAgentsReady string = 'false'
@description('Explicit deployed insurance manager ID/version. Packaging does not populate or certify this value.')
param insuranceOrchestratorAgentId string = ''
@description('Opt in to the authenticated manual business APIs only after identity mappings are approved.')
@allowed(['true', 'false'])
param businessApiEnabled string = 'false'
param insuranceAuthTenantId string = ''
param insuranceAuthAudience string = ''
param insuranceAuthRequiredScope string = 'access_as_user'
param insuranceAuthClientIds string = ''
@description('Origin of the API Management gateway (https://<name>.azure-api.net). When set, API_URI and the MCP URLs point at the gateway instead of the Container Apps. Empty keeps the direct URLs.')
param gatewayUrl string = ''
@description('Gateway API stack that fronts the Container Apps this environment manages. The insurance stack is the only one this repository manages.')
@allowed(['insurance'])
param gatewayStack string = 'insurance'
@description('Preserved azd outputs for existing Foundry/runtime configuration. Required and validated only in reuse mode.')
param existingSettings object = {}

@description('Principal ID of the user running azd up (granted data-plane RBAC on Foundry / Search / Cosmos for local dev).')
param principalId string = ''

@description('Model deployment: planner')
param plannerModelName string = 'gpt-5.4'
param plannerModelVersion string = '2026-03-05'
param plannerModelCapacity int = 50

@description('Model deployment: worker (fast + cheap for high-volume tool turns)')
param workerModelName string = 'gpt-5.4-mini'
param workerModelVersion string = '2026-03-17'
param workerModelCapacity int = 100

@description('Model deployment: prompt agents. gpt-4o is required because Foundry prompt-agent function tools do not yet work on gpt-5.x.')
param promptAgentModelName string = 'gpt-4o'
param promptAgentModelVersion string = '2024-11-20'
param promptAgentModelCapacity int = 100

@description('Model deployment: embeddings for AI Search integrated vectorization.')
param embeddingModelName string = 'text-embedding-3-small'
param embeddingModelVersion string = '1'
param embeddingModelCapacity int = 120

@description('Optional: extra Azure-direct model deployments on the shared Foundry account, for example a partner model (Kimi) offered to adjuster support. Each entry: { name, version, capacity, sku?, format? }. Default is empty because partner-model catalog availability varies per subscription + region.')
param additionalModelDeployments array = []

@description('Enable the bounded Copilot SDK synthetic-demo harness. Existing workflows are unaffected when disabled.')
@allowed(['true', 'false'])
param harnessEnabled string = 'false'

@description('Additional Foundry projects — one per Northwind department beyond the claims concierge. Each project sits inside the same AI Services account so they share model deployments + UAMI, but each keeps its own agents, connections, evaluators, and Observability lens. To add or remove a department, edit this array and re-run `azd provision`. The project names match the live deployment.')
param additionalProjects array = [
  {
    slug: 'adjuster-support'
    projectName: 'nw-insurance-support'
    displayName: 'Northwind Adjuster Support'
    description: 'RAG-only Foundry project for adjuster support: grounded answers about claims handling procedures, evidence handling and review boundaries. It cannot approve, deny or pay a claim.'
  }
  {
    slug: 'claims-documents'
    projectName: 'nw-insurance-documents'
    displayName: 'Northwind Claims Documents'
    description: 'Content Understanding project for claim documents: FNOL reports, repair estimates, supporting invoices and damage evidence. Owns the nw-claims Search index, the northwindInsuranceClaimsV1 Content Understanding analyzer, and the northwind-claims-triage prompt agent.'
  }
  {
    slug: 'contact-center'
    projectName: 'nw-insurance-contact-center'
    displayName: 'Northwind Policyholder Contact Center'
    description: 'Voice Live front door project for policyholder intake, claim status and adjuster handoff. Owns the nw-contact-center Search index, the northwind-intent-classifier prompt agent, and cross-project handoff to the claims concierge orchestrator.'
  }
]

var tags = {
  'azd-env-name': environmentName
  application: 'northwind-copilot'
  owner: 'foundry-frontier'
}

var abbrs = loadJsonContent('./abbreviations.json')
var resourceToken = 'zurlo-northwind'
// Storage accounts and ACR only allow lowercase alphanumerics — strip the dash.
var compactToken = replace(resourceToken, '-', '')
var reuseExisting = reuseExistingResources == 'true'
var gatewayBase = empty(gatewayUrl) ? '' : '${gatewayUrl}/${gatewayStack}'
var existingLogAnalyticsGroup = empty(logAnalyticsResourceGroupName) ? appsResourceGroupName : logAnalyticsResourceGroupName
var existingEnvironmentGroup = empty(containerAppsEnvironmentResourceGroupName) ? appsResourceGroupName : containerAppsEnvironmentResourceGroupName
var existingVnetGroup = empty(vnetResourceGroupName) ? networkingResourceGroupName : vnetResourceGroupName

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = if (!reuseExisting) {
  name: '${abbrs.resourcesResourceGroups}${environmentName}'
  location: location
  tags: tags
}

// Conditions alone do not short-circuit ARM reference/list expressions.
// Guard output-dependent inputs too, so adoption never reads skipped deployments.
module observability 'modules/observability.bicep' = if (!reuseExisting) {
  scope: rg
  name: 'observability'
  params: {
    location: location
    tags: tags
    logAnalyticsName: '${abbrs.operationalInsightsWorkspaces}${resourceToken}'
    appInsightsName: '${abbrs.insightsComponents}${resourceToken}'
  }
}

module identity 'modules/uami.bicep' = if (!reuseExisting) {
  scope: rg
  name: 'uami'
  params: {
    location: location
    tags: tags
    name: '${abbrs.managedIdentityUserAssignedIdentities}app-${resourceToken}'
  }
}

module acr 'modules/acr.bicep' = if (!reuseExisting) {
  scope: rg
  name: 'acr'
  params: {
    location: location
    tags: tags
    name: '${abbrs.containerRegistries}${compactToken}'
    appPrincipalId: reuseExisting ? '' : identity!.outputs.principalId
    devPrincipalId: principalId
  }
}

module vnet 'modules/vnet.bicep' = if (!reuseExisting) {
  scope: rg
  name: 'vnet'
  params: {
    location: location
    tags: tags
    vnetName: '${abbrs.networkVirtualNetworks}${resourceToken}'
  }
}

module storage 'modules/storage.bicep' = if (!reuseExisting) {
  scope: rg
  name: 'storage'
  params: {
    location: location
    tags: tags
    name: '${abbrs.storageStorageAccounts}${compactToken}'
    appPrincipalId: reuseExisting ? '' : identity!.outputs.principalId
    devPrincipalId: principalId
    searchPrincipalId: reuseExisting ? '' : search!.outputs.principalId
  }
}

module search 'modules/search.bicep' = if (!reuseExisting) {
  scope: rg
  name: 'search'
  params: {
    location: location
    tags: tags
    name: '${abbrs.searchSearchServices}${resourceToken}'
    appPrincipalId: reuseExisting ? '' : identity!.outputs.principalId
    devPrincipalId: principalId
  }
}

module cosmos 'modules/cosmos.bicep' = if (!reuseExisting) {
  scope: rg
  name: 'cosmos'
  params: {
    location: aiLocation
    tags: tags
    name: '${abbrs.documentDBDatabaseAccounts}${resourceToken}'
    appPrincipalId: reuseExisting ? '' : identity!.outputs.principalId
    devPrincipalId: principalId
  }
}

module cosmosPrivateEndpoint 'modules/cosmos-private-endpoint.bicep' = if (!reuseExisting) {
  scope: rg
  name: 'cosmos-private-endpoint'
  params: {
    location: location
    tags: tags
    cosmosAccountId: reuseExisting ? '' : cosmos!.outputs.id
    vnetId: reuseExisting ? '' : vnet!.outputs.vnetId
    privateEndpointSubnetId: reuseExisting ? '' : vnet!.outputs.privateEndpointsSubnetId
    privateEndpointName: 'pe-cosmos-${resourceToken}'
  }
}

module foundry 'modules/foundry.bicep' = if (!reuseExisting) {
  scope: rg
  name: 'foundry'
  params: {
    location: aiLocation
    tags: tags
    aiServicesName: '${abbrs.cognitiveServicesAccounts}${resourceToken}'
    projectName: insuranceProjectName
    appPrincipalId: reuseExisting ? '' : identity!.outputs.principalId
    devPrincipalId: principalId
    searchPrincipalId: reuseExisting ? '' : search!.outputs.principalId
    plannerModelName: plannerModelName
    plannerModelVersion: plannerModelVersion
    plannerModelCapacity: plannerModelCapacity
    workerModelName: workerModelName
    workerModelVersion: workerModelVersion
    workerModelCapacity: workerModelCapacity
    promptAgentModelName: promptAgentModelName
    promptAgentModelVersion: promptAgentModelVersion
    promptAgentModelCapacity: promptAgentModelCapacity
    embeddingModelName: embeddingModelName
    embeddingModelVersion: embeddingModelVersion
    embeddingModelCapacity: embeddingModelCapacity
    additionalModelDeployments: additionalModelDeployments
    additionalProjects: additionalProjects
  }
}

// Foundry agent traces flow through the workspace-based App Insights
// component into Log Analytics. Keep this post-facto to avoid coupling the
// core Foundry module to the observability module's lifecycle.
module foundryObservability 'modules/foundry-observability.bicep' = if (!reuseExisting) {
  scope: rg
  name: 'foundry-observability'
  params: {
    aiServicesName: '${abbrs.cognitiveServicesAccounts}${resourceToken}'
    projectName: insuranceProjectName
    appInsightsName: '${abbrs.insightsComponents}${resourceToken}'
    projectPrincipalId: reuseExisting ? '' : foundry!.outputs.projectPrincipalId
  }
  dependsOn: [
    observability
    foundry
  ]
}

// Post-facto: give the Foundry project MI reader access on the Search
// service so agent MCP retrieval calls can hit the knowledge base.
// NOTE: currently unused because the Search service runs in apiKeyOnly
// auth mode and the KB MCP connection uses `CustomKeys` (see
// scripts/connect-knowledge-base.py). Leaving here so a future switch
// back to RBAC-based auth just needs a flip of the connection authType.
module searchRole 'modules/search-role.bicep' = if (!reuseExisting) {
  scope: rg
  name: 'search-role'
  params: {
    searchName: reuseExisting ? '' : search!.outputs.name
    projectPrincipalId: reuseExisting ? '' : foundry!.outputs.projectPrincipalId
  }
}

module containerapps 'modules/containerapps.bicep' = if (!reuseExisting) {
  scope: rg
  name: 'containerapps'
  params: {
    location: location
    tags: tags
    envName: '${abbrs.appManagedEnvironments}${resourceToken}'
    logAnalyticsCustomerId: reuseExisting ? '' : observability!.outputs.logAnalyticsCustomerId
    logAnalyticsSharedKey: reuseExisting ? '' : observability!.outputs.logAnalyticsSharedKey
    infrastructureSubnetId: reuseExisting ? '' : vnet!.outputs.containerAppsSubnetId
    identityId: reuseExisting ? '' : identity!.outputs.id
    appInsightsConnectionString: reuseExisting ? '' : observability!.outputs.appInsightsConnectionString
    acrLoginServer: reuseExisting ? '' : acr!.outputs.loginServer
    // wired settings for the API + workers
    foundryEndpoint: reuseExisting ? '' : foundry!.outputs.aiServicesEndpoint
    foundryProjectEndpoint: reuseExisting ? '' : foundry!.outputs.projectEndpoint
    insuranceProjectName: insuranceProjectName
    hostedAgentsEnabled: hostedAgentsEnabled == 'true'
    foundryProjectEndpoints: reuseExisting ? '' : format(
      '{{"claims-concierge":"{0}","adjuster-support":"{1}","claims-documents":"{2}","contact-center":"{3}"}}',
      foundry!.outputs.projectEndpoint,
      foundry!.outputs.additionalProjectEndpoints[0].endpoint,
      foundry!.outputs.additionalProjectEndpoints[1].endpoint,
      foundry!.outputs.additionalProjectEndpoints[2].endpoint
    )
    plannerDeployment: reuseExisting ? '' : foundry!.outputs.plannerDeploymentName
    workerDeployment: reuseExisting ? '' : foundry!.outputs.workerDeploymentName
    promptAgentDeployment: reuseExisting ? '' : foundry!.outputs.promptAgentDeploymentName
    embeddingDeployment: reuseExisting ? '' : foundry!.outputs.embeddingDeploymentName
    searchEndpoint: reuseExisting ? '' : search!.outputs.endpoint
    cosmosEndpoint: reuseExisting ? '' : cosmos!.outputs.endpoint
    cosmosDatabaseName: reuseExisting ? '' : cosmos!.outputs.databaseName
    storageAccountName: reuseExisting ? '' : storage!.outputs.name
    harnessEnabled: harnessEnabled == 'true'
    insuranceAgentsReady: insuranceAgentsReady == 'true'
    insuranceOrchestratorAgentId: insuranceOrchestratorAgentId
    businessApiEnabled: businessApiEnabled == 'true'
    insuranceAuthTenantId: insuranceAuthTenantId
    insuranceAuthAudience: insuranceAuthAudience
    insuranceAuthRequiredScope: insuranceAuthRequiredScope
    insuranceAuthClientIds: insuranceAuthClientIds
  }
}

module reuse 'modules/existing-resources.bicep' = if (reuseExisting) {
  name: 'reuse-existing'
  params: {
    appsResourceGroupName: appsResourceGroupName
    dataResourceGroupName: dataResourceGroupName
    networkingResourceGroupName: networkingResourceGroupName
    aiResourceGroupName: aiResourceGroupName
    foundryResourceGroupName: foundryResourceGroupName
    observabilityResourceGroupName: observabilityResourceGroupName
    logAnalyticsResourceGroupName: existingLogAnalyticsGroup
    containerAppsEnvironmentResourceGroupName: existingEnvironmentGroup
    vnetResourceGroupName: existingVnetGroup
    cosmosPrivateEndpointName: cosmosPrivateEndpointName
    resourceToken: resourceToken
    compactToken: compactToken
    insuranceProjectName: insuranceProjectName
    preservedSettings: existingSettings
  }
}

// ── Outputs consumed by azd + apps ─────────────────────────────────────────
output AZURE_LOCATION string = location
output AZURE_AI_LOCATION string = aiLocation
// The insurance agent fleet is deployed to the Foundry project, so this group must follow the project, not the legacy app environment.
output AZURE_RESOURCE_GROUP string = reuseExisting ? foundryResourceGroupName : rg!.name
output AZURE_APPS_RESOURCE_GROUP string = reuseExisting ? appsResourceGroupName : rg!.name
output AZURE_DATA_RESOURCE_GROUP string = reuseExisting ? dataResourceGroupName : rg!.name
output AZURE_NETWORKING_RESOURCE_GROUP string = reuseExisting ? networkingResourceGroupName : rg!.name
output AZURE_AI_RESOURCE_GROUP string = reuseExisting ? aiResourceGroupName : rg!.name
output AZURE_FOUNDRY_RESOURCE_GROUP string = reuseExisting ? foundryResourceGroupName : rg!.name
output AZURE_OBSERVABILITY_RESOURCE_GROUP string = reuseExisting ? observabilityResourceGroupName : rg!.name
output AZURE_LOG_ANALYTICS_RESOURCE_GROUP string = reuseExisting ? existingLogAnalyticsGroup : rg!.name
output AZURE_LEGACY_RESOURCE_GROUP string = reuseExisting ? legacyResourceGroupName : rg!.name
output AZURE_TENANT_ID string = tenant().tenantId

output AZURE_CLIENT_ID string = reuseExisting ? reuse!.outputs.settings.AZURE_CLIENT_ID : identity!.outputs.clientId
output AZURE_MANAGED_IDENTITY_ID string = reuseExisting ? reuse!.outputs.settings.AZURE_MANAGED_IDENTITY_ID : identity!.outputs.id
output AZURE_MANAGED_IDENTITY_PRINCIPAL_ID string = reuseExisting ? reuse!.outputs.settings.AZURE_MANAGED_IDENTITY_PRINCIPAL_ID : identity!.outputs.principalId

output APPLICATIONINSIGHTS_CONNECTION_STRING string = reuseExisting ? reuse!.outputs.settings.APPLICATIONINSIGHTS_CONNECTION_STRING : observability!.outputs.appInsightsConnectionString

output FOUNDRY_ENDPOINT string = reuseExisting ? reuse!.outputs.settings.FOUNDRY_ENDPOINT : foundry!.outputs.aiServicesEndpoint
output FOUNDRY_PROJECT_ENDPOINT string = reuseExisting ? reuse!.outputs.settings.FOUNDRY_PROJECT_ENDPOINT : foundry!.outputs.projectEndpoint
output INSURANCE_PROJECT_NAME string = insuranceProjectName
// Greenfield proposes the insurance project as the account default for a separately reviewed change.
// Adoption instead reports the effective account default without changing it.
output FOUNDRY_DEFAULT_PROJECT string = reuseExisting ? reuse!.outputs.settings.FOUNDRY_DEFAULT_PROJECT : insuranceProjectName
output HOSTED_AGENTS_ENABLED string = hostedAgentsEnabled
output AGENT_KB_CONNECTION_NAME string = reuseExisting ? reuse!.outputs.settings.AGENT_KB_CONNECTION_NAME : '${insuranceProjectName}-kb-mcp'
output FOUNDRY_KB_CONNECTION_NAME string = reuseExisting ? reuse!.outputs.settings.FOUNDRY_KB_CONNECTION_NAME : '${insuranceProjectName}-kb-mcp'
output AGENT_OPENAI_ENDPOINT string = reuseExisting ? reuse!.outputs.settings.AGENT_OPENAI_ENDPOINT : 'https://${abbrs.cognitiveServicesAccounts}${resourceToken}.openai.azure.com/openai/v1/'
// The Foundry project the insurance agent fleet is deployed to; the deployment
// scripts resolve the project through these exact env-var names.
output AZURE_AI_PROJECT_ENDPOINT string = reuseExisting ? reuse!.outputs.settings.AZURE_AI_PROJECT_ENDPOINT : foundry!.outputs.projectEndpoint
output AZURE_AI_PROJECT_ID string = reuseExisting ? reuse!.outputs.settings.AZURE_AI_PROJECT_ID : foundry!.outputs.projectId

// department-slug -> Foundry project endpoint map (JSON string). The API
// reads FOUNDRY_PROJECT_ENDPOINTS at startup, parses it, and picks the
// right endpoint per request based on the X-Department header. The
// claims-concierge entry is always the flagship insurance project so
// existing X-Department-less callers keep working. An adopted value may
// still use the deprecated slugs (order-concierge, employee-help-desk,
// claims-warranty); the API and scripts/validate-reuse.py accept both.
//
// Bicep's `string(union(...))` emits a Bicep-object representation, not
// real JSON — the API's json.loads() rejects it. We hand-format the JSON
// here so the wire format is stable. Order matches
// data/northwind/company.yaml.
output FOUNDRY_PROJECT_ENDPOINTS string = reuseExisting ? reuse!.outputs.settings.FOUNDRY_PROJECT_ENDPOINTS : format(
  '{{"claims-concierge":"{0}","adjuster-support":"{1}","claims-documents":"{2}","contact-center":"{3}"}}',
  foundry!.outputs.projectEndpoint,
  foundry!.outputs.additionalProjectEndpoints[0].endpoint,
  foundry!.outputs.additionalProjectEndpoints[1].endpoint,
  foundry!.outputs.additionalProjectEndpoints[2].endpoint
)
output FOUNDRY_PLANNER_DEPLOYMENT string = reuseExisting ? reuse!.outputs.settings.FOUNDRY_PLANNER_DEPLOYMENT : foundry!.outputs.plannerDeploymentName
output FOUNDRY_WORKER_DEPLOYMENT string = reuseExisting ? reuse!.outputs.settings.FOUNDRY_WORKER_DEPLOYMENT : foundry!.outputs.workerDeploymentName
output FOUNDRY_PROMPT_AGENT_DEPLOYMENT string = reuseExisting ? reuse!.outputs.settings.FOUNDRY_PROMPT_AGENT_DEPLOYMENT : foundry!.outputs.promptAgentDeploymentName
output FOUNDRY_EMBEDDING_DEPLOYMENT string = reuseExisting ? reuse!.outputs.settings.FOUNDRY_EMBEDDING_DEPLOYMENT : foundry!.outputs.embeddingDeploymentName
output FOUNDRY_PROJECT_PRINCIPAL_ID string = reuseExisting ? reuse!.outputs.settings.FOUNDRY_PROJECT_PRINCIPAL_ID : foundry!.outputs.projectPrincipalId

// Foundry hosting expects this exact env-var name for the manager model.
output AZURE_AI_MODEL_DEPLOYMENT_NAME string = reuseExisting ? reuse!.outputs.settings.AZURE_AI_MODEL_DEPLOYMENT_NAME : foundry!.outputs.plannerDeploymentName

output AZURE_SEARCH_ENDPOINT string = reuseExisting ? reuse!.outputs.settings.AZURE_SEARCH_ENDPOINT : search!.outputs.endpoint
output AZURE_OPENAI_ENDPOINT string = reuseExisting ? reuse!.outputs.settings.AZURE_OPENAI_ENDPOINT : foundry!.outputs.aiServicesEndpointOpenAI

output COSMOS_ENDPOINT string = reuseExisting ? reuse!.outputs.settings.COSMOS_ENDPOINT : cosmos!.outputs.endpoint
output COSMOS_DATABASE string = reuseExisting ? reuse!.outputs.settings.COSMOS_DATABASE : cosmos!.outputs.databaseName

output STORAGE_ACCOUNT string = reuseExisting ? reuse!.outputs.settings.STORAGE_ACCOUNT : storage!.outputs.name
output STORAGE_CU_CONTAINER string = reuseExisting ? reuse!.outputs.settings.STORAGE_CU_CONTAINER : storage!.outputs.cuContainerName
output STORAGE_SEED_CONTAINER string = reuseExisting ? reuse!.outputs.settings.STORAGE_SEED_CONTAINER : storage!.outputs.seedContainerName

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = reuseExisting ? reuse!.outputs.settings.AZURE_CONTAINER_REGISTRY_ENDPOINT : acr!.outputs.loginServer
output AZURE_CONTAINER_REGISTRY_NAME string = reuseExisting ? reuse!.outputs.settings.AZURE_CONTAINER_REGISTRY_NAME : acr!.outputs.name

output NORTHWIND_GATEWAY_URL string = gatewayUrl
output NORTHWIND_GATEWAY_STACK string = gatewayStack
// With a gateway configured these are the gateway URLs, so re-provisioning cannot silently revert the cutover.
output API_URI string = !empty(gatewayBase) ? gatewayBase : (reuseExisting ? reuse!.outputs.settings.API_URI : containerapps!.outputs.apiUri)
output WEB_PORTAL_URI string = reuseExisting ? reuse!.outputs.settings.WEB_PORTAL_URI : containerapps!.outputs.webPortalUri
output WEB_VOICE_URI string = reuseExisting ? reuse!.outputs.settings.WEB_VOICE_URI : containerapps!.outputs.webVoiceUri

// MCP server URLs consumed by scripts/create-prompt-agents.py to render
// the type: mcp tool blocks of the prompt agents. The paths are the live API
// Management API identifiers (tech-lookup, work-order).
output MCP_POLICY_LOOKUP_URL string = !empty(gatewayBase) ? '${gatewayBase}/mcp/tech-lookup' : (reuseExisting ? reuse!.outputs.settings.MCP_POLICY_LOOKUP_URL : containerapps!.outputs.mcpPolicyLookupUrl)
output MCP_CLAIM_REVIEW_URL string = !empty(gatewayBase) ? '${gatewayBase}/mcp/work-order' : (reuseExisting ? reuse!.outputs.settings.MCP_CLAIM_REVIEW_URL : containerapps!.outputs.mcpClaimReviewUrl)

output AZURE_LOG_ANALYTICS_RESOURCE_ID string = reuseExisting ? reuse!.outputs.settings.AZURE_LOG_ANALYTICS_RESOURCE_ID : observability!.outputs.logAnalyticsId
output NORTHWIND_EXISTING_RESOURCE_IDS string = reuseExisting ? string(union(reuse!.outputs.resourceIds, {
  containerApps: reuse!.outputs.containerAppResources
})) : '{}'
