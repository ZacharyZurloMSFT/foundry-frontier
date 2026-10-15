param location string
param tags object
param name string
param appPrincipalId string
param devPrincipalId string = ''

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: name
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
    anonymousPullEnabled: false
  }
}

// AcrPull — app UAMI pulls images at runtime
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
// AcrPush — dev user pushes images from `azd deploy`
var acrPushRoleId = '8311e382-0749-4cb8-b61a-304f252e45ec'

resource appPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, appPrincipalId, acrPullRoleId)
  properties: {
    principalId: appPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

resource devPush 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(devPrincipalId)) {
  scope: acr
  name: guid(acr.id, devPrincipalId, acrPushRoleId)
  properties: {
    principalId: devPrincipalId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPushRoleId)
  }
}

output id string = acr.id
output name string = acr.name
output loginServer string = acr.properties.loginServer
