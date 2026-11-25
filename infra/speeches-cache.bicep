targetScope = 'resourceGroup'

var location = 'eastus'
var cacheName = 'fiqspeeches9c6b00f4227ca'
var perimeterName = 'nsp-fiq-speeches-9c65c09f'
var tags = {
  environment: 'demo'
  purpose: 'speech-entity-graph-cache'
  'foundry-iq-run': 'fiq-speeches-9c65c09f'
}
var cacheRoles = [
  'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
]

resource search 'Microsoft.Search/searchServices@2025-05-01' existing = {
  name: 'srch-graphdemo-cfb3dbc7a0'
}

resource cache 'Microsoft.Storage/storageAccounts@2026-09-01' = {
  // checkov:skip=CKV_AZURE_206:User-selected LRS cache is recreatable; original speeches remain separately preserved.
  name: cacheName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    accessTier: 'Hot'
    publicNetworkAccess: 'SecuredByPerimeter'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    isHnsEnabled: false
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
      ipRules: []
      virtualNetworkRules: []
    }
    encryption: {
      keySource: 'Microsoft.Storage'
      requireInfrastructureEncryption: true
      services: {
        blob: {
          enabled: true
          keyType: 'Account'
        }
        table: {
          enabled: true
          keyType: 'Account'
        }
      }
    }
  }
}

resource cacheBlobs 'Microsoft.Storage/storageAccounts/blobServices@2026-09-01' = {
  parent: cache
  name: 'default'
  properties: {
    isVersioningEnabled: false
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource perimeter 'Microsoft.Network/networkSecurityPerimeters@2026-07-01' = {
  name: perimeterName
  location: location
  tags: tags
  properties: {}
}

resource cacheProfile 'Microsoft.Network/networkSecurityPerimeters/profiles@2026-07-01' = {
  parent: perimeter
  name: 'cache'
  properties: {}
}

resource allowSearchSubscription 'Microsoft.Network/networkSecurityPerimeters/profiles/accessRules@2026-07-01' = {
  parent: cacheProfile
  name: 'allow-search-subscription'
  properties: {
    direction: 'Inbound'
    subscriptions: [
      {
        id: subscription().id
      }
    ]
  }
}

resource cacheAssociation 'Microsoft.Network/networkSecurityPerimeters/resourceAssociations@2026-07-01' = {
  parent: perimeter
  name: 'cache'
  properties: {
    accessMode: 'Enforced'
    privateLinkResource: {
      id: cache.id
    }
    profile: {
      id: cacheProfile.id
    }
  }
  dependsOn: [
    allowSearchSubscription
  ]
}

resource searchCacheRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in cacheRoles: {
  scope: cache
  name: guid(cache.id, search.id, role)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
    principalId: search.identity.principalId!
    principalType: 'ServicePrincipal'
  }
  dependsOn: [
    cacheAssociation
  ]
}]

output cacheResourceId string = cache.id
output perimeterResourceId string = perimeter.id
output cacheAssociationResourceId string = cacheAssociation.id
