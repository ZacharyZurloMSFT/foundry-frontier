targetScope = 'subscription'

@minLength(1)
param appsResourceGroupName string
@minLength(1)
param dataResourceGroupName string
@minLength(1)
param networkingResourceGroupName string
@minLength(1)
param aiResourceGroupName string
@minLength(1)
param foundryResourceGroupName string
@minLength(1)
param observabilityResourceGroupName string
@minLength(1)
param logAnalyticsResourceGroupName string
@minLength(1)
param containerAppsEnvironmentResourceGroupName string
@minLength(1)
param vnetResourceGroupName string
@minLength(1)
param cosmosPrivateEndpointName string
@minLength(5)
param resourceToken string
@minLength(5)
param compactToken string
@minLength(1)
param insuranceProjectName string

// These are azd's existing deployment outputs, not defaults for a fresh deployment.
// Nested constraints apply only when this adoption deployment is selected.
@sealed()
type preservedConfiguration = {
  @minLength(1)
  FOUNDRY_ENDPOINT: string
  @minLength(1)
  FOUNDRY_PROJECT_ENDPOINT: string
  @minLength(1)
  AZURE_AI_PROJECT_ENDPOINT: string
  @minLength(1)
  FOUNDRY_PROJECT_ENDPOINTS: string
  @minLength(1)
  AGENT_OPENAI_ENDPOINT: string
  @minLength(1)
  AZURE_OPENAI_ENDPOINT: string
  @minLength(1)
  AGENT_KB_CONNECTION_NAME: string
  @minLength(1)
  FOUNDRY_KB_CONNECTION_NAME: string
  @minLength(1)
  FOUNDRY_PLANNER_DEPLOYMENT: string
  @minLength(1)
  FOUNDRY_WORKER_DEPLOYMENT: string
  @minLength(1)
  FOUNDRY_PROMPT_AGENT_DEPLOYMENT: string
  @minLength(1)
  FOUNDRY_EMBEDDING_DEPLOYMENT: string
  @minLength(1)
  AZURE_AI_MODEL_DEPLOYMENT_NAME: string
  @minLength(1)
  COSMOS_DATABASE: string
  @minLength(1)
  STORAGE_CU_CONTAINER: string
  @minLength(1)
  STORAGE_SEED_CONTAINER: string
}
param preservedSettings preservedConfiguration

var abbrs = loadJsonContent('../abbreviations.json')

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  scope: resourceGroup(appsResourceGroupName)
  name: '${abbrs.managedIdentityUserAssignedIdentities}app-${resourceToken}'
}
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  scope: resourceGroup(appsResourceGroupName)
  name: '${abbrs.containerRegistries}${compactToken}'
}
resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  scope: resourceGroup(observabilityResourceGroupName)
  name: '${abbrs.insightsComponents}${resourceToken}'
}
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  scope: resourceGroup(logAnalyticsResourceGroupName)
  name: '${abbrs.operationalInsightsWorkspaces}${resourceToken}'
}
resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  scope: resourceGroup(foundryResourceGroupName)
  name: '${abbrs.cognitiveServicesAccounts}${resourceToken}'
}
resource insuranceProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: foundry
  name: insuranceProjectName
}
resource search 'Microsoft.Search/searchServices@2024-06-01-preview' existing = {
  scope: resourceGroup(dataResourceGroupName)
  name: '${abbrs.searchSearchServices}${resourceToken}'
}
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' existing = {
  scope: resourceGroup(dataResourceGroupName)
  name: '${abbrs.documentDBDatabaseAccounts}${resourceToken}'
}
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  scope: resourceGroup(dataResourceGroupName)
  name: '${abbrs.storageStorageAccounts}${compactToken}'
}
resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-10-02-preview' existing = {
  scope: resourceGroup(containerAppsEnvironmentResourceGroupName)
  name: '${abbrs.appManagedEnvironments}${resourceToken}'
}
resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' existing = {
  scope: resourceGroup(vnetResourceGroupName)
  name: '${abbrs.networkVirtualNetworks}${resourceToken}'
}
resource cosmosPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' existing = {
  scope: resourceGroup(networkingResourceGroupName)
  name: cosmosPrivateEndpointName
}
resource cosmosPrivateDnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' existing = {
  scope: resourceGroup(networkingResourceGroupName)
  name: 'privatelink.documents.azure.com'
}
resource aiGateway 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  scope: resourceGroup(aiResourceGroupName)
  name: 'ai-gateway-northwind'
}

