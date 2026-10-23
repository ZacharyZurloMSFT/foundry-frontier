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
param insuranceProjectName string = 'northwind-orders'
param hostedAgentsEnabled bool = false
@description('JSON string mapping department slug -> Foundry project endpoint. The API parses this at startup and picks the right endpoint per X-Department header. The claims-concierge entry = foundryProjectEndpoint.')
param foundryProjectEndpoints string = ''
param plannerDeployment string
param workerDeployment string
param promptAgentDeployment string
param embeddingDeployment string
param searchEndpoint string
param cosmosEndpoint string
param cosmosDatabaseName string
param storageAccountName string
param harnessEnabled bool = false
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
  { name: 'INSURANCE_PROJECT_NAME',                value: insuranceProjectName }
  { name: 'HOSTED_AGENTS_ENABLED',                 value: string(hostedAgentsEnabled) }
  { name: 'NORTHWIND_INSURANCE_AGENTS_READY',      value: string(insuranceAgentsReady) }
  { name: 'FOUNDRY_INSURANCE_ORCHESTRATOR_AGENT_ID', value: insuranceOrchestratorAgentId }
  { name: 'FOUNDRY_PROJECT_ENDPOINTS',             value: foundryProjectEndpoints }
  { name: 'FOUNDRY_PLANNER_DEPLOYMENT',            value: plannerDeployment }
  { name: 'FOUNDRY_WORKER_DEPLOYMENT',             value: workerDeployment }
  { name: 'FOUNDRY_PROMPT_AGENT_DEPLOYMENT',       value: promptAgentDeployment }
  { name: 'FOUNDRY_EMBEDDING_DEPLOYMENT',          value: embeddingDeployment }
  { name: 'AZURE_SEARCH_ENDPOINT',                 value: searchEndpoint }
  { name: 'COSMOS_ENDPOINT',                       value: cosmosEndpoint }
  { name: 'COSMOS_DATABASE',                       value: cosmosDatabaseName }
  { name: 'STORAGE_ACCOUNT',                       value: storageAccountName }
]

// Placeholder image used until `azd deploy` pushes a real image
var placeholder = 'mcr.microsoft.com/k8se/quickstart:latest'

var acrRegistry = [{
  server: acrLoginServer
  identity: identityId
}]

// ── API (FastAPI) ──────────────────────────────────────────────────────────
resource api 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: 'ca-insurance-api'
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
            { name: 'MCP_CLAIM_REVIEW_URL', value: 'https://${mcpClaimReview.properties.configuration.ingress.fqdn}/mcp/' }
          ])
          resources: { cpu: json('1.0'), memory: '2Gi' }
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 5 }
    }
  }
}

// ── Portal (staff and policyholder web app; azd service web-portal) ────────
resource webPortal 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: 'ca-insurance-portal'
  location: location
  tags: union(tags, { 'azd-service-name': 'web-portal' })
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
          name: 'web-portal'
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

// ── Voice intake web app (azd service web-voice) ───────────────────────────
resource webVoice 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: 'ca-insurance-voice'
  location: location
  tags: union(tags, { 'azd-service-name': 'web-voice' })
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
          name: 'web-voice'
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

// ── MCP: policy and claim lookup (azd service mcp-policy-lookup) ───────────
// Exposes the lookup_policy and lookup_claim tools over MCP streamable-HTTP so
// the northwind-insurance-policy-claim-lookup agent can call them as first-class
// MCP tools. Published through API Management as the live tech-lookup API.
// Public ingress, no auth — demo trade-off documented in the README.
resource mcpPolicyLookup 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: 'ca-insurance-lookup'
  location: location
  tags: union(tags, { 'azd-service-name': 'mcp-policy-lookup' })
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
          name: 'mcp-policy-lookup'
          image: placeholder
          env: [
            { name: 'AZURE_CLIENT_ID',                       value: reference(identityId, '2023-01-31').clientId }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
            { name: 'OTEL_SERVICE_NAME',                     value: 'northwind-insurance-lookup' }
            { name: 'PORT',                                  value: '8000' }
            { name: 'NORTHWIND_INSURANCE_AGENTS_READY',         value: string(insuranceAgentsReady) }
          ]
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 3 }
    }
  }
}

// ── MCP: claim-action review drafts (azd service mcp-claim-review) ─────────
// Exposes draft_claim_action, which queues a draft for human review. Published
// through API Management as the live work-order API.
resource mcpClaimReview 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: 'ca-insurance-review'
  location: location
  tags: union(tags, { 'azd-service-name': 'mcp-claim-review' })
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
          name: 'mcp-claim-review'
          image: placeholder
          env: [
            { name: 'AZURE_CLIENT_ID',                       value: reference(identityId, '2023-01-31').clientId }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
            { name: 'OTEL_SERVICE_NAME',                     value: 'northwind-insurance-review' }
            { name: 'PORT',                                  value: '8000' }
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
output webPortalUri string = 'https://${webPortal.properties.configuration.ingress.fqdn}'
output webVoiceUri string = 'https://${webVoice.properties.configuration.ingress.fqdn}'
// Full MCP endpoint URLs consumed by the prompt-agent yamls via
// ${MCP_POLICY_LOOKUP_URL} / ${MCP_CLAIM_REVIEW_URL} at agent-creation time.
// Note the TRAILING SLASH: FastMCP mounts at `/mcp/` and issues a 307
// redirect from `/mcp` to `/mcp/`. Foundry's MCP client does not follow
// redirects (returns 400 tool_user_error). Emit the canonical URL directly.
output mcpPolicyLookupUrl string = 'https://${mcpPolicyLookup.properties.configuration.ingress.fqdn}/mcp/'
output mcpClaimReviewUrl string = 'https://${mcpClaimReview.properties.configuration.ingress.fqdn}/mcp/'
