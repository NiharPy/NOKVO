<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import axios from 'axios';
import QrcodeVue from 'qrcode.vue';
import {
  ArrowUpDown,
  Bell,
  Bot,
  CheckCircle2,
  Database,
  Filter,
  LogOut,
  MessageSquare,
  Moon,
  Search,
  Settings2,
  Shield,
  SunMedium,
  SunMedium as Sun,
  UserPlus,
  Users,
  Wrench,
  XCircle,
} from 'lucide-vue-next';

const API_BASE_URL = 'http://localhost:8000/api/nokvo-one';
const ACCESS_TOKEN_KEY = 'nokvo_one_access_token';
const REFRESH_TOKEN_KEY = 'nokvo_one_refresh_token';
const THEME_KEY = 'nokvo_one_theme_mode';

defineEmits(['switch-mode']);

const api = axios.create({ baseURL: API_BASE_URL });

const orgShellRef = ref(null);
const themeMode = ref(localStorage.getItem(THEME_KEY) || 'light');
const cursorTimer = ref(null);
const authConfig = ref(null);
const googleLoginButtonRef = ref(null);
const googleSignupButtonRef = ref(null);

const authState = ref('login'); // login | signup | check_email | mfa_setup | mfa_verify | login_totp | accept_invite | ready
const errorMsg = ref('');
const infoMsg = ref('');
const isAuthenticating = ref(false);
const dashboardQuery = ref('');
const currentPage = ref('dashboard'); // dashboard | members | agent
const memberSortMode = ref('name');
const memberFilter = ref('all');

const signup = ref({ org_name: '', admin_name: '', admin_email: '', password: '' });
const login = ref({ email: '', password: '' });
const totpCode = ref('');
const totpUri = ref('');
const totpSecret = ref('');
const setupToken = ref('');
const loginTempToken = ref('');

const currentUser = ref(null);
const currentOrganization = ref(null);
const members = ref([]);
const agents = ref([]);
const predefinedTools = ref([]);
const activeAgent = ref(null);
const chatLog = ref([]);
const chatInput = ref('');
const emailDrafts = ref([]);
const provisioning = ref(null);
const inviteForm = ref({ email: '', full_name: '', role: 'member' });
const newAgent = ref({ name: '', description: '', system_prompt: '', tool_keys: [] });
const isSavingMember = ref(false);
const isLoadingMembers = ref(false);

const inviteToken = ref('');
const inviteContext = ref(null);
const invitePassword = ref('');

const themeToggleLabel = computed(() => (themeMode.value === 'dark' ? 'Light Mode' : 'Dark Mode'));

const organizationInitial = computed(
  () => (currentOrganization.value?.name || 'N').trim().charAt(0).toUpperCase(),
);

const inviteDomain = computed(() => currentOrganization.value?.email_domain || '');
const isInviteDomainValid = computed(() => {
  if (!inviteForm.value.email) return true;
  const at = inviteForm.value.email.indexOf('@');
  if (at < 0) return false;
  return inviteForm.value.email.slice(at + 1).toLowerCase() === inviteDomain.value.toLowerCase();
});
const inviteCanSubmit = computed(
  () => inviteForm.value.email && isInviteDomainValid.value,
);
const inviteValidationMessage = computed(() => {
  if (!inviteForm.value.email) return `Invitees must use @${inviteDomain.value}.`;
  if (!isInviteDomainValid.value) return `Email must belong to @${inviteDomain.value}.`;
  return 'A one-time link will be emailed. The invitee sets their own password and TOTP.';
});

const memberFilterLabel = computed(() => ({ all: 'All members', invited: 'Invited', active: 'Active' }[memberFilter.value]));
const memberSortLabel = computed(
  () => ({ name: 'Sort: Name', email: 'Sort: Email', role: 'Sort: Role' }[memberSortMode.value]),
);

const filteredMembers = computed(() => {
  let list = [...members.value];
  if (memberFilter.value === 'invited') list = list.filter((m) => m.status === 'invited' || m.status === 'pending_totp');
  if (memberFilter.value === 'active') list = list.filter((m) => m.status === 'active');
  if (dashboardQuery.value.trim()) {
    const q = dashboardQuery.value.toLowerCase();
    list = list.filter((m) => (m.email || '').toLowerCase().includes(q) || (m.full_name || '').toLowerCase().includes(q));
  }
  const key = memberSortMode.value === 'name' ? 'full_name' : memberSortMode.value;
  list.sort((a, b) => String(a[key] || '').localeCompare(String(b[key] || '')));
  return list;
});

const dashboardSearchPlaceholder = computed(() => {
  if (currentPage.value === 'members') return 'Search members by name or email';
  if (currentPage.value === 'agent') return 'Search agents';
  return 'Search workspace';
});

const updateCursorGlow = (event) => {
  if (!orgShellRef.value) return;
  const rect = orgShellRef.value.getBoundingClientRect();
  const x = ((event.clientX - rect.left) / rect.width) * 100;
  const y = ((event.clientY - rect.top) / rect.height) * 100;
  orgShellRef.value.style.setProperty('--cursor-x', `${x}%`);
  orgShellRef.value.style.setProperty('--cursor-y', `${y}%`);
};

const toggleThemeMode = () => {
  themeMode.value = themeMode.value === 'dark' ? 'light' : 'dark';
  localStorage.setItem(THEME_KEY, themeMode.value);
};

const toggleMemberFilter = () => {
  memberFilter.value = { all: 'invited', invited: 'active', active: 'all' }[memberFilter.value];
};

const cycleMemberSort = () => {
  memberSortMode.value = { name: 'email', email: 'role', role: 'name' }[memberSortMode.value];
};

const switchPage = (page) => {
  currentPage.value = page;
  errorMsg.value = '';
  infoMsg.value = '';
};

const resetLoginState = () => {
  errorMsg.value = '';
  infoMsg.value = '';
  setupToken.value = '';
  totpUri.value = '';
  totpSecret.value = '';
  totpCode.value = '';
  loginTempToken.value = '';
  authState.value = 'login';
};

const persistSession = (data) => {
  localStorage.setItem(ACCESS_TOKEN_KEY, data.access_token);
  localStorage.setItem(REFRESH_TOKEN_KEY, data.refresh_token);
  currentUser.value = data.user;
  currentOrganization.value = data.organization;
};

const authHeader = () => ({ Authorization: `Bearer ${localStorage.getItem(ACCESS_TOKEN_KEY)}` });

const PROVISIONING_LABELS = {
  resource_group: 'Azure resource group',
  azure_openai_realtime_mini: 'Azure OpenAI realtime-mini',
  shared_key_vault: 'Shared Azure Key Vault',
  blob_prefix: 'Shared blob prefix',
  qdrant_collection: 'Qdrant collection (shared cluster)',
  redis_namespace: 'Redis namespace (shared)',
  exotel_placeholder: 'Exotel slot',
};

const stepLabel = (name) => PROVISIONING_LABELS[name] || name;

const stepDescription = (name, status, summary) => {
  if (!summary) return '';
  switch (name) {
    case 'resource_group':
      return summary.azure_resource_group_name
        ? `${summary.azure_resource_group_name} · ${summary.azure_region || ''}`
        : 'Pending';
    case 'azure_openai_realtime_mini':
      if (status === 'skipped_no_azure_subscription') return 'Skipped (Azure subscription not configured)';
      return [summary.llm_model, summary.llm_deployment, summary.llm_region].filter(Boolean).join(' · ');
    case 'shared_key_vault':
      if (status === 'skipped_no_shared_vault') return 'Skipped (no shared Key Vault configured)';
      return [summary.key_vault_name, summary.llm_api_key_secret_ref].filter(Boolean).join(' · ');
    case 'blob_prefix':
      return summary.blob_prefix || 'Pending';
    case 'qdrant_collection':
      return summary.qdrant_collection_name || 'Pending';
    case 'redis_namespace':
      return summary.redis_namespace || 'Pending';
    case 'exotel_placeholder':
      return summary.exotel_status === 'pending_credentials'
        ? 'Slot reserved · credentials to be added by superadmin'
        : summary.exotel_status || 'Pending';
    default:
      return '';
  }
};

const extractErrorMessage = (err, fallback) => {
  const detail = err?.response?.data?.detail;
  if (!detail) return fallback;
  if (typeof detail === 'string') return detail;
  if (detail.message) return detail.message;
  try { return JSON.stringify(detail); } catch (_) { return fallback; }
};

const googleScriptPromise = (() => {
  let promise = null;
  return () => {
    if (window.google?.accounts?.id) return Promise.resolve(window.google);
    if (promise) return promise;
    promise = new Promise((resolve, reject) => {
      const existing = document.querySelector('script[data-nokvo-one-gsi="true"]');
      if (existing) {
        existing.addEventListener('load', () => resolve(window.google), { once: true });
        existing.addEventListener('error', () => reject(new Error('Failed to load Google Identity Services')));
        return;
      }
      const script = document.createElement('script');
      script.src = 'https://accounts.google.com/gsi/client';
      script.async = true;
      script.defer = true;
      script.dataset.nokvoOneGsi = 'true';
      script.onload = () => resolve(window.google);
      script.onerror = () => reject(new Error('Failed to load Google Identity Services'));
      document.head.appendChild(script);
    });
    return promise;
  };
})();

const renderGoogleButtons = async () => {
  if (!authConfig.value?.google_client_id) return;
  try {
    await googleScriptPromise();
  } catch (err) {
    errorMsg.value = err.message || 'Failed to load Google.';
    return;
  }
  if (!window.google?.accounts?.id) return;
  window.google.accounts.id.initialize({
    client_id: authConfig.value.google_client_id,
    callback: handleGoogleCredential,
    ux_mode: 'popup',
    auto_select: false,
  });
  for (const host of [googleLoginButtonRef.value, googleSignupButtonRef.value]) {
    if (!host) continue;
    host.innerHTML = '';
    window.google.accounts.id.renderButton(host, {
      type: 'standard',
      theme: themeMode.value === 'dark' ? 'filled_black' : 'outline',
      size: 'large',
      shape: 'pill',
      text: 'continue_with',
      logo_alignment: 'left',
      width: Math.min(360, Math.max(280, host.clientWidth || 360)),
    });
  }
};

const handleGoogleCredential = async (response) => {
  if (!response?.credential) {
    errorMsg.value = 'Google did not return a credential.';
    return;
  }
  errorMsg.value = '';
  infoMsg.value = '';
  isAuthenticating.value = true;
  try {
    const { data } = await api.post('/google/login', { id_token: response.credential });
    if (data.provisioning) provisioning.value = data.provisioning;
    if (data.code === 'totp_setup_required') {
      setupToken.value = data.setup_token;
      infoMsg.value = data.created_via_google
        ? `Nokvo One organization created for ${data.email}. Set up TOTP to finish.`
        : `Signed in as ${data.email}. Set up TOTP to continue.`;
      await beginTotpSetup();
    } else if (data.code === 'totp_verify_required' || data.mfa_pending) {
      loginTempToken.value = data.access_token;
      authState.value = 'login_totp';
    } else if (data.access_token && data.user) {
      persistSession(data);
      authState.value = 'ready';
      await loadWorkspace();
    } else {
      errorMsg.value = 'Unexpected Google sign-in response.';
    }
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Google sign-in failed.');
  } finally {
    isAuthenticating.value = false;
  }
};

