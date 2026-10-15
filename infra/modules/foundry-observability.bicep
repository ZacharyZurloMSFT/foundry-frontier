// Connects the flagship Foundry project to the workspace-based Application
// Insights resource. Foundry exports agent traces to Application Insights;
// the component stores them in its linked Log Analytics workspace.
param aiServicesName string
param projectName string
param appInsightsName string
param projectPrincipalId string

resource ai 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: aiServicesName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' existing = {
  parent: ai
  name: projectName
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource accountConnection 'Microsoft.CognitiveServices/accounts/connections@2025-04-01-preview' = {
  parent: ai
  name: '${appInsightsName}-account'
  properties: {
    category: 'AppInsights'
    target: appInsights.id
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: appInsights.properties.ConnectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: appInsights.id
    }
  }
}

resource projectConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = {
  parent: project
  name: '${projectName}-appinsights'
  properties: {
    category: 'AppInsights'
    target: appInsights.id
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: appInsights.properties.ConnectionString
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: appInsights.id
    }
  }
}

// Log Analytics Reader + Privileged Monitoring Data Reader. The latter is
// required when Foundry needs to read protected GenAI trace fields.
var monitoringReaderRoleIds = [
  '73c42c96-874c-492b-b04d-ab87d138a893'
  'dbc9c667-e97f-4491-aee6-90b9cf960190'
]

resource projectMonitoringReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for roleId in monitoringReaderRoleIds: {
  scope: appInsights
  name: guid(project.id, roleId, appInsights.id)
  properties: {
    principalId: projectPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
  }
}]
