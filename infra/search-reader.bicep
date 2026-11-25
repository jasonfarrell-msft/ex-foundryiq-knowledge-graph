targetScope = 'resourceGroup'

param searchName string
param principalId string
param functionId string

resource search 'Microsoft.Search/searchServices@2026-11-01' existing = {
  name: searchName
}
var role = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '1407120a-92aa-4202-b7e9-c0e197c71c8f')
resource access 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, functionId, role)
  scope: search
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: role
  }
}
