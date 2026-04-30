<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import { Building2, Shield, LogOut, CheckCircle2, AlertTriangle, MapPin, Mail, User, Phone, Languages, BadgeCheck, Database, FolderTree, Mic } from 'lucide-vue-next';

const props = defineProps({
  theme: {
    type: String,
    required: true,
  },
  toggleTheme: {
    type: Function,
    required: true,
  },
  homeSignal: {
    type: Number,
    required: true,
  },
});

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
const twilioAutoProvision = ref(false);

const isCreating = ref(false);
const errorMsg = ref('');
const provisionResult = ref(null);
const activeOrganizationId = ref(null);
const loadingStartedAt = ref(0);
const MIN_LOADING_MS = 1800;
const organizations = ref([]);
const orgSummary = ref({ count: 0, total_minutes: 0, total_cost_usd: 0 });
const showCreateOrg = ref(false);
const SUPERADMIN_ACCESS_TOKEN_KEY = 'superadmin_access_token';

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

const screenMode = computed(() => {
  if (!showCreateOrg.value && !isCreating.value && !provisionResult.value && !errorMsg.value) return 'list';
  if (isCreating.value) return 'loading';
  if (provisionResult.value || errorMsg.value) return 'result';
  return 'form';
});

const latestStep = computed(() => {
  if (!liveSteps.value.length) return null;
  const running = [...liveSteps.value].reverse().find((step) => step.status === 'running');
  return running || liveSteps.value[liveSteps.value.length - 1];
});

const currentProvisioningState = computed(() => {
  if (errorMsg.value && !provisionResult.value) return 'Provisioning failed';
  if (provisionResult.value?.status === 'success') return 'Provisioned';
  if (provisionResult.value?.status === 'partial') return 'Partially provisioned';
  if (provisionResult.value?.status === 'failed') return 'Provisioning failed';
  if (latestStep.value?.message) return latestStep.value.message;
  if (latestStep.value?.name) return `Running ${formatStepName(latestStep.value.name)}`;
  if (isCreating.value) return 'Initializing tenant provisioning';
  return '';
});

const finalProvisionState = computed(() => {
  if (!provisionResult.value) return '';
  if (provisionResult.value.status === 'success') return 'Provisioned';
  if (provisionResult.value.status === 'partial') return 'Partially Provisioned';
  return 'Provisioning Failed';
});

function formatStepName(name) {
  return name.replaceAll('_', ' ').toUpperCase();
}

function formatCurrency(value) {
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    minimumFractionDigits: 2,
  }).format(value || 0);
}

const loadOrganizations = async () => {
  try {
    const token = localStorage.getItem(SUPERADMIN_ACCESS_TOKEN_KEY);
    const response = await fetch('http://localhost:8000/superadmin/tenants', {
      headers: {
        'Authorization': `Bearer ${token}`,
      },
    });
    if (!response.ok) {
      return;
    }
    const data = await response.json();
    organizations.value = data.organizations || [];
    orgSummary.value = data.summary || { count: 0, total_minutes: 0, total_cost_usd: 0 };
  } catch (_) {
    // dashboard list is best-effort
  }
};

const handleCreateOrg = async () => {
  if (!organizationName.value || !adminEmail.value || !adminName.value) return;
  
  isCreating.value = true;
  loadingStartedAt.value = Date.now();
  errorMsg.value = '';
  provisionResult.value = null;
  liveSteps.value = [];
  
  try {
    const token = localStorage.getItem(SUPERADMIN_ACCESS_TOKEN_KEY);
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
      twilio_auto_provision: twilioAutoProvision.value
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
    const elapsed = Date.now() - loadingStartedAt.value;
    if (elapsed < MIN_LOADING_MS) {
      await new Promise((resolve) => setTimeout(resolve, MIN_LOADING_MS - elapsed));
    }
    isCreating.value = false;
    await loadOrganizations();
  }
};

const resetForm = () => {
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
  twilioAutoProvision.value = false;
  provisionResult.value = null;
  errorMsg.value = '';
  activeOrganizationId.value = null;
  liveSteps.value = [];
  showCreateOrg.value = false;
};

const handleLogout = () => {
  emit('logout');
};

const openCreateOrg = (event) => {
  provisionResult.value = null;
  errorMsg.value = '';
  liveSteps.value = [];
  showCreateOrg.value = true;
};