const fetchAuthConfig = async () => {
  try {
    const { data } = await api.get('/config');
    authConfig.value = data;
  } catch (_) {
    authConfig.value = { google_client_id: '', google_login_enabled: false };
  }
};

const loadWorkspace = async () => {
  isLoadingMembers.value = true;
  try {
    const [m, a, t, p] = await Promise.allSettled([
      api.get('/members/', { headers: authHeader() }),
      api.get('/agents/', { headers: authHeader() }),
      api.get('/agents/tools/predefined', { headers: authHeader() }),
      api.get('/me/provisioning', { headers: authHeader() }),
    ]);
    if (m.status === 'fulfilled') members.value = m.value.data;
    if (a.status === 'fulfilled') agents.value = a.value.data;
    if (t.status === 'fulfilled') predefinedTools.value = t.value.data;
    if (p.status === 'fulfilled') provisioning.value = p.value.data;
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Failed to load workspace.');
  } finally {
    isLoadingMembers.value = false;
  }
};

const restoreSession = async () => {
  const token = localStorage.getItem(ACCESS_TOKEN_KEY);
  if (!token) return false;
  try {
    const { data } = await api.get('/me', { headers: { Authorization: `Bearer ${token}` } });
    currentUser.value = data.user;
    currentOrganization.value = data.organization;
    authState.value = 'ready';
    await loadWorkspace();
    return true;
  } catch (_) {
    localStorage.removeItem(ACCESS_TOKEN_KEY);
    localStorage.removeItem(REFRESH_TOKEN_KEY);
    return false;
  }
};

const PROVISIONING_STEP_ORDER = [
  'resource_group',
  'azure_openai_realtime_mini',
  'shared_key_vault',
  'blob_prefix',
  'qdrant_collection',
  'redis_namespace',
  'exotel_placeholder',
];

const seedProvisioningSteps = () => {
  provisioning.value = {
    tenant_id: '',
    provisioning_status: 'in_progress',
    steps: PROVISIONING_STEP_ORDER.map((name) => ({ name, status: 'pending' })),
  };
};

const applyStepEvent = (event) => {
  if (!provisioning.value) seedProvisioningSteps();
  const steps = provisioning.value.steps;
  const idx = steps.findIndex((s) => s.name === event.name);
  if (idx >= 0) {
    steps[idx] = { ...steps[idx], status: event.status, message: event.message || null };
  } else {
    steps.push({ name: event.name, status: event.status, message: event.message || null });
  }
};

const parseSseLines = (buffer) => {
  // Splits buffer on blank lines; returns [parsedEvents, remainder]
  const blocks = buffer.split('\n\n');
  const remainder = blocks.pop() || '';
  const events = [];
  for (const block of blocks) {
    for (const line of block.split('\n')) {
      const trimmed = line.startsWith('data:') ? line.slice(5).trim() : null;
      if (!trimmed) continue;
      try {
        events.push(JSON.parse(trimmed));
      } catch (_) {
        // ignore malformed line
      }
    }
  }
  return [events, remainder];
};

const handleSignup = async () => {
  errorMsg.value = '';
  infoMsg.value = '';
  isAuthenticating.value = true;
  seedProvisioningSteps();
  authState.value = 'provisioning_running';

  try {
    const response = await fetch(`${API_BASE_URL}/signup`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
      body: JSON.stringify(signup.value),
    });

    if (!response.ok) {
      let detail = `Sign up failed (HTTP ${response.status})`;
      try {
        const body = await response.json();
        detail = typeof body.detail === 'string' ? body.detail : detail;
      } catch (_) {}
      errorMsg.value = detail;
      authState.value = 'signup';
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let completePayload = null;
    let errorEvent = null;

    while (true) {
      const { value, done } = await reader.read();
      if (value) buffer += decoder.decode(value, { stream: true });
      const [events, remainder] = parseSseLines(buffer);
      buffer = remainder;
      for (const event of events) {
        if (event.event === 'step') {
          applyStepEvent(event);
        } else if (event.event === 'complete') {
          completePayload = event;
        } else if (event.event === 'error') {
          errorEvent = event;
        }
      }
      if (done) break;
    }

    if (errorEvent) {
      errorMsg.value = `Provisioning failed at "${errorEvent.step}": ${errorEvent.message}`;
      if (provisioning.value) provisioning.value.provisioning_status = 'failed';
      authState.value = 'signup';
      return;
    }
    if (!completePayload) {
      errorMsg.value = 'Sign up ended without a completion event.';
      authState.value = 'signup';
      return;
    }
    provisioning.value = completePayload.provisioning || provisioning.value;
    if (provisioning.value) provisioning.value.provisioning_status = 'success';
    infoMsg.value = `Verification link sent to ${signup.value.admin_email}. Click it to continue setup.`;
    authState.value = 'check_email';
  } catch (err) {
    errorMsg.value = err?.message || 'Sign up failed.';
    authState.value = 'signup';
  } finally {
    isAuthenticating.value = false;
  }
};

const beginTotpSetup = async () => {
  isAuthenticating.value = true;
  try {
    const { data } = await api.post('/signup/totp/setup', { setup_token: setupToken.value });
    setupToken.value = data.setup_token;
    totpUri.value = data.uri;
    totpSecret.value = data.secret;
    authState.value = 'mfa_setup';
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Could not initialise TOTP.');
  } finally {
    isAuthenticating.value = false;
  }
};

const verifySignupTotp = async () => {
  errorMsg.value = '';
  isAuthenticating.value = true;
  try {
    await api.post('/signup/totp/verify', { setup_token: setupToken.value, code: totpCode.value });
    infoMsg.value = 'TOTP enrolled. Your organization is pending Nokvo activation. Sign in once approved.';
    resetLoginState();
    authState.value = 'login';
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Invalid TOTP code.');
  } finally {
    isAuthenticating.value = false;
  }
};

const handleLogin = async () => {
  errorMsg.value = '';
  isAuthenticating.value = true;
  try {
    const { data } = await api.post('/login', login.value);
    loginTempToken.value = data.access_token;
    authState.value = 'login_totp';
  } catch (err) {
    const detail = err.response?.data?.detail;
    if (err.response?.status === 403 && detail && typeof detail === 'object' && detail.code === 'totp_setup_required') {
      setupToken.value = detail.setup_token;
      infoMsg.value = 'Complete TOTP setup to continue.';
      await beginTotpSetup();
    } else {
      errorMsg.value = extractErrorMessage(err, 'Sign in failed.');
    }
  } finally {
    isAuthenticating.value = false;
  }
};

const verifyLoginTotp = async () => {
  errorMsg.value = '';
  isAuthenticating.value = true;
  try {
    const { data } = await api.post(
      '/login/totp/verify',
      { code: totpCode.value },
      { headers: { Authorization: `Bearer ${loginTempToken.value}` } },
    );
    persistSession(data);
    authState.value = 'ready';
    totpCode.value = '';
    await loadWorkspace();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Invalid TOTP code.');
  } finally {
    isAuthenticating.value = false;
  }
};

const handleVerifyEmailUrlIfPresent = async () => {
  const params = new URLSearchParams(window.location.search);
  const token = params.get('token');
  const path = window.location.pathname;
  if (path.includes('/nokvo-one/verify-email') && token) {
    isAuthenticating.value = true;
    try {
      const { data } = await api.get('/signup/verify-email', { params: { token } });
      setupToken.value = data.setup_token;
      infoMsg.value = `Email verified for ${data.email}. Set up TOTP next.`;
      await beginTotpSetup();
    } catch (err) {
      errorMsg.value = extractErrorMessage(err, 'Email verification failed.');
      authState.value = 'login';
    } finally {
      isAuthenticating.value = false;
    }
  } else if (path.includes('/nokvo-one/accept-invite') && token) {
    inviteToken.value = token;
    authState.value = 'accept_invite';
    try {
      const { data } = await api.get(`/members/invitations/${token}`);
      inviteContext.value = data;
    } catch (err) {
      errorMsg.value = extractErrorMessage(err, 'Invitation not found.');
    }
  }
};

const acceptInvitation = async () => {
  errorMsg.value = '';
  isAuthenticating.value = true;
  try {
    const { data } = await api.post(`/members/invitations/${inviteToken.value}/accept`, {
      token: inviteToken.value,
      password: invitePassword.value,
    });
    setupToken.value = data.setup_token;
    totpUri.value = data.uri;
    totpSecret.value = data.secret;
    authState.value = 'mfa_setup';
    infoMsg.value = 'Password set. Scan the QR with your authenticator and verify the 6-digit code.';
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Invitation acceptance failed.');
  } finally {
    isAuthenticating.value = false;
  }
};

const inviteMember = async () => {
  if (!inviteCanSubmit.value) return;
  errorMsg.value = '';
  isSavingMember.value = true;
  try {
    await api.post('/members/invite', inviteForm.value, { headers: authHeader() });
    infoMsg.value = `Invitation sent to ${inviteForm.value.email}.`;
    inviteForm.value = { email: '', full_name: '', role: 'member' };
    const { data } = await api.get('/members/', { headers: authHeader() });
    members.value = data;
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Invite failed.');
  } finally {
    isSavingMember.value = false;
  }
};

const toggleAgentTool = (key) => {
  const idx = newAgent.value.tool_keys.indexOf(key);
  if (idx >= 0) newAgent.value.tool_keys.splice(idx, 1);
  else newAgent.value.tool_keys.push(key);
};

const createAgent = async () => {
  errorMsg.value = '';
  try {
    const { data } = await api.post('/agents/', newAgent.value, { headers: authHeader() });
    agents.value.unshift(data);
    activeAgent.value = data;
    newAgent.value = { name: '', description: '', system_prompt: '', tool_keys: [] };
    chatLog.value = [];
    infoMsg.value = 'Agent created. Try it in the test console.';
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Agent creation failed.');
  }
};

const sendChat = async () => {
  if (!activeAgent.value || !chatInput.value.trim()) return;
  const userText = chatInput.value;
  chatLog.value.push({ role: 'user', text: userText });
  chatInput.value = '';
  try {
    const { data } = await api.post(
      `/agents/${activeAgent.value.id}/chat`,
      { message: userText },
      { headers: authHeader() },
    );
    chatLog.value.push({ role: 'agent', text: data.reply, tool_calls: data.tool_calls });
  } catch (err) {
    chatLog.value.push({ role: 'system', text: extractErrorMessage(err, 'Chat error.') });
  }
};

const loadEmailDrafts = async () => {
  try {
    const { data } = await api.get('/agents/records/email-drafts', { headers: authHeader() });
    emailDrafts.value = data;
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Failed to load drafts.');
  }
};

const discardDraft = async (id) => {
  await api.post(`/agents/records/email-drafts/${id}/discard`, {}, { headers: authHeader() });
  await loadEmailDrafts();
};

