// Incremental management boundary for the Northwind API Management configuration (apim-northwind).
// Never redeclare the API Management service, Container Apps or Application Insights here: they are
// existing references. Only the allow-listed child objects below are created (see scripts/apim-release.py).
// Service-scope policy and settings are deliberately untouched. No modules: the compiled template must stay flat.
targetScope = 'resourceGroup'

@description('Existing API Management instance in this resource group.')
param apimName string = 'apim-northwind'

@description('Existing Application Insights component that receives gateway telemetry.')
param appInsightsName string = 'appi-zurlo-northwind'

@description('Container App stacks to publish. name is the immutable first APIM path segment of every API in the stack.')
param stacks array = [
  {
    name: 'northwind'
    apiApp: 'ca-api'
    techLookupApp: 'ca-mcp-tech-lookup'
    workOrderApp: 'ca-mcp-work-order'
    webApp: 'ca-web-tech'
    voiceApp: 'ca-web-voice'
  }
  {
    name: 'insurance'
    apiApp: 'ca-insurance-api'
    techLookupApp: 'ca-insurance-lookup'
    workOrderApp: 'ca-insurance-review'
    webApp: 'ca-insurance-portal'
    voiceApp: 'ca-insurance-voice'
  }
]

@description('Extra browser origins allowed by CORS (local development).')
param extraAllowedOrigins array = [
  'http://localhost:5173'
  'http://localhost:5174'
]

@description('Entra audience (GUID or api://GUID) for the gateway token APIM injects as X-Northwind-Gateway-Token: the client ID of the northwind-gateway application created by scripts/configure-gateway-identity.py. Empty = do not inject.')
param gatewayAudience string = '0940b5c3-18ec-46f0-8962-7894247c2710'

@description('Optional delegated-token pre-validation for /api/v1: tenantId, audience, clientIds (array) and scope. Empty = backend validation only.')
param businessAuth object = {}

@minValue(1)
@maxValue(100000)
param webRateLimitPerMinute int = 600

@minValue(1)
@maxValue(100000)
param agentsRateLimitPerMinute int = 300

resource apim 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource apiApps 'Microsoft.App/containerApps@2024-03-01' existing = [for stack in stacks: {
  name: stack.apiApp
}]

resource techLookupApps 'Microsoft.App/containerApps@2024-03-01' existing = [for stack in stacks: {
  name: stack.techLookupApp
}]

resource workOrderApps 'Microsoft.App/containerApps@2024-03-01' existing = [for stack in stacks: {
  name: stack.workOrderApp
}]

resource webApps 'Microsoft.App/containerApps@2024-03-01' existing = [for stack in stacks: {
  name: stack.webApp
}]

resource voiceApps 'Microsoft.App/containerApps@2024-03-01' existing = [for stack in stacks: {
  name: stack.voiceApp
}]

// One committed OpenAPI document (scripts/export-openapi.py) describes both stacks: they run the same code.
var restSpec = loadTextContent('apim/specs/northwind-api.openapi.json')
var restPolicyTemplate = loadTextContent('apim/policies/rest-api.xml')
var voicePolicyTemplate = loadTextContent('apim/policies/voice-api.xml')
var mcpPolicyTemplate = loadTextContent('apim/policies/mcp-api.xml')

var gatewayTokenXml = empty(gatewayAudience)
  ? loadTextContent('apim/policies/fragment-gateway-token-off.xml')
  : replace(loadTextContent('apim/policies/fragment-gateway-token.xml'), '__GATEWAY_AUDIENCE__', gatewayAudience)

var jwtTenantId = businessAuth.?tenantId ?? ''
var jwtAudience = businessAuth.?audience ?? ''
var jwtScope = businessAuth.?scope ?? ''
var jwtClientIds = businessAuth.?clientIds ?? []
var jwtEnabled = !empty(jwtTenantId) && !empty(jwtAudience) && !empty(jwtScope) && !empty(jwtClientIds)
var jwtXml = jwtEnabled
  ? replace(
      replace(
        replace(
          replace(loadTextContent('apim/policies/fragment-validate-jwt.xml'), '__TENANT_ID__', jwtTenantId),
          '__AUDIENCE__',
          jwtAudience
        ),
        '__SCOPE__',
        jwtScope
      ),
      '__CLIENT_ID_VALUES__',
      join(map(jwtClientIds, clientId => '<value>${clientId}</value>'), '')
    )
  : ''

var localOriginsXml = join(map(extraAllowedOrigins, origin => '<origin>${origin}</origin>'), '')