onMounted(async () => {
  await loadOrganizations();
});

watch(() => props.homeSignal, () => {
  if (isCreating.value) return;
  provisionResult.value = null;
  errorMsg.value = '';
  liveSteps.value = [];
  showCreateOrg.value = false;
});
</script>

<template>
  <div class="dashboard-container" :class="`theme-${props.theme}`">
    <div class="dashboard-header">
      <div class="header-left">
        <Shield :size="32" color="var(--success-color)" />
        <div class="header-title">
          <h2>SUPERADMIN CONSOLE</h2>
          <span class="status-badge">SECURE SESSION ACTIVE</span>
        </div>
      </div>
      <div class="header-actions">
        <button class="theme-toggle" @click="props.toggleTheme">
          {{ props.theme === 'dark' ? 'LIGHT MODE' : 'DARK MODE' }}
        </button>
        <button class="logout-btn" @click="handleLogout">
          <LogOut :size="16" />
          TERMINATE SESSION
        </button>
      </div>
    </div>

    <Transition name="console-swap" mode="out-in">
      <div v-if="screenMode === 'list'" key="dashboard" class="dashboard-content">
        <section class="orgs-panel top-panel">
        <div class="orgs-panel-header">
          <div>
            <span class="stage-eyebrow">ORGANIZATION DASHBOARD</span>
            <h3>Usage And Cost Tracker</h3>
          </div>
          <div class="orgs-actions">
            <div class="summary-strip">
              <div class="summary-chip">
                <span class="summary-label">ORGS</span>
                <strong>{{ orgSummary.count }}</strong>
              </div>
              <div class="summary-chip">
                <span class="summary-label">MINUTES</span>
                <strong>{{ orgSummary.total_minutes }}</strong>
              </div>
              <div class="summary-chip">
                <span class="summary-label">SPEND</span>
                <strong>{{ formatCurrency(orgSummary.total_cost_usd) }}</strong>
              </div>
            </div>
            <button type="button" class="create-org-btn" @click="openCreateOrg">
              CREATE NEW ORG
            </button>
          </div>
        </div>

        <div v-if="organizations.length" class="org-grid">
          <article v-for="org in organizations" :key="org.organization_id" class="org-card">
            <div class="org-card-top">
              <div>
                <h4>{{ org.organization_name }}</h4>
                <p>{{ org.organization_profile.admin_name || 'No admin assigned' }}</p>
              </div>
              <span class="org-status" :class="org.provisioning_status">
                {{ (org.provisioning_status || 'pending').toUpperCase() }}
              </span>
            </div>

            <div class="org-card-meta">
              <span>{{ org.environment?.toUpperCase() }}</span>
              <span>{{ org.region }}</span>
              <span>{{ org.organization_profile.language }}</span>
            </div>

            <div class="tracker-band">
              <div class="tracker-metric">
                <span class="tracker-label">Revenue</span>
                <strong>{{ formatCurrency(org.money_tracker?.revenue_usd) }}</strong>
              </div>
              <div class="tracker-divider"></div>
              <div class="tracker-metric">
                <span class="tracker-label">Cost</span>
                <strong>{{ formatCurrency(org.money_tracker?.cost_usd) }}</strong>
              </div>
              <div class="tracker-divider"></div>
              <div class="tracker-metric">
                <span class="tracker-label">Margin</span>
                <strong>{{ formatCurrency(org.money_tracker?.margin_usd) }}</strong>
              </div>
              <div class="tracker-divider"></div>
              <div class="tracker-metric">
                <span class="tracker-label">Billing</span>
                <strong>{{ org.billing_status?.toUpperCase() }}</strong>
              </div>
            </div>

            <div class="usage-grid">
              <div class="usage-metric">
                <span class="usage-label">Total Minutes</span>
                <strong>{{ org.usage_tracker?.minutes || 0 }}</strong>
              </div>
              <div class="usage-metric">
                <span class="usage-label">LLM Tokens</span>
                <strong>{{ org.usage_tracker?.llm_total_tokens || 0 }}</strong>
              </div>
              <div class="usage-metric">
                <span class="usage-label">API Calls</span>
                <strong>{{ org.usage_tracker?.llm_api_calls || 0 }}</strong>
              </div>
              <div class="usage-metric">
                <span class="usage-label">Voice Usage</span>
                <strong>{{ org.usage_tracker?.voice_minutes || 0 }}</strong>
              </div>
              <div class="usage-meter">
                <div class="usage-meter-head">
                  <span>Storage</span>
                  <span>{{ org.usage_tracker?.storage_gb || 0 }} GB</span>
                </div>
                <div class="meter-track"><div class="meter-fill blue" :style="{ width: `${Math.min((org.usage_tracker?.storage_gb || 0) * 10, 100)}%` }"></div></div>
              </div>
              <div class="usage-meter">
                <div class="usage-meter-head">
                  <span>Infrastructure</span>
                  <span>{{ org.usage_tracker?.infrastructure_units || 0 }} units</span>
                </div>
                <div class="meter-track"><div class="meter-fill green" :style="{ width: `${Math.min((org.usage_tracker?.infrastructure_units || 0) * 12, 100)}%` }"></div></div>
              </div>
            </div>
          </article>
        </div>

        <div v-else class="empty-orgs">
          <span class="stage-eyebrow">NO ORGANIZATIONS YET</span>
          <p>Provision an account to start tracking usage minutes and spend.</p>
          <button type="button" class="create-org-btn empty-cta" @click="openCreateOrg">
            CREATE NEW ORG
          </button>
        </div>
        </section>
      </div>

      <div v-else key="create" class="dashboard-content create-page">
        <section class="panel provision-panel">
          <div class="create-page-heading">
            <span class="stage-eyebrow">ORGANIZATION CREATION</span>
            <h3>{{ screenMode === 'form' ? 'Provision New Organization' : currentProvisioningState }}</h3>
            <p>Create and initialize a new tenant environment. Click `NOKVO` in the header to return to the dashboard.</p>
          </div>
          <div class="create-page-divider"></div>
          <div class="panel-header" v-if="screenMode === 'form'">
            <h3>PROVISION NEW ORGANIZATION</h3>
            <p>Deploy a new secure Azure tenant environment.</p>
            <div class="api-hint">POST /superadmin/tenants/provision</div>
          </div>
          <form v-if="screenMode === 'form'" @submit.prevent="handleCreateOrg" class="provision-form">
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
            <label class="tier-option" :class="{ 'selected': twilioAutoProvision }">
              <input type="checkbox" v-model="twilioAutoProvision" class="sr-only" />
              <Phone :size="14" />
              <span class="tier-name">TWILIO AUTO PROVISION</span>
            </label>
          </div>

          <button type="submit" class="auth-button" :class="{ 'authenticating': isCreating }" :disabled="isCreating">
            <span class="btn-content">
              {{ isCreating ? 'PROVISIONING AZURE RESOURCES...' : 'INITIALIZE TENANT ENVIRONMENT' }}
            </span>
            <div class="scanner"></div>
          </button>
          </form>

          <section v-else-if="screenMode === 'loading'" class="stage-screen">
            <div class="stage-visual">
              <div class="call-rings">
                <span></span>
                <span></span>
                <span></span>
              </div>
              <div class="call-core">
                <Phone :size="26" />
              </div>
            </div>
            <div class="stage-copy">
              <span class="stage-eyebrow">ACCOUNT INITIALIZATION</span>
              <h3>{{ currentProvisioningState }}</h3>
              <p v-if="latestStep">{{ formatStepName(latestStep.name) }} · {{ latestStep.status.toUpperCase() }}</p>
              <p v-else>Preparing tenant resources and provider services.</p>
            </div>
            <div v-if="liveSteps.length" class="status-step-strip centered">
              <div v-for="step in liveSteps.slice(-5)" :key="step.name" class="status-step-chip">
                <span class="step-status" :class="step.status">{{ step.status.toUpperCase() }}</span>
                <span class="status-chip-name">{{ formatStepName(step.name) }}</span>
              </div>
            </div>
          </section>

          <section v-else class="stage-screen result-screen">
            <div class="stage-visual result-visual">
              <CheckCircle2 v-if="provisionResult?.status === 'success'" :size="34" color="var(--success-color)" />
              <AlertTriangle v-else :size="34" :color="provisionResult?.status === 'partial' ? '#f59e0b' : 'var(--danger-color)'" />
            </div>
            <div class="stage-copy">
              <span class="stage-eyebrow">FINAL STATE</span>
              <h3>{{ finalProvisionState || 'Provisioning Failed' }}</h3>
              <p v-if="provisionResult?.tenant_id">Tenant ID: <code>{{ provisionResult.tenant_id }}</code></p>
              <p v-else>{{ errorMsg }}</p>
            </div>
            <button type="button" class="auth-button stage-action" @click="resetForm">
              <span class="btn-content">PROVISION ANOTHER</span>
              <div class="scanner"></div>
            </button>
          </section>
        </section>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.dashboard-container {
  --success-color: #34d399;
  --danger-color: #f87171;
  --accent-color: #60a5fa;
  --accent-glow: rgba(96, 165, 250, 0.35);
  --text-primary: #f8fafc;
  --text-secondary: #cbd5e1;
  --text-muted: #94a3b8;
  --border-color: rgba(148, 163, 184, 0.16);
  --border-focus: rgba(96, 165, 250, 0.55);
  --bg-input: rgba(15, 23, 42, 0.82);
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

.theme-light {
  --success-color: #059669;
  --danger-color: #dc2626;
  --accent-color: #2563eb;
  --accent-glow: rgba(37, 99, 235, 0.22);
  --text-primary: #0f172a;
  --text-secondary: #334155;
  --text-muted: #64748b;
  --border-color: rgba(15, 23, 42, 0.12);
  --border-focus: rgba(37, 99, 235, 0.38);
  --bg-input: rgba(255, 255, 255, 0.92);
  background: rgba(241, 245, 249, 0.86);
  border-color: rgba(148, 163, 184, 0.18);
  box-shadow: 0 20px 45px -18px rgba(15, 23, 42, 0.18);
}

.theme-light::before,
.theme-light::after {
  border-color: rgba(15, 23, 42, 0.12);
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

.header-actions {
  display: flex;
  gap: 0.75rem;
  align-items: center;
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

.theme-toggle {
  background: transparent;
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
  padding: 0.5rem 0.9rem;
  font-size: 0.7rem;
  letter-spacing: 1.4px;
  cursor: pointer;
  transition: all 0.25s ease;
}

.theme-toggle:hover {
  color: var(--accent-color);
  border-color: var(--accent-color);
  box-shadow: 0 0 16px var(--accent-glow);
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
  display: block;
}

.console-swap-enter-active,
.console-swap-leave-active {
  transition:
    opacity 0.22s ease,
    transform 0.22s ease,
    filter 0.22s ease;
}

.console-swap-enter-from {
  opacity: 0;
  transform: translateX(18px);
  filter: saturate(0.92);
}

.console-swap-leave-to {
  opacity: 0;
  transform: translateX(-18px);
  filter: saturate(0.92);
}

.orgs-panel {
  margin-top: 1.5rem;
  padding-top: 1.5rem;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
}

.top-panel {
  margin-top: 0;
  padding-top: 0;
  border-top: none;
}

.orgs-panel-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 1rem;
  margin-bottom: 1rem;
}

.orgs-panel-header h3 {
  margin: 0.2rem 0 0;
  font-size: 1rem;
  color: var(--text-primary);
  letter-spacing: 1px;
}

.orgs-actions {
  display: flex;
  align-items: flex-end;
  gap: 0.75rem;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.create-org-btn {
  background: rgba(15, 23, 42, 0.82);
  border: 1px solid rgba(59, 130, 246, 0.28);
  color: var(--text-primary);
  padding: 0.8rem 1rem;
  font-size: 0.72rem;
  letter-spacing: 1.6px;
  cursor: pointer;
  transition: all 0.25s ease;
}

.create-org-btn:hover {
  border-color: var(--accent-color);
  color: var(--accent-color);
  box-shadow: 0 0 18px rgba(59, 130, 246, 0.16);
}

.summary-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem;
}

.summary-chip {
  min-width: 110px;
  padding: 0.7rem 0.8rem;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.06);
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  border-radius: 0;
}

.summary-label {
  font-size: 0.62rem;
  letter-spacing: 1.6px;
  color: var(--text-muted);
}

.summary-chip strong {
  color: var(--text-primary);
  font-size: 0.92rem;
  font-weight: 600;
}

.org-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 1rem;
}