const handleLogout = async () => {
  try {
    await api.post('/logout', {}, { headers: authHeader() });
  } catch (_) {}
  localStorage.removeItem(ACCESS_TOKEN_KEY);
  localStorage.removeItem(REFRESH_TOKEN_KEY);
  currentUser.value = null;
  currentOrganization.value = null;
  members.value = [];
  agents.value = [];
  predefinedTools.value = [];
  authState.value = 'login';
};

onMounted(async () => {
  await fetchAuthConfig();
  await handleVerifyEmailUrlIfPresent();
  if (authState.value === 'login') await restoreSession();
  await nextTick();
  await renderGoogleButtons();
});

watch([authState, themeMode], async () => {
  await nextTick();
  await renderGoogleButtons();
});

onBeforeUnmount(() => {
  if (cursorTimer.value) clearTimeout(cursorTimer.value);
});
</script>

<template>
  <section
    ref="orgShellRef"
    :class="['org-shell', themeMode]"
    @pointermove="updateCursorGlow"
  >
    <div class="ambient-layer" aria-hidden="true">
      <div class="ambient-orb orb-top"></div>
      <div class="ambient-orb orb-bottom"></div>
    </div>

    <div class="mode-bar">
      <button type="button" class="mode-link" @click="$emit('switch-mode')">
        <Shield :size="14" />
        Nokvo Prime / SuperAdmin
      </button>
    </div>

    <main v-if="authState !== 'ready'" class="login-layout">
      <div class="brand-block">
        <h1>NOKVO ONE</h1>
        <p>Simple agent ops for teams that ship today.</p>
      </div>

      <div class="login-card">
        <div v-if="errorMsg" class="message error">{{ errorMsg }}</div>
        <div v-else-if="infoMsg" class="message info">{{ infoMsg }}</div>

        <!-- LOGIN -->
        <div v-if="authState === 'login'" class="mfa-panel">
          <div class="mfa-head">
            <strong>Sign in to Nokvo One</strong>
            <span>Continue with Google or use your work email + password</span>
          </div>
          <div v-if="authConfig?.google_login_enabled" class="google-action">
            <div ref="googleLoginButtonRef" class="google-button-host" :class="{ disabled: isAuthenticating }"></div>
          </div>
          <div class="auth-divider"><span>or</span></div>
          <label class="code-label" for="nokvo-one-email">Work Email</label>
          <input id="nokvo-one-email" v-model="login.email" class="totp-input" type="email" placeholder="you@yourcompany.com" />
          <label class="code-label" for="nokvo-one-password">Password</label>
          <input id="nokvo-one-password" v-model="login.password" class="totp-input" type="password" placeholder="••••••••" />
          <div class="mfa-actions">
            <button type="button" class="ghost-button" @click="authState = 'signup'">Create org</button>
            <button type="button" class="primary-button" :disabled="isAuthenticating" @click="handleLogin">
              {{ isAuthenticating ? 'Continuing...' : 'Continue' }}
            </button>
          </div>
          <p class="login-help">
            Access is restricted to organization members on an approved work email domain.
          </p>
        </div>

        <!-- LOGIN TOTP -->
        <div v-else-if="authState === 'login_totp'" class="mfa-panel">
          <div class="mfa-head">
            <strong>Enter Organization TOTP</strong>
            <span>{{ login.email }}</span>
          </div>
          <p class="login-help compact">
            Use the authenticator linked to this work email. A different email's TOTP will not work.
          </p>
          <label class="code-label" for="nokvo-one-totp-login">6-digit code</label>
          <input
            id="nokvo-one-totp-login"
            v-model="totpCode"
            class="totp-input"
            type="text"
            inputmode="numeric"
            maxlength="6"
            placeholder="000000"
          />
          <div class="mfa-actions">
            <button type="button" class="ghost-button" :disabled="isAuthenticating" @click="resetLoginState">Back</button>
            <button type="button" class="primary-button" :disabled="isAuthenticating" @click="verifyLoginTotp">
              {{ isAuthenticating ? 'Verifying...' : 'Authenticate' }}
            </button>
          </div>
        </div>

        <!-- SIGNUP -->
        <div v-else-if="authState === 'signup'" class="mfa-panel">
          <div class="mfa-head">
            <strong>Create your Nokvo One organization</strong>
            <span>Continue with Google or self-serve with email + password. TOTP is required.</span>
          </div>
          <div v-if="authConfig?.google_login_enabled" class="google-action">
            <div ref="googleSignupButtonRef" class="google-button-host" :class="{ disabled: isAuthenticating }"></div>
          </div>
          <div class="auth-divider"><span>or</span></div>
          <label class="code-label" for="signup-org">Organization name</label>
          <input id="signup-org" v-model="signup.org_name" class="totp-input" type="text" placeholder="Acme Inc." />
          <label class="code-label" for="signup-name">Your name</label>
          <input id="signup-name" v-model="signup.admin_name" class="totp-input" type="text" placeholder="Full name" />
          <label class="code-label" for="signup-email">Work email</label>
          <input id="signup-email" v-model="signup.admin_email" class="totp-input" type="email" placeholder="you@yourcompany.com" />
          <label class="code-label" for="signup-password">Password</label>
          <input id="signup-password" v-model="signup.password" class="totp-input" type="password" placeholder="min 10 chars · letters + digits" />
          <div class="mfa-actions">
            <button type="button" class="ghost-button" @click="authState = 'login'">Have an account?</button>
            <button type="button" class="primary-button" :disabled="isAuthenticating" @click="handleSignup">
              {{ isAuthenticating ? 'Creating...' : 'Create organization' }}
            </button>
          </div>
        </div>

        <!-- CHECK EMAIL -->
        <!-- PROVISIONING LIVE STREAM -->
        <div v-else-if="authState === 'provisioning_running'" class="mfa-panel">
          <div class="mfa-head">
            <strong>Setting up your environment</strong>
            <span>{{ signup.admin_email }}</span>
          </div>
          <p class="login-help compact">
            Provisioning your Azure resources, Qdrant collection, Redis namespace, and Exotel slot. This usually
            takes 60–90 seconds. Don't close this tab.
          </p>
          <div v-if="provisioning" class="provisioning-block">
            <ul class="provisioning-steps">
              <li v-for="step in provisioning.steps" :key="step.name" :data-state="step.status">
                <span class="step-marker" :data-state="step.status"></span>
                <div>
                  <strong>{{ stepLabel(step.name) }}</strong>
                  <small>{{ stepDescription(step.name, step.status, provisioning) || step.status }}</small>
                </div>
                <span class="step-state">{{ step.status }}</span>
              </li>
            </ul>
          </div>
        </div>

        <div v-else-if="authState === 'check_email'" class="mfa-panel">
          <div class="mfa-head">
            <strong>Check your inbox</strong>
            <span>{{ signup.admin_email }}</span>
          </div>
          <p class="login-help compact">
            Click the verification link we just sent to continue setup. The link is valid for 24 hours.
          </p>

          <div v-if="provisioning" class="provisioning-block">
            <div class="provisioning-head">
              <strong>Tenant provisioning</strong>
              <span class="status-chip" :class="{ active: provisioning.provisioning_status === 'success' }">
                <span class="status-dot"></span>
                {{ provisioning.provisioning_status }}
              </span>
            </div>
            <ul class="provisioning-steps">
              <li v-for="step in provisioning.steps" :key="step.name" :data-state="step.status">
                <span class="step-marker" :data-state="step.status"></span>
                <div>
                  <strong>{{ stepLabel(step.name) }}</strong>
                  <small>{{ stepDescription(step.name, step.status, provisioning) }}</small>
                </div>
                <span class="step-state">{{ step.status }}</span>
              </li>
            </ul>
          </div>

          <div class="mfa-actions">
            <button type="button" class="ghost-button" @click="resetLoginState">Back to sign in</button>
          </div>
        </div>

        <!-- TOTP SETUP -->
        <div v-else-if="authState === 'mfa_setup'" class="mfa-panel">
          <div class="mfa-head">
            <strong>Link Your Authenticator</strong>
            <span>{{ signup.admin_email || inviteContext?.email }}</span>
          </div>
          <p class="login-help compact">
            Scan this QR with the authenticator for this work email. Your TOTP secret is encrypted at rest.
          </p>
          <div class="qr-shell">
            <QrcodeVue :value="totpUri" :size="168" level="M" background="#ffffff" foreground="#111111" />
          </div>
          <div class="secret-note">
            Manual entry key: <code>{{ totpSecret }}</code>
          </div>
          <label class="code-label" for="nokvo-one-totp-setup">6-digit code</label>
          <input
            id="nokvo-one-totp-setup"
            v-model="totpCode"
            class="totp-input"
            type="text"
            inputmode="numeric"
            maxlength="6"
            placeholder="000000"
          />
          <div class="mfa-actions">
            <button type="button" class="ghost-button" :disabled="isAuthenticating" @click="resetLoginState">Cancel</button>
            <button type="button" class="primary-button" :disabled="isAuthenticating" @click="verifySignupTotp">
              {{ isAuthenticating ? 'Verifying...' : 'Verify & Continue' }}
            </button>
          </div>
        </div>

        <!-- ACCEPT INVITE -->
        <div v-else-if="authState === 'accept_invite'" class="mfa-panel">
          <div class="mfa-head">
            <strong>You're invited to {{ inviteContext?.organization_name || 'Nokvo One' }}</strong>
            <span>{{ inviteContext?.email }} · {{ inviteContext?.role }}</span>
          </div>
          <p class="login-help compact">
            Set a password to accept this invitation. You'll set up TOTP on the next step.
          </p>
          <label class="code-label" for="invite-password">Password</label>
          <input id="invite-password" v-model="invitePassword" class="totp-input" type="password" placeholder="min 10 chars · letters + digits" />
          <div class="mfa-actions">
            <button type="button" class="primary-button" :disabled="isAuthenticating" @click="acceptInvitation">
              {{ isAuthenticating ? 'Accepting...' : 'Accept invitation' }}
            </button>
          </div>
        </div>
      </div>

      <div class="footer-links">
        <p>
          Self-serve with work-email signup. Activations are reviewed by Nokvo before calling features unlock.
        </p>
      </div>
    </main>

    <main v-else class="workspace-layout dashboard-layout">
      <div class="floating-top-nav">
        <nav class="dashboard-nav">
          <div class="dashboard-brand">
            <div class="brand-mark">N1</div>
            <div class="brand-copy">
              <strong>NOKVO ONE</strong>
              <span>{{ currentOrganization?.name }}</span>
            </div>
          </div>

          <div class="nav-search">
            <Search :size="18" class="nav-search-icon" />
            <input v-model="dashboardQuery" type="text" :placeholder="dashboardSearchPlaceholder" />
          </div>

          <div class="dashboard-nav-actions">
            <button type="button" class="nav-page-button" :class="{ active: currentPage === 'dashboard' }" @click="switchPage('dashboard')">
              <Database :size="17" />
              <span>Dashboard</span>
            </button>
            <button type="button" class="nav-page-button" :class="{ active: currentPage === 'members' }" @click="switchPage('members')">
              <Users :size="17" />
              <span>Members</span>
            </button>
            <button type="button" class="nav-page-button" :class="{ active: currentPage === 'agent' }" @click="switchPage('agent')">
              <Bot :size="17" />
              <span>Agent Studio</span>
            </button>
            <button type="button" class="nav-icon-button" @click="toggleMemberFilter">
              <Filter :size="18" />
            </button>
            <button type="button" class="nav-icon-button" @click="cycleMemberSort">
              <ArrowUpDown :size="18" />
            </button>
            <button type="button" class="nav-icon-button">
              <Bell :size="18" />
            </button>
            <button type="button" class="nav-icon-button">
              <Settings2 :size="18" />
            </button>
            <button type="button" class="theme-toggle-button" @click="toggleThemeMode">
              <SunMedium v-if="themeMode === 'dark'" :size="17" />
              <Moon v-else :size="17" />
              <span>{{ themeToggleLabel }}</span>
            </button>
            <button type="button" class="org-avatar-button" @click="handleLogout">
              <span>{{ organizationInitial }}</span>
            </button>
          </div>
        </nav>
      </div>

      <section class="dashboard-header">
        <div>
          <span class="section-kicker">Nokvo One Workspace</span>
          <h2>{{ currentOrganization?.name }}</h2>
          <p>
            Signed in as {{ currentUser?.email }}. Scoped to <strong>{{ currentOrganization?.email_domain }}</strong>.
            Use predefined tools to keep agent actions safe and auditable.
          </p>
        </div>
        <div class="dashboard-header-actions">
          <button type="button" class="dashboard-secondary-button" @click="handleLogout">
            <LogOut :size="16" />
            Log Out
          </button>
        </div>
      </section>

      <section v-if="currentPage === 'dashboard'" class="dashboard-summary-bar">
        <div class="summary-pill-group">
          <div class="summary-pill">
            <span>Domain</span>
            <strong>{{ currentOrganization?.email_domain }}</strong>
          </div>
          <div class="summary-pill">
            <span>Members</span>
            <strong>{{ members.length }}</strong>
          </div>
          <div class="summary-pill">
            <span>Agents</span>
            <strong>{{ agents.length }}</strong>
          </div>
          <div class="summary-pill">
            <span>Status</span>
            <strong>{{ currentOrganization?.status }}</strong>
          </div>
          <div class="summary-pill">
            <span>Calling</span>
            <strong>{{ currentOrganization?.calling_enabled ? 'Enabled' : 'Gated' }}</strong>
          </div>
          <div class="summary-pill">
            <span>Role</span>
            <strong>{{ currentUser?.role }}</strong>
          </div>
        </div>
        <div class="dashboard-filter-actions">
          <button type="button" class="dashboard-chip-button" @click="toggleMemberFilter">
            <Filter :size="16" />
            {{ memberFilterLabel }}
          </button>
          <button type="button" class="dashboard-chip-button" @click="cycleMemberSort">
            <ArrowUpDown :size="16" />
            {{ memberSortLabel }}
          </button>
        </div>
      </section>

      <div v-if="errorMsg" class="message error dashboard-message">{{ errorMsg }}</div>
      <div v-else-if="infoMsg" class="message info dashboard-message">{{ infoMsg }}</div>

      <!-- DASHBOARD -->
      <section v-if="currentPage === 'dashboard'" class="dashboard-section">
        <div class="dashboard-section-head">
          <div>
            <span class="section-kicker">Overview</span>
            <h3>Organization Snapshot</h3>
          </div>
          <p>Workspace status, agent readiness, and access health in one glance.</p>
        </div>

        <div class="dashboard-grid overview-grid">
          <article class="dashboard-card organization-card">
            <div class="dashboard-card-glow"></div>
            <div class="organization-card-head">
              <div class="organization-ident">
                <div class="organization-mark">{{ organizationInitial }}</div>
                <div>
                  <h3>{{ currentOrganization?.name }}</h3>
                  <p>{{ currentUser?.role }} workspace</p>
                </div>
              </div>
              <span class="status-chip" :class="{ active: currentOrganization?.status === 'active' }">
                <span class="status-dot"></span>
                {{ currentOrganization?.status }}
              </span>
            </div>

            <p class="organization-description">
              Signed in as {{ currentUser?.email }}. This workspace is scoped to {{ currentOrganization?.email_domain }}.
              Predefined tools keep agent actions auditable; outbound calling unlocks after Nokvo activation.
            </p>

            <div class="usage-block">
              <div class="usage-labels">
                <span>Agent Coverage</span>
                <strong>{{ agents.length }} agent(s) configured</strong>
              </div>
              <div class="usage-track">
                <div class="usage-fill" :style="{ width: `${Math.min(agents.length * 25, 100)}%` }"></div>
              </div>
            </div>

            <div class="organization-metrics">
              <div>
                <span>Members</span>
                <strong>{{ members.length }}</strong>
              </div>
              <div>
                <span>Predefined Tools</span>
                <strong>{{ predefinedTools.length }}</strong>
              </div>
              <div>
                <span>Region</span>
                <strong>{{ currentOrganization?.region }}</strong>
              </div>
              <div>
                <span>Environment</span>
                <strong>{{ currentOrganization?.environment }}</strong>
              </div>
            </div>
          </article>

          <article class="dashboard-card compact-card">
            <div class="compact-card-head">
              <div class="compact-icon-shell">
                <Bot :size="18" />
              </div>
              <div>
                <h3>Agent Studio</h3>
                <p>{{ agents.length ? `${agents.length} agent(s) created` : 'No agents yet' }}</p>
              </div>
            </div>
            <dl class="dashboard-detail-list">
              <div>
                <dt>Predefined Tools</dt>
                <dd>{{ predefinedTools.length }} available</dd>
              </div>
              <div>
                <dt>Calling</dt>
                <dd>{{ currentOrganization?.calling_enabled ? 'Enabled' : 'Awaiting approval' }}</dd>
              </div>
              <div>
                <dt>Chat Mode</dt>
                <dd>Limited (no external sends)</dd>
              </div>
            </dl>
            <button type="button" class="dashboard-inline-button" @click="switchPage('agent')">
              <Wrench :size="15" />
              Open Agent Studio
            </button>
          </article>

          <article class="dashboard-card compact-card access-card">
            <div class="compact-card-head">
              <div class="compact-icon-shell">
                <UserPlus :size="18" />
              </div>
              <div>
                <h3>Access</h3>
                <p>Invite teammates under @{{ currentOrganization?.email_domain }}</p>
              </div>
            </div>
            <dl class="dashboard-detail-list">
              <div>
                <dt>Total Members</dt>
                <dd>{{ members.length }}</dd>
              </div>
              <div>
                <dt>Pending Invites</dt>
                <dd>{{ members.filter((m) => m.status === 'invited' || m.status === 'pending_totp').length }}</dd>
              </div>
              <div>
                <dt>Auth</dt>
                <dd>Password + TOTP (encrypted)</dd>
              </div>
            </dl>
            <button type="button" class="dashboard-inline-button" @click="switchPage('members')">
              <Users :size="15" />
              Manage Members
            </button>
          </article>
        </div>

        <div class="dashboard-section-head">
          <div>
            <span class="section-kicker">Infrastructure</span>
            <h3>Tenant Provisioning</h3>
          </div>
          <p>What Nokvo One created in Azure, Qdrant, Redis, and Exotel for your organization.</p>
        </div>

        <article class="dashboard-card wide-card provisioning-card">
          <div v-if="!provisioning" class="empty-state">Provisioning state will appear here.</div>
          <div v-else>
            <div class="members-card-head">
              <div>
                <h3>Resources for tenant {{ provisioning.tenant_id }}</h3>
                <p>{{ provisioning.azure_resource_group_name || 'Resource group' }} · {{ provisioning.azure_region }}</p>
              </div>
              <span class="status-chip" :class="{ active: provisioning.provisioning_status === 'success' }">
                <span class="status-dot"></span>
                {{ provisioning.provisioning_status }}
              </span>
            </div>
            <ul class="provisioning-steps">
              <li v-for="step in provisioning.steps" :key="step.name" :data-state="step.status">
                <span class="step-marker" :data-state="step.status"></span>
                <div>
                  <strong>{{ stepLabel(step.name) }}</strong>
                  <small>{{ stepDescription(step.name, step.status, provisioning) }}</small>
                </div>
                <span class="step-state">{{ step.status }}</span>
              </li>
            </ul>
          </div>
        </article>

        <div class="dashboard-section-head">
          <div>
            <span class="section-kicker">Controls</span>
            <h3>Quick Actions</h3>
          </div>
          <p>Invite teammates and review your workspace profile.</p>
        </div>

        <div class="dashboard-grid control-grid">
          <article class="dashboard-card invite-card">
            <div class="compact-card-head">
              <div class="compact-icon-shell">
                <UserPlus :size="18" />
              </div>
              <div>
                <h3>Invite Member</h3>
                <p>Email invite link. Invitee sets their own password and TOTP.</p>
              </div>
            </div>
            <div class="invite-domain-banner">
              <span>Allowed Domain</span>
              <strong>{{ currentOrganization?.email_domain }}</strong>
            </div>
            <form class="invite-form dashboard-invite-form" @submit.prevent="inviteMember">
              <label class="invite-field">
                <span>Work Email</span>
                <input v-model="inviteForm.email" type="email" placeholder="name@yourcompany.com" required />
              </label>
              <label class="invite-field">
                <span>Full Name</span>
                <input v-model="inviteForm.full_name" type="text" placeholder="Full name" />
              </label>
              <label class="invite-field">
                <span>Role</span>
                <select v-model="inviteForm.role">
                  <option value="member">Member</option>
                  <option value="manager">Manager</option>
                  <option value="viewer">Viewer</option>
                  <option value="admin">Admin</option>
                </select>
              </label>
              <div class="invite-action-block">
                <button type="submit" :disabled="isSavingMember || !inviteCanSubmit">
                  {{ isSavingMember ? 'Inviting...' : 'Send Invite' }}
                </button>
                <p :class="['invite-helper', { invalid: !isInviteDomainValid }]">
                  {{ inviteValidationMessage }}
                </p>
              </div>
            </form>
          </article>

          <article class="dashboard-card workspace-card">
            <div class="compact-card-head">
              <div class="compact-icon-shell">
                <Shield :size="18" />
              </div>
              <div>
                <h3>Workspace Profile</h3>
                <p>Identity and tier metadata for this Nokvo One organization.</p>
              </div>
            </div>
            <div class="workspace-profile-grid">
              <div>
                <span>Name</span>
                <strong>{{ currentOrganization?.name }}</strong>
              </div>
              <div>
                <span>Domain</span>
                <strong>{{ currentOrganization?.email_domain }}</strong>
              </div>
              <div>
                <span>Admin</span>
                <strong>{{ currentOrganization?.admin_email || 'Not set' }}</strong>
              </div>
              <div>
                <span>Tier</span>
                <strong>{{ currentOrganization?.product_tier }}</strong>
              </div>
            </div>
          </article>
        </div>
      </section>

      <!-- MEMBERS -->
      <section v-if="currentPage === 'members'" class="dashboard-section">
        <div class="dashboard-section-head">
          <div>
            <span class="section-kicker">Access</span>
            <h3>Organization Members</h3>
          </div>
          <p>Searchable access roster. Invitees receive a one-time link to set password + TOTP.</p>
        </div>

        <div class="dashboard-grid control-grid">
          <article class="dashboard-card invite-card">
            <div class="compact-card-head">
              <div class="compact-icon-shell">
                <UserPlus :size="18" />
              </div>
              <div>
                <h3>Invite Member</h3>
                <p>Add admins, managers, or members under the same verified domain.</p>
              </div>
            </div>
            <div class="invite-domain-banner">
              <span>Allowed Domain</span>
              <strong>{{ currentOrganization?.email_domain }}</strong>
            </div>
            <form class="invite-form dashboard-invite-form" @submit.prevent="inviteMember">
              <label class="invite-field">
                <span>Work Email</span>
                <input v-model="inviteForm.email" type="email" placeholder="name@yourcompany.com" required />
              </label>
              <label class="invite-field">
                <span>Full Name</span>
                <input v-model="inviteForm.full_name" type="text" placeholder="Full name" />
              </label>
              <label class="invite-field">
                <span>Role</span>
                <select v-model="inviteForm.role">
                  <option value="member">Member</option>
                  <option value="manager">Manager</option>
                  <option value="viewer">Viewer</option>
                  <option value="admin">Admin</option>
                </select>
              </label>
              <div class="invite-action-block">
                <button type="submit" :disabled="isSavingMember || !inviteCanSubmit">
                  {{ isSavingMember ? 'Inviting...' : 'Send Invite' }}
                </button>
                <p :class="['invite-helper', { invalid: !isInviteDomainValid }]">
                  {{ inviteValidationMessage }}
                </p>
              </div>
            </form>
          </article>

          <article class="dashboard-card wide-card members-card">
            <div class="members-card-head">
              <div>
                <h3>Members</h3>
                <p>Use filter & sort in the top nav to slice this list.</p>
              </div>
              <div class="members-summary">
                <span>{{ filteredMembers.length }} visible</span>
                <span>{{ members.length }} total</span>
              </div>
            </div>

            <div v-if="isLoadingMembers" class="empty-state">Loading members...</div>
            <div v-else-if="!filteredMembers.length" class="empty-state">No members match the current filter.</div>
            <div v-else class="member-table dashboard-member-table">
              <div class="member-row member-head">
                <span>Member</span>
                <span>Role</span>
                <span>Status</span>
                <span></span>
              </div>
              <div v-for="m in filteredMembers" :key="m.id" class="member-row">
                <div class="member-meta">
                  <strong>{{ m.full_name || 'Unnamed member' }}</strong>
                  <small>{{ m.email }}</small>
                </div>
                <span class="readonly-tag">{{ m.role }}</span>
                <span class="readonly-tag">{{ m.status }}</span>
                <span class="readonly-tag">{{ m.auth_provider }}</span>
              </div>
            </div>
          </article>
        </div>
      </section>

      <!-- AGENT STUDIO -->
      <section v-if="currentPage === 'agent'" class="dashboard-section">
        <div class="dashboard-section-head">
          <div>
            <span class="section-kicker">Agent Studio</span>
            <h3>Build & Test Agents</h3>
          </div>
          <p>Compose agents from predefined tools. Chat is sandboxed and never sends external email.</p>
        </div>

        <div v-if="currentOrganization?.status === 'pending_approval'" class="message info dashboard-message">
          Your organization is awaiting Nokvo activation. Chat with agents in limited mode is available; calling
          unlocks after approval.
        </div>

        <div class="dashboard-grid agent-page-grid">
          <article class="dashboard-card agent-upload-card">
            <div class="compact-card-head">
              <div class="compact-icon-shell">
                <Bot :size="18" />
              </div>
              <div>
                <h3>Create Agent</h3>
                <p>Pick from the safe predefined tool catalog. No MCP toolkit generator on Nokvo One.</p>
              </div>
            </div>

            <div class="db-form-block">
              <label class="db-label" for="agent-name">Agent Name</label>
              <input id="agent-name" v-model="newAgent.name" class="db-input" type="text" placeholder="Sales Sidekick" />
            </div>

            <div class="db-form-block">
              <label class="db-label" for="agent-description">Description</label>
              <input id="agent-description" v-model="newAgent.description" class="db-input" type="text" placeholder="One-line purpose" />
            </div>

            <div class="db-form-block">
              <label class="db-label" for="agent-prompt">System Prompt</label>
              <textarea id="agent-prompt" v-model="newAgent.system_prompt" class="db-input toolkit-textarea" placeholder="Instructions, tone, escalation rules..."></textarea>
            </div>

            <div class="db-form-block">
              <label class="db-label">Enabled Tools</label>
              <div class="provider-grid provider-grid-dual">
                <label
                  v-for="t in predefinedTools"
                  :key="t.key"
                  class="provider-option"
                  :class="{ active: newAgent.tool_keys.includes(t.key) }"
                >
                  <input
                    type="checkbox"
                    class="sr-only"
                    :checked="newAgent.tool_keys.includes(t.key)"
                    @change="toggleAgentTool(t.key)"
                  />
                  <strong class="provider-name">{{ t.display_name }}</strong>
                  <small>{{ t.description }}</small>
                  <small v-if="t.requires_confirmation" class="agent-warning">Requires human confirmation</small>
                </label>
              </div>
            </div>

            <div class="db-actions">
              <button type="button" class="primary-button" @click="createAgent">
                <CheckCircle2 :size="16" />
                Create Agent
              </button>
            </div>
          </article>

          <article class="dashboard-card agent-documents-card">
            <div class="members-card-head">
              <div>
                <h3>Agents</h3>
                <p>Pick an agent to test in the chat console below.</p>
              </div>
              <span class="status-chip">{{ agents.length }} agents</span>
            </div>

            <div class="agent-document-list">
              <div v-if="!agents.length" class="empty-state compact">No agents yet. Create one to start testing.</div>
              <button
                v-for="a in agents"
                :key="a.id"
                type="button"
                class="agent-document-row toolkit-list-row"
                :class="{ active: activeAgent?.id === a.id }"
                @click="activeAgent = a; chatLog = []"
              >
                <div class="agent-document-main">
                  <div class="agent-document-icon">
                    <Bot :size="18" />
                  </div>
                  <div>
                    <strong>{{ a.name }}</strong>
                    <small>{{ (a.tool_keys || []).length }} tool(s) · {{ a.description || 'No description' }}</small>
                  </div>
                </div>
                <span class="status-chip">{{ activeAgent?.id === a.id ? 'Active' : 'Idle' }}</span>
              </button>
            </div>
          </article>

          <article class="dashboard-card wide-card agent-test-card">
            <div class="members-card-head">
              <div>
                <h3>Chat Tester</h3>
                <p>Talk to {{ activeAgent?.name || 'an agent' }}. Tool calls are sandboxed and audited.</p>
              </div>
              <span class="status-chip">{{ activeAgent ? activeAgent.name : 'No agent selected' }}</span>
            </div>

            <div class="agent-console-grid">
              <div class="db-form-block">
                <label class="db-label" for="chat-input">Message</label>
                <textarea id="chat-input" v-model="chatInput" class="db-input toolkit-textarea compact" placeholder="Ask the agent..."></textarea>
                <div class="db-actions">
                  <button type="button" class="ghost-button" :disabled="!activeAgent" @click="chatLog = []">Clear</button>
                  <button type="button" class="primary-button" :disabled="!activeAgent || !chatInput.trim()" @click="sendChat">
                    <MessageSquare :size="16" />
                    Send
                  </button>
                </div>
              </div>

              <div class="agent-console-results">
                <div class="agent-result-panel">
                  <strong>Conversation</strong>
                  <p v-if="!chatLog.length">Pick an agent and send a message to start.</p>
                  <div v-else class="agent-event-list">
                    <div
                      v-for="(turn, i) in chatLog"
                      :key="i"
                      class="agent-event-row"
                      :class="{
                        'event-transcript': turn.role === 'user',
                        'event-answer': turn.role === 'agent',
                        'event-error': turn.role === 'system',
                      }"
                    >
                      <span>{{ turn.role }}</span>
                      <p>{{ turn.text }}</p>
                      <p v-if="turn.tool_calls?.length" class="event-detail">
                        Tool calls:
                        <span v-for="(tc, idx) in turn.tool_calls" :key="idx">
                          {{ tc.tool }}{{ tc.ok === false ? ` (error: ${tc.error})` : '' }}{{ idx < turn.tool_calls.length - 1 ? ', ' : '' }}
                        </span>
                      </p>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <div class="agent-voice-panel">
              <div class="members-card-head">
                <div>
                  <h3>Email Drafts</h3>
                  <p>Drafts created by the agent stay queued until a human confirms or discards them. Nokvo One never sends email automatically.</p>
                </div>
                <button type="button" class="ghost-button compact" @click="loadEmailDrafts">Refresh</button>
              </div>

              <div class="agent-document-list">
                <div v-if="!emailDrafts.length" class="empty-state compact">No drafts yet. They appear here when the agent calls send_email_draft.</div>
                <div v-for="d in emailDrafts" :key="d.id" class="agent-document-row">
                  <div class="agent-document-main">
                    <div class="agent-document-icon">
                      <Bot :size="18" />
                    </div>
                    <div>
                      <strong>{{ d.data?.subject }}</strong>
                      <small>to {{ d.data?.to_email }} · {{ d.status }}</small>
                      <p class="agent-warning">{{ d.data?.body }}</p>
                    </div>
                  </div>
                  <div class="agent-document-actions">
                    <span class="status-chip">{{ d.status }}</span>
                    <button
                      v-if="d.status === 'pending_confirmation'"
                      type="button"
                      class="ghost-button compact"
                      @click="discardDraft(d.id)"
                    >
                      <XCircle :size="15" />
                      Discard
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </article>
        </div>
      </section>
    </main>

    <footer class="portal-footer">
      <span>© Nokvo · Nokvo One</span>
      <nav class="footer-nav">
        <a href="#" @click.prevent>Status</a>
        <a href="#" @click.prevent>Docs</a>
        <a href="#" @click.prevent="$emit('switch-mode')">Prime / SuperAdmin</a>
      </nav>
    </footer>
  </section>
