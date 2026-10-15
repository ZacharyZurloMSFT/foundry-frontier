param location string
param tags object
param name string
param appPrincipalId string
param devPrincipalId string = ''
@description('Principal ID of the Azure AI Search service MI. Granted Storage Blob Data Reader so the search indexer can pull PDFs/markdown from the seed container.')
param searchPrincipalId string = ''

resource sa 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: name
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
    networkAcls: { defaultAction: 'Allow', bypass: 'AzureServices' }
  }
}

resource blobSvc 'Microsoft.Storage/storageAccounts/blobServices@2024-01-01' = {
  parent: sa
  name: 'default'
}

resource seedContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2024-01-01' = {
  parent: blobSvc
  name: 'seed'
}

resource cuContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2024-01-01' = {
  parent: blobSvc
  name: 'cu-inbox'
}

// Storage Blob Data Contributor
var blobContribRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
// Storage Blob Data Reader — indexer only needs read.
var blobReaderRoleId = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'

resource appRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: sa
  name: guid(sa.id, appPrincipalId, blobContribRoleId)
  properties: {
    principalId: appPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobContribRoleId)
  }
}

resource searchRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(searchPrincipalId)) {
  scope: sa
  name: guid(sa.id, searchPrincipalId, blobReaderRoleId)
  properties: {
    principalId: searchPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobReaderRoleId)
  }
}

resource devRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(devPrincipalId)) {
  scope: sa
  name: guid(sa.id, devPrincipalId, blobContribRoleId)
  properties: {
    principalId: devPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobContribRoleId)
  }
}

output name string = sa.name
output id string = sa.id
output blobEndpoint string = sa.properties.primaryEndpoints.blob
output seedContainerName string = seedContainer.name
output cuContainerName string = cuContainer.name
