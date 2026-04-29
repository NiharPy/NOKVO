<script setup>
import { ref, onUnmounted } from 'vue';
import { Building2, Shield, LogOut, CheckCircle2, AlertTriangle, MapPin, ArrowRight, Server, Mail, User, Phone, Languages, BadgeCheck, Database, FolderTree, Mic } from 'lucide-vue-next';
import axios from 'axios';

const emit = defineEmits(['logout']);

const organizationName = ref('');
const adminEmail = ref('');
const adminName = ref('');
const selectedRegion = ref('centralindia');
const environment = ref('production');
const callType = ref('inbound');
const language = ref('en-IN');
const planType = ref('pilot');
const storesPii = ref(true);
const recordCalls = ref(true);
const createResourceGroup = ref(true);
const plivoAutoProvision = ref(false);

const isCreating = ref(false);
const showSuccess = ref(false);
const errorMsg = ref('');
const provisionResult = ref(null);
const activeOrganizationId = ref(null);
let statusPoller = null;

// Matches backend ALLOWED_REGIONS exactly
const REGIONS = [
  { value: 'centralindia', label: 'Central India' },
  { value: 'southindia', label: 'South India' },
  { value: 'westindia', label: 'West India' },
  { value: 'eastus', label: 'East US' },
  { value: 'westus', label: 'West US' },
  { value: 'westeurope', label: 'West Europe' },
  { value: 'southeastasia', label: 'Southeast Asia' }
];

const liveSteps = ref([]);

const handleCreateOrg = async () => {
  if (!organizationName.value || !adminEmail.value || !adminName.value) return;
  
  isCreating.value = true;
  errorMsg.value = '';
  provisionResult.value = null;
  liveSteps.value = [];
  
  try {
    const token = localStorage.getItem('access_token');
    const body = JSON.stringify({
      organization_name: organizationName.value,
      admin_email: adminEmail.value,
      admin_name: adminName.value,
      region: selectedRegion.value,
      environment: environment.value,
      call_type: callType.value,
      language: language.value,
      plan_type: planType.value,
      stores_pii: storesPii.value,
      record_calls: recordCalls.value,
      create_resource_group: createResourceGroup.value,
      plivo_auto_provision: plivoAutoProvision.value
    });
    
    const response = await fetch('http://localhost:8000/superadmin/tenants/provision/stream', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
      },
      body
    });

    if (!response.ok) {
      const err = await response.json();
      errorMsg.value = err.detail || 'Failed to provision organization';
      isCreating.value = false;
      return;
    }

    showSuccess.value = true;
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      let eventType = 'step';
      for (const line of lines) {
        if (line.startsWith('event: ')) {
          eventType = line.slice(7).trim();
        } else if (line.startsWith('data: ')) {
          const data = JSON.parse(line.slice(6));
          if (eventType === 'step') {
            // Update or add step in liveSteps
            const idx = liveSteps.value.findIndex(s => s.name === data.name);
            if (idx >= 0) {
              liveSteps.value[idx] = data;
            } else {
              liveSteps.value.push(data);
            }
          } else if (eventType === 'complete') {
            provisionResult.value = data;
            activeOrganizationId.value = data.organization_id || null;
          } else if (eventType === 'error') {
            errorMsg.value = data.error || 'Provisioning failed';
          }
        }
      }
    }
  } catch (error) {
    errorMsg.value = error.message || 'Failed to provision organization';
  } finally {
    isCreating.value = false;
  }
};

const refreshProvisionStatus = async () => {
  if (!activeOrganizationId.value) return;
  try {
    const token = localStorage.getItem('access_token');
    const response = await axios.get(`http://localhost:8000/superadmin/tenants/provision/${activeOrganizationId.value}/status`, {
      headers: { Authorization: `Bearer ${token}` }
    });
    provisionResult.value = response.data;
  } catch (error) {
    // best-effort sync
  }
};