.org-card {
  background: linear-gradient(180deg, rgba(2, 6, 23, 0.75), rgba(15, 23, 42, 0.58));
  border: 1px solid rgba(148, 163, 184, 0.12);
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
  border-radius: 0;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
}

.org-card-top {
  display: flex;
  justify-content: space-between;
  gap: 0.8rem;
  align-items: flex-start;
}

.org-card-top h4 {
  margin: 0;
  color: var(--text-primary);
  font-size: 0.95rem;
  letter-spacing: 0.4px;
}

.org-card-top p {
  margin: 0.25rem 0 0;
  color: var(--text-secondary);
  font-size: 0.76rem;
}

.org-status {
  font-size: 0.6rem;
  padding: 0.18rem 0.45rem;
  border-radius: 2px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  color: var(--text-secondary);
  letter-spacing: 0.7px;
}

.org-status.success {
  color: var(--success-color);
  border-color: rgba(16, 185, 129, 0.3);
  background: rgba(16, 185, 129, 0.12);
}

.org-status.partial {
  color: #f59e0b;
  border-color: rgba(245, 158, 11, 0.3);
  background: rgba(245, 158, 11, 0.12);
}

.org-status.failed {
  color: var(--danger-color);
  border-color: rgba(239, 68, 68, 0.3);
  background: rgba(239, 68, 68, 0.12);
}

