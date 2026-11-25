targetScope = 'resourceGroup'

@allowed(['approved-synthetic-demo'])
@description('Explicit exception: public HTTPS endpoints for fictional data only, never production.')
param networkApproval string
param searchName string
param storageName string
param foundryName string
param location string = 'eastus'
param principalId string
@allowed(['User', 'ServicePrincipal'])
param principalType string = 'User'
param chatModel string
param chatVersion string
param embeddingModel string
param embeddingVersion string
@allowed(['GlobalStandard', 'DataZoneStandard', 'Standard'])
param modelSku string = 'GlobalStandard'
@minValue(1)
param chatCapacity int = 10
@minValue(1)
param embeddingCapacity int = 1
@allowed(['basic', 'standard', 'standard2'])
param searchSku string = 'basic'

var publicAccess = networkApproval == 'approved-synthetic-demo' ? 'Enabled' : 'Disabled'
var tags = {
  purpose: 'synthetic-entity-graph'
  environment: 'demo'
}
var searchRoles = [
  '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
  '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
]
var blobReaderRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1')
var blobContributorRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
var openAIUserRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')

// Latest registered contract exposing knowledgeRetrieval in the target subscription.
resource search 'Microsoft.Search/searchServices@2026-09-01-preview' = {
  // checkov:skip=CKV_AZURE_208:Single-replica fictional-data demo; production indexing SLA is intentionally out of scope.
  // checkov:skip=CKV_AZURE_209:Single-replica fictional-data demo; production query SLA is intentionally out of scope.
  name: searchName
  location: location
  tags: tags
  sku: { name: searchSku }
  identity: { type: 'SystemAssigned' }
  properties: {
    disableLocalAuth: true
    publicNetworkAccess: publicAccess
    replicaCount: 1
    partitionCount: 1
    semanticSearch: 'free'
    knowledgeRetrieval: 'free'
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2026-09-01' = {
  // checkov:skip=CKV_AZURE_35:User explicitly approved public-network synthetic demo; OAuth and anonymous-blob prohibition remain enforced.
  // checkov:skip=CKV_AZURE_43:Name is supplied by setup_resources.py, which generates 3-24 lowercase alphanumeric characters.
  // checkov:skip=CKV_AZURE_206:Disposable two-document demo uses LRS, not production geo/zone replication.
  name: storageName
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    allowBlobPublicAccess: false
    allowCrossTenantReplication: false
    isHnsEnabled: false
    publicNetworkAccess: publicAccess
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'None'
    }
    encryption: {
      keySource: 'Microsoft.Storage'
      services: {
        blob: { enabled: true, keyType: 'Account' }
        file: { enabled: true, keyType: 'Account' }
      }
    }
  }
}

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2026-09-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: { enabled: true, days: 7 }
    isVersioningEnabled: false
  }
}

resource foundry 'Microsoft.CognitiveServices/accounts@2026-09-01' = {
  // checkov:skip=CKV_AZURE_134:User explicitly approved public-network synthetic demo; local authentication is disabled.
  name: foundryName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: foundryName
    allowProjectManagement: true
    disableLocalAuth: true
    publicNetworkAccess: publicAccess
    networkAcls: {
      defaultAction: 'Allow'
      ipRules: []
      virtualNetworkRules: []
    }
  }
}

resource chat 'Microsoft.CognitiveServices/accounts/deployments@2026-09-01' = {
  parent: foundry
  name: 'chat'
  sku: { name: modelSku, capacity: chatCapacity }
  properties: {
    model: { format: 'OpenAI', name: chatModel, version: chatVersion }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource embedding 'Microsoft.CognitiveServices/accounts/deployments@2026-09-01' = {
  parent: foundry
  name: 'embedding'
  sku: { name: modelSku, capacity: embeddingCapacity }
  properties: {
    model: { format: 'OpenAI', name: embeddingModel, version: embeddingVersion }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [chat]
}

resource callerSearchRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in searchRoles: {
  scope: search
  name: guid(search.id, principalId, role)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
    principalId: principalId
    principalType: principalType
  }
}]

resource searchStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, search.id, blobReaderRole)
  properties: {
    roleDefinitionId: blobReaderRole
    principalId: search.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource callerStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, principalId, blobContributorRole)
  properties: {
    roleDefinitionId: blobContributorRole
    principalId: principalId
    principalType: principalType
  }
}

resource searchModelRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, search.id, openAIUserRole)
  properties: {
    roleDefinitionId: openAIUserRole
    principalId: search.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource callerModelRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, principalId, openAIUserRole)
  properties: {
    roleDefinitionId: openAIUserRole
    principalId: principalId
    principalType: principalType
  }
}

output searchResourceId string = search.id
output storageResourceId string = storage.id
output modelResourceId string = foundry.id
output chatDeployment string = chat.name
output embeddingDeployment string = embedding.name
output searchPrincipalId string = search.identity.principalId