const startStatusPolling = () => {
  stopStatusPolling();
  statusPoller = setInterval(async () => {
    await refreshProvisionStatus();
    const state = provisionResult.value?.status;
    if (state === 'success' || state === 'partial' || state === 'failed') {
      stopStatusPolling();
    }
  }, 3000);
};

const stopStatusPolling = () => {
  if (statusPoller) {
    clearInterval(statusPoller);
    statusPoller = null;
  }
};

const resetForm = () => {
  stopStatusPolling();
  organizationName.value = '';
  adminEmail.value = '';
  adminName.value = '';
  selectedRegion.value = 'centralindia';
  environment.value = 'production';
  callType.value = 'inbound';
  language.value = 'en-IN';
  planType.value = 'pilot';
  storesPii.value = true;
  recordCalls.value = true;
  createResourceGroup.value = true;
  plivoAutoProvision.value = false;
  showSuccess.value = false;
  provisionResult.value = null;
  errorMsg.value = '';
  activeOrganizationId.value = null;
};

const handleLogout = () => {
  stopStatusPolling();
  emit('logout');
};

onUnmounted(() => {
  stopStatusPolling();
});
</script>

<template>
  <div class="dashboard-container">
    <div class="dashboard-header">
      <div class="header-left">
        <Shield :size="32" color="var(--success-color)" />
        <div class="header-title">
          <h2>SUPERADMIN CONSOLE</h2>
          <span class="status-badge">SECURE SESSION ACTIVE</span>
        </div>
      </div>
      <button class="logout-btn" @click="handleLogout">
        <LogOut :size="16" />
        TERMINATE SESSION
      </button>
    </div>

    <div class="dashboard-content">
      <div class="panel provision-panel">
        <div class="panel-header">
          <h3>PROVISION NEW ORGANIZATION</h3>
          <p>Deploy a new secure Azure tenant environment.</p>
          <div class="api-hint">POST /superadmin/tenants/provision</div>
        </div>
        <!-- LIVE PROGRESS: Show steps as they stream in -->
        <div v-if="isCreating || (showSuccess && !provisionResult && liveSteps.length)" class="provision-result">
          <div class="success-alert" style="border-color: #3b82f6; background: rgba(59, 130, 246, 0.1);">
            <Server :size="24" color="#3b82f6" />
            <div class="alert-text">
              <strong style="color: #3b82f6;">PROVISIONING IN PROGRESS</strong>
              <p>Creating Azure resources...</p>
            </div>
          </div>

          <div class="steps-section">
            <h4>LIVE PROGRESS</h4>
            <div v-for="step in liveSteps" :key="step.name" class="step-item">
              <span class="step-status" :class="step.status">{{ step.status.toUpperCase() }}</span>
              <span class="step-name">{{ step.name }}</span>
              <span v-if="step.message" class="step-msg">{{ step.message }}</span>
            </div>
          </div>
        </div>

        <!-- SUCCESS: Show provisioning result -->
        <div v-if="showSuccess && provisionResult" class="provision-result">
          <div :class="provisionResult.status === 'success' ? 'success-alert' : 'warning-alert'">
            <CheckCircle2 v-if="provisionResult.status === 'success'" :size="24" color="var(--success-color)" />
            <AlertTriangle v-else :size="24" color="#f59e0b" />
            <div class="alert-text">
              <strong>{{ provisionResult.status === 'success' ? 'PROVISIONING COMPLETE' : 'PARTIAL PROVISIONING' }}</strong>
              <p>Tenant ID: <code>{{ provisionResult.tenant_id }}</code></p>
            </div>
          </div>

          <div v-if="provisionResult.azure" class="azure-info">
            <div class="info-row"><span class="info-label">RESOURCE GROUP</span><code>{{ provisionResult.azure.resource_group }}</code></div>
            <div class="info-row"><span class="info-label">REGION</span><code>{{ provisionResult.azure.region }}</code></div>
          </div>

          <div v-if="provisionResult.organization_profile" class="azure-info">
            <div class="info-row"><span class="info-label">ORG</span><code>{{ provisionResult.organization_name }}</code></div>
            <div v-if="provisionResult.organization_profile.admin_name" class="info-row"><span class="info-label">ADMIN</span><code>{{ provisionResult.organization_profile.admin_name }}</code></div>
            <div v-if="provisionResult.organization_profile.admin_email" class="info-row"><span class="info-label">ADMIN EMAIL</span><code>{{ provisionResult.organization_profile.admin_email }}</code></div>
            <div v-if="provisionResult.organization_profile.call_type" class="info-row"><span class="info-label">CALL TYPE</span><code>{{ provisionResult.organization_profile.call_type }}</code></div>
            <div v-if="provisionResult.organization_profile.language" class="info-row"><span class="info-label">LANGUAGE</span><code>{{ provisionResult.organization_profile.language }}</code></div>
            <div v-if="provisionResult.organization_profile.plan_type" class="info-row"><span class="info-label">PLAN</span><code>{{ provisionResult.organization_profile.plan_type }}</code></div>
          </div>

          <div v-if="provisionResult.resources" class="azure-info">
            <div v-if="provisionResult.resources.qdrant_collection" class="info-row"><span class="info-label">QDRANT</span><code>{{ provisionResult.resources.qdrant_collection }}</code></div>
            <div v-if="provisionResult.resources.redis_namespace" class="info-row"><span class="info-label">REDIS NAMESPACE</span><code>{{ provisionResult.resources.redis_namespace }}</code></div>
            <div v-if="provisionResult.resources.redis_host" class="info-row"><span class="info-label">DEDICATED REDIS</span><code>{{ provisionResult.resources.redis_host }}</code></div>
            <div v-if="provisionResult.resources.blob_prefix" class="info-row"><span class="info-label">BLOB PREFIX</span><code>{{ provisionResult.resources.blob_prefix }}</code></div>
            <div v-if="provisionResult.resources.key_vault" class="info-row"><span class="info-label">KEY VAULT</span><code>{{ provisionResult.resources.key_vault }}</code></div>
            <div v-if="provisionResult.resources.llm_provider" class="info-row"><span class="info-label">LLM PROVIDER</span><code>{{ provisionResult.resources.llm_provider }}</code></div>
            <div v-if="provisionResult.resources.llm_model" class="info-row"><span class="info-label">LLM MODEL</span><code>{{ provisionResult.resources.llm_model }}</code></div>
            <div v-if="provisionResult.resources.llm_endpoint" class="info-row"><span class="info-label">LLM ENDPOINT</span><code>{{ provisionResult.resources.llm_endpoint }}</code></div>
            <div v-if="provisionResult.resources.llm_status" class="info-row"><span class="info-label">LLM STATUS</span><code>{{ provisionResult.resources.llm_status }}</code></div>
            <div v-if="provisionResult.resources.plivo_status" class="info-row"><span class="info-label">PLIVO</span><code>{{ provisionResult.resources.plivo_status }}</code></div>
          </div>

          <div class="steps-section">
            <h4>PROVISIONING STEPS</h4>
            <div v-for="step in provisionResult.steps" :key="step.name" class="step-item">
              <span class="step-status" :class="step.status">{{ step.status.toUpperCase() }}</span>
              <span class="step-name">{{ step.name }}</span>
              <span v-if="step.message" class="step-msg">{{ step.message }}</span>
            </div>
          </div>

          <div v-if="provisionResult.next_steps && provisionResult.next_steps.length" class="next-steps-section">
            <h4>NEXT STEPS</h4>
            <ul class="next-steps-list">
              <li v-for="ns in provisionResult.next_steps" :key="ns">
                <ArrowRight :size="12" />
                {{ ns }}
              </li>
            </ul>
          </div>

          <button class="auth-button" @click="resetForm" style="margin-top: 1rem;">
            <span class="btn-content">PROVISION ANOTHER</span>
          </button>
        </div>

        <!-- ERROR (only when no result at all) -->
        <div v-if="errorMsg && !provisionResult" class="error-alert">
          <AlertTriangle :size="20" color="var(--danger-color)" />
          <span>{{ errorMsg }}</span>
          <button class="auth-button" @click="resetForm" style="margin-top: 1rem; width: 100%;">
            <span class="btn-content">TRY AGAIN</span>
          </button>
        </div>

        <!-- FORM -->
        <form v-if="!showSuccess && !errorMsg" @submit.prevent="handleCreateOrg" class="provision-form">
          <div class="form-row">
            <div class="input-group">
              <label for="org-name">
                <Building2 :size="14" class="label-icon" />
                Organization Name
                <span class="field-tag">REQUIRED</span>
              </label>
              <div class="input-wrapper">
                <input 
                  id="org-name" 
                  v-model="organizationName" 
                  type="text" 
                  placeholder="e.g. Acme Support Pvt Ltd" 
                  required
                />
                <div class="input-glow"></div>
              </div>
            </div>

            <div class="input-group">
              <label for="admin-name">
                <User :size="14" class="label-icon" />
                Admin Name
                <span class="field-tag">REQUIRED</span>
              </label>
              <div class="input-wrapper">
                <input
                  id="admin-name"
                  v-model="adminName"
                  type="text"
                  placeholder="e.g. Rahul Sharma"
                  required
                />
                <div class="input-glow"></div>
              </div>
            </div>
          </div>

          <div class="input-group">
            <label for="admin-email">
              <Mail :size="14" class="label-icon" />
              Admin Email
              <span class="field-tag">REQUIRED</span>
            </label>
            <div class="input-wrapper">
              <input
                id="admin-email"
                v-model="adminEmail"
                type="email"
                placeholder="e.g. admin@acme.com"
                required
              />
              <div class="input-glow"></div>
            </div>
          </div>

          <div class="form-row">
            <div class="input-group">
              <label for="region">
                <MapPin :size="14" class="label-icon" />
                Azure Region
                <span class="field-tag">REQUIRED</span>
              </label>
              <div class="input-wrapper">
                <select id="region" v-model="selectedRegion" class="select-input">
                  <option v-for="r in REGIONS" :key="r.value" :value="r.value">{{ r.label }}</option>
                </select>
                <div class="input-glow"></div>
              </div>
            </div>

            <div class="input-group">
              <label for="language">
                <Languages :size="14" class="label-icon" />
                Language
                <span class="field-tag">REQUIRED</span>
              </label>
              <div class="input-wrapper">
                <input
                  id="language"
                  v-model="language"
                  type="text"
                  placeholder="e.g. en-IN"
                  required
                />
                <div class="input-glow"></div>
              </div>
            </div>
          </div>

          <div class="form-row">
            <div class="input-group">
              <label for="call-type">
                <Phone :size="14" class="label-icon" />
                Call Type
                <span class="field-tag">REQUIRED</span>
              </label>
              <div class="input-wrapper">
                <select id="call-type" v-model="callType" class="select-input">
                  <option value="inbound">Inbound</option>
                  <option value="outbound">Outbound</option>
                  <option value="blended">Blended</option>
                </select>
                <div class="input-glow"></div>
              </div>
            </div>

            <div class="input-group">
              <label for="plan-type">
                <BadgeCheck :size="14" class="label-icon" />
                Plan Type
                <span class="field-tag">REQUIRED</span>
              </label>
              <div class="input-wrapper">
                <select id="plan-type" v-model="planType" class="select-input">
                  <option value="pilot">Pilot</option>
                  <option value="growth">Growth</option>
                  <option value="enterprise">Enterprise</option>
                </select>
                <div class="input-glow"></div>
              </div>
            </div>
          </div>

          <!-- FIELD 1: name (string, required) -->
          <div class="input-group">
            <label>
              <Building2 :size="14" class="label-icon" />
              Provisioning Environment
              <span class="field-tag">REQUIRED</span>
            </label>
            <div class="tier-selector">
              <label class="tier-option" :class="{ 'selected': environment === 'staging' }">
                <input type="radio" v-model="environment" value="staging" name="env" class="sr-only" />
                <span class="tier-name">STAGING</span>
              </label>
              <label class="tier-option" :class="{ 'selected': environment === 'production' }">
                <input type="radio" v-model="environment" value="production" name="env" class="sr-only" />
                <span class="tier-name">PRODUCTION</span>
              </label>
              <label class="tier-option" :class="{ 'selected': environment === 'dedicated' }">
                <input type="radio" v-model="environment" value="dedicated" name="env" class="sr-only" />
                <span class="tier-name">DEDICATED</span>
              </label>
            </div>
          </div>

          <div class="form-row">
            <label class="tier-option" :class="{ 'selected': storesPii }">
              <input type="checkbox" v-model="storesPii" class="sr-only" />
              <Database :size="14" />
              <span class="tier-name">STORES PII</span>
            </label>
            <label class="tier-option" :class="{ 'selected': recordCalls }">
              <input type="checkbox" v-model="recordCalls" class="sr-only" />
              <Mic :size="14" />
              <span class="tier-name">RECORD CALLS</span>
            </label>
          </div>

          <div class="form-row">
            <label class="tier-option" :class="{ 'selected': createResourceGroup }">
              <input type="checkbox" v-model="createResourceGroup" class="sr-only" />
              <FolderTree :size="14" />
              <span class="tier-name">CREATE RESOURCE GROUP</span>
            </label>
            <label class="tier-option" :class="{ 'selected': plivoAutoProvision }">
              <input type="checkbox" v-model="plivoAutoProvision" class="sr-only" />
              <Phone :size="14" />
              <span class="tier-name">PLIVO AUTO PROVISION</span>
            </label>
          </div>

          <button type="submit" class="auth-button" :class="{ 'authenticating': isCreating }" :disabled="isCreating">
            <span class="btn-content">
              {{ isCreating ? 'PROVISIONING AZURE RESOURCES...' : 'INITIALIZE TENANT ENVIRONMENT' }}
            </span>
            <div class="scanner"></div>
          </button>
        </form>
      </div>

      <div class="side-panels">
        <div class="panel stats-panel">
          <h3>SYSTEM STATUS</h3>
          <div class="stat-grid">
            <div class="stat-box">
              <span class="stat-label">CLOUD</span>
              <span class="stat-value text-success">AZURE</span>
            </div>
            <div class="stat-box">
              <span class="stat-label">DEFAULT REGION</span>
              <span class="stat-value">Central India</span>
            </div>
            <div class="stat-box">
              <span class="stat-label">ENCRYPTION</span>
              <span class="stat-value text-success">AES-256</span>
            </div>
            <div class="stat-box">
              <span class="stat-label">VECTOR DB</span>
              <span class="stat-value text-success">QDRANT</span>
            </div>
          </div>
        </div>
        
        <div class="panel log-panel">
          <h3>PROVISIONING SERVICES</h3>
          <ul class="log-list">
            <li><span class="log-badge badge-active">ACTIVE</span> Azure Resource Groups</li>
            <li><span class="log-badge badge-active">ACTIVE</span> Azure Blob Storage</li>
            <li><span class="log-badge badge-active">ACTIVE</span> Azure Key Vault</li>
            <li><span class="log-badge badge-active">ACTIVE</span> Qdrant Vector DB</li>
            <li><span class="log-badge badge-active">ACTIVE</span> Redis Cache</li>
            <li><span class="log-badge badge-pending">PENDING</span> Plivo Telephony</li>
          </ul>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dashboard-container {
  background: rgba(17, 24, 39, 0.7);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid rgba(255, 255, 255, 0.05);
  box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
  width: 100%;
  max-width: 900px;
  padding: 2rem;
  display: flex;
  flex-direction: column;
  position: relative;
  overflow: hidden;
  animation: fadeIn 0.5s ease-out;
}

