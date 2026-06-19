// NOKVO API — Azure Container App + migration job.
//
// Deployed AFTER platform.bicep, once secrets are in Key Vault and the image is
// pushed to ACR. Env/secret wiring is passed as arrays so the full ~40-var
// config is generated from .env by deploy.sh rather than hardcoded here.
//
//   az deployment group create -g rg-nokvo-prod -f app.bicep -p @app.params.json

targetScope = 'resourceGroup'

param location string = resourceGroup().location
param namePrefix string = 'nokvo'

@description('Resource id of the Container Apps managed environment (platform output).')
param managedEnvironmentId string

@description('Resource id of the user-assigned managed identity (platform output).')
param identityId string

@description('ACR login server, e.g. nokvoacr1234.azurecr.io (platform output).')
param acrLoginServer string

@description('Full image ref incl. tag, e.g. nokvoacr1234.azurecr.io/nokvo-api:<sha>.')
param image string

@description('Env vars passed straight to the container; each item is { name, value } or { name, secretRef }.')
param appEnv array = []

@description('Key Vault secret references; each item is { name, kvSecretUri }, resolved with the app identity.')
param secretsKv array = []

@description('Calls/replica target for the HTTP concurrency scaler — set from the load test.')
param concurrentRequests int = 10

param minReplicas int = 2
param maxReplicas int = 15
param cpu string = '2.0'
param memory string = '4Gi'

param tags object = {
  app: 'nokvo'
  env: 'production'
}

var appName = '${namePrefix}-api'
var jobName = '${namePrefix}-migrate'

// Attach the app identity to every KV-referenced secret.
var secrets = [for s in secretsKv: {
  name: s.name
  keyVaultUrl: s.kvSecretUri
  identity: identityId
}]

var registries = [
  {
    server: acrLoginServer
    identity: identityId
  }
]

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityId}': {} }
  }
  properties: {
    managedEnvironmentId: managedEnvironmentId
    configuration: {
      activeRevisionsMode: 'Multiple' // blue/green: drain old revision's calls
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto' // HTTP + WebSocket
        allowInsecure: false
        // No stickySessions block → session affinity OFF (stateless replicas).
      }
      registries: registries
      secrets: secrets
    }
    template: {
      containers: [
        {
          name: 'api'
          image: image
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: appEnv
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/health/live', port: 8000 }
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: { path: '/health', port: 8000 }
              periodSeconds: 15
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Startup'
              httpGet: { path: '/health/live', port: 8000 }
              periodSeconds: 10
              failureThreshold: 30
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: { concurrentRequests: string(concurrentRequests) }
            }
          }
        ]
      }
    }
  }
}

// Migration job: same image, `alembic upgrade head`, run before each rollout.
resource migrate 'Microsoft.App/jobs@2024-03-01' = {
  name: jobName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityId}': {} }
  }
  properties: {
    environmentId: managedEnvironmentId
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 1800
      replicaRetryLimit: 1
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: registries
      secrets: secrets
    }
    template: {
      containers: [
        {
          name: 'migrate'
          image: image
          command: [ 'alembic', 'upgrade', 'head' ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: appEnv
        }
      ]
    }
  }
}

output apiFqdn string = api.properties.configuration.ingress.fqdn
output apiName string = api.name
output migrationJobName string = migrate.name
