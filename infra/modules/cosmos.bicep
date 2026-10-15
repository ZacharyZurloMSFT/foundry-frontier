param location string
param tags object
param name string
param appPrincipalId string
param devPrincipalId string = ''

var databaseName = 'northwind'
var containers = [
  { name: 'conversations',   partitionKey: '/sessionId' }
  { name: 'workflow_state',  partitionKey: '/workflowId' }
  { name: 'hitl_queue',      partitionKey: '/workflowId' }
  { name: 'cu_extractions',  partitionKey: '/docType' }
]

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: name
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    locations: [ { locationName: location, failoverPriority: 0, isZoneRedundant: false } ]
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
    disableLocalAuth: false
    publicNetworkAccess: 'Disabled'
    networkAclBypass: 'None'
    isVirtualNetworkFilterEnabled: false
    ipRules: []
    virtualNetworkRules: []
    capabilities: [ { name: 'EnableServerless' } ]
  }
}

resource db 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: cosmos
  name: databaseName
  properties: { resource: { id: databaseName } }
}

resource cont 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = [for c in containers: {
  parent: db
  name: c.name
  properties: {
    resource: {
      id: c.name
      partitionKey: { paths: [c.partitionKey], kind: 'Hash' }
      defaultTtl: -1
    }
  }
}]

// Cosmos DB Built-in Data Contributor
resource dataRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmos
  name: guid(cosmos.id, appPrincipalId, 'data-contributor')
  properties: {
    principalId: appPrincipalId
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: cosmos.id
  }
}

resource devDataRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = if (!empty(devPrincipalId)) {
  parent: cosmos
  name: guid(cosmos.id, devPrincipalId, 'data-contributor')
  properties: {
    principalId: devPrincipalId
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: cosmos.id
  }
}

output name string = cosmos.name
output id string = cosmos.id
output endpoint string = cosmos.properties.documentEndpoint
output databaseName string = databaseName