.dashboard-container::before, .dashboard-container::after {
  content: '';
  position: absolute;
  width: 20px;
  height: 20px;
  border-color: rgba(255, 255, 255, 0.1);
  border-style: solid;
  pointer-events: none;
}
.dashboard-container::before { top: 0; left: 0; border-width: 1px 0 0 1px; }
.dashboard-container::after { bottom: 0; right: 0; border-width: 0 1px 1px 0; }

.dashboard-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 2rem;
  padding-bottom: 1rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

.header-left {
  display: flex;
  align-items: center;
  gap: 1rem;
}

.header-title h2 {
  font-size: 1.2rem;
  color: var(--text-primary);
  letter-spacing: 3px;
  font-weight: 400;
  margin: 0 0 0.2rem 0;
}

.status-badge {
  font-size: 0.65rem;
  background: rgba(16, 185, 129, 0.1);
  color: var(--success-color);
  border: 1px solid rgba(16, 185, 129, 0.3);
  padding: 0.2rem 0.5rem;
  border-radius: 4px;
  letter-spacing: 1px;
}

.logout-btn {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  background: transparent;
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
  padding: 0.5rem 1rem;
  font-size: 0.75rem;
  letter-spacing: 1px;
  transition: all 0.3s ease;
  cursor: pointer;
}

