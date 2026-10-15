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

@description('Order Concierge project within the existing shared Foundry account.')
param orderProjectName string = 'northwind-orders'
@description('Use independently hosted Order Concierge specialists after they have been deployed.')
@allowed(['true', 'false'])
param orderAgentsHosted string = 'true'

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
@description('Explicit greenfield-only opt-in for the retired ingestion worker. Content Understanding API capability does not require this workload.')
@allowed(['true', 'false'])
param enableCuIngest string = 'false'
@description('Explicit domain activation. Existing retail behavior remains the greenfield default.')
@allowed(['retail', 'insurance'])
param businessDomain string = 'retail'
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

@description('Optional: extra Azure-direct model deployments for the "Model bake-off" tab. Each entry: { name, version, capacity, sku?, format? }. Includes first-party Microsoft models AND partner models from the Foundry Models catalog (Moonshot/Kimi, DeepSeek, Llama, Mistral, etc.) — all served through this Foundry project with a single UAMI and OpenAI-compatible SDK. Default is empty because partner-model catalog availability varies per subscription + region. See docs/departments.md for the `azd env set BAKEOFFDEPLOYMENTS` override pattern. Bake-off with an empty array still works — it races the always-deployed planner + worker.')
param bakeoffDeployments array = []

@description('Enable the bounded Copilot SDK synthetic-demo harness. Existing workflows are unaffected when disabled.')
@allowed(['true', 'false'])
param harnessEnabled string = 'false'