resource api 'Microsoft.App/containerApps@2024-10-02-preview' existing = {
  scope: resourceGroup(appsResourceGroupName)
  name: 'ca-insurance-api'
}
resource mcpPolicyLookup 'Microsoft.App/containerApps@2024-10-02-preview' existing = {
  scope: resourceGroup(appsResourceGroupName)
  name: 'ca-insurance-lookup'
}
resource mcpClaimReview 'Microsoft.App/containerApps@2024-10-02-preview' existing = {
  scope: resourceGroup(appsResourceGroupName)
  name: 'ca-insurance-review'
}
resource webPortal 'Microsoft.App/containerApps@2024-10-02-preview' existing = {
  scope: resourceGroup(appsResourceGroupName)
  name: 'ca-insurance-portal'
}
resource webVoice 'Microsoft.App/containerApps@2024-10-02-preview' existing = {
  scope: resourceGroup(appsResourceGroupName)
  name: 'ca-insurance-voice'
}
// Report each app's actual attachment, even when a replacement environment is selected.
output containerAppResources array = [
  { service: 'api', name: api.name, id: api.id, environmentId: api.properties.managedEnvironmentId }
  { service: 'mcp-policy-lookup', name: mcpPolicyLookup.name, id: mcpPolicyLookup.id, environmentId: mcpPolicyLookup.properties.managedEnvironmentId }
  { service: 'mcp-claim-review', name: mcpClaimReview.name, id: mcpClaimReview.id, environmentId: mcpClaimReview.properties.managedEnvironmentId }
  { service: 'web-portal', name: webPortal.name, id: webPortal.id, environmentId: webPortal.properties.managedEnvironmentId }
  { service: 'web-voice', name: webVoice.name, id: webVoice.id, environmentId: webVoice.properties.managedEnvironmentId }
]

output settings object = union(preservedSettings, {
  AZURE_CLIENT_ID: identity.properties.clientId
  AZURE_MANAGED_IDENTITY_ID: identity.id
  AZURE_MANAGED_IDENTITY_PRINCIPAL_ID: identity.properties.principalId
  APPLICATIONINSIGHTS_CONNECTION_STRING: appInsights.properties.ConnectionString
  AZURE_AI_PROJECT_ID: insuranceProject.id
  FOUNDRY_PROJECT_PRINCIPAL_ID: insuranceProject.identity.principalId
  // Report the effective account default; never PATCH it to the insurance project.
  FOUNDRY_DEFAULT_PROJECT: foundry.properties.defaultProject
  AZURE_SEARCH_ENDPOINT: 'https://${search.name}.search.windows.net'
  COSMOS_ENDPOINT: cosmos.properties.documentEndpoint
  STORAGE_ACCOUNT: storage.name
  AZURE_CONTAINER_REGISTRY_ENDPOINT: acr.properties.loginServer
  AZURE_CONTAINER_REGISTRY_NAME: acr.name
  API_URI: 'https://${api.properties.configuration.ingress.fqdn}'
  MCP_POLICY_LOOKUP_URL: 'https://${mcpPolicyLookup.properties.configuration.ingress.fqdn}/mcp/'
  MCP_CLAIM_REVIEW_URL: 'https://${mcpClaimReview.properties.configuration.ingress.fqdn}/mcp/'
  WEB_PORTAL_URI: 'https://${webPortal.properties.configuration.ingress.fqdn}'
  WEB_VOICE_URI: 'https://${webVoice.properties.configuration.ingress.fqdn}'
  AZURE_LOG_ANALYTICS_RESOURCE_ID: logAnalytics.id
})

// Live readback only. No keys, secrets, role assignments, connections or app
// templates are deployed, and additional Foundry projects/models are untouched.
output resourceIds object = {
  foundry: foundry.id
  insuranceProject: insuranceProject.id
  identity: identity.id
  acr: acr.id
  appInsights: appInsights.id
  logAnalytics: logAnalytics.id
  logAnalyticsCustomerId: logAnalytics.properties.customerId
  search: search.id
  searchPrincipalId: search.identity.principalId
  cosmos: cosmos.id
  storage: storage.id
  storageBlobEndpoint: storage.properties.primaryEndpoints.blob
  containerAppsEnvironment: containerAppsEnvironment.id
  containerAppsSubnet: containerAppsEnvironment.properties.vnetConfiguration.infrastructureSubnetId
  vnet: vnet.id
  vnetAddressPrefixes: vnet.properties.addressSpace.addressPrefixes
  cosmosPrivateEndpoint: cosmosPrivateEndpoint.id
  privateEndpointSubnet: cosmosPrivateEndpoint.properties.subnet.id
  cosmosPrivateDnsZone: cosmosPrivateDnsZone.id
  privateDnsRecordSets: cosmosPrivateDnsZone.properties.numberOfRecordSets
  aiGateway: aiGateway.id
  aiGatewayLocation: aiGateway.location
}