var voicePolicy = replace(voicePolicyTemplate, '__GATEWAY_TOKEN__', gatewayTokenXml)
var mcpPolicy = replace(
  replace(mcpPolicyTemplate, '__RATE_LIMIT__', string(agentsRateLimitPerMinute)),
  '__GATEWAY_TOKEN__',
  gatewayTokenXml
)
var restPolicyBase = replace(
  replace(replace(restPolicyTemplate, '__RATE_LIMIT__', string(webRateLimitPerMinute)), '__JWT_VALIDATION__', jwtXml),
  '__GATEWAY_TOKEN__',
  gatewayTokenXml
)

// Each stack publishes one MCP API per FastMCP backend; they are flattened so one loop creates them all.
var mcpKinds = [
  'tech-lookup'
  'work-order'
]
var mcpMethods = [
  'POST'
  'GET'
  'DELETE'
]
var mcpApiDefinitions = flatten(map(range(0, length(stacks)), index => map(mcpKinds, kind => {
  stackIndex: index
  stack: stacks[index].name
  kind: kind
})))
var mcpOperationDefinitions = flatten(map(range(0, length(mcpApiDefinitions)), apiIndex => map(mcpMethods, method => {
  apiIndex: apiIndex
  method: method
})))

resource logger 'Microsoft.ApiManagement/service/loggers@2024-05-01' = {
  parent: apim
  name: 'northwind-appinsights'
  properties: {
    loggerType: 'applicationInsights'
    description: 'Northwind API gateway telemetry'
    resourceId: appInsights.id
    credentials: {
      instrumentationKey: appInsights.properties.InstrumentationKey
    }
    isBuffered: true
  }
}

resource webProducts 'Microsoft.ApiManagement/service/products@2024-05-01' = [for stack in stacks: {
  parent: apim
  name: '${stack.name}-web'
  properties: {
    displayName: 'Northwind ${stack.name} web'
    description: 'Browser-facing REST and Voice Live APIs for the ${stack.name} stack (no subscription key; CORS and rate limits apply).'
    state: 'published'
    subscriptionRequired: false
  }
}]

resource agentProducts 'Microsoft.ApiManagement/service/products@2024-05-01' = [for stack in stacks: {
  parent: apim
  name: '${stack.name}-agents'
  properties: {
    displayName: 'Northwind ${stack.name} agents'
    description: 'MCP servers for the ${stack.name} stack. A subscription key is required.'
    state: 'published'
    subscriptionRequired: true
    approvalRequired: false
  }
}]

resource restApis 'Microsoft.ApiManagement/service/apis@2024-05-01' = [for (stack, index) in stacks: {
  parent: apim
  name: '${stack.name}-api'
  properties: {
    displayName: 'Northwind ${stack.name} API'
    description: 'Northwind Copilot API (${stack.apiApp}). Imported from infra/live/apim/specs/northwind-api.openapi.json.'
    path: stack.name
    protocols: [
      'https'
    ]
    serviceUrl: 'https://${apiApps[index].properties.configuration.ingress.fqdn}'
    subscriptionRequired: false
    format: 'openapi+json'
    value: restSpec
  }
}]

resource restPolicies 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = [for (stack, index) in stacks: {
  parent: restApis[index]
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: replace(
      restPolicyBase,
      '__ALLOWED_ORIGINS__',
      '<origin>https://${webApps[index].properties.configuration.ingress.fqdn}</origin><origin>https://${voiceApps[index].properties.configuration.ingress.fqdn}</origin>${localOriginsXml}'
    )
  }
}]

resource voiceApis 'Microsoft.ApiManagement/service/apis@2024-05-01' = [for (stack, index) in stacks: {
  parent: apim
  name: '${stack.name}-voice'
  properties: {
    type: 'websocket'
    displayName: 'Northwind ${stack.name} Voice Live'
    description: 'Voice Live WebSocket proxy (${stack.apiApp}). Origin and one-time ticket are enforced by the backend.'
    path: '${stack.name}/voice/realtime'
    protocols: [
      'wss'
    ]
    serviceUrl: 'wss://${apiApps[index].properties.configuration.ingress.fqdn}/voice/realtime'
    subscriptionRequired: false
  }
}]

// WebSocket APIs accept policies only on their system-created onHandshake operation, not at API scope.
resource voiceHandshakes 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' existing = [for (stack, index) in stacks: {
  parent: voiceApis[index]
  name: 'onHandshake'
}]