.logout-btn:hover {
  background: rgba(239, 68, 68, 0.1);
  color: var(--danger-color);
  border-color: var(--danger-color);
}

.dashboard-content {
  display: grid;
  grid-template-columns: 1.5fr 1fr;
  gap: 2rem;
}

@media (max-width: 768px) {
  .dashboard-content {
    grid-template-columns: 1fr;
  }
}

.panel {
  background: rgba(0, 0, 0, 0.2);
  border: 1px solid rgba(255, 255, 255, 0.05);
  padding: 1.5rem;
  position: relative;
}

.panel-header h3 {
  font-size: 1rem;
  color: var(--text-primary);
  letter-spacing: 2px;
  margin-bottom: 0.2rem;
}

.panel-header p {
  font-size: 0.8rem;
  color: var(--text-secondary);
  margin-bottom: 0.5rem;
}

.api-hint {
  font-size: 0.65rem;
  font-family: monospace;
  color: var(--text-muted);
  background: rgba(0,0,0,0.3);
  display: inline-block;
  padding: 0.2rem 0.5rem;
  letter-spacing: 0.5px;
  margin-bottom: 1rem;
}

.field-tag {
  font-size: 0.55rem;
  background: rgba(59, 130, 246, 0.15);
  color: var(--accent-color);
  padding: 0.1rem 0.35rem;
  border-radius: 2px;
  letter-spacing: 0.5px;
  margin-left: 0.3rem;
  vertical-align: middle;
}

