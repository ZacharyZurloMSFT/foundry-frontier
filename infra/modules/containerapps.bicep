param location string
param tags object
param envName string
param logAnalyticsCustomerId string
@secure()
param logAnalyticsSharedKey string
param infrastructureSubnetId string
param identityId string
param appInsightsConnectionString string
param acrLoginServer string

param foundryEndpoint string
param foundryProjectEndpoint string
param orderProjectName string = 'northwind-orders'
param orderAgentsHosted bool = false
@description('JSON string mapping department slug -> Foundry project endpoint. The API parses this at startup and picks the right endpoint per X-Department header. Order Concierge default = foundryProjectEndpoint.')
param foundryProjectEndpoints string = ''
param plannerDeployment string
param workerDeployment string
param promptAgentDeployment string
param embeddingDeployment string
param searchEndpoint string
param searchIndexName string
param searchKnowledgeBase string
param searchKnowledgeSource string
param cosmosEndpoint string
param cosmosDatabaseName string
param storageAccountName string
@description('Comma-separated list of Azure-direct model deployments to expose to the /bakeoff endpoint on the API container.')
param bakeoffDeployments string = ''
param harnessEnabled bool = false
param enableCuIngest bool = false
@allowed(['retail', 'insurance'])
param businessDomain string = 'retail'
param insuranceAgentsReady bool = false
param insuranceOrchestratorAgentId string = ''
param businessApiEnabled bool = false
param insuranceAuthTenantId string = ''
param insuranceAuthAudience string = ''
param insuranceAuthRequiredScope string = 'access_as_user'
param insuranceAuthClientIds string = ''

resource env 'Microsoft.App/managedEnvironments@2024-10-02-preview' = {
  name: envName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: logAnalyticsSharedKey
      }
    }
    vnetConfiguration: {
      internal: false
      infrastructureSubnetId: infrastructureSubnetId
    }
    workloadProfiles: [
      { name: 'Consumption', workloadProfileType: 'Consumption' }
    ]
    zoneRedundant: false
  }
}

// Common env vars injected into every container
var commonEnv = [
  { name: 'AZURE_CLIENT_ID',                       value: reference(identityId, '2023-01-31').clientId }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
  { name: 'OTEL_SERVICE_NAME',                     value: 'northwind-copilot' }
  { name: 'OTEL_EXPORTER_OTLP_PROTOCOL',           value: 'http/protobuf' }
  { name: 'FOUNDRY_ENDPOINT',                      value: foundryEndpoint }
  { name: 'FOUNDRY_PROJECT_ENDPOINT',              value: foundryProjectEndpoint }
  { name: 'ORDER_PROJECT_NAME',                   value: orderProjectName }
  { name: 'ORDER_AGENTS_HOSTED',                   value: string(orderAgentsHosted) }
  { name: 'NORTHWIND_BUSINESS_DOMAIN',             value: businessDomain }
  { name: 'NORTHWIND_INSURANCE_AGENTS_READY',      value: string(insuranceAgentsReady) }
  { name: 'FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID', value: insuranceOrchestratorAgentId }
  { name: 'FOUNDRY_PROJECT_ENDPOINTS',             value: foundryProjectEndpoints }
  { name: 'FOUNDRY_PLANNER_DEPLOYMENT',            value: plannerDeployment }
  { name: 'FOUNDRY_WORKER_DEPLOYMENT',             value: workerDeployment }
  { name: 'FOUNDRY_PROMPT_AGENT_DEPLOYMENT',       value: promptAgentDeployment }
  { name: 'FOUNDRY_EMBEDDING_DEPLOYMENT',          value: embeddingDeployment }
  { name: 'AZURE_SEARCH_ENDPOINT',                 value: searchEndpoint }
  { name: 'AZURE_SEARCH_INDEX',                    value: searchIndexName }
  { name: 'AZURE_SEARCH_KNOWLEDGE_BASE',           value: searchKnowledgeBase }
  { name: 'AZURE_SEARCH_KNOWLEDGE_SOURCE',         value: searchKnowledgeSource }
  { name: 'COSMOS_ENDPOINT',                       value: cosmosEndpoint }
  { name: 'COSMOS_DATABASE',                       value: cosmosDatabaseName }
  { name: 'STORAGE_ACCOUNT',                       value: storageAccountName }
  { name: 'FOUNDRY_BAKEOFF_DEPLOYMENTS',           value: bakeoffDeployments }
]

// Placeholder image used until `azd deploy` pushes a real image
var placeholder = 'mcr.microsoft.com/k8se/quickstart:latest'

var acrRegistry = [{
  server: acrLoginServer
  identity: identityId
}]

