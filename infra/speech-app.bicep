targetScope = 'resourceGroup'

param location string = 'eastus2'
param suffix string = '9c65c09f'
param searchName string = 'srch-graphdemo-cfb3dbc7a0'
param searchResourceGroup string = resourceGroup().name
param recreationOwner string = ''
@description('Enable only after linking the empty Function App to this Static Web App.')
param hardenLinkedAuthentication bool = false

var tags = {
  application: 'speech-atlas'
  corpus: 'all-speeches'
  recreationOwner: recreationOwner
}
var storageName = 'stfiqweb${suffix}'
var blobOwnerRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')

resource site 'Microsoft.Web/staticSites@2025-05-01' = {
  name: 'swa-fiq-speeches-${suffix}'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
  properties: {
    provider: 'Custom'
    stagingEnvironmentPolicy: 'Disabled'
    allowConfigFileUpdates: true
  }
}

resource network 'Microsoft.Network/virtualNetworks@2026-07-01' = {
  name: 'vnet-fiq-web-${suffix}'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: ['10.96.0.0/24']
    }
    subnets: [
      {
        name: 'functions'
        properties: {
          addressPrefix: '10.96.0.0/26'
          networkSecurityGroup: {
            id: nsg.id
          }
          delegations: [
            {
              name: 'flex'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'private-endpoints'
        properties: {
          addressPrefix: '10.96.0.64/27'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource nsg 'Microsoft.Network/networkSecurityGroups@2026-07-01' = {
  name: 'nsg-fiq-web-${suffix}'
  location: location
  tags: tags
  properties: {
    securityRules: []
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2026-09-01' = {
  name: storageName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
    }
  }
}

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2026-09-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource packages 'Microsoft.Storage/storageAccounts/blobServices/containers@2026-09-01' = {
  parent: blobs
  name: 'deployment'
  properties: {
    publicAccess: 'None'
  }
}

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.blob.${environment().suffixes.storage}'
  location: 'global'
  tags: tags
}

resource dnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: zone
  name: 'speech-web-${suffix}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: network.id
    }
  }
}

resource endpoint 'Microsoft.Network/privateEndpoints@2026-07-01' = {
  name: 'pe-fiq-web-blob-${suffix}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: '${network.id}/subnets/private-endpoints'
    }
    privateLinkServiceConnections: [
      {
        name: 'host-and-packages'
        properties: {
          privateLinkServiceId: storage.id
          groupIds: ['blob']
        }
      }
    ]
  }
}

resource dnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2026-07-01' = {
  parent: endpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'blob'
        properties: {
          privateDnsZoneId: zone.id
        }
      }
    ]
  }
}

resource plan 'Microsoft.Web/serverfarms@2026-08-01' = {
  name: 'asp-fiq-web-${suffix}'
  location: location
  tags: tags
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource api 'Microsoft.Web/sites@2026-08-01' = {
  name: 'func-fiq-speeches-${suffix}'
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    virtualNetworkSubnetId: '${network.id}/subnets/functions'
    functionAppConfig: {
      runtime: {
        name: 'python'
        version: '3.14'
      }
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${packages.name}'
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 1
        instanceMemoryMB: 2048
        triggers: {
          http: {
            perInstanceConcurrency: 4
          }
        }
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: [
        {
          name: 'AzureWebJobsStorage__accountName'
          value: storage.name
        }
        {
          name: 'AzureWebJobsStorage__credential'
          value: 'managedidentity'
        }
        {
          name: 'APP_ALLOWED_ORIGIN'
          value: 'https://${site.properties.defaultHostname}'
        }
      ]
      cors: {
        allowedOrigins: [
          'https://${site.properties.defaultHostname}'
        ]
      }
    }
  }
  dependsOn: [dnsGroup, dnsLink]
}

resource scmPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2026-08-01' = {
  parent: api
  name: 'scm'
  properties: {
    allow: false
  }
}

resource ftpPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2026-08-01' = {
  parent: api
  name: 'ftp'
  properties: {
    allow: false
  }
}

resource linkedAuthentication 'Microsoft.Web/sites/config@2026-08-01' = if (hardenLinkedAuthentication) {
  parent: api
  name: 'authsettingsV2'
  properties: {
    platform: {
      enabled: true
      runtimeVersion: '~1'
    }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'Return401'
    }
    httpSettings: {
      requireHttps: true
    }
    identityProviders: {
      azureStaticWebApps: {
        enabled: true
        registration: {
          clientId: site.properties.defaultHostname
        }
      }
      azureActiveDirectory: { enabled: false }
      apple: { enabled: false }
      facebook: { enabled: false }
      gitHub: { enabled: false }
      google: { enabled: false }
      legacyMicrosoftAccount: { enabled: false }
      twitter: { enabled: false }
    }
    login: {
      tokenStore: {
        enabled: false
      }
    }
  }
}

resource hostAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, api.id, blobOwnerRole)
  scope: storage
  properties: {
    principalId: api.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: blobOwnerRole
  }
}

resource search 'Microsoft.Search/searchServices@2026-11-01' existing = {
  name: searchName
  scope: resourceGroup(searchResourceGroup)
}

resource searchZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.search.windows.net'
  location: 'global'
  tags: tags
}

resource searchDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: searchZone
  name: 'speech-web-${suffix}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: network.id
    }
  }
}

resource searchEndpoint 'Microsoft.Network/privateEndpoints@2026-07-01' = {
  name: 'pe-fiq-web-search-${suffix}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: '${network.id}/subnets/private-endpoints'
    }
    privateLinkServiceConnections: [
      {
        name: 'sample-search'
        properties: {
          privateLinkServiceId: search.id
          groupIds: ['searchService']
        }
      }
    ]
  }
}

resource searchDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2026-07-01' = {
  parent: searchEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'search'
        properties: {
          privateDnsZoneId: searchZone.id
        }
      }
    ]
  }
}

module queryAccess 'search-reader.bicep' = {
  name: 'speech-search-reader-${suffix}'
  scope: resourceGroup(searchResourceGroup)
  params: {
    searchName: searchName
    principalId: api.identity.principalId
    functionId: api.id
  }
}

output staticSiteName string = site.name
output staticSiteUrl string = 'https://${site.properties.defaultHostname}'
output functionName string = api.name
output functionId string = api.id
output functionUrl string = 'https://${api.properties.defaultHostName}'
output functionPrincipalId string = api.identity.principalId
output storageAccountName string = storage.name
output privateEndpointSubnetId string = '${network.id}/subnets/private-endpoints'
output virtualNetworkId string = network.id