.azure-info {
  background: rgba(0,0,0,0.2);
  border: 1px solid rgba(255,255,255,0.05);
  padding: 0.8rem;
  margin-top: 0.8rem;
}

.info-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0.3rem 0;
  border-bottom: 1px solid rgba(255,255,255,0.03);
}

.info-row:last-child { border-bottom: none; }

.info-label {
  font-size: 0.65rem;
  color: var(--text-secondary);
  letter-spacing: 1px;
}

.info-row code {
  font-size: 0.72rem;
  background: rgba(0,0,0,0.3);
  padding: 0.1rem 0.4rem;
  color: var(--accent-color);
}

.step-msg {
  font-size: 0.65rem;
  color: var(--text-muted);
  margin-left: auto;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.provision-form {
  display: flex;
  flex-direction: column;
  gap: 1.2rem;
}

.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
}

.input-group { display: flex; flex-direction: column; gap: 0.5rem; }
.input-group label { font-size: 0.8rem; color: var(--text-secondary); display: flex; align-items: center; gap: 0.5rem; }
.label-icon { opacity: 0.7; }
.input-wrapper { position: relative; width: 100%; display: flex; }

input {
  width: 100%;
  background: var(--bg-input);
  border: 1px solid var(--border-color);
  padding: 0.8rem 1rem;
  color: var(--text-primary);
  font-size: 0.9rem;
  outline: none;
  transition: all 0.3s ease;
  position: relative;
  z-index: 2;
}

