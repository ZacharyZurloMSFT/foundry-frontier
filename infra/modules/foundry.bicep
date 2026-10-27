// Azure AI Foundry (AI Services + project) with the planner, worker, prompt-agent and embedding model deployments
// for the Northwind Insurance apps.
param location string
param tags object
param aiServicesName string
param projectName string
param appPrincipalId string
param devPrincipalId string = ''
@description('Principal ID of the Azure AI Search service managed identity. Granted Cognitive Services User on this Foundry account so knowledge-base agentic retrieval can call the gpt-4o deployment for query planning and answer synthesis.')
param searchPrincipalId string = ''

param plannerModelName string
param plannerModelVersion string
param plannerModelCapacity int
param workerModelName string
param workerModelVersion string
param workerModelCapacity int
param promptAgentModelName string
param promptAgentModelVersion string
param promptAgentModelCapacity int
param embeddingModelName string
param embeddingModelVersion string
param embeddingModelCapacity int

resource ai 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: aiServicesName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: aiServicesName
    publicNetworkAccess: 'Enabled'
    networkAcls: { defaultAction: 'Allow' }
    allowProjectManagement: true
  }
}

@description('Optional extra Azure-direct model deployments on this Foundry account, for example a partner model offered to adjuster support. Each item: { name, version, capacity, sku?, format? }.')
param additionalModelDeployments array = []

resource plannerDep 'Microsoft.CognitiveServices/accounts/deployments@2025-04-01-preview' = {
  parent: ai
  name: plannerModelName
  sku: { name: 'GlobalStandard', capacity: plannerModelCapacity }
  properties: {
    model: { format: 'OpenAI', name: plannerModelName, version: plannerModelVersion }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
}

resource workerDep 'Microsoft.CognitiveServices/accounts/deployments@2025-04-01-preview' = {
  parent: ai
  name: workerModelName
  sku: { name: 'GlobalStandard', capacity: workerModelCapacity }
  properties: {
    model: { format: 'OpenAI', name: workerModelName, version: workerModelVersion }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
  dependsOn: [ plannerDep ]
}

// Prompt agents use a separate deployment because function-tool calling from
// prompt agents is not yet supported on gpt-5.x — gpt-4o handles the flat
// Responses-API function shape reliably.
resource promptAgentDep 'Microsoft.CognitiveServices/accounts/deployments@2025-04-01-preview' = {
  parent: ai
  name: promptAgentModelName
  sku: { name: 'GlobalStandard', capacity: promptAgentModelCapacity }
  properties: {
    model: { format: 'OpenAI', name: promptAgentModelName, version: promptAgentModelVersion }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
  dependsOn: [ workerDep ]
}

// Embedding model for AI Search integrated vectorization. Deployed alongside
// the chat models so the Search skillset MI can call it via the shared
// Cognitive Services User role assignment below.
resource embeddingDep 'Microsoft.CognitiveServices/accounts/deployments@2025-04-01-preview' = {
  parent: ai
  name: embeddingModelName
  sku: { name: 'GlobalStandard', capacity: embeddingModelCapacity }
  properties: {
    model: { format: 'OpenAI', name: embeddingModelName, version: embeddingModelVersion }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
  dependsOn: [ promptAgentDep ]
}

// ── Optional additional model deployments ──────────────────────────────────
// Deployed sequentially after the embedding deployment. Each item can specify
// its own SKU (defaults to GlobalStandard). Kept as a `batchSize(1)` loop so
// Azure doesn't reject concurrent capacity requests on the same account.
@batchSize(1)
resource additionalModelDeps 'Microsoft.CognitiveServices/accounts/deployments@2025-04-01-preview' = [for (m, i) in additionalModelDeployments: {
  parent: ai
  name: m.name
  sku: {
    name: m.?sku ?? 'GlobalStandard'
    capacity: m.capacity
  }
  properties: {
    model: { format: m.?format ?? 'OpenAI', name: m.name, version: m.version }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
  }
  dependsOn: [ embeddingDep ]
}]

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: ai
  name: projectName
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: projectName
    description: 'Northwind Insurance claims concierge Foundry project'
  }
  dependsOn: [ plannerDep, workerDep, promptAgentDep, embeddingDep ]
}

// ── Additional Foundry projects — one per Northwind department ─────────────
// The claims concierge project (above) hosts the multi-agent flagship. Every
// other department gets its own project inside this same AI Services
// account so it has its own agents, connections, evaluators, red-team
// runs, and Foundry Observability lens. Model deployments are shared at
// the account level — no per-project quota fragmentation.
//
// Adding/removing a department is a pure change to the `additionalProjects`
// param in main.bicep — the RBAC and endpoint export loops below scale
// with it automatically.
@description('Additional Foundry projects to create alongside the claims concierge project. Each item: { slug, projectName, displayName, description }.')
param additionalProjects array = []

resource extraProjects 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = [for p in additionalProjects: {
  parent: ai
  name: p.projectName
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: p.displayName
    description: p.description
  }
  dependsOn: [ project ]
}]

// ── RBAC ───────────────────────────────────────────────────────────────────
// Azure AI User (data-plane calls: agents, Voice Live, inference)
var aiUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'
// Cognitive Services OpenAI User
var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
// Cognitive Services User — needed by the Azure AI Search managed identity so
// Foundry IQ knowledge bases can invoke the gpt-4o deployment for query
// planning and answer synthesis.
var cogSvcUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource appAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: ai
  name: guid(ai.id, appPrincipalId, aiUserRoleId)
  properties: {
    principalId: appPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', aiUserRoleId)
  }
}

resource appOpenAi 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: ai
  name: guid(ai.id, appPrincipalId, openAiUserRoleId)
  properties: {
    principalId: appPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
  }
}

resource devAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(devPrincipalId)) {
  scope: ai
  name: guid(ai.id, devPrincipalId, aiUserRoleId)
  properties: {
    principalId: devPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', aiUserRoleId)
  }
}

resource devOpenAi 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(devPrincipalId)) {
  scope: ai
  name: guid(ai.id, devPrincipalId, openAiUserRoleId)
  properties: {
    principalId: devPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
  }
}

resource searchCogSvc 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(searchPrincipalId)) {
  scope: ai
  name: guid(ai.id, searchPrincipalId, cogSvcUserRoleId)
  properties: {
    principalId: searchPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cogSvcUserRoleId)
  }
}

output aiServicesId string = ai.id
output aiServicesName string = ai.name
output aiServicesEndpoint string = ai.properties.endpoint
output projectId string = project.id
output projectName string = project.name
output projectPrincipalId string = project.identity.principalId
output projectEndpoint string = '${ai.properties.endpoint}api/projects/${project.name}'
output plannerDeploymentName string = plannerDep.name
output workerDeploymentName string = workerDep.name
output promptAgentDeploymentName string = promptAgentDep.name
output embeddingDeploymentName string = embeddingDep.name
output aiServicesEndpointOpenAI string = 'https://${ai.name}.openai.azure.com/'

// One entry per additional Foundry project. Consumers stitch these into a
// department -> endpoint map (see main.bicep FOUNDRY_PROJECT_ENDPOINTS output).
output additionalProjectEndpoints array = [for (p, i) in additionalProjects: {
  slug: p.slug
  projectName: p.projectName
  endpoint: '${ai.properties.endpoint}api/projects/${p.projectName}'
  principalId: extraProjects[i].identity.principalId
}]
