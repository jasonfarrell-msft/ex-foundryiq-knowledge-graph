targetScope = 'resourceGroup'

param location string
param searchName string
param foundryName string
param principalId string
@allowed(['User', 'ServicePrincipal'])
param principalType string = 'User'
param chatModel string
param chatVersion string
param chatCapacity int
param publisherIp string
param recreationOwner string

var tags = {
  application: 'speech-atlas'
  recreationOwner: recreationOwner
}
resource search 'Microsoft.Search/searchServices@2026-11-01' = {
  name: searchName
  location: location
  tags: union(tags, { bootstrapAccess: 'publisher-ip-temporary' })
  sku: { name: 'standard' }
  identity: { type: 'SystemAssigned' }
  properties: {
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    networkRuleSet: { ipRules: [{ value: '${publisherIp}/32' }] }
    replicaCount: 1
    partitionCount: 1
    semanticSearch: 'free'
    knowledgeRetrieval: 'free'
  }
}
resource foundry 'Microsoft.CognitiveServices/accounts@2026-11-01' = {
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
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
      ipRules: []
      virtualNetworkRules: []
    }
  }
}
resource chat 'Microsoft.CognitiveServices/accounts/deployments@2026-11-01' = {
  parent: foundry
  name: 'chat'
  sku: { name: 'GlobalStandard', capacity: chatCapacity }
  properties: {
    model: { format: 'OpenAI', name: chatModel, version: chatVersion }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}
var callerRoles = [
  '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
  '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
]
resource callerAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in callerRoles: {
  name: guid(search.id, principalId, role)
  scope: search
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
  }
}]
resource modelAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, search.id, '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  scope: foundry
  properties: {
    principalId: search.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  }
}