input::placeholder { color: var(--text-muted); }
input:focus { border-color: var(--border-focus); }

.domain-suffix {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid var(--border-color);
  border-left: none;
  color: var(--text-muted);
  padding: 0.8rem 1rem;
  font-size: 0.9rem;
  display: flex;
  align-items: center;
}

.input-glow {
  position: absolute;
  top: 0; left: 0; right: 0; bottom: 0;
  box-shadow: 0 0 15px var(--accent-glow);
  opacity: 0;
  transition: opacity 0.3s ease;
  z-index: 1;
  pointer-events: none;
}
input:focus + .input-glow, input:focus ~ .input-glow { opacity: 1; }

.tier-selector {
  display: flex;
  gap: 0.5rem;
}

.tier-option {
  flex: 1;
  background: rgba(0, 0, 0, 0.3);
  border: 1px solid var(--border-color);
  padding: 0.8rem 0;
  text-align: center;
  cursor: pointer;
  transition: all 0.3s ease;
}

.tier-option.selected {
  background: rgba(59, 130, 246, 0.1);
  border-color: var(--accent-color);
}

.tier-option.selected .tier-name {
  color: var(--accent-color);
}

.tier-name {
  font-size: 0.75rem;
  letter-spacing: 1px;
  color: var(--text-secondary);
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  border: 0;
}

