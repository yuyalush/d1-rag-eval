// Decision-1 RAG 評価用の最小構成。リソースグループ単位でデプロイし、検証後は RG ごと削除する。
targetScope = 'resourceGroup'

@description('Foundry（AI Services）のリージョン。Decision-1 が使えるリージョンにする')
param foundryLocation string = 'eastus2'

@description('PostgreSQL のリージョン')
param pgLocation string = 'japaneast'

param namePrefix string = 'd1rag'

@description('config/models.yaml の deployments（name, model, format, version, sku, capacity）')
param deployments array

@description('ロールを割り当てる自分のアカウントのオブジェクト ID')
param principalId string

@description('Azure の PostgreSQL を作るか。開発中はローカル Docker を使うので false')
param deployPostgres bool = false

@description('PostgreSQL の Entra 管理者名（接続時の PG_USER と同じ値）')
param pgAdminName string = ''

@description('PostgreSQL への接続を許可する自分のグローバル IP')
param clientIp string = ''

@description('月額予算（請求通貨。このサブスクリプションは JPY）')
param budgetAmount int = 10000

param budgetContactEmails array

param budgetStartDate string = '${utcNow('yyyy-MM')}-01T00:00:00Z'

var suffix = uniqueString(resourceGroup().id)
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: '${namePrefix}-ais-${suffix}'
  location: foundryLocation
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: '${namePrefix}-ais-${suffix}'
    allowProjectManagement: true
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: '${namePrefix}-proj'
  location: foundryLocation
  identity: {
    type: 'SystemAssigned'
  }
  properties: {}
}

// 同じアカウントへのデプロイは並列にすると競合するため 1 件ずつ作る
@batchSize(1)
resource modelDeployments 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = [
  for d in deployments: {
    parent: foundry
    name: d.name
    sku: {
      name: d.sku
      capacity: d.capacity
    }
    properties: {
      model: {
        format: d.format
        name: d.model
        version: d.version
      }
      versionUpgradeOption: 'NoAutoUpgrade'
    }
    dependsOn: [
      project
    ]
  }
]

resource foundryUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, principalId, cognitiveServicesUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalId: principalId
    principalType: 'User'
  }
}

resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = if (deployPostgres) {
  name: '${namePrefix}-pg-${suffix}'
  location: pgLocation
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    storage: {
      storageSizeGB: 32
      autoGrow: 'Disabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: subscription().tenantId
    }
  }
}

resource pgAdmin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = if (deployPostgres) {
  parent: pg
  name: principalId
  properties: {
    principalType: 'User'
    principalName: pgAdminName
    tenantId: subscription().tenantId
  }
}

// サーバー設定の変更は管理者登録と同時に走らせると競合するため順番に行う
resource pgExtensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = if (deployPostgres) {
  parent: pg
  name: 'azure.extensions'
  properties: {
    value: 'VECTOR'
    source: 'user-override'
  }
  dependsOn: [
    pgAdmin
  ]
}

resource pgDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = if (deployPostgres) {
  parent: pg
  name: 'ragdb'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
  dependsOn: [
    pgExtensions
  ]
}

resource pgFirewall 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = if (deployPostgres && !empty(clientIp)) {
  parent: pg
  name: 'allow-client-ip'
  properties: {
    startIpAddress: clientIp
    endIpAddress: clientIp
  }
  dependsOn: [
    pgDatabase
  ]
}

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: 'budget-${namePrefix}'
  properties: {
    category: 'Cost'
    amount: budgetAmount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: budgetStartDate
    }
    notifications: {
      actual50: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 50
        thresholdType: 'Actual'
        contactEmails: budgetContactEmails
      }
      actual80: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: budgetContactEmails
      }
      actual100: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Actual'
        contactEmails: budgetContactEmails
      }
    }
  }
}

// Azure CLI は出力名を camelCase に書き換える（FOUNDRY_ENDPOINT → foundrY_ENDPOINT）ため camelCase で定義する
output foundryEndpoint string = 'https://${foundry.properties.customSubDomainName}.services.ai.azure.com'
output foundryAccount string = foundry.name
output pgHost string = deployPostgres ? pg!.properties.fullyQualifiedDomainName : ''
output pgServer string = deployPostgres ? pg.name : ''