</template>

<style scoped>
.org-shell {
  position: relative;
  min-height: 100vh;
  width: 100%;
  overflow: hidden;
  --cursor-x: 50%;
  --cursor-y: 30%;
  background: #fbfaee;
  color: #1b1c15;
  display: flex;
  flex-direction: column;
}

.ambient-layer {
  position: fixed;
  inset: 0;
  pointer-events: none;
  overflow: hidden;
}

.ambient-orb {
  position: absolute;
  width: 60vw;
  height: 60vw;
  border-radius: 999px;
  filter: blur(120px);
  opacity: 0.55;
}

.orb-top {
  top: -20%;
  right: -10%;
  background: rgba(229, 227, 212, 0.9);
}

.orb-bottom {
  bottom: -20%;
  left: -10%;
  background: rgba(233, 233, 221, 0.9);
}

.mode-bar {
  position: relative;
  z-index: 1;
  width: 100%;
  padding: 1.5rem 2rem 0;
  display: flex;
  justify-content: flex-end;
}

.mode-link {
  border: 1px solid rgba(27, 28, 21, 0.12);
  background: rgba(255, 255, 255, 0.82);
  color: #1b1c15;
  padding: 0.8rem 1rem;
  border-radius: 999px;
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  font-size: 0.82rem;
  font-weight: 700;
  letter-spacing: 0.02em;
}