.auth-button {
  background: rgba(15, 23, 42, 0.8);
  border: 1px solid var(--border-color);
  color: var(--text-primary);
  padding: 1rem;
  font-size: 0.85rem;
  letter-spacing: 2px;
  font-weight: 500;
  position: relative;
  overflow: hidden;
  transition: all 0.3s ease;
  width: 100%;
  margin-top: 0.5rem;
}
.btn-content { display: flex; align-items: center; justify-content: center; gap: 0.8rem; position: relative; z-index: 2; }
.auth-button:hover:not(:disabled) {
  background: rgba(30, 41, 59, 0.8);
  border-color: var(--accent-color);
  box-shadow: 0 0 20px rgba(59, 130, 246, 0.2);
}
.auth-button:disabled { opacity: 0.7; cursor: not-allowed; }

.scanner {
  position: absolute;
  top: 0; left: 0; width: 100%; height: 2px;
  background: var(--accent-color);
  box-shadow: 0 0 10px var(--accent-glow);
  opacity: 0;
  z-index: 1;
}
.auth-button:hover:not(:disabled) .scanner { opacity: 0.5; animation: scanline 2s linear infinite; }
.authenticating .btn-content { color: var(--accent-color); text-shadow: 0 0 8px var(--accent-glow); }

.success-alert {
  background: rgba(16, 185, 129, 0.1);
  border: 1px solid var(--success-color);
  padding: 1.5rem;
  display: flex;
  align-items: center;
  gap: 1rem;
  margin-top: 1rem;
  animation: fadeIn 0.3s ease-out;
}

.warning-alert {
  background: rgba(245, 158, 11, 0.1);
  border: 1px solid #f59e0b;
  padding: 1.5rem;
  display: flex;
  align-items: center;
  gap: 1rem;
  margin-top: 1rem;
  animation: fadeIn 0.3s ease-out;
}

.warning-alert .alert-text strong {
  color: #f59e0b;
}

.alert-text strong {
  display: block;
  color: var(--success-color);
  font-size: 0.9rem;
  letter-spacing: 1px;
  margin-bottom: 0.2rem;
}

.alert-text p {
  margin: 0;
  font-size: 0.8rem;
  color: var(--text-secondary);
}

.error-alert {
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid var(--danger-color);
  color: var(--danger-color);
  padding: 1rem;
  display: flex;
  align-items: center;
  gap: 0.8rem;
  margin-bottom: 1rem;
  font-size: 0.85rem;
  animation: fadeIn 0.3s ease-out;
}