// ── API (FastAPI) ──────────────────────────────────────────────────────────
resource api 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: 'ca-api'
  location: location
  tags: union(tags, { 'azd-service-name': 'api' })
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identityId}': {} } }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        corsPolicy: {
          allowedOrigins: ['*']
          allowedMethods: ['GET','POST','PUT','PATCH','DELETE','OPTIONS']
          allowedHeaders: ['*']
          exposeHeaders: ['X-Correlation-ID','ETag','Location']
        }
      }
      registries: acrRegistry
    }
    template: {
      containers: [
        {
          name: 'api'
          image: placeholder
          env: union(commonEnv, [
            { name: 'PORT', value: '8000' }
            { name: 'COPILOT_HARNESS_ENABLED', value: string(harnessEnabled) }
            { name: 'NORTHWIND_BUSINESS_API_ENABLED', value: string(businessApiEnabled) }
            { name: 'INSURANCE_AUTH_TENANT_ID', value: insuranceAuthTenantId }
            { name: 'INSURANCE_AUTH_AUDIENCE', value: insuranceAuthAudience }
            { name: 'INSURANCE_AUTH_REQUIRED_SCOPE', value: insuranceAuthRequiredScope }
            { name: 'INSURANCE_AUTH_CLIENT_IDS', value: insuranceAuthClientIds }
            { name: 'MCP_WORK_ORDER_URL', value: 'https://${mcpWorkOrder.properties.configuration.ingress.fqdn}/mcp/' }
          ])
          resources: { cpu: json('1.0'), memory: '2Gi' }
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 5 }
    }
  }
}

// ── Web-Tech ───────────────────────────────────────────────────────────────
resource webTech 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: 'ca-web-tech'
  location: location
  tags: union(tags, { 'azd-service-name': 'web-tech' })
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identityId}': {} } }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: { external: true, targetPort: 80, transport: 'auto' }
      registries: acrRegistry
    }
    template: {
      containers: [
        {
          name: 'web-tech'
          image: placeholder
          env: [
            { name: 'VITE_API_BASE_URL', value: 'https://${api.properties.configuration.ingress.fqdn}' }
          ]
          resources: { cpu: json('0.5'), memory: '1Gi' }
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 3 }
    }
  }
}

// ── Content Understanding ingestion worker ─────────────────────────────────
resource cuIngest 'Microsoft.App/containerApps@2024-10-02-preview' = if (enableCuIngest) {
  name: 'ca-cu-ingest'
  location: location
  tags: union(tags, { 'azd-service-name': 'cu-ingest' })
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identityId}': {} } }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: { external: false, targetPort: 8080, transport: 'auto' }
      registries: acrRegistry
    }
    template: {
      containers: [
        { name: 'cu-ingest', image: placeholder, env: commonEnv, resources: { cpu: json('0.5'), memory: '1Gi' } }
      ]
      scale: { minReplicas: 0, maxReplicas: 3 }
    }
  }
}

// ── MCP: tech-lookup ───────────────────────────────────────────────────────
// Exposes the lookup_order tool over MCP streamable-HTTP so the
// northwind-order-lookup prompt agent can call it as a first-class MCP tool
// (was a type: function tool client-side-dispatched by the orchestrator).
// Public ingress, no auth — demo trade-off documented in the README.
resource mcpTechLookup 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: 'ca-mcp-tech-lookup'
  location: location
  tags: union(tags, { 'azd-service-name': 'mcp-tech-lookup' })
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identityId}': {} } }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: acrRegistry
    }
    template: {
      containers: [
        {
          name: 'mcp-tech-lookup'
          image: placeholder
          env: [
            { name: 'AZURE_CLIENT_ID',                       value: reference(identityId, '2023-01-31').clientId }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
            { name: 'OTEL_SERVICE_NAME',                     value: 'northwind-mcp-tech-lookup' }
            { name: 'PORT',                                  value: '8000' }
            { name: 'NORTHWIND_BUSINESS_DOMAIN',               value: businessDomain }
            { name: 'NORTHWIND_INSURANCE_AGENTS_READY',         value: string(insuranceAgentsReady) }
          ]
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 3 }
    }
  }
}

// ── MCP: work-order ────────────────────────────────────────────────────────
resource mcpWorkOrder 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: 'ca-mcp-work-order'
  location: location
  tags: union(tags, { 'azd-service-name': 'mcp-work-order' })
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identityId}': {} } }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: acrRegistry
    }
    template: {
      containers: [
        {
          name: 'mcp-work-order'
          image: placeholder
          env: [
            { name: 'AZURE_CLIENT_ID',                       value: reference(identityId, '2023-01-31').clientId }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
            { name: 'OTEL_SERVICE_NAME',                     value: 'northwind-mcp-work-order' }
            { name: 'PORT',                                  value: '8000' }
            { name: 'NORTHWIND_BUSINESS_DOMAIN',               value: businessDomain }
            { name: 'NORTHWIND_INSURANCE_AGENTS_READY',         value: string(insuranceAgentsReady) }
            { name: 'COSMOS_ENDPOINT',                       value: cosmosEndpoint }
            { name: 'COSMOS_DATABASE',                       value: cosmosDatabaseName }
          ]
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 3 }
    }
  }
}

output apiUri string = 'https://${api.properties.configuration.ingress.fqdn}'
output webTechUri string = 'https://${webTech.properties.configuration.ingress.fqdn}'
// Full MCP endpoint URLs consumed by the prompt-agent yamls via
// ${MCP_TECH_LOOKUP_URL} / ${MCP_WORK_ORDER_URL} at agent-creation time.
// Note the TRAILING SLASH: FastMCP mounts at `/mcp/` and issues a 307
// redirect from `/mcp` to `/mcp/`. Foundry's MCP client does not follow
// redirects (returns 400 tool_user_error). Emit the canonical URL directly.
output mcpTechLookupUrl string = 'https://${mcpTechLookup.properties.configuration.ingress.fqdn}/mcp/'
output mcpWorkOrderUrl string = 'https://${mcpWorkOrder.properties.configuration.ingress.fqdn}/mcp/'