.login-layout,
.workspace-layout {
  position: relative;
  z-index: 1;
  flex: 1;
  width: min(100%, 1120px);
  margin: 0 auto;
  padding: 2rem 1.5rem 3rem;
}

.login-layout {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 1.5rem;
  max-width: 460px;
}

.brand-block {
  text-align: center;
}

.brand-block h1 {
  font-family: Manrope, sans-serif;
  font-size: clamp(2.25rem, 4vw, 3rem);
  letter-spacing: -0.05em;
  font-weight: 700;
}

.brand-block p {
  margin-top: 0.6rem;
  color: #5f5f53;
  font-size: 1rem;
}

.login-card {
  background: rgba(255, 255, 255, 0.82);
  border: 1px solid #e4e3d7;
  box-shadow: 0 20px 60px -30px rgba(27, 28, 21, 0.18);
  backdrop-filter: blur(16px);
  width: 100%;
  border-radius: 1.1rem;
  padding: 2rem;
}

.google-action {
  display: flex;
  justify-content: center;
}

.google-button-host {
  width: 100%;
  min-height: 48px;
  display: flex;
  justify-content: center;
}

.google-button-host.disabled {
  opacity: 0.65;
  pointer-events: none;
}

.auth-divider {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin: 0.4rem 0;
  color: #5f5f53;
  font-size: 0.78rem;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  font-weight: 700;
}