/* Side Panels */
.side-panels {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}

.side-panels h3 {
  font-size: 0.85rem;
  color: var(--text-primary);
  letter-spacing: 2px;
  margin-bottom: 1rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
  padding-bottom: 0.5rem;
}

.stat-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 0.8rem;
}

.stat-box {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: rgba(255, 255, 255, 0.02);
  padding: 0.8rem;
  border: 1px solid rgba(255, 255, 255, 0.05);
}

.stat-label {
  font-size: 0.7rem;
  color: var(--text-secondary);
  letter-spacing: 1px;
}

.stat-value {
  font-size: 0.9rem;
  color: var(--text-primary);
  font-weight: 500;
}

.text-success { color: var(--success-color); }

.log-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.log-list li {
  font-size: 0.75rem;
  color: var(--text-secondary);
  display: flex;
  gap: 0.5rem;
}

.log-badge {
  font-size: 0.6rem;
  padding: 0.15rem 0.4rem;
  letter-spacing: 0.5px;
  border-radius: 2px;
  font-weight: 600;
  min-width: 52px;
  text-align: center;
}

.badge-active {
  background: rgba(16, 185, 129, 0.15);
  color: var(--success-color);
  border: 1px solid rgba(16, 185, 129, 0.3);
}

.badge-pending {
  background: rgba(245, 158, 11, 0.15);
  color: #f59e0b;
  border: 1px solid rgba(245, 158, 11, 0.3);
}

/* Select Input */
.select-input {
  width: 100%;
  background: var(--bg-input);
  border: 1px solid var(--border-color);
  padding: 0.8rem 1rem;
  color: var(--text-primary);
  font-size: 0.9rem;
  outline: none;
  transition: all 0.3s ease;
  position: relative;
  z-index: 2;
  appearance: none;
  cursor: pointer;
}
.select-input:focus { border-color: var(--border-focus); }
.select-input option { background: #111827; color: var(--text-primary); }

/* Provisioning Result */
.provision-result {
  animation: fadeIn 0.4s ease-out;
}

.provision-result code {
  background: rgba(0,0,0,0.3);
  padding: 0.15rem 0.4rem;
  font-size: 0.75rem;
  letter-spacing: 0.5px;
  color: var(--accent-color);
}

.steps-section {
  margin-top: 1.2rem;
}

.steps-section h4, .next-steps-section h4 {
  font-size: 0.75rem;
  color: var(--text-secondary);
  letter-spacing: 1.5px;
  margin-bottom: 0.6rem;
}

.step-item {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  padding: 0.4rem 0;
  border-bottom: 1px solid rgba(255,255,255,0.03);
  font-size: 0.8rem;
}

.step-status {
  font-size: 0.6rem;
  padding: 0.15rem 0.5rem;
  letter-spacing: 0.5px;
  border-radius: 2px;
  font-weight: 600;
  min-width: 55px;
  text-align: center;
}

.step-status.success {
  background: rgba(16, 185, 129, 0.15);
  color: var(--success-color);
  border: 1px solid rgba(16, 185, 129, 0.3);
}

.step-status.failed {
  background: rgba(239, 68, 68, 0.15);
  color: var(--danger-color);
  border: 1px solid rgba(239, 68, 68, 0.3);
}

.step-status.skipped {
  background: rgba(245, 158, 11, 0.15);
  color: #f59e0b;
  border: 1px solid rgba(245, 158, 11, 0.3);
}

.step-status.running {
  background: rgba(59, 130, 246, 0.15);
  color: #3b82f6;
  border: 1px solid rgba(59, 130, 246, 0.3);
  animation: pulse 1.5s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

.step-name {
  color: var(--text-secondary);
}

.next-steps-section {
  margin-top: 1.2rem;
}

.next-steps-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.next-steps-list li {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.78rem;
  color: var(--text-secondary);
}

@keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
@keyframes scanline { 0% { top: 0; } 100% { top: 100%; } }
</style>