.org-status.pending {
  color: #93c5fd;
  border-color: rgba(59, 130, 246, 0.3);
  background: rgba(59, 130, 246, 0.12);
}

.org-card-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.org-card-meta span {
  font-size: 0.64rem;
  letter-spacing: 0.9px;
  color: var(--text-secondary);
  padding: 0.18rem 0.4rem;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.05);
}

.tracker-band {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  flex-wrap: wrap;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.06);
  padding: 0.85rem;
  border-radius: 0;
}

.tracker-metric {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}

.tracker-label {
  font-size: 0.62rem;
  color: var(--text-muted);
  letter-spacing: 1.5px;
}

.tracker-metric strong {
  font-size: 1rem;
  color: var(--text-primary);
}

.tracker-divider {
  width: 1px;
  align-self: stretch;
  background: rgba(255, 255, 255, 0.08);
}

.usage-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.7rem;
}

.usage-metric,
.usage-meter {
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.06);
  padding: 0.7rem;
  border-radius: 0;
}

.usage-label {
  display: block;
  font-size: 0.62rem;
  color: var(--text-muted);
  letter-spacing: 1.2px;
  margin-bottom: 0.18rem;
}

.usage-metric strong {
  color: var(--text-primary);
  font-size: 0.9rem;
}

.usage-meter {
  grid-column: span 2;
}

