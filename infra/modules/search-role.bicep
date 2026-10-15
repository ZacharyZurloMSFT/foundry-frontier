// Post-facto role assignment: grants the Foundry project's system-assigned
// MI the Search Index Data Reader role on the Search service. Lives in its
// own module so main.bicep can create it after both `search` and `foundry`
// modules complete — avoids the circular dep that would otherwise arise
// (foundry already consumes search.principalId).
param searchName string
param projectPrincipalId string

// Search Index Data Reader
var indexReaderRoleId = '1407120a-92aa-4202-b7e9-c0e197c71c8f'

resource search 'Microsoft.Search/searchServices@2024-06-01-preview' existing = {
  name: searchName
}

resource projectSearchReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, projectPrincipalId, indexReaderRoleId)
  properties: {
    principalId: projectPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', indexReaderRoleId)
  }
}
