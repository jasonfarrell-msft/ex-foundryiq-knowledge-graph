targetScope = 'resourceGroup'

param foundryName string
param subnetId string
param virtualNetworkId string
param location string
param suffix string
param recreationOwner string

resource foundry 'Microsoft.CognitiveServices/accounts@2026-11-01' existing = {
  name: foundryName
}
var tags = { application: 'speech-atlas', recreationOwner: recreationOwner }
resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.cognitiveservices.azure.com'
  location: 'global'
  tags: tags
}
resource link 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: zone
  name: 'speech-foundry-${suffix}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: virtualNetworkId }
  }
}
resource endpoint 'Microsoft.Network/privateEndpoints@2026-07-01' = {
  name: 'pe-fiq-web-foundry-${suffix}'
  location: location
  tags: tags
  properties: {
    subnet: { id: subnetId }
    privateLinkServiceConnections: [{
      name: 'foundry'
      properties: {
        privateLinkServiceId: foundry.id
        groupIds: ['account']
      }
    }]
  }
}
resource dns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2026-07-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [{
      name: 'foundry'
      properties: { privateDnsZoneId: zone.id }
    }]
  }
}