.auth-divider::before,
.auth-divider::after {
  content: "";
  flex: 1;
  height: 1px;
  background: rgba(196, 199, 199, 0.55);
}

.org-shell.dark .auth-divider {
  color: #b8b7ab;
}

.org-shell.dark .auth-divider::before,
.org-shell.dark .auth-divider::after {
  background: rgba(102, 108, 92, 0.4);
}

.login-help,
.footer-links p {
  margin-top: 1rem;
  text-align: center;
  color: #5f5f53;
  line-height: 1.6;
}

.login-help.compact {
  margin-top: 0;
}

.mfa-panel {
  display: grid;
  gap: 1rem;
}

.mfa-head {
  display: flex;
  flex-direction: column;
  gap: 0.18rem;
}

.mfa-head strong {
  font-family: Manrope, sans-serif;
  font-size: 1.1rem;
}

.mfa-head span {
  color: #5f5f53;
  font-size: 0.92rem;
}

.qr-shell {
  display: flex;
  justify-content: center;
  padding: 1rem;
  border-radius: 1rem;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid #e4e3d7;
}

.secret-note {
  color: #5f5f53;
  font-size: 0.92rem;
  text-align: center;
}

.secret-note code {
  color: #1b1c15;
  font-weight: 700;
}

.code-label {
  font-size: 0.86rem;
  font-weight: 700;
  color: #1b1c15;
}

.totp-input {
  width: 100%;
  border-radius: 0.9rem;
  border: 1px solid #d9d8ce;
  background: rgba(255, 255, 255, 0.8);
  color: #1b1c15;
  padding: 1rem;
  font-size: 1.05rem;
}

.mfa-actions {
  display: flex;
  gap: 0.8rem;
}

.ghost-button,
.primary-button {
  flex: 1;
  border-radius: 999px;
  padding: 0.95rem 1rem;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.45rem;
}

.ghost-button {
  border: 1px solid #d8d7cc;
  background: #fffef8;
  color: #1b1c15;
}

.primary-button {
  border: none;
  background: #1d1c0f;
  color: #ffffff;
}

.ghost-button.compact,
.primary-button.compact {
  flex: unset;
  padding: 0.7rem 0.95rem;
}

.db-form-block {
  margin-bottom: 1.5rem;
}

.db-label {
  display: block;
  margin-bottom: 0.75rem;
  font-size: 0.82rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.provider-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.75rem;
}

.provider-grid-dual {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.provider-option {
  border: 1px solid #e4e3d7;
  background: #fbfaee;
  border-radius: 0.95rem;
  padding: 1rem 0.9rem;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  cursor: pointer;
  transition: all 0.2s ease;
}

.provider-option:hover {
  border-color: #8a8f86;
  transform: translateY(-1px);
}

.provider-option.active {
  border-color: #000000;
  background: #efeee3;
  box-shadow: inset 0 0 0 1px #000000;
}

.provider-name {
  font-weight: 700;
  color: #1b1c15;
}

.provider-option small {
  color: #5f5f53;
}

.db-input {
  width: 100%;
  border-radius: 0.9rem;
  border: 1px solid #c4c7c7;
  background: #f5f4e8;
  color: #1b1c15;
  padding: 1rem 1rem;
  font-size: 1rem;
  box-shadow: inset 0 1px 2px rgba(27, 28, 21, 0.04);
}

.db-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.8rem;
  padding-top: 0.25rem;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.workspace-layout {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}

.dashboard-layout {
  width: min(100%, 1320px);
  padding-top: 7.5rem;
  gap: 2rem;
}

.floating-top-nav {
  position: fixed;
  top: 1.5rem;
  left: 0;
  right: 0;
  z-index: 20;
  display: flex;
  justify-content: center;
  padding: 0 1.5rem;
  pointer-events: none;
}

.dashboard-nav {
  width: min(100%, 1320px);
  padding: 0.9rem 1.15rem;
  border-radius: 1.35rem;
  border: 1px solid rgba(196, 199, 199, 0.45);
  background: rgba(255, 255, 255, 0.82);
  backdrop-filter: blur(18px);
  box-shadow: 0 16px 45px -28px rgba(27, 28, 21, 0.28);
  display: grid;
  grid-template-columns: auto minmax(240px, 1fr) auto;
  gap: 1rem;
  align-items: center;
  pointer-events: auto;
}

.dashboard-brand {
  display: flex;
  align-items: center;
  gap: 0.8rem;
}

.brand-mark {
  width: 2.2rem;
  height: 2.2rem;
  border-radius: 0.8rem;
  background: #1b1c15;
  color: #ffffff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: Manrope, sans-serif;
  font-weight: 800;
}

.brand-copy {
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
}

.brand-copy strong {
  font-family: Manrope, sans-serif;
  font-size: 1.05rem;
  line-height: 1;
}

.brand-copy span {
  color: #5f5f53;
  font-size: 0.78rem;
}

.nav-search {
  position: relative;
}

.nav-search input {
  width: 100%;
  border-radius: 999px;
  border: 1px solid transparent;
  background: #efeee3;
  color: #1b1c15;
  padding: 0.85rem 1rem 0.85rem 2.8rem;
  font-size: 0.95rem;
  transition: border-color 0.2s ease, background 0.2s ease;
}

.nav-search input:focus {
  outline: none;
  border-color: rgba(116, 120, 120, 0.65);
  background: #ffffff;
}

.nav-search-icon {
  position: absolute;
  left: 0.95rem;
  top: 50%;
  transform: translateY(-50%);
  color: rgba(68, 71, 72, 0.72);
}

.dashboard-nav-actions,
.dashboard-header-actions,
.dashboard-filter-actions {
  display: flex;
  align-items: center;
  gap: 0.7rem;
}

.nav-icon-button,
.org-avatar-button,
.nav-page-button,
.theme-toggle-button,
.dashboard-secondary-button,
.dashboard-chip-button,
.dashboard-inline-button {
  border: 1px solid rgba(196, 199, 199, 0.6);
  background: rgba(255, 255, 255, 0.88);
  color: #1b1c15;
  border-radius: 999px;
  transition: transform 0.2s ease, box-shadow 0.2s ease, background 0.2s ease;
}

.nav-icon-button,
.org-avatar-button {
  width: 2.6rem;
  height: 2.6rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.theme-toggle-button {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  padding: 0.75rem 0.9rem;
  font-size: 0.8rem;
  font-weight: 700;
  white-space: nowrap;
}

.nav-page-button {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  padding: 0.72rem 0.9rem;
  font-size: 0.8rem;
  font-weight: 800;
  white-space: nowrap;
}

.nav-page-button.active {
  background: #1b1c15;
  border-color: #1b1c15;
  color: #ffffff;
}

.nav-icon-button:hover,
.org-avatar-button:hover,
.nav-page-button:hover,
.dashboard-secondary-button:hover,
.dashboard-chip-button:hover,
.dashboard-inline-button:hover,
.dashboard-primary-button:hover {
  transform: translateY(-1px);
  box-shadow: 0 10px 20px -16px rgba(27, 28, 21, 0.55);
}

.org-avatar-button {
  border-color: rgba(116, 120, 120, 0.35);
  overflow: hidden;
  font-family: Manrope, sans-serif;
  font-weight: 800;
}

.dashboard-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 1.5rem;
}

.dashboard-header h2 {
  font-family: Manrope, sans-serif;
  font-size: clamp(2.1rem, 4vw, 3rem);
  letter-spacing: -0.04em;
  line-height: 1.05;
}

.dashboard-header p {
  margin-top: 0.55rem;
  max-width: 42rem;
  color: #444748;
  font-size: 1.05rem;
  line-height: 1.7;
}

.dashboard-primary-button,
.dashboard-secondary-button,
.dashboard-chip-button,
.dashboard-inline-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.55rem;
  padding: 0.88rem 1.15rem;
  font-size: 0.84rem;
  font-weight: 700;
}

.dashboard-primary-button {
  border: 1px solid #1b1c15;
  background: #1b1c15;
  color: #ffffff;
  border-radius: 0.8rem;
}

.dashboard-secondary-button {
  border-radius: 0.8rem;
}

.dashboard-summary-bar {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  align-items: center;
}

.summary-pill-group {
  display: flex;
  flex-wrap: wrap;
  gap: 0.8rem;
}

.summary-pill {
  min-width: 10rem;
  padding: 0.85rem 1rem;
  border-radius: 1rem;
  border: 1px solid rgba(196, 199, 199, 0.45);
  background: rgba(255, 255, 255, 0.72);
  box-shadow: 0 10px 30px -24px rgba(27, 28, 21, 0.18);
}