@description('Additional Foundry projects — one per Northwind department beyond Order Concierge. Each project sits inside the same AI Services account so they share model deployments + UAMI, but each keeps its own agents, connections, evaluators, and Observability lens. To add or remove a department, edit this array and re-run `azd provision`.')
param additionalProjects array = [
  {
    slug: 'employee-help-desk'
    projectName: 'nw-employee-help-desk'
    displayName: 'Northwind Employee Help Desk'
    description: 'RAG-only Foundry project for HR/IT internal Q&A. Owns the nw-hr Search index and the northwind-hr-assistant prompt agent.'
  }
  {
    slug: 'claims-warranty'
    projectName: 'nw-claims-warranty'
    displayName: 'Northwind Claims & Warranty Ops'
    description: 'Content Understanding project for warranty PDFs, damage photos, and intake forms. Owns the nw-claims Search index, the northwind-claim-intake CU analyzer, and the northwind-claims-triage prompt agent.'
  }
  {
    slug: 'contact-center'
    projectName: 'nw-contact-center'
    displayName: 'Northwind Customer Contact Center'
    description: 'Voice Live front door project. Owns the nw-contact-center Search index, the northwind-intent-classifier prompt agent, and cross-project handoff to the Order Concierge orchestrator.'
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
    projectName: orderProjectName
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
    bakeoffDeployments: bakeoffDeployments
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
    projectName: orderProjectName
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
    orderProjectName: orderProjectName
    orderAgentsHosted: orderAgentsHosted == 'true'
    foundryProjectEndpoints: reuseExisting ? '' : format(
      '{{"order-concierge":"{0}","employee-help-desk":"{1}","claims-warranty":"{2}","contact-center":"{3}"}}',
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
    searchIndexName: reuseExisting ? '' : search!.outputs.indexName
    searchKnowledgeBase: reuseExisting ? '' : search!.outputs.knowledgeBaseName
    searchKnowledgeSource: reuseExisting ? '' : search!.outputs.knowledgeSourceName
    cosmosEndpoint: reuseExisting ? '' : cosmos!.outputs.endpoint
    cosmosDatabaseName: reuseExisting ? '' : cosmos!.outputs.databaseName
    storageAccountName: reuseExisting ? '' : storage!.outputs.name
    bakeoffDeployments: reuseExisting ? '' : join(concat([foundry!.outputs.plannerDeploymentName, foundry!.outputs.workerDeploymentName], foundry!.outputs.bakeoffDeploymentNames), ',')
    harnessEnabled: harnessEnabled == 'true'
    enableCuIngest: enableCuIngest == 'true'
    businessDomain: businessDomain
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
    orderProjectName: orderProjectName
    preservedSettings: existingSettings
  }
}

// ── Outputs consumed by azd + apps ─────────────────────────────────────────
output AZURE_LOCATION string = location
output AZURE_AI_LOCATION string = aiLocation
// azd's hosted-agent target must follow the project, not the legacy app environment.
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
output ORDER_PROJECT_NAME string = orderProjectName
// Greenfield proposes the Orders default for a separately reviewed migration.
// Adoption instead reports the effective account default without changing it.
output ORDER_DEFAULT_PROJECT string = reuseExisting ? reuse!.outputs.settings.ORDER_DEFAULT_PROJECT : orderProjectName
output ORDER_AGENTS_HOSTED string = orderAgentsHosted
output NORTHWIND_BUSINESS_DOMAIN string = businessDomain
output ORDERS_KB_CONNECTION_NAME string = reuseExisting ? reuse!.outputs.settings.ORDERS_KB_CONNECTION_NAME : '${orderProjectName}-kb-mcp'
output FOUNDRY_KB_CONNECTION_NAME string = reuseExisting ? reuse!.outputs.settings.FOUNDRY_KB_CONNECTION_NAME : '${orderProjectName}-kb-mcp'
output ORDERS_OPENAI_ENDPOINT string = reuseExisting ? reuse!.outputs.settings.ORDERS_OPENAI_ENDPOINT : 'https://${abbrs.cognitiveServicesAccounts}${resourceToken}.openai.azure.com/openai/v1/'
// azd AI Agent extension (host: azure.ai.agent) resolves the Foundry project
// via these exact env-var names during package/deploy/postdeploy.
output AZURE_AI_PROJECT_ENDPOINT string = reuseExisting ? reuse!.outputs.settings.AZURE_AI_PROJECT_ENDPOINT : foundry!.outputs.projectEndpoint
output AZURE_AI_PROJECT_ID string = reuseExisting ? reuse!.outputs.settings.AZURE_AI_PROJECT_ID : foundry!.outputs.projectId

// department-slug -> Foundry project endpoint map (JSON string). The API
// reads FOUNDRY_PROJECT_ENDPOINTS at startup, parses it, and picks the
// right endpoint per request based on the X-Department header. The
// Order Concierge entry is always the flagship Orders project so
// existing X-Department-less callers keep working.
//
// Bicep's `string(union(...))` emits a Bicep-object representation, not
// real JSON — the API's json.loads() rejects it. We hand-format the JSON
// here so the wire format is stable. Order matches
// data/northwind/company.yaml.
output FOUNDRY_PROJECT_ENDPOINTS string = reuseExisting ? reuse!.outputs.settings.FOUNDRY_PROJECT_ENDPOINTS : format(
  '{{"order-concierge":"{0}","employee-help-desk":"{1}","claims-warranty":"{2}","contact-center":"{3}"}}',
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

// Greenfield agent-ID outputs remain compatibility placeholders; reuse
// preserves any IDs already populated in the azd environment.
output FOUNDRY_AGENT_TRIAGE_ID string = reuseExisting ? reuse!.outputs.settings.FOUNDRY_AGENT_TRIAGE_ID : ''
output FOUNDRY_AGENT_TECH_LOOKUP_ID string = reuseExisting ? reuse!.outputs.settings.FOUNDRY_AGENT_TECH_LOOKUP_ID : ''
output FOUNDRY_AGENT_MANUAL_RETRIEVAL_ID string = reuseExisting ? reuse!.outputs.settings.FOUNDRY_AGENT_MANUAL_RETRIEVAL_ID : ''
output FOUNDRY_AGENT_WORK_ORDER_DRAFT_ID string = reuseExisting ? reuse!.outputs.settings.FOUNDRY_AGENT_WORK_ORDER_DRAFT_ID : ''
output FOUNDRY_ORCHESTRATOR_AGENT_ID string = reuseExisting ? reuse!.outputs.settings.FOUNDRY_ORCHESTRATOR_AGENT_ID : ''
// Foundry hosting expects this exact env-var name for the manager model.
output AZURE_AI_MODEL_DEPLOYMENT_NAME string = reuseExisting ? reuse!.outputs.settings.AZURE_AI_MODEL_DEPLOYMENT_NAME : foundry!.outputs.plannerDeploymentName

// Comma-joined list consumed by the API's bake-off endpoint
// (FOUNDRY_BAKEOFF_DEPLOYMENTS). Includes the flagship planner + worker so
// users always get a 3-way race even with an empty bakeoffDeployments param.
output FOUNDRY_BAKEOFF_DEPLOYMENTS string = reuseExisting ? reuse!.outputs.settings.FOUNDRY_BAKEOFF_DEPLOYMENTS : join(
  concat(
    [foundry!.outputs.plannerDeploymentName, foundry!.outputs.workerDeploymentName],
    foundry!.outputs.bakeoffDeploymentNames
  ), ','
)

output AZURE_SEARCH_ENDPOINT string = reuseExisting ? reuse!.outputs.settings.AZURE_SEARCH_ENDPOINT : search!.outputs.endpoint
output AZURE_SEARCH_INDEX string = reuseExisting ? reuse!.outputs.settings.AZURE_SEARCH_INDEX : search!.outputs.indexName
output AZURE_SEARCH_KNOWLEDGE_BASE string = reuseExisting ? reuse!.outputs.settings.AZURE_SEARCH_KNOWLEDGE_BASE : search!.outputs.knowledgeBaseName
output AZURE_SEARCH_KNOWLEDGE_SOURCE string = reuseExisting ? reuse!.outputs.settings.AZURE_SEARCH_KNOWLEDGE_SOURCE : search!.outputs.knowledgeSourceName
output AZURE_OPENAI_ENDPOINT string = reuseExisting ? reuse!.outputs.settings.AZURE_OPENAI_ENDPOINT : foundry!.outputs.aiServicesEndpointOpenAI

output COSMOS_ENDPOINT string = reuseExisting ? reuse!.outputs.settings.COSMOS_ENDPOINT : cosmos!.outputs.endpoint
output COSMOS_DATABASE string = reuseExisting ? reuse!.outputs.settings.COSMOS_DATABASE : cosmos!.outputs.databaseName

output STORAGE_ACCOUNT string = reuseExisting ? reuse!.outputs.settings.STORAGE_ACCOUNT : storage!.outputs.name
output STORAGE_CU_CONTAINER string = reuseExisting ? reuse!.outputs.settings.STORAGE_CU_CONTAINER : storage!.outputs.cuContainerName
output STORAGE_SEED_CONTAINER string = reuseExisting ? reuse!.outputs.settings.STORAGE_SEED_CONTAINER : storage!.outputs.seedContainerName

output AZURE_CONTAINER_REGISTRY_ENDPOINT string = reuseExisting ? reuse!.outputs.settings.AZURE_CONTAINER_REGISTRY_ENDPOINT : acr!.outputs.loginServer
output AZURE_CONTAINER_REGISTRY_NAME string = reuseExisting ? reuse!.outputs.settings.AZURE_CONTAINER_REGISTRY_NAME : acr!.outputs.name

output API_URI string = reuseExisting ? reuse!.outputs.settings.API_URI : containerapps!.outputs.apiUri
output WEB_TECH_URI string = reuseExisting ? reuse!.outputs.settings.WEB_TECH_URI : containerapps!.outputs.webTechUri
output WEB_VOICE_URI string = reuseExisting ? reuse!.outputs.settings.WEB_VOICE_URI : containerapps!.outputs.webVoiceUri

// MCP server URLs consumed by scripts/create-prompt-agents.py to render
// the type: mcp tool blocks in agents/tech_lookup.yaml + work_order_draft.yaml.
output MCP_TECH_LOOKUP_URL string = reuseExisting ? reuse!.outputs.settings.MCP_TECH_LOOKUP_URL : containerapps!.outputs.mcpTechLookupUrl
output MCP_WORK_ORDER_URL string = reuseExisting ? reuse!.outputs.settings.MCP_WORK_ORDER_URL : containerapps!.outputs.mcpWorkOrderUrl

output AZURE_LOG_ANALYTICS_RESOURCE_ID string = reuseExisting ? reuse!.outputs.settings.AZURE_LOG_ANALYTICS_RESOURCE_ID : observability!.outputs.logAnalyticsId
output NORTHWIND_EXISTING_RESOURCE_IDS string = reuseExisting ? string(union(reuse!.outputs.resourceIds, {
  containerApps: reuse!.outputs.containerAppResources
})) : '{}'