.usage-meter-head {
  display: flex;
  justify-content: space-between;
  font-size: 0.68rem;
  color: var(--text-secondary);
  margin-bottom: 0.45rem;
}

.meter-track {
  height: 8px;
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.05);
  overflow: hidden;
}

.meter-fill {
  height: 100%;
}

.meter-fill.blue {
  background: linear-gradient(90deg, rgba(59, 130, 246, 0.55), rgba(96, 165, 250, 0.95));
}

.meter-fill.green {
  background: linear-gradient(90deg, rgba(16, 185, 129, 0.55), rgba(52, 211, 153, 0.95));
}

.empty-orgs {
  padding: 1.2rem 0;
  text-align: center;
}

.empty-orgs p {
  margin: 0.4rem 0 0;
  color: var(--text-secondary);
  font-size: 0.8rem;
}

.empty-cta {
  margin-top: 1rem;
}

.panel {
  background: rgba(0, 0, 0, 0.2);
  border: 1px solid rgba(255, 255, 255, 0.05);
  padding: 1.5rem;
  position: relative;
}

.create-page {
  max-width: 980px;
  margin: 0 auto;
}

.create-page-heading {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  padding: 1.4rem 1.4rem 0;
}

.create-page-heading h3 {
  margin: 0;
  color: var(--text-primary);
  font-size: 1.25rem;
  letter-spacing: 0.8px;
}

.create-page-heading p {
  margin: 0;
  color: var(--text-secondary);
  font-size: 0.82rem;
}