.summary-pill span {
  display: block;
  margin-bottom: 0.22rem;
  color: #5f5f53;
  font-size: 0.72rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.summary-pill strong {
  font-family: Manrope, sans-serif;
  font-size: 0.98rem;
}

.dashboard-section {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.dashboard-section-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 1rem;
}

.dashboard-section-head h3 {
  font-family: Manrope, sans-serif;
  font-size: 1.55rem;
  letter-spacing: -0.03em;
}

.dashboard-section-head p {
  max-width: 28rem;
  color: #5f5f53;
  font-size: 0.94rem;
  line-height: 1.7;
  text-align: right;
}

.section-kicker {
  display: inline-block;
  margin-bottom: 0.35rem;
  color: #5f5f53;
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.dashboard-message {
  margin-bottom: -0.5rem;
}

.dashboard-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1.5rem;
}

.overview-grid .organization-card {
  grid-column: span 2;
}

.overview-grid .compact-card {
  min-height: 16rem;
}

.overview-grid .access-card {
  grid-column: span 1;
}

.control-grid {
  align-items: start;
}

.control-grid .invite-card,
.control-grid .workspace-card {
  grid-column: span 1;
}

.dashboard-card {
  position: relative;
  overflow: hidden;
  border-radius: 1.2rem;
  border: 1px solid #e5e3d4;
  background: rgba(255, 255, 255, 0.78);
  box-shadow: 0 16px 42px -28px rgba(27, 28, 21, 0.22);
  padding: 1.5rem;
  backdrop-filter: blur(14px);
}

.dashboard-card.wide-card {
  grid-column: 1 / -1;
}

.organization-card {
  min-height: 21rem;
}

.dashboard-card-glow {
  position: absolute;
  top: 0;
  right: 0;
  width: 9rem;
  height: 9rem;
  border-bottom-left-radius: 999px;
  background: linear-gradient(225deg, rgba(229, 226, 225, 0.85), rgba(229, 226, 225, 0));
  opacity: 0;
  transition: opacity 0.35s ease;
}

.dashboard-card:hover .dashboard-card-glow {
  opacity: 1;
}

.organization-card-head,
.compact-card-head,
.members-card-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
}

.organization-ident {
  display: flex;
  align-items: center;
  gap: 0.85rem;
}

.organization-mark,
.compact-icon-shell {
  width: 2.6rem;
  height: 2.6rem;
  border-radius: 0.9rem;
  border: 1px solid rgba(196, 199, 199, 0.45);
  background: #efeee3;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: Manrope, sans-serif;
  font-weight: 800;
}

.organization-card h3,
.compact-card-head h3,
.members-card-head h3 {
  font-family: Manrope, sans-serif;
  font-size: 1.2rem;
  line-height: 1.15;
}

.organization-card p,
.compact-card-head p,
.members-card-head p {
  margin-top: 0.18rem;
  color: #5f5f53;
  font-size: 0.9rem;
  line-height: 1.6;
}

.status-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.45rem 0.7rem;
  border-radius: 0.7rem;
  background: #e5e3d4;
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.status-dot {
  width: 0.45rem;
  height: 0.45rem;
  border-radius: 999px;
  background: #d97706;
}

.status-chip.active .status-dot {
  background: #059669;
}

.organization-description {
  margin-top: 1.15rem;
  color: #444748;
  font-size: 0.96rem;
  line-height: 1.75;
}

.usage-block {
  margin-top: 1.5rem;
}

