// NOKVO production platform — Azure Container Apps + managed data tier.
//
// Cost-optimized per the deployment plan: ACR Basic, Postgres Burstable
// single-zone, Redis Standard C1, Container Apps Consumption, Static Web Apps
// Free, India region. Data tier is public + firewall-locked (no VNet
// injection), per the chosen network posture.
//
// This deploys the PLATFORM only. The Container App + migration job live in
// app.bicep and are deployed AFTER secrets are loaded into Key Vault and the
// image is pushed (see deploy.sh / README.md), so they can reference both.
//
//   az deployment group create -g rg-nokvo-prod -f platform.bicep \
//     -p pgAdminPassword=<secret>

targetScope = 'resourceGroup'

@description('Azure region for the data tier + compute. Co-locate near Plivo/Sarvam + tenants (India).')
param location string = 'centralindia'

@description('Region for the Static Web App (limited region set; content is global via CDN).')
@allowed([ 'eastasia', 'centralus', 'eastus2', 'westus2', 'westeurope' ])
param swaLocation string = 'eastasia'

@description('Short prefix for resource names.')
param namePrefix string = 'nokvo'

@description('PostgreSQL admin login.')
param pgAdminLogin string = 'nokvoadmin'

@secure()
@description('PostgreSQL admin password.')
param pgAdminPassword string

@description('Postgres SKU — Burstable to start; upsize to a GP D-series on CPU-credit depletion.')
param pgSkuName string = 'Standard_B2ms'

@description('Postgres storage (GB).')
param pgStorageGB int = 32


param tags object = {
  app: 'nokvo'
  env: 'production'
}

var suffix = uniqueString(resourceGroup().id)
var shortSuffix = substring(suffix, 0, 6)
var acrName = toLower('${namePrefix}acr${suffix}')
var storageName = toLower('${namePrefix}st${shortSuffix}')
var kvName = toLower('${namePrefix}-kv-${shortSuffix}')
var pgName = toLower('${namePrefix}-pg-${shortSuffix}')
var redisName = toLower('${namePrefix}-redis-${shortSuffix}')
var lawName = '${namePrefix}-law-${shortSuffix}'
var aiName = '${namePrefix}-ai-${shortSuffix}'
var miName = '${namePrefix}-id'
var envName = '${namePrefix}-cae'
var swaName = '${namePrefix}-portal'

// Well-known built-in role definition IDs.
var roleAcrPull = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var roleKvSecretsUser = '4633458b-17de-408a-b874-0445c86b69e6'
var roleStorageBlobContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

// ---------------- Observability ----------------
resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: lawName
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    // Cost guardrail: cap ingestion so a chatty voice app can't run up a bill.
    workspaceCapping: { dailyQuotaGb: 5 }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: aiName
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: law.id
    IngestionMode: 'LogAnalytics'
  }
}

// ---------------- Identity ----------------
resource mi 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: miName
  location: location
  tags: tags
}

// ---------------- Container Registry (Basic) ----------------
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

// ---------------- Storage (brochures / documents) ----------------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

// ---------------- Key Vault (RBAC) ----------------
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  tags: tags
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
  }
}

// ---------------- PostgreSQL Flexible Server (Burstable, single-zone) ----------------
resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: pgName
  location: location
  tags: tags
  sku: {
    name: pgSkuName
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: pgAdminLogin
    administratorLoginPassword: pgAdminPassword
    storage: { storageSizeGB: pgStorageGB }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
    authConfig: {
      passwordAuth: 'Enabled'
      activeDirectoryAuth: 'Disabled'
    }
  }
}

resource pgDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: pg
  name: 'nokvo'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// Public + firewall: allow Azure-internal services (the Container Apps env's
// outbound) to reach Postgres. 0.0.0.0/0.0.0.0 is the "Allow Azure services"
// sentinel — tighten to the env's static outbound IPs post-deploy if desired.
resource pgFwAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: pg
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// ---------------- Azure Managed Redis (classic Cache for Redis is retired for new creation) ----------------
resource redis 'Microsoft.Cache/redisEnterprise@2024-10-01' = {
  name: redisName
  location: location
  tags: tags
  sku: {
    name: 'Balanced_B0' // smallest Azure Managed Redis SKU
  }
}

resource redisDb 'Microsoft.Cache/redisEnterprise/databases@2024-10-01' = {
  parent: redis
  name: 'default'
  properties: {
    clientProtocol: 'Encrypted'           // TLS only
    port: 10000
    clusteringPolicy: 'EnterpriseCluster' // single logical endpoint → non-cluster redis client + multi-key Lua work
    evictionPolicy: 'VolatileLRU'         // evict only keys with a TTL (sessions/budgets/locks all set one)
  }
}

// ---------------- Container Apps Environment (Consumption) ----------------
resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
  }
}

// ---------------- Static Web App (Free) — Vue portal ----------------
resource swa 'Microsoft.Web/staticSites@2023-12-01' = {
  name: swaName
  location: swaLocation
  tags: tags
  sku: { name: 'Free', tier: 'Free' }
  properties: {}
}

// ---------------- Role assignments (narrow data-plane) ----------------
resource raAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, mi.id, roleAcrPull)
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleAcrPull)
    principalId: mi.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource raKvSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, mi.id, roleKvSecretsUser)
  scope: kv
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleKvSecretsUser)
    principalId: mi.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource raStorageBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, mi.id, roleStorageBlobContributor)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleStorageBlobContributor)
    principalId: mi.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------- Outputs (consumed by app.bicep / deploy.sh) ----------------
output acrLoginServer string = acr.properties.loginServer
output acrName string = acr.name
output managedEnvironmentId string = cae.id
output managedEnvironmentDefaultDomain string = cae.properties.defaultDomain
output identityId string = mi.id
output identityClientId string = mi.properties.clientId
output identityPrincipalId string = mi.properties.principalId
output keyVaultName string = kv.name
output keyVaultUri string = kv.properties.vaultUri
output postgresFqdn string = pg.properties.fullyQualifiedDomainName
output postgresName string = pg.name
output redisHostName string = redis.properties.hostName
output redisPort int = 10000
output storageAccountName string = storage.name
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output staticWebAppName string = swa.name
output staticWebAppDefaultHostname string = swa.properties.defaultHostname
