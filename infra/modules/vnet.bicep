// VNet solely for Container Apps environment integration.
// Data-plane services (Foundry, Cosmos, KV, Storage, Search) are exposed publicly
// and accessed over the internet — no private endpoints or private DNS zones.
param location string
param tags object
param vnetName string

var addressSpace = '10.60.0.0/16'
var subnets = [
  {
    name: 'container-apps'
    properties: {
      addressPrefix: '10.60.0.0/23'
      delegations: [
        {
          name: 'ca-delegation'
          properties: { serviceName: 'Microsoft.App/environments' }
        }
      ]
    }
  }
  {
    name: 'private-endpoints'
    properties: {
      addressPrefix: '10.60.2.0/24'
      privateEndpointNetworkPolicies: 'Disabled'
    }
  }
]

resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [addressSpace] }
    subnets: subnets
  }
}

output vnetId string = vnet.id
output containerAppsSubnetId string = vnet.properties.subnets[0].id
output privateEndpointsSubnetId string = vnet.properties.subnets[1].id