.create-page-divider {
  height: 1px;
  background: var(--border-color);
  margin: 1rem 1.4rem 0;
}

.provision-panel {
  max-width: 980px;
  margin: 0 auto;
  border-radius: 0;
  box-shadow: 0 24px 60px rgba(2, 6, 23, 0.18);
  background:
    linear-gradient(180deg, rgba(15, 23, 42, 0.96), rgba(15, 23, 42, 0.9)),
    linear-gradient(135deg, rgba(59, 130, 246, 0.08), transparent 38%);
  border-color: rgba(148, 163, 184, 0.16);
  padding: 0;
}

.theme-light .panel,
.theme-light .org-card,
.theme-light .summary-chip,
.theme-light .tracker-band,
.theme-light .usage-metric,
.theme-light .usage-meter,
.theme-light .status-step-chip {
  background: rgba(255, 255, 255, 0.68);
  border-color: rgba(148, 163, 184, 0.18);
}

.theme-light .provision-panel {
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.98), rgba(248, 250, 252, 0.96)),
    linear-gradient(135deg, rgba(37, 99, 235, 0.05), transparent 42%);
  border-color: rgba(148, 163, 184, 0.22);
  box-shadow: 0 24px 60px rgba(15, 23, 42, 0.12);
}

.theme-light .call-core,
.theme-light .result-visual {
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.95), rgba(226, 232, 240, 0.92));
  color: var(--accent-color);
}


.theme-light .call-rings span {
  border-color: rgba(37, 99, 235, 0.22);
  background: radial-gradient(circle, rgba(37, 99, 235, 0.08), transparent 70%);
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

.panel-header,
.provision-form,
.stage-screen {
  padding-left: 1.4rem;
  padding-right: 1.4rem;
}

.panel-header {
  padding-top: 1.15rem;
}

.provision-form,
.stage-screen {
  padding-bottom: 1.4rem;
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

.stage-screen {
  min-height: 560px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 1.5rem;
  padding: 2rem 1rem;
  text-align: center;
  animation: fadeIn 0.35s ease-out;
}

.stage-visual {
  position: relative;
  width: 180px;
  height: 180px;
  display: grid;
  place-items: center;
}

.call-rings {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
}

.call-rings span {
  position: absolute;
  border-radius: 999px;
  border: 1px solid rgba(59, 130, 246, 0.28);
  background: radial-gradient(circle, rgba(59, 130, 246, 0.1), transparent 70%);
  animation: callPulse 2.4s ease-out infinite;
}

.call-rings span:nth-child(1) {
  width: 72px;
  height: 72px;
}

.call-rings span:nth-child(2) {
  width: 112px;
  height: 112px;
  animation-delay: 0.4s;
}

.call-rings span:nth-child(3) {
  width: 152px;
  height: 152px;
  animation-delay: 0.8s;
}

.call-core,
.result-visual {
  width: 72px;
  height: 72px;
  border-radius: 999px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(180deg, rgba(30, 41, 59, 0.92), rgba(15, 23, 42, 0.92));
  border: 1px solid rgba(59, 130, 246, 0.28);
  color: #93c5fd;
  box-shadow: 0 0 30px rgba(59, 130, 246, 0.15);
}

.stage-copy {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  max-width: 480px;
}

.stage-eyebrow {
  font-size: 0.68rem;
  letter-spacing: 2px;
  color: var(--text-muted);
}

.stage-copy h3 {
  margin: 0;
  font-size: 1.35rem;
  color: var(--text-primary);
  letter-spacing: 1px;
  font-weight: 500;
}

.stage-copy p {
  margin: 0;
  font-size: 0.82rem;
  color: var(--text-secondary);
}

.result-screen {
  min-height: 440px;
}

.stage-action {
  max-width: 320px;
}

.status-step-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  justify-content: center;
}

.status-step-chip {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.06);
  padding: 0.38rem 0.45rem;
}

.status-chip-name {
  font-size: 0.68rem;
  color: var(--text-secondary);
  letter-spacing: 0.8px;
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

@keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
@keyframes callPulse {
  0% {
    opacity: 0.9;
    transform: scale(0.92);
  }
  100% {
    opacity: 0;
    transform: scale(1.16);
  }
}
@keyframes scanline { 0% { top: 0; } 100% { top: 100%; } }
</style>