.usage-labels {
  display: flex;
  justify-content: space-between;
  gap: 0.8rem;
  margin-bottom: 0.55rem;
  color: #5f5f53;
  font-size: 0.78rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.usage-labels strong {
  color: #1b1c15;
}

.usage-track {
  width: 100%;
  height: 0.45rem;
  overflow: hidden;
  border-radius: 999px;
  background: #e4e3d7;
}

.usage-fill {
  height: 100%;
  border-radius: 999px;
  background: #1b1c15;
}

.organization-metrics,
.workspace-profile-grid {
  margin-top: 1.5rem;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem;
  padding-top: 1rem;
  border-top: 1px solid rgba(196, 199, 199, 0.3);
}

.organization-metrics span,
.workspace-profile-grid span {
  display: block;
  color: #5f5f53;
  font-size: 0.72rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-bottom: 0.25rem;
}

.organization-metrics strong,
.workspace-profile-grid strong {
  font-family: Manrope, sans-serif;
  font-size: 1rem;
}

.dashboard-detail-list {
  margin-top: 1.2rem;
  display: grid;
  gap: 0.8rem;
}

.dashboard-detail-list dt {
  color: #5f5f53;
  font-size: 0.76rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.dashboard-detail-list dd {
  margin-top: 0.22rem;
  font-family: Manrope, sans-serif;
  font-size: 1rem;
  font-weight: 700;
}

.dashboard-inline-button {
  margin-top: 1.3rem;
  width: 100%;
  border-radius: 0.85rem;
}

.toolkit-textarea {
  min-height: 8rem;
  resize: vertical;
}

.toolkit-textarea.compact {
  min-height: 5.5rem;
}

.agent-page-grid {
  grid-template-columns: minmax(0, 0.95fr) minmax(0, 1.05fr);
  align-items: start;
}

.agent-upload-card,
.agent-documents-card {
  min-height: 34rem;
}

.agent-document-list {
  display: grid;
  gap: 0.85rem;
  margin-top: 1.2rem;
}

.agent-document-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 1rem;
  align-items: center;
  padding: 0.95rem;
  border: 1px solid rgba(196, 199, 199, 0.42);
  border-radius: 1rem;
  background: rgba(255, 255, 255, 0.68);
}

.agent-document-row.active,
.agent-document-row.toolkit-list-row:hover {
  border-color: rgba(27, 28, 21, 0.28);
  background: rgba(255, 255, 255, 0.92);
  cursor: pointer;
}

.agent-document-main {
  min-width: 0;
  display: flex;
  gap: 0.8rem;
  align-items: flex-start;
}

.agent-document-icon {
  flex: 0 0 auto;
  width: 2.35rem;
  height: 2.35rem;
  border-radius: 0.8rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: #efeee3;
  border: 1px solid rgba(196, 199, 199, 0.45);
}

.agent-document-main strong,
.agent-document-main small,
.agent-warning {
  display: block;
}

.agent-document-main strong {
  font-family: Manrope, sans-serif;
  line-height: 1.3;
}

.agent-document-main small {
  margin-top: 0.24rem;
  color: #5f5f53;
  font-size: 0.78rem;
  line-height: 1.45;
  word-break: break-word;
}

.agent-warning {
  margin-top: 0.35rem;
  color: #9f5f10;
  font-size: 0.78rem;
  line-height: 1.45;
}

.agent-document-actions {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.agent-console-grid {
  display: grid;
  grid-template-columns: minmax(0, 0.85fr) minmax(0, 1.15fr);
  gap: 1rem;
  margin-top: 1.15rem;
}

.agent-console-results {
  display: grid;
  gap: 1rem;
}

.agent-result-panel {
  min-height: 9rem;
  padding: 1rem;
  border-radius: 1rem;
  background: rgba(239, 238, 227, 0.72);
  border: 1px solid rgba(196, 199, 199, 0.42);
}

.agent-result-panel strong {
  display: block;
  margin-bottom: 0.55rem;
  font-family: Manrope, sans-serif;
}

.agent-result-panel p {
  color: #444748;
  font-size: 0.9rem;
  line-height: 1.65;
  white-space: pre-wrap;
}

.agent-voice-panel {
  margin-top: 1.1rem;
  padding-top: 1rem;
  border-top: 1px solid rgba(196, 199, 199, 0.35);
}

.agent-event-list {
  display: grid;
  gap: 0.65rem;
  margin-top: 1rem;
}

.agent-event-row {
  padding: 0.75rem 0.85rem;
  border-radius: 0.85rem;
  background: rgba(255, 255, 255, 0.68);
  border: 1px solid rgba(196, 199, 199, 0.38);
}

.agent-event-row span {
  display: block;
  margin-bottom: 0.3rem;
  color: #5f5f53;
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

.agent-event-row p {
  color: #444748;
  font-size: 0.88rem;
  line-height: 1.5;
}

.agent-event-row .event-detail {
  color: #7a7d7d;
  font-size: 0.78rem;
  font-style: italic;
  margin-top: 0.15rem;
}

.agent-event-row.event-answer {
  border-left: 3px solid #10b981;
  background: rgba(16, 185, 129, 0.06);
}

.agent-event-row.event-transcript {
  border-left: 3px solid #3b82f6;
  background: rgba(59, 130, 246, 0.06);
}

.agent-event-row.event-error {
  border-left: 3px solid #ef4444;
  background: rgba(239, 68, 68, 0.06);
}

.provisioning-block {
  margin-top: 1rem;
  padding: 1rem;
  border-radius: 1rem;
  border: 1px solid rgba(196, 199, 199, 0.42);
  background: rgba(255, 255, 255, 0.7);
}

.provisioning-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 0.7rem;
}

.provisioning-head strong {
  font-family: Manrope, sans-serif;
}

.provisioning-steps {
  list-style: none;
  padding: 0;
  margin: 0.5rem 0 0;
  display: grid;
  gap: 0.55rem;
}

.provisioning-steps li {
  display: grid;
  grid-template-columns: 1.1rem 1fr auto;
  gap: 0.7rem;
  align-items: center;
  padding: 0.65rem 0.75rem;
  border-radius: 0.75rem;
  background: rgba(255, 255, 255, 0.5);
  border: 1px solid rgba(196, 199, 199, 0.32);
}

.provisioning-steps li[data-state="success"] {
  border-color: rgba(34, 197, 94, 0.35);
  background: rgba(220, 252, 231, 0.55);
}

.provisioning-steps li[data-state="failed"] {
  border-color: rgba(239, 68, 68, 0.4);
  background: rgba(254, 226, 226, 0.55);
}

.provisioning-steps li[data-state="pending_credentials"],
.provisioning-steps li[data-state="skipped_no_azure_subscription"],
.provisioning-steps li[data-state="skipped_no_shared_vault"] {
  border-color: rgba(217, 119, 6, 0.35);
  background: rgba(254, 243, 199, 0.55);
}

.provisioning-steps li[data-state="running"] {
  border-color: rgba(59, 130, 246, 0.4);
  background: rgba(219, 234, 254, 0.55);
}

.provisioning-steps li[data-state="pending"] {
  opacity: 0.72;
}

.step-marker {
  width: 0.8rem;
  height: 0.8rem;
  border-radius: 999px;
  background: rgba(116, 120, 120, 0.4);
}

.step-marker[data-state="success"] { background: #059669; }
.step-marker[data-state="failed"] { background: #dc2626; }
.step-marker[data-state="running"] {
  background: #2563eb;
  animation: stepPulse 1.2s ease-in-out infinite;
}
.step-marker[data-state="pending_credentials"],
.step-marker[data-state="skipped_no_azure_subscription"],
.step-marker[data-state="skipped_no_shared_vault"] { background: #d97706; }

@keyframes stepPulse {
  0%, 100% { transform: scale(1); opacity: 1; }
  50% { transform: scale(1.25); opacity: 0.65; }
}

.provisioning-steps strong {
  display: block;
  font-family: Manrope, sans-serif;
  font-size: 0.92rem;
  line-height: 1.2;
}

.provisioning-steps small {
  display: block;
  margin-top: 0.18rem;
  color: #5f5f53;
  font-size: 0.78rem;
}

.step-state {
  font-size: 0.7rem;
  font-weight: 800;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: #5f5f53;
}

.org-shell.dark .provisioning-block,
.org-shell.dark .provisioning-steps li {
  background: rgba(28, 31, 24, 0.78);
  border-color: rgba(102, 108, 92, 0.45);
}

.org-shell.dark .provisioning-steps li[data-state="success"] {
  background: rgba(16, 64, 36, 0.6);
  border-color: rgba(52, 211, 153, 0.4);
}

.org-shell.dark .provisioning-steps li[data-state="failed"] {
  background: rgba(92, 36, 31, 0.78);
  border-color: rgba(239, 68, 68, 0.5);
}

.org-shell.dark .provisioning-steps li[data-state="pending_credentials"],
.org-shell.dark .provisioning-steps li[data-state="skipped_no_azure_subscription"] {
  background: rgba(88, 56, 16, 0.6);
  border-color: rgba(245, 158, 11, 0.5);
}

.org-shell.dark .provisioning-steps small {
  color: #c8c7bc;
}

.members-summary {
  display: inline-flex;
  gap: 0.7rem;
  flex-wrap: wrap;
}

.members-summary span {
  padding: 0.45rem 0.65rem;
  border-radius: 999px;
  background: #efeee3;
  color: #444748;
  font-size: 0.76rem;
  font-weight: 700;
}

.invite-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem;
}

.dashboard-invite-form {
  margin-top: 1.15rem;
}

.invite-domain-banner {
  margin-top: 1rem;
  padding: 0.9rem 1rem;
  border-radius: 1rem;
  border: 1px solid rgba(196, 199, 199, 0.4);
  background: linear-gradient(180deg, rgba(239, 238, 227, 0.9), rgba(255, 255, 255, 0.84));
}

.invite-domain-banner span,
.invite-field span {
  display: block;
  margin-bottom: 0.35rem;
  color: #5f5f53;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.invite-domain-banner strong {
  font-family: Manrope, sans-serif;
  font-size: 1rem;
}

.invite-field {
  display: flex;
  flex-direction: column;
}

.invite-action-block {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 0.75rem;
}

.invite-card,
.workspace-card {
  min-height: 100%;
}

.invite-form input,
.invite-form select {
  width: 100%;
  border-radius: 0.85rem;
  border: 1px solid #d9d8ce;
  background: rgba(255, 255, 255, 0.8);
  color: #1b1c15;
  padding: 0.9rem 1rem;
  font-size: 0.95rem;
}

.invite-form button {
  border-radius: 0.9rem;
  border: none;
  background: #1d1c0f;
  color: #ffffff;
  padding: 0.95rem 1.1rem;
  font-weight: 700;
}

.invite-form button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.invite-helper {
  color: #5f5f53;
  font-size: 0.82rem;
  line-height: 1.6;
}

.invite-helper.invalid {
  color: #93000a;
}

.member-table {
  display: grid;
  gap: 0.75rem;
}

.dashboard-member-table {
  margin-top: 1.2rem;
}

.member-row {
  display: grid;
  grid-template-columns: 2.2fr 1fr 1fr 1fr;
  gap: 0.8rem;
  align-items: center;
  padding: 0.85rem 0;
  border-top: 1px solid #ecebdd;
}

.member-head {
  border-top: none;
  color: #5f5f53;
  font-size: 0.82rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: 700;
}

.member-meta {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}

.member-meta strong {
  font-size: 0.98rem;
}

.member-meta small {
  color: #5f5f53;
}

.readonly-tag {
  color: #5f5f53;
  font-size: 0.9rem;
}

.message {
  border-radius: 0.85rem;
  padding: 0.9rem 1rem;
  margin-bottom: 1rem;
  font-size: 0.95rem;
  line-height: 1.5;
}

.message.error {
  background: #ffdad6;
  color: #93000a;
}

.message.info {
  background: #e9e9dd;
  color: #1b1c15;
}

.empty-state {
  padding: 1rem 0;
  color: #5f5f53;
}

.empty-state.compact {
  padding: 0.7rem 0;
  font-size: 0.9rem;
}

.portal-footer {
  position: relative;
  z-index: 1;
  margin-top: auto;
  width: 100%;
  border-top: 1px solid #e4e3d7;
  padding: 1.5rem 2rem;
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  align-items: center;
  color: #747878;
  font-size: 0.78rem;
}

.footer-nav {
  display: flex;
  gap: 1rem;
  flex-wrap: wrap;
}

.footer-nav a {
  color: inherit;
  text-decoration: underline;
  text-decoration-color: #d7d6cb;
  text-underline-offset: 0.22rem;
}

/* ─── Dark theme ─── */

.org-shell.dark {
  background: #131511;
  color: #f2f1e5;
}

.org-shell.dark::before {
  content: "";
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 0;
  background:
    radial-gradient(
      280px circle at var(--cursor-x) var(--cursor-y),
      rgba(255, 255, 255, 0.14),
      rgba(255, 255, 255, 0.06) 28%,
      rgba(255, 255, 255, 0) 72%
    );
  transition: background-position 0.06s linear;
}

.org-shell.dark .orb-top {
  background: rgba(67, 71, 57, 0.82);
}

.org-shell.dark .orb-bottom {
  background: rgba(50, 54, 45, 0.82);
}

.org-shell.dark .mode-link,
.org-shell.dark .dashboard-nav,
.org-shell.dark .dashboard-card,
.org-shell.dark .login-card,
.org-shell.dark .summary-pill,
.org-shell.dark .nav-icon-button,
.org-shell.dark .org-avatar-button,
.org-shell.dark .nav-page-button,
.org-shell.dark .theme-toggle-button,
.org-shell.dark .dashboard-secondary-button,
.org-shell.dark .dashboard-chip-button,
.org-shell.dark .dashboard-inline-button {
  background: rgba(28, 31, 24, 0.84);
  color: #f2f1e5;
  border-color: rgba(102, 108, 92, 0.45);
}

.org-shell.dark .brand-mark,
.org-shell.dark .dashboard-primary-button,
.org-shell.dark .invite-form button,
.org-shell.dark .primary-button {
  background: #f2f1e5;
  color: #131511;
  border-color: #f2f1e5;
}

.org-shell.dark .nav-search input,
.org-shell.dark .db-input,
.org-shell.dark .totp-input,
.org-shell.dark .invite-form input,
.org-shell.dark .invite-form select {
  background: rgba(17, 19, 15, 0.92);
  color: #f2f1e5;
  border-color: rgba(102, 108, 92, 0.4);
}

.org-shell.dark .brand-copy span,
.org-shell.dark .dashboard-header p,
.org-shell.dark .section-kicker,
.org-shell.dark .summary-pill span,
.org-shell.dark .organization-description,
.org-shell.dark .compact-card-head p,
.org-shell.dark .members-card-head p,
.org-shell.dark .dashboard-detail-list dt,
.org-shell.dark .member-meta small,
.org-shell.dark .empty-state,
.org-shell.dark .invite-helper,
.org-shell.dark .invite-domain-banner span,
.org-shell.dark .invite-field span,
.org-shell.dark .portal-footer,
.org-shell.dark .footer-nav a,
.org-shell.dark .login-help,
.org-shell.dark .footer-links p {
  color: #b8b7ab;
}

.org-shell.dark .organization-mark,
.org-shell.dark .compact-icon-shell,
.org-shell.dark .status-chip,
.org-shell.dark .invite-domain-banner,
.org-shell.dark .usage-track {
  background: rgba(33, 37, 30, 0.92);
  border-color: rgba(102, 108, 92, 0.35);
}

.org-shell.dark .provider-option {
  background: linear-gradient(180deg, rgba(36, 40, 33, 0.98), rgba(24, 27, 22, 0.96));
  border-color: rgba(144, 150, 136, 0.55);
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.03);
}

.org-shell.dark .provider-option:hover {
  border-color: rgba(215, 219, 208, 0.75);
  background: linear-gradient(180deg, rgba(45, 49, 41, 0.98), rgba(29, 33, 27, 0.98));
}

.org-shell.dark .provider-name {
  color: #f6f5ea;
}

.org-shell.dark .provider-option small {
  color: #d0d0c3;
}

.org-shell.dark .usage-fill {
  background: #f2f1e5;
}

.org-shell.dark .nav-page-button.active {
  background: #f2f1e5;
  color: #131511;
  border-color: #f2f1e5;
}

.org-shell.dark .provider-option.active {
  background: linear-gradient(180deg, rgba(67, 74, 61, 0.98), rgba(45, 50, 41, 0.98));
  box-shadow:
    inset 0 0 0 1px #f2f1e5,
    0 0 0 1px rgba(255, 255, 255, 0.08),
    0 12px 28px -20px rgba(255, 255, 255, 0.35);
  border-color: #f2f1e5;
}

.org-shell.dark .message.info {
  background: rgba(51, 56, 45, 0.95);
  color: #f2f1e5;
}

.org-shell.dark .agent-result-panel,
.org-shell.dark .agent-event-row,
.org-shell.dark .agent-document-row {
  background: rgba(28, 31, 24, 0.78);
  color: #f2f1e5;
  border-color: rgba(102, 108, 92, 0.45);
}

.org-shell.dark .agent-result-panel p,
.org-shell.dark .agent-event-row p,
.org-shell.dark .agent-event-row span,
.org-shell.dark .agent-document-main small {
  color: #c8c7bc;
}

.org-shell.dark .message.error {
  background: rgba(92, 36, 31, 0.92);
  color: #ffd9d4;
}

@media (max-width: 900px) {
  .dashboard-nav,
  .dashboard-header,
  .dashboard-summary-bar,
  .dashboard-section-head {
    grid-template-columns: 1fr;
    flex-direction: column;
    align-items: stretch;
  }

  .dashboard-grid {
    grid-template-columns: 1fr;
  }

  .overview-grid .organization-card,
  .overview-grid .access-card,
  .control-grid .invite-card,
  .control-grid .workspace-card {
    grid-column: auto;
  }

  .invite-form,
  .member-row {
    grid-template-columns: 1fr;
  }

  .provider-grid,
  .provider-grid-dual {
    grid-template-columns: 1fr 1fr;
  }

  .agent-page-grid,
  .agent-console-grid {
    grid-template-columns: 1fr;
  }

  .member-head {
    display: none;
  }
}

@media (max-width: 720px) {
  .portal-footer {
    flex-direction: column;
    align-items: flex-start;
  }

  .mode-bar {
    padding: 1rem 1rem 0;
  }

  .floating-top-nav {
    top: 0.9rem;
    padding: 0 1rem;
  }

  .login-layout,
  .workspace-layout {
    padding: 1.2rem 1rem 2rem;
  }

  .dashboard-layout {
    padding-top: 6.8rem;
  }

  .login-card,
  .dashboard-card {
    padding: 1.2rem;
  }

  .dashboard-header-actions,
  .dashboard-filter-actions,
  .dashboard-nav-actions,
  .members-card-head {
    width: 100%;
    justify-content: flex-start;
    flex-wrap: wrap;
  }

  .dashboard-section-head p {
    text-align: left;
  }

  .nav-search {
    order: 3;
  }

  .theme-toggle-button {
    width: auto;
  }

  .dashboard-nav {
    padding: 0.95rem;
  }

  .organization-metrics,
  .workspace-profile-grid {
    grid-template-columns: 1fr;
  }
}
</style>
