param location string
param tags object
param name string
param appPrincipalId string
param devPrincipalId string = ''

resource search 'Microsoft.Search/searchServices@2024-06-01-preview' = {
  name: name
  location: location
  tags: tags
  sku: { name: 'basic' }
  identity: { type: 'SystemAssigned' }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    publicNetworkAccess: 'enabled'
    semanticSearch: 'free'
    // Default: apiKeyOnly. We prefer admin-key auth end-to-end (matches
    // enable-public-access.ps1). The Foundry KB MCP connection is wired
    // with CustomKeys (see scripts/connect-knowledge-base.py) so the
    // agent sends `api-key: <admin-key>` on every MCP call.
    authOptions: {
      apiKeyOnly: {}
    }
  }
}

// Search Index Data Contributor (data plane R/W on documents + indexes)
var indexContribRoleId = '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
// Search Service Contributor (control plane — create indexes/indexers)
var svcContribRoleId = '7ca78c08-252a-4471-8644-bb5ff32d4ba0'

resource appIdxRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, appPrincipalId, indexContribRoleId)
  properties: {
    principalId: appPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', indexContribRoleId)
  }
}

resource appSvcRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, appPrincipalId, svcContribRoleId)
  properties: {
    principalId: appPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', svcContribRoleId)
  }
}

resource devIdxRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(devPrincipalId)) {
  scope: search
  name: guid(search.id, devPrincipalId, indexContribRoleId)
  properties: {
    principalId: devPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', indexContribRoleId)
  }
}

resource devSvcRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(devPrincipalId)) {
  scope: search
  name: guid(search.id, devPrincipalId, svcContribRoleId)
  properties: {
    principalId: devPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', svcContribRoleId)
  }
}

output name string = search.name
output endpoint string = 'https://${search.name}.search.windows.net'
output indexName string = 'northwind-insurance-knowledge'
output knowledgeSourceName string = 'northwind-insurance-knowledge-ks'
output knowledgeBaseName string = 'northwind-insurance-kb'
output principalId string = search.identity.principalId