resource voicePolicies 'Microsoft.ApiManagement/service/apis/operations/policies@2024-05-01' = [for (stack, index) in stacks: {
  parent: voiceHandshakes[index]
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: voicePolicy
  }
}]

resource mcpApis 'Microsoft.ApiManagement/service/apis@2024-05-01' = [for definition in mcpApiDefinitions: {
  parent: apim
  name: '${definition.stack}-mcp-${definition.kind}'
  properties: {
    displayName: 'Northwind ${definition.stack} MCP ${definition.kind}'
    description: 'FastMCP streamable-HTTP server passthrough. The backend endpoint is /mcp/; this API is served at the API root.'
    path: '${definition.stack}/mcp/${definition.kind}'
    protocols: [
      'https'
    ]
    serviceUrl: 'https://${definition.kind == 'tech-lookup' ? techLookupApps[definition.stackIndex].properties.configuration.ingress.fqdn : workOrderApps[definition.stackIndex].properties.configuration.ingress.fqdn}'
    subscriptionRequired: true
  }
}]

resource mcpOperations 'Microsoft.ApiManagement/service/apis/operations@2024-05-01' = [for operation in mcpOperationDefinitions: {
  parent: mcpApis[operation.apiIndex]
  name: 'mcp-${toLower(operation.method)}'
  properties: {
    displayName: 'MCP ${operation.method}'
    method: operation.method
    urlTemplate: '/'
  }
}]

resource mcpPolicies 'Microsoft.ApiManagement/service/apis/policies@2024-05-01' = [for (definition, index) in mcpApiDefinitions: {
  parent: mcpApis[index]
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: mcpPolicy
  }
  dependsOn: [
    mcpOperations
  ]
}]

resource webProductRestApis 'Microsoft.ApiManagement/service/products/apis@2024-05-01' = [for (stack, index) in stacks: {
  parent: webProducts[index]
  name: restApis[index].name
}]

resource webProductVoiceApis 'Microsoft.ApiManagement/service/products/apis@2024-05-01' = [for (stack, index) in stacks: {
  parent: webProducts[index]
  name: voiceApis[index].name
}]

resource agentProductMcpApis 'Microsoft.ApiManagement/service/products/apis@2024-05-01' = [for (definition, index) in mcpApiDefinitions: {
  parent: agentProducts[definition.stackIndex]
  name: mcpApis[index].name
}]

// Keys are generated by API Management and never appear in this template or its outputs.
resource agentSubscriptions 'Microsoft.ApiManagement/service/subscriptions@2024-05-01' = [for (stack, index) in stacks: {
  parent: apim
  name: '${stack.name}-agents'
  properties: {
    displayName: 'Northwind ${stack.name} agents'
    scope: agentProducts[index].id
    state: 'active'
    allowTracing: false
  }
}]

var diagnosticProperties = {
  alwaysLog: 'allErrors'
  httpCorrelationProtocol: 'W3C'
  logClientIp: true
  loggerId: logger.id
  sampling: {
    percentage: 100
    samplingType: 'fixed'
  }
  verbosity: 'information'
  frontend: {
    request: {
      body: {
        bytes: 0
      }
    }
    response: {
      body: {
        bytes: 0
      }
    }
  }
  backend: {
    request: {
      body: {
        bytes: 0
      }
    }
    response: {
      body: {
        bytes: 0
      }
    }
  }
}

resource restDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2024-05-01' = [for (stack, index) in stacks: {
  parent: restApis[index]
  name: 'applicationinsights'
  properties: diagnosticProperties
}]

resource voiceDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2024-05-01' = [for (stack, index) in stacks: {
  parent: voiceApis[index]
  name: 'applicationinsights'
  properties: diagnosticProperties
}]

resource mcpDiagnostics 'Microsoft.ApiManagement/service/apis/diagnostics@2024-05-01' = [for (definition, index) in mcpApiDefinitions: {
  parent: mcpApis[index]
  name: 'applicationinsights'
  properties: diagnosticProperties
}]

output gatewayUrl string = apim.properties.gatewayUrl
output endpoints array = [for stack in stacks: {
  stack: stack.name
  api: '${apim.properties.gatewayUrl}/${stack.name}'
  voice: 'wss://${replace(apim.properties.gatewayUrl, 'https://', '')}/${stack.name}/voice/realtime'
  mcpTechLookup: '${apim.properties.gatewayUrl}/${stack.name}/mcp/tech-lookup'
  mcpWorkOrder: '${apim.properties.gatewayUrl}/${stack.name}/mcp/work-order'
}]
