<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import axios from 'axios';
import QrcodeVue from 'qrcode.vue';
import nokvoLogo from '../assets/nokvo-one-logo.png';
import {
  Activity,
  Bell,
  BookOpen,
  Bot,
  Brain,
  CalendarDays,
  CalendarOff,
  CheckCircle2,
  Clock,
  ChevronLeft,
  ChevronRight,
  Cpu,
  Database,
  FileText,
  Globe,
  Layers,
  LogOut,
  MessageSquare,
  Mic,
  MicOff,
  Moon,
  PhoneCall,
  Play,
  Plug,
  Plus,
  Radio,
  Search,
  Settings2,
  Shield,
  Square,
  SunMedium,
  SunMedium as Sun,
  Trash2,
  Upload,
  UserPlus,
  Users,
  Volume2,
  Wrench,
  XCircle,
} from 'lucide-vue-next';

const API_BASE_URL = 'http://localhost:8000/api/nokvo-one';
const ACCESS_TOKEN_KEY = 'nokvo_one_access_token';
const REFRESH_TOKEN_KEY = 'nokvo_one_refresh_token';
const THEME_KEY = 'nokvo_one_theme_mode';

const props = defineProps({
  initialAuthState: {
    type: String,
    default: 'login',
  },
});
defineEmits(['switch-mode']);
const router = useRouter();

const api = axios.create({ baseURL: API_BASE_URL });
const connectApi = axios.create({ baseURL: 'http://localhost:8000/api/nokvo-one/connect' });

const orgShellRef = ref(null);
const themeMode = ref(localStorage.getItem(THEME_KEY) || 'light');
const cursorTimer = ref(null);
const authConfig = ref(null);
const googleLoginButtonRef = ref(null);
const googleSignupButtonRef = ref(null);

const authState = ref(props.initialAuthState || 'login'); // login | signup | check_email | mfa_setup | mfa_verify | login_totp | accept_invite | business_type_setup | outcome_setup | sample_upload | ready
const onboardingV2Enabled = ref(false);
const outcomeWizard = ref({
  outcomes: [],
  selected: {},
  agentName: '',
  isSaving: false,
});
const sampleUpload = ref({
  mode: 'document',          // 'document' | 'prompt'
  file: null,
  prompt: '',
  isUploading: false,
});
const settingsMenuOpen = ref(false);

// ── Nokvo Connect — API key management state ────────────────────────────────
const connect = ref({
  isLoadingList: false,
  isCreating: false,
  keys: [],
  errorMsg: '',
  newKeySecret: '',
  newWebhookSecret: '',
  draft: {
    label: '',
    mode: 'live',
    rate_limit_rpm: 60,
    max_concurrent_sessions: 5,
    allowed_origins_raw: '',
    webhook_url: '',
  },
});

const loadConnectKeys = async () => {
  if (authState.value !== 'ready') return;
  connect.value.isLoadingList = true;
  connect.value.errorMsg = '';
  try {
    const { data } = await connectApi.get('/api-keys', { headers: authHeader() });
    connect.value.keys = Array.isArray(data) ? data : [];
  } catch (exc) {
    connect.value.errorMsg = exc?.response?.data?.detail || 'Failed to load API keys.';
  } finally {
    connect.value.isLoadingList = false;
  }
};

const createConnectKey = async () => {
  connect.value.errorMsg = '';
  connect.value.newKeySecret = '';
  connect.value.newWebhookSecret = '';
  connect.value.isCreating = true;
  try {
    const draft = connect.value.draft;
    const allowed = (draft.allowed_origins_raw || '')
      .split('\n')
      .map((line) => line.trim())
      .filter((line) => line.length > 0);
    const payload = {
      label: draft.label,
      mode: draft.mode || 'live',
      rate_limit_rpm: Number(draft.rate_limit_rpm) || 60,
      max_concurrent_sessions: Number(draft.max_concurrent_sessions) || 5,
      allowed_origins: allowed,
    };
    if (draft.webhook_url) payload.webhook_url = draft.webhook_url;
    const { data } = await connectApi.post('/api-keys', payload, { headers: authHeader() });
    connect.value.newKeySecret = data.secret;
    connect.value.newWebhookSecret = data.webhook_secret || '';
    connect.value.draft.label = '';
    connect.value.draft.webhook_url = '';
    connect.value.draft.allowed_origins_raw = '';
    await loadConnectKeys();
  } catch (exc) {
    connect.value.errorMsg = exc?.response?.data?.detail || 'Failed to mint API key.';
  } finally {
    connect.value.isCreating = false;
  }
};

const revokeConnectKey = async (keyId) => {
  if (!keyId) return;
  connect.value.errorMsg = '';
  try {
    await connectApi.post(`/api-keys/${keyId}/revoke`, {}, { headers: authHeader() });
    await loadConnectKeys();
  } catch (exc) {
    connect.value.errorMsg = exc?.response?.data?.detail || 'Failed to revoke API key.';
  }
};

const dismissConnectSecret = () => {
  connect.value.newKeySecret = '';
  connect.value.newWebhookSecret = '';
};
const errorMsg = ref('');
const infoMsg = ref('');
const isAuthenticating = ref(false);
const currentPage = ref('dashboard'); // dashboard | members | tickets | leads | appointments | agent | outgoing_agent | knowledge_base

const signup = ref({ org_name: '', admin_name: '', admin_email: '', password: '' });
const login = ref({ email: '', password: '' });
const totpCode = ref('');
const totpUri = ref('');
const totpSecret = ref('');
const mfaSetupMode = ref('signup'); // signup | session_setup | session_verify
const setupToken = ref('');
const loginTempToken = ref('');

const currentUser = ref(null);
const currentOrganization = ref(null);
const members = ref([]);
const assignmentSettings = ref([]);
const clinicScheduleSettings = ref({});
const blockedSlots = ref({});
const tabRecords = ref({ leads: [], tickets: [], appointments: [] });
const tabRecordsLoading = ref({});
const timetableViewer = ref({ member: null, isLoading: false, selectedDate: '', visibleMonth: '' });
const agents = ref([]);
const predefinedTools = ref([]);
const toolCatalogGroups = ref([]);
const toolCatalogDefaults = ref([]);
const customTabs = ref([]);
const customTabActionInProgress = ref(false);
const newCustomTab = ref({
  label: '',
  slug: '',
  statusList: 'open,in_progress,done,archived',
  fields: [{ key: 'name', label: 'Name', type: 'text', required: true }],
});
const activeAgent = ref(null);
const chatLog = ref([]);
const chatInput = ref('');
const emailDrafts = ref([]);
const provisioning = ref(null);
const kbDocuments = ref([]);
const kbForm = ref({
  name: '',
  document_type: 'policy',
  description: '',
  tags: '',
  file: null,
});
const kbSinglePromptConfig = ref(null);
const kbSinglePromptForm = ref({
  prompt: '',
});
const kbUploadInputRef = ref(null);
const isLoadingKb = ref(false);
const isUploadingKb = ref(false);
const isSavingSinglePromptAgent = ref(false);
const isDisablingSinglePromptAgent = ref(false);
const isReconcilingKb = ref(false);
const kbError = ref('');
const kbInfo = ref('');
const kbQuery = ref('');
const kbResults = ref([]);
const isSearchingKb = ref(false);
const runtimeStatus = ref(null);
const phoneLink = ref(null);
const phoneLinkInput = ref('');
const isSavingPhoneLink = ref(false);
const campaigns = ref([]);
const defaultCampaignObjectives = [
  'Confirm this is a good time to talk.',
  'Briefly explain why we are calling.',
  'Understand whether the lead is interested and what they need.',
  'Capture the next step: appointment, callback, site visit, demo, or opt-out.',
].join('\n');
const defaultCampaignExitConditions = [
  'Lead asks not to be called again.',
  'Lead says they are not interested.',
  'Lead says this is the wrong number.',
  'Lead is busy and asks for a callback.',
].join('\n');
const emptyCampaignForm = () => ({
  name: '',
  from_number: '',
  doc_file: null,
  agent_prompt: 'You are making a consented outbound call. Be concise, explain the reason for the call, and guide the lead toward one clear next step.',
  objectives: defaultCampaignObjectives,
  exit_conditions: defaultCampaignExitConditions,
  tone: 'warm, direct, and respectful',
  silence_timeout_seconds: 5,
});
const campaignForm = ref(emptyCampaignForm());
const isCreatingCampaign = ref(false);
const isLaunchingCampaign = ref(null);
const outgoingTab = ref('leads');
const leadConnections = ref([]);
const leadForms = ref([]);
const outgoingLeads = ref([]);
const selectedLeadIds = ref([]);
const isLoadingLeadSources = ref(false);
const isSyncingLeadConnection = ref(null);
const pendingLeadOAuth = ref(null);
const connectionAccountInputs = ref({});
const nokvoLeadForm = ref({
  name: '',
  consent_text: 'I agree to receive a phone call from this business about my enquiry.',
  fields: [
    { key: 'email', label: 'Email', type: 'email', required: false },
  ],
});
const externalLeadForm = ref({
  provider: 'google_forms',
  name: '',
  provider_form_id: '',
  source_connection_id: '',
  field_mapping: '{\n  "name": "name",\n  "phone": "phone",\n  "email": "email"\n}',
  consent_field_key: '',
  consent_text: '',
  default_call_consent: false,
});
const selectedCallableLeads = computed(() =>
  outgoingLeads.value.filter((lead) => selectedLeadIds.value.includes(lead.id) && lead.callable),
);
const voice = ref({
  ws: null,
  audioCtx: null,
  micStream: null,
  micNode: null,
  status: 'idle', // idle | connecting | listening | thinking | speaking | error
  callId: null,
  language: 'en',
  liveTranscript: '',
  transcriptLang: '',
  turns: [], // { id, query, sentences[], answer, latencyMs, cacheHit, citations[] }
  // Gapless playback state (see decodeAndSchedule / stopAllPlayback)
  playbackGeneration: 0,
  scheduledSources: [],
  pendingPlaybackChunks: 0,
  nextPlaybackTime: 0,
  playbackChain: Promise.resolve(),
  errorMsg: '',
  firstSentenceMs: null,
  ttsFirstAudioMs: null,
  ambienceAudio: null,
  ambienceGain: null,
});
const voiceLanguageOptions = [
  { value: 'en', label: 'English' },
  { value: 'hi', label: 'Hindi' },
  { value: 'ta', label: 'Tamil' },
  { value: 'te', label: 'Telugu' },
  { value: 'bn', label: 'Bengali' },
  { value: 'mr', label: 'Marathi' },
  { value: 'kn', label: 'Kannada' },
  { value: 'ml', label: 'Malayalam' },
  { value: 'gu', label: 'Gujarati' },
  { value: 'pa', label: 'Punjabi' },
];
const kbDocumentTypes = [
  { value: 'policy', label: 'Policy' },
  { value: 'faq', label: 'FAQ' },
  { value: 'script', label: 'Script' },
  { value: 'compliance', label: 'Compliance' },
  { value: 'product_docs', label: 'Product Docs' },
  { value: 'training', label: 'Training' },
  { value: 'other', label: 'Other' },
];
const inviteForm = ref({ email: '', full_name: '', role: 'member' });
const newAgent = ref({ name: '', description: '', system_prompt: '', tool_keys: [] });
const isSavingMember = ref(false);
const isLoadingMembers = ref(false);
const businessTypeOptions = ref([]);
const organizationBusinessTemplate = ref(null);
const selectedBusinessType = ref('');
const isSavingBusinessType = ref(false);
const fieldEditor = ref({
  key: null,
  title: '',
  fields: [],
  isSaving: false,
});
const assignmentEditor = ref({
  member: null,
  settings: null,
  clinic: null,
  blockedSlot: { date: '', start_time: '', end_time: '', reason: '' },
  isSaving: false,
  isSavingClinic: false,
  isSavingBlock: false,
});

const inviteToken = ref('');
const inviteContext = ref(null);
const invitePassword = ref('');

const themeToggleLabel = computed(() => (themeMode.value === 'dark' ? 'Light Mode' : 'Dark Mode'));

const organizationInitial = computed(
  () => (currentOrganization.value?.name || 'N').trim().charAt(0).toUpperCase(),
);

const inviteDomain = computed(() => currentOrganization.value?.email_domain || '');
const currentBusinessTemplate = computed(() =>
  organizationBusinessTemplate.value
  || businessTypeOptions.value.find((option) => option.value === currentOrganization.value?.industry)
  || null,
);
const businessTypeLabel = computed(() => currentBusinessTemplate.value?.label || 'Not selected');
const memberPageLabel = computed(() => currentBusinessTemplate.value?.member_label || 'Members');
const businessTypeRequired = computed(() => !currentOrganization.value?.industry);
const businessTemplateTabs = computed(() => currentBusinessTemplate.value?.tabs || []);
const showAppointmentsTab = computed(() => businessTemplateTabs.value.includes('appointments'));
const isClinicTemplate = computed(() => currentOrganization.value?.industry === 'clinics');
const schemaFor = (key) => currentBusinessTemplate.value?.schemas?.[key] || [];
const fieldTypes = ['text', 'phone', 'email', 'number', 'currency', 'date', 'datetime', 'select'];
const scheduleDays = [
  { value: 'mon', label: 'Mon', full: 'Monday' },
  { value: 'tue', label: 'Tue', full: 'Tuesday' },
  { value: 'wed', label: 'Wed', full: 'Wednesday' },
  { value: 'thu', label: 'Thu', full: 'Thursday' },
  { value: 'fri', label: 'Fri', full: 'Friday' },
  { value: 'sat', label: 'Sat', full: 'Saturday' },
  { value: 'sun', label: 'Sun', full: 'Sunday' },
];
const requestTypeOptions = computed(() => currentBusinessTemplate.value?.request_types || []);
const consultationTypeOptions = computed(() => currentBusinessTemplate.value?.consultation_types || []);
const allScheduleDayValues = computed(() => scheduleDays.map((day) => day.value));
const scheduleDefaultForBusiness = computed(() => {
  const businessType = currentOrganization.value?.industry;
  if (businessType === 'clinics') {
    return { days: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat'], start: '10:00', end: '21:00', label: 'Clinic day' };
  }
  if (businessType === 'real_estate') {
    return { days: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'], start: '09:00', end: '20:00', label: 'Agent day' };
  }
  if (businessType === 'hospitality') {
    return { days: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'], start: '08:00', end: '22:00', label: 'Service day' };
  }
  return { days: ['mon', 'tue', 'wed', 'thu', 'fri'], start: '09:00', end: '18:00', label: 'Office day' };
});
const scheduleTimePresets = computed(() => {
  const preferred = scheduleDefaultForBusiness.value;
  return [
    preferred,
    { label: 'Office day', start: '09:00', end: '18:00' },
    { label: 'Morning', start: '09:00', end: '13:00' },
    { label: 'Evening', start: '14:00', end: '20:00' },
  ].filter((preset, index, list) => list.findIndex((item) => item.start === preset.start && item.end === preset.end) === index);
});
const requestTypeValues = computed(() => requestTypeOptions.value.map((type) => type.value));
const consultationTypeValues = computed(() => consultationTypeOptions.value.map((type) => type.value));
const businessPromptPlaceholder = computed(() => {
  if (!currentBusinessTemplate.value) return 'Business Type rules will be injected after setup. Add agent-specific tone, escalation, and workflow details here.';
  return `${currentBusinessTemplate.value.label} rules are injected automatically. Add agent-specific tone, escalation, and workflow details here.`;
});
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

const filteredMembers = computed(() => {
  // Removed members stay in the table for audit but are hidden from the
  // default roster — they're not real teammates anymore.
  const list = members.value.filter((m) => m.status !== 'removed');
  list.sort((a, b) => String(a.full_name || '').localeCompare(String(b.full_name || '')));
  return list;
});

const serviceStatusIsBad = (status) => {
  const value = String(status || '').toLowerCase();
  return ['error', 'failed', 'offline', 'unhealthy', 'degraded'].includes(value);
};

const organizationHealth = computed(() => {
  const approvedDocs = kbStats.value.approved;
  const kbIssueCount = kbStats.value.pending + kbStats.value.errors;
  const runtimeServices = [runtimeStatus.value?.stt, runtimeStatus.value?.llm, runtimeStatus.value?.tts].filter(Boolean);
  const runtimeHasBadStatus = runtimeServices.some((service) => serviceStatusIsBad(service.status));
  const callableLeadCount = outgoingLeads.value.filter((lead) => lead.callable).length;
  const provisioningStatus = String(provisioning.value?.provisioning_status || '').toLowerCase();

  const checks = [
    {
      key: 'security',
      label: 'Access security',
      state: currentUser.value?.mfa_pending ? 'blocked' : 'good',
      detail: currentUser.value?.mfa_pending ? 'MFA is required before advanced actions unlock.' : 'MFA is active for this workspace.',
    },
    {
      key: 'agent',
      label: 'Agent readiness',
      state: agents.value.length ? 'good' : 'blocked',
      detail: agents.value.length ? 'At least one agent is configured and ready to test.' : 'Create an agent before live workflows can run.',
    },
    {
      key: 'knowledge',
      label: 'Knowledge base',
      state: approvedDocs ? (kbIssueCount ? 'warn' : 'good') : 'warn',
      detail: approvedDocs
        ? `${approvedDocs} approved document${approvedDocs === 1 ? '' : 's'}${kbIssueCount ? `, ${kbIssueCount} item${kbIssueCount === 1 ? '' : 's'} need review.` : '.'}`
        : 'No approved knowledge documents yet.',
    },
    {
      key: 'runtime',
      label: 'Voice runtime',
      state: runtimeHasBadStatus ? 'blocked' : (runtimeStatus.value ? 'good' : 'warn'),
      detail: runtimeHasBadStatus ? 'One or more voice services are reporting errors.' : (runtimeStatus.value ? 'STT, LLM, and TTS status checked.' : 'Runtime status has not reported yet.'),
    },
    {
      key: 'outbound',
      label: 'Outbound readiness',
      state: currentOrganization.value?.calling_enabled
        ? (leadConnections.value.length && callableLeadCount ? 'good' : 'warn')
        : 'blocked',
      detail: currentOrganization.value?.calling_enabled
        ? (leadConnections.value.length ? `${callableLeadCount} callable lead${callableLeadCount === 1 ? '' : 's'} available.` : 'Connect a consented lead source before outbound campaigns.')
        : 'Calling is still gated for this organization.',
    },
    {
      key: 'provisioning',
      label: 'Provisioning',
      state: provisioningStatus === 'success' ? 'good' : (provisioningStatus === 'failed' ? 'blocked' : 'warn'),
      detail: provisioningStatus === 'success' ? 'Tenant infrastructure is provisioned.' : (provisioningStatus || 'Provisioning status unavailable.'),
    },
  ];

  const valueForState = { good: 1, warn: 0.55, blocked: 0 };
  const score = Math.round((checks.reduce((sum, check) => sum + valueForState[check.state], 0) / checks.length) * 100);
  const label = score >= 85 ? 'Healthy' : (score >= 60 ? 'Needs attention' : 'Action required');
  const state = score >= 85 ? 'good' : (score >= 60 ? 'warn' : 'blocked');
  return {
    checks,
    score,
    label,
    state,
    openIssues: checks.filter((check) => check.state !== 'good').length,
    ringStyle: `conic-gradient(${state === 'good' ? '#2f6d3a' : state === 'warn' ? '#b7791f' : '#b42318'} ${score}%, #e7e6dc 0)`,
  };
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

const switchPage = (page) => {
  if (page === 'appointments' && !showAppointmentsTab.value) return;
  currentPage.value = page;
  errorMsg.value = '';
  infoMsg.value = '';
  if (page === 'my_timetable') {
    loadMyTimetable();
    return;
  }
  if (page === 'knowledge_base') {
    loadKnowledgeDocuments();
    loadSinglePromptAgent();
  }
  if (page === 'agent') {
    loadRuntimeStatus();
    loadPhoneLink();
  }
  if (page === 'outgoing_agent') {
    loadOutgoingAgentWorkspace();
    loadCampaigns();
  }
  if (['leads', 'tickets', 'appointments'].includes(page)) {
    loadTabRecords(page);
  }
  if (page === 'nokvo_connect_step2') {
    loadConnectKeys();
  }
};

const scrollToDashboardMembers = async () => {
  currentPage.value = 'dashboard';
  errorMsg.value = '';
  infoMsg.value = '';
  await nextTick();
  document.getElementById('dashboard-members')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
};

const resetLoginState = () => {
  errorMsg.value = '';
  infoMsg.value = '';
  setupToken.value = '';
  totpUri.value = '';
  totpSecret.value = '';
  totpCode.value = '';
  mfaSetupMode.value = 'signup';
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
  if (!detail && err?.response?.data?.message) return err.response.data.message;
  if (!detail && err?.message && !err.response) return err.message;
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
      await enterWorkspaceAfterAuth();
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
    onboardingV2Enabled.value = !!data?.onboarding_v2_enabled;
  } catch (_) {
    authConfig.value = { google_client_id: '', google_login_enabled: false };
    onboardingV2Enabled.value = false;
  }
};

const loadBusinessTypeOptions = async () => {
  if (businessTypeOptions.value.length) return;
  try {
    const { data } = await api.get('/business-template/options');
    businessTypeOptions.value = data;
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Failed to load Business Type options.');
  }
};

const loadBusinessTemplate = async () => {
  if (businessTypeRequired.value) {
    organizationBusinessTemplate.value = null;
    return;
  }
  try {
    const { data } = await api.get('/business-template', { headers: authHeader() });
    organizationBusinessTemplate.value = data;
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Failed to load Business Type fields.');
  }
};

const enterWorkspaceAfterAuth = async () => {
  // Member-role users skip the admin onboarding gates entirely and land
  // straight on their own timetable. Business-type setup is an
  // admin-only concern (the admin completes it before sending invites),
  // and ``loadWorkspace`` pulls a bunch of admin-scoped resources we
  // don't want the member dashboard depending on.
  if (isMemberOnly.value) {
    authState.value = 'ready';
    currentPage.value = 'my_timetable';
    await loadMyTimetable();
    return;
  }
  await loadBusinessTypeOptions();
  if (businessTypeRequired.value) {
    selectedBusinessType.value = '';
    authState.value = 'business_type_setup';
    return;
  }
  await loadBusinessTemplate();
  authState.value = 'ready';
  await loadWorkspace();
};

const loadWorkspace = async () => {
  isLoadingMembers.value = true;
  try {
    const [m, a, t, p, s, c, r, k, lc, lf, ll] = await Promise.allSettled([
      api.get('/members/', { headers: authHeader() }),
      api.get('/agents/', { headers: authHeader() }),
      api.get('/agents/tools/catalog', { headers: authHeader() }),
      api.get('/me/provisioning', { headers: authHeader() }),
      api.get('/members/assignment-settings', { headers: authHeader() }),
      api.get('/business-template/custom-tabs', { headers: authHeader() }),
      api.get('/agents/runtime/status', { headers: authHeader() }),
      api.get('/knowledge-base/documents', { headers: authHeader() }),
      api.get('/agents/lead-sources/connections', { headers: authHeader() }),
      api.get('/agents/lead-sources/forms', { headers: authHeader() }),
      api.get('/agents/lead-sources/leads', { headers: authHeader(), params: { limit: 300 } }),
    ]);
    if (c.status === 'fulfilled') customTabs.value = c.value.data || [];
    if (m.status === 'fulfilled') members.value = m.value.data;
    if (a.status === 'fulfilled') agents.value = a.value.data;
    if (t.status === 'fulfilled') {
      const catalog = t.value.data || {};
      toolCatalogGroups.value = catalog.groups || [];
      toolCatalogDefaults.value = catalog.default_tool_keys || [];
      predefinedTools.value = (catalog.groups || []).flatMap((g) => g.tools || []);
      if (!newAgent.value.tool_keys || !newAgent.value.tool_keys.length) {
        newAgent.value.tool_keys = [...toolCatalogDefaults.value];
      }
    }
    if (p.status === 'fulfilled') provisioning.value = p.value.data;
    if (s.status === 'fulfilled') assignmentSettings.value = s.value.data;
    if (r.status === 'fulfilled') runtimeStatus.value = r.value.data;
    if (k.status === 'fulfilled') kbDocuments.value = k.value.data?.documents || [];
    if (lc.status === 'fulfilled') leadConnections.value = lc.value.data || [];
    if (lf.status === 'fulfilled') leadForms.value = lf.value.data || [];
    if (ll.status === 'fulfilled') outgoingLeads.value = ll.value.data || [];
    if (m.status === 'fulfilled') await loadMemberScheduleExtras(m.value.data);
    if (['leads', 'tickets', 'appointments'].includes(currentPage.value)) {
      await loadTabRecords(currentPage.value);
    }
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Failed to load workspace.');
  } finally {
    isLoadingMembers.value = false;
  }
};

const loadTabRecords = async (tab) => {
  if (!tab) return;
  if (tab === 'appointments' && !showAppointmentsTab.value) return;
  tabRecordsLoading.value = { ...tabRecordsLoading.value, [tab]: true };
  try {
    const { data } = await api.get(`/agents/records/tab/${tab}`, {
      headers: authHeader(),
      params: { limit: 100 },
    });
    tabRecords.value = {
      ...tabRecords.value,
      [tab]: Array.isArray(data) ? data : [],
    };
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, `Could not load ${tab} records.`);
  } finally {
    tabRecordsLoading.value = { ...tabRecordsLoading.value, [tab]: false };
  }
};

const loadMemberScheduleExtras = async (memberList = members.value) => {
  const scheduleEntries = {};
  const slotEntries = {};
  await Promise.allSettled(
    memberList.map(async (member) => {
      const requests = [
        isClinicTemplate.value
          ? api.get(`/members/${member.id}/clinic-schedule-settings`, { headers: authHeader() })
          : Promise.resolve({ data: clinicForMember(member.id) }),
        api.get(`/members/${member.id}/blocked-slots`, { headers: authHeader() }),
      ];
      const [schedule, slots] = await Promise.allSettled(requests);
      if (schedule.status === 'fulfilled') scheduleEntries[member.id] = schedule.value.data;
      if (slots.status === 'fulfilled') slotEntries[member.id] = slots.value.data;
    }),
  );
  clinicScheduleSettings.value = scheduleEntries;
  blockedSlots.value = slotEntries;
};

const restoreSession = async () => {
  const token = localStorage.getItem(ACCESS_TOKEN_KEY);
  if (!token) return false;
  try {
    const { data } = await api.get('/me', { headers: { Authorization: `Bearer ${token}` } });
    currentUser.value = data.user;
    currentOrganization.value = data.organization;
    await enterWorkspaceAfterAuth();
    return true;
  } catch (_) {
    localStorage.removeItem(ACCESS_TOKEN_KEY);
    localStorage.removeItem(REFRESH_TOKEN_KEY);
    return false;
  }
};

const saveBusinessType = async () => {
  if (!selectedBusinessType.value) return;
  errorMsg.value = '';
  infoMsg.value = '';
  isSavingBusinessType.value = true;
  try {
    const { data } = await api.post(
      '/business-template',
      { business_type: selectedBusinessType.value },
      { headers: authHeader() },
    );
    currentOrganization.value = data.organization;
    if (data.business_template) {
      organizationBusinessTemplate.value = data.business_template;
      const idx = businessTypeOptions.value.findIndex((option) => option.value === data.business_template.value);
      if (idx >= 0) businessTypeOptions.value[idx] = data.business_template;
      else businessTypeOptions.value.push(data.business_template);
    }
    infoMsg.value = `Business Type set to ${data.business_template?.label || businessTypeLabel.value}.`;
    if (onboardingV2Enabled.value) {
      await loadWorkspace();
      await beginOutcomeWizard();
      return;
    }
    authState.value = 'ready';
    await loadWorkspace();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Business Type could not be saved.');
  } finally {
    isSavingBusinessType.value = false;
  }
};

const beginOutcomeWizard = async () => {
  errorMsg.value = '';
  try {
    const { data } = await api.get('/agents/outcomes', { headers: authHeader() });
    const opts = data?.outcomes || [];
    const selected = {};
    opts.forEach((o) => {
      selected[o.slug] = !!o.default_on;
    });
    outcomeWizard.value = {
      outcomes: opts,
      selected,
      agentName: data?.default_agent_name || '',
      isSaving: false,
    };
    authState.value = 'outcome_setup';
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Could not load setup options.');
    authState.value = 'ready';
  }
};

const toggleWizardOutcome = (slug) => {
  outcomeWizard.value.selected[slug] = !outcomeWizard.value.selected[slug];
};

const submitOutcomeWizard = async () => {
  if (outcomeWizard.value.isSaving) return;
  outcomeWizard.value.isSaving = true;
  errorMsg.value = '';
  try {
    const selected = Object.entries(outcomeWizard.value.selected)
      .filter(([, on]) => on)
      .map(([slug]) => slug);
    const { data } = await api.post(
      '/agents/from-outcomes',
      { outcomes: selected, agent_name: outcomeWizard.value.agentName || null },
      { headers: authHeader() },
    );
    agents.value.unshift(data);
    activeAgent.value = data;
    // Pre-fill the prompt textarea with the materialized starter prompt so the
    // user sees the agent is already configured — they can keep, tweak, or replace.
    sampleUpload.value.prompt = data?.system_prompt || '';
    infoMsg.value = `Your agent "${data.name}" is ready to test.`;
    authState.value = 'sample_upload';
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Could not create your starter agent.');
  } finally {
    outcomeWizard.value.isSaving = false;
  }
};

const skipSampleUpload = () => {
  sampleUpload.value = { mode: 'document', file: null, prompt: '', isUploading: false };
  authState.value = 'ready';
};

const handleSampleFileChange = (event) => {
  const file = (event.target?.files || [])[0] || null;
  sampleUpload.value.file = file;
};

const setSampleUploadMode = (mode) => {
  if (sampleUpload.value.isUploading) return;
  sampleUpload.value.mode = mode;
};

const submitSampleUpload = async () => {
  if (sampleUpload.value.isUploading) return;
  const mode = sampleUpload.value.mode;
  if (mode === 'document') {
    if (!sampleUpload.value.file) {
      skipSampleUpload();
      return;
    }
    sampleUpload.value.isUploading = true;
    errorMsg.value = '';
    try {
      const file = sampleUpload.value.file;
      const content_base64 = await fileToBase64(file);
      await kbApi.post(
        '/documents/upload',
        {
          name: file.name,
          document_type: 'general',
          description: 'Uploaded during onboarding',
          tags: [],
          filename: file.name,
          content_type: file.type || null,
          content_base64,
        },
        { headers: authHeader() },
      );
      infoMsg.value = 'Your starter document is being indexed in the background.';
      skipSampleUpload();
    } catch (err) {
      errorMsg.value = extractErrorMessage(err, 'Could not upload the starter document.');
    } finally {
      sampleUpload.value.isUploading = false;
    }
    return;
  }
  // mode === 'prompt'
  const prompt = (sampleUpload.value.prompt || '').trim();
  if (prompt.length < 20) {
    errorMsg.value = 'Prompt must be at least 20 characters.';
    return;
  }
  sampleUpload.value.isUploading = true;
  errorMsg.value = '';
  try {
    await kbApi.post(
      '/single-prompt-agent',
      { prompt },
      { headers: authHeader() },
    );
    infoMsg.value = 'Your single-prompt agent is configured. You can refine it later in Settings.';
    skipSampleUpload();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Could not save the single-prompt setup.');
  } finally {
    sampleUpload.value.isUploading = false;
  }
};

const cloneFields = (key) => schemaFor(key).map((field) => ({ ...field }));

const startFieldEdit = (key, title) => {
  fieldEditor.value = {
    key,
    title,
    fields: cloneFields(key),
    isSaving: false,
  };
};

const closeFieldEdit = () => {
  fieldEditor.value = { key: null, title: '', fields: [], isSaving: false };
};

const addCustomTabField = () => {
  newCustomTab.value.fields.push({ key: '', label: '', type: 'text', required: false });
};

const removeCustomTabField = (idx) => {
  if (newCustomTab.value.fields.length <= 1) return;
  newCustomTab.value.fields.splice(idx, 1);
};

const _slugify = (value) =>
  String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, '_')
    .replace(/[^a-z0-9_]/g, '')
    .slice(0, 32);

const _parseStatusList = (value) =>
  String(value || '')
    .split(',')
    .map((s) => _slugify(s))
    .filter(Boolean);

const _resetNewCustomTab = () => {
  newCustomTab.value = {
    label: '',
    slug: '',
    statusList: 'open,in_progress,done,archived',
    fields: [{ key: 'name', label: 'Name', type: 'text', required: true }],
  };
};

const submitCustomTab = async () => {
  errorMsg.value = '';
  if (customTabActionInProgress.value) return;
  const label = (newCustomTab.value.label || '').trim();
  const slug = _slugify(newCustomTab.value.slug || newCustomTab.value.label);
  if (!label || !slug) {
    errorMsg.value = 'Label and slug are required.';
    return;
  }
  const fields = (newCustomTab.value.fields || [])
    .map((field) => ({
      key: _slugify(field.key || field.label),
      label: (field.label || field.key || '').trim(),
      type: field.type || 'text',
      required: !!field.required,
    }))
    .filter((field) => field.key && field.label);
  const statuses = _parseStatusList(newCustomTab.value.statusList);
  const payload = {
    slug,
    label,
    fields,
    search_keys: fields.filter((f) => ['text', 'phone', 'email'].includes(f.type)).map((f) => f.key),
  };
  if (statuses.length) {
    payload.status_vocabulary = {
      initial: statuses[0],
      all: statuses,
      forward: statuses,
    };
  }
  customTabActionInProgress.value = true;
  try {
    const { data } = await api.post('/business-template/custom-tabs', payload, {
      headers: authHeader(),
    });
    customTabs.value = data || [];
    _resetNewCustomTab();
    infoMsg.value = `Custom tab "${label}" created — agents now have ${fields.length ? '8 CRUD tools' : 'CRUD tools'} for it.`;
    await loadWorkspace();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Could not create custom tab.');
  } finally {
    customTabActionInProgress.value = false;
  }
};

const deleteCustomTab = async (slug) => {
  if (!slug || customTabActionInProgress.value) return;
  if (!confirm(`Remove custom tab "${slug}"? Agents will lose its CRUD tools immediately. Stored records remain intact.`)) {
    return;
  }
  customTabActionInProgress.value = true;
  try {
    const { data } = await api.delete(`/business-template/custom-tabs/${slug}`, {
      headers: authHeader(),
    });
    customTabs.value = data || [];
    infoMsg.value = `Custom tab "${slug}" removed.`;
    await loadWorkspace();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Could not remove custom tab.');
  } finally {
    customTabActionInProgress.value = false;
  }
};

const assignmentForMember = (memberId) =>
  assignmentSettings.value.find((item) => item.member_id === memberId) || {
    member_id: memberId,
    is_assignable: false,
    working_days: [],
    start_time: null,
    end_time: null,
    timezone: 'Asia/Kolkata',
    request_types: [],
    max_active_requests: 100,
    max_requests_per_day: null,
    max_requests_per_hour: 6,
    appointment_duration_minutes: 30,
    active_request_count: 0,
    availability_summary: 'Not assignable',
  };

const clinicForMember = (memberId) =>
  clinicScheduleSettings.value[memberId] || {
    member_id: memberId,
    appointment_duration_minutes: 30,
    buffer_minutes: 0,
    max_patients_per_hour: null,
    max_patients_per_day: null,
    consultation_types: [],
  };

const todayDateInputValue = () => {
  const now = new Date();
  const localDate = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
  return localDate.toISOString().slice(0, 10);
};

const createEmptyBlockedSlot = () => ({ date: todayDateInputValue(), start_time: '', end_time: '', reason: '' });

const normalizeTime = (value) => (value ? String(value).slice(0, 5) : '');

const timeToMinutes = (value) => {
  const normalized = normalizeTime(value);
  const [hours, minutes] = normalized.split(':').map(Number);
  if (!Number.isFinite(hours) || !Number.isFinite(minutes)) return null;
  return hours * 60 + minutes;
};

const formatSimpleTime = (value) => {
  const normalized = normalizeTime(value);
  const minutes = timeToMinutes(normalized);
  if (minutes === null) return '--';
  const date = new Date(2000, 0, 1, Math.floor(minutes / 60), minutes % 60);
  return date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
};

const timeRangeDuration = (start, end) => {
  const startMinutes = timeToMinutes(start);
  const endMinutes = timeToMinutes(end);
  if (startMinutes === null || endMinutes === null) return 'Set daily working hours';
  const diff = endMinutes - startMinutes;
  if (diff <= 0) return 'End time must be after start time';
  const hours = Math.floor(diff / 60);
  const minutes = diff % 60;
  if (!hours) return `${minutes} min window`;
  if (!minutes) return `${hours} hr window`;
  return `${hours} hr ${minutes} min window`;
};

const applyScheduleTimePreset = (preset) => {
  if (!assignmentEditor.value.settings) return;
  assignmentEditor.value.settings.start_time = preset.start;
  assignmentEditor.value.settings.end_time = preset.end;
};

const isScheduleTimePresetActive = (preset) => {
  const settings = assignmentEditor.value.settings;
  if (!settings) return false;
  return normalizeTime(settings.start_time) === preset.start && normalizeTime(settings.end_time) === preset.end;
};

const withScheduleDefaults = (settings = {}, { enable = false, force = false } = {}) => {
  const defaults = scheduleDefaultForBusiness.value;
  const requestTypes = requestTypeValues.value;
  return {
    ...settings,
    is_assignable: enable ? true : Boolean(settings.is_assignable),
    working_days: force || !(settings.working_days || []).length ? [...defaults.days] : [...settings.working_days],
    start_time: force || !settings.start_time ? defaults.start : normalizeTime(settings.start_time),
    end_time: force || !settings.end_time ? defaults.end : normalizeTime(settings.end_time),
    timezone: 'Asia/Kolkata',
    request_types: force || !(settings.request_types || []).length ? [...requestTypes] : [...settings.request_types],
    max_active_requests: settings.max_active_requests || 100,
    max_requests_per_day: settings.max_requests_per_day || null,
    max_requests_per_hour: settings.max_requests_per_hour || 6,
    appointment_duration_minutes: settings.appointment_duration_minutes || 30,
  };
};

const withClinicDefaults = (clinic = {}, { force = false } = {}) => ({
  ...clinic,
  appointment_duration_minutes: clinic.appointment_duration_minutes || 30,
  buffer_minutes: clinic.buffer_minutes || 0,
  max_patients_per_hour: clinic.max_patients_per_hour || null,
  max_patients_per_day: clinic.max_patients_per_day || null,
  consultation_types:
    force || !(clinic.consultation_types || []).length
      ? [...consultationTypeValues.value]
      : [...clinic.consultation_types],
});

const assignmentEditorStatus = computed(() => {
  const settings = assignmentEditor.value.settings;
  if (!settings) return '';
  if (!settings.is_assignable) return 'Inactive: this person will not receive agent-assigned work.';
  if (!settings.working_days?.length) return 'Incomplete: choose at least one working day.';
  if (!settings.start_time || !settings.end_time) return 'Incomplete: set the daily time window.';
  if (timeRangeDuration(settings.start_time, settings.end_time).startsWith('End time')) return 'Incomplete: end time must be after start time.';
  if (!settings.request_types?.length) return 'Incomplete: choose at least one request type.';
  return `Ready: available ${settings.working_days.length} day(s), ${formatSimpleTime(settings.start_time)} - ${formatSimpleTime(settings.end_time)}.`;
});

const applyRecommendedAssignmentSetup = () => {
  if (!assignmentEditor.value.settings) return;
  assignmentEditor.value.settings = withScheduleDefaults(assignmentEditor.value.settings, { enable: true, force: true });
  if (isClinicTemplate.value && assignmentEditor.value.clinic) {
    assignmentEditor.value.clinic = withClinicDefaults(assignmentEditor.value.clinic, { force: true });
  }
};

const handleAssignableToggle = () => {
  if (!assignmentEditor.value.settings?.is_assignable) return;
  assignmentEditor.value.settings = withScheduleDefaults(assignmentEditor.value.settings, { enable: true });
  if (isClinicTemplate.value && assignmentEditor.value.clinic) {
    assignmentEditor.value.clinic = withClinicDefaults(assignmentEditor.value.clinic);
  }
};

const setScheduleDays = (days) => {
  if (!assignmentEditor.value.settings) return;
  assignmentEditor.value.settings.working_days = [...days];
};

const selectAllRequestTypes = () => {
  if (!assignmentEditor.value.settings) return;
  assignmentEditor.value.settings.request_types = [...requestTypeValues.value];
};

const selectAllConsultationTypes = () => {
  if (!assignmentEditor.value.clinic) return;
  assignmentEditor.value.clinic.consultation_types = [...consultationTypeValues.value];
};

const startAssignmentEdit = (member) => {
  assignmentEditor.value = {
    member,
    settings: withScheduleDefaults(JSON.parse(JSON.stringify(assignmentForMember(member.id)))),
    clinic: withClinicDefaults(JSON.parse(JSON.stringify(clinicForMember(member.id)))),
    blockedSlot: createEmptyBlockedSlot(),
    isSaving: false,
    isSavingClinic: false,
    isSavingBlock: false,
  };
};

const closeAssignmentEdit = () => {
  assignmentEditor.value = {
    member: null,
    settings: null,
    clinic: null,
    blockedSlot: { date: '', start_time: '', end_time: '', reason: '' },
    isSaving: false,
    isSavingClinic: false,
    isSavingBlock: false,
  };
};

const toggleListValue = (list, value) => {
  const idx = list.indexOf(value);
  if (idx >= 0) list.splice(idx, 1);
  else list.push(value);
};

const validateMemberScheduleDraft = () => {
  const settings = assignmentEditor.value.settings;
  if (!settings?.is_assignable) return true;
  if (!settings.working_days?.length) {
    errorMsg.value = 'Choose at least one working day before enabling assignments.';
    return false;
  }
  if (!settings.start_time || !settings.end_time || timeRangeDuration(settings.start_time, settings.end_time).startsWith('End time')) {
    errorMsg.value = 'Set a valid daily time window before enabling assignments.';
    return false;
  }
  if (!settings.request_types?.length) {
    errorMsg.value = 'Choose at least one request type this member can handle.';
    return false;
  }
  return true;
};

const saveAssignmentSettings = async () => {
  const member = assignmentEditor.value.member;
  const settings = assignmentEditor.value.settings;
  if (!member || !settings) return;
  assignmentEditor.value.isSaving = true;
  try {
    const payload = {
      ...settings,
      timezone: 'Asia/Kolkata',
      max_active_requests: settings.max_active_requests || 100,
      max_requests_per_day: null,
      max_requests_per_hour: settings.max_requests_per_hour || 6,
      appointment_duration_minutes: settings.appointment_duration_minutes || 30,
    };
    const { data } = await api.put(`/members/${member.id}/assignment-settings`, payload, { headers: authHeader() });
    const idx = assignmentSettings.value.findIndex((item) => item.member_id === member.id);
    if (idx >= 0) assignmentSettings.value[idx] = data;
    else assignmentSettings.value.push(data);
    assignmentEditor.value.settings = JSON.parse(JSON.stringify(data));
    infoMsg.value = `${member.full_name || member.email} assignment settings saved.`;
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Assignment settings could not be saved.');
  } finally {
    assignmentEditor.value.isSaving = false;
  }
};

const saveMemberSchedule = async () => {
  const member = assignmentEditor.value.member;
  const settings = assignmentEditor.value.settings;
  if (!member || !settings || !validateMemberScheduleDraft()) return;
  assignmentEditor.value.isSaving = true;
  assignmentEditor.value.isSavingClinic = isClinicTemplate.value;
  try {
    const payload = {
      ...settings,
      timezone: 'Asia/Kolkata',
      working_days: settings.is_assignable ? settings.working_days : [],
      start_time: settings.is_assignable ? settings.start_time : null,
      end_time: settings.is_assignable ? settings.end_time : null,
      request_types: settings.is_assignable ? settings.request_types : [],
      max_active_requests: settings.max_active_requests || 100,
      max_requests_per_day: null,
      max_requests_per_hour: settings.max_requests_per_hour || 6,
      appointment_duration_minutes: settings.appointment_duration_minutes || 30,
    };
    const { data } = await api.put(`/members/${member.id}/assignment-settings`, payload, { headers: authHeader() });
    const idx = assignmentSettings.value.findIndex((item) => item.member_id === member.id);
    if (idx >= 0) assignmentSettings.value[idx] = data;
    else assignmentSettings.value.push(data);
    assignmentEditor.value.settings = withScheduleDefaults(JSON.parse(JSON.stringify(data)));

    if (isClinicTemplate.value && assignmentEditor.value.clinic) {
      const clinic = withClinicDefaults(assignmentEditor.value.clinic);
      const clinicPayload = {
        ...clinic,
        max_patients_per_hour: clinic.max_patients_per_hour || null,
        max_patients_per_day: clinic.max_patients_per_day || null,
      };
      const clinicResult = await api.put(`/members/${member.id}/clinic-schedule-settings`, clinicPayload, { headers: authHeader() });
      clinicScheduleSettings.value = { ...clinicScheduleSettings.value, [member.id]: clinicResult.data };
      assignmentEditor.value.clinic = withClinicDefaults(JSON.parse(JSON.stringify(clinicResult.data)));
    }
    infoMsg.value = `${member.full_name || member.email} schedule saved.`;
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Schedule could not be saved.');
  } finally {
    assignmentEditor.value.isSaving = false;
    assignmentEditor.value.isSavingClinic = false;
  }
};

const saveClinicSettings = async () => {
  const member = assignmentEditor.value.member;
  const clinic = assignmentEditor.value.clinic;
  if (!member || !clinic) return;
  assignmentEditor.value.isSavingClinic = true;
  try {
    const payload = {
      ...clinic,
      max_patients_per_hour: clinic.max_patients_per_hour || null,
      max_patients_per_day: clinic.max_patients_per_day || null,
    };
    const { data } = await api.put(`/members/${member.id}/clinic-schedule-settings`, payload, { headers: authHeader() });
    clinicScheduleSettings.value = { ...clinicScheduleSettings.value, [member.id]: data };
    assignmentEditor.value.clinic = JSON.parse(JSON.stringify(data));
    infoMsg.value = `${member.full_name || member.email} clinic schedule saved.`;
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Clinic schedule could not be saved.');
  } finally {
    assignmentEditor.value.isSavingClinic = false;
  }
};

const addBlockedSlot = async () => {
  const member = assignmentEditor.value.member;
  const slot = assignmentEditor.value.blockedSlot;
  if (!member || !slot.date || !slot.start_time || !slot.end_time) return;
  const startDate = new Date(`${slot.date}T${slot.start_time}`);
  const endDate = new Date(`${slot.date}T${slot.end_time}`);
  if (Number.isNaN(startDate.getTime()) || Number.isNaN(endDate.getTime())) {
    errorMsg.value = 'Choose a valid blocked date and time.';
    return;
  }
  if (endDate <= startDate) {
    errorMsg.value = 'Blocked time must end after it starts.';
    return;
  }
  assignmentEditor.value.isSavingBlock = true;
  try {
    const { data } = await api.post(
      `/members/${member.id}/blocked-slots`,
      {
        start_time: startDate.toISOString(),
        end_time: endDate.toISOString(),
        reason: slot.reason || null,
      },
      { headers: authHeader() },
    );
    blockedSlots.value = {
      ...blockedSlots.value,
      [member.id]: [...(blockedSlots.value[member.id] || []), data],
    };
    assignmentEditor.value.blockedSlot = createEmptyBlockedSlot();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Blocked slot could not be added.');
  } finally {
    assignmentEditor.value.isSavingBlock = false;
  }
};

const deleteBlockedSlot = async (slotId) => {
  const member = assignmentEditor.value.member;
  if (!member) return;
  try {
    await api.delete(`/blocked-slots/${slotId}`, { headers: authHeader() });
    blockedSlots.value = {
      ...blockedSlots.value,
      [member.id]: (blockedSlots.value[member.id] || []).filter((slot) => slot.id !== slotId),
    };
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Blocked slot could not be deleted.');
  }
};

const formatCalendarDate = (value) => {
  if (!value) return 'No date';
  return new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
};

const formatCalendarTime = (value) => {
  if (!value) return '';
  return new Date(value).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
};

const parseRecordDate = (value) => {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
};

const localDateKey = (value) => {
  const date = value instanceof Date ? value : parseRecordDate(value);
  if (!date) return '';
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
};

const dateFromKey = (key) => {
  if (!key) return null;
  const [year, month, day] = key.split('-').map(Number);
  if (!year || !month || !day) return null;
  return new Date(year, month - 1, day);
};

const todayKey = () => localDateKey(new Date());

const recordScheduledDate = (record) =>
  parseRecordDate(firstRecordValue(record, [
    'scheduled_time',
    'appointment_time',
    'appointment_start',
    'visit_at',
    'visit_date',
    'callback_at',
    'follow_up_at',
    'due_at',
    'due_date',
    'requested_time',
    'preferred_time',
    'scheduled_at',
  ]));

const recordCalendarDate = (record) =>
  recordScheduledDate(record) || parseRecordDate(record?.created_at);

const scheduleRecordTypeLabel = (record) => {
  if (record.record_type === 'appointment') return 'Appointment';
  if (record.record_type === 'lead') return 'Lead';
  if (record.record_type === 'ticket') return 'Ticket';
  return 'Request';
};

const recordAssignedToMember = (record, member) => {
  const data = recordData(record);
  const memberId = String(member?.id || '');
  const memberName = String(member?.full_name || '').trim().toLowerCase();
  const memberEmail = String(member?.email || '').trim().toLowerCase();
  const directIds = [
    data.assigned_member_id,
    data.assigned_doctor_id,
    data.assigned_agent_id,
    data.selected_member_id,
    record?.selected_member_id,
  ].filter(Boolean).map(String);
  if (directIds.includes(memberId)) return true;
  const assignedNames = [
    data.assigned_member_name,
    data.doctor,
    data.assigned_to,
    data.agent,
    data.owner,
    data.assigned_member_email,
    data.owner_email,
    data.agent_email,
  ].filter(Boolean).map((value) => String(value).trim().toLowerCase());
  return (!!memberName && assignedNames.includes(memberName)) || (!!memberEmail && assignedNames.includes(memberEmail));
};

const scheduleRecordLabel = (record) => {
  if (record.record_type === 'appointment') return appointmentRecordTitle(record);
  if (record.record_type === 'lead') return leadRecordTitle(record);
  if (record.record_type === 'ticket') return ticketRecordTitle(record);
  return formatRecordValue(firstRecordValue(record, ['summary', 'reason', 'name']), 'Scheduled item');
};

const scheduleRecordDetail = (record) => {
  if (record.record_type === 'appointment') return appointmentRecordSubtitle(record);
  if (record.record_type === 'lead') return leadRecordSubtitle(record);
  if (record.record_type === 'ticket') return ticketRecordSubtitle(record);
  return formatRecordValue(firstRecordValue(record, ['summary', 'request_type']), '');
};

const memberAssignedWorkItems = computed(() => {
  const member = timetableViewer.value.member;
  if (!member) return [];
  const records = [
    ...(tabRecords.value.appointments || []),
    ...(tabRecords.value.leads || []),
    ...(tabRecords.value.tickets || []),
  ];
  const matchedRecords = records.filter((record) => recordAssignedToMember(record, member));
  const activeStatuses = new Set(['assigned', 'in_progress', 'scheduled', 'open']);
  const fallbackRecords = !matchedRecords.length && assignmentForMember(member.id).active_request_count
    ? records.filter((record) => activeStatuses.has(String(record.status || '').toLowerCase()))
    : [];
  const sourceRecords = matchedRecords.length ? matchedRecords : fallbackRecords;
  return sourceRecords
    .map((record) => {
      const start = recordCalendarDate(record);
      const duration = assignmentForMember(member.id).appointment_duration_minutes || 30;
      const end = start ? new Date(start.getTime() + duration * 60000) : null;
      return {
        id: record.id,
        type: record.record_type || 'request',
        typeLabel: scheduleRecordTypeLabel(record),
        title: scheduleRecordLabel(record),
        detail: scheduleRecordDetail(record),
        status: record.status || 'scheduled',
        priority: record.record_type === 'ticket' ? ticketRecordPriority(record) : null,
        owner: record.record_type === 'ticket' ? ticketRecordOwner(record) : null,
        phone: recordPhone(record),
        phoneHref: phoneHref(recordPhone(record)),
        createdAt: record.created_at,
        start,
        end,
        isBlocked: false,
      };
    });
});

const memberScheduleItems = computed(() => {
  const member = timetableViewer.value.member;
  if (!member) return [];
  const blocked = (blockedSlots.value[member.id] || []).map((slot) => ({
    id: slot.id,
    type: 'blocked',
    typeLabel: 'Blocked',
    title: slot.reason || 'Unavailable',
    detail: 'Blocked time',
    status: 'blocked',
    start: parseRecordDate(slot.start_time),
    end: parseRecordDate(slot.end_time),
    isBlocked: true,
  }));
  return [...memberAssignedWorkItems.value, ...blocked]
    .filter((item) => item.start)
    .sort((a, b) => a.start.getTime() - b.start.getTime());
});

const memberQueuedItems = computed(() =>
  memberAssignedWorkItems.value
    .filter((item) => !item.start)
    .sort((a, b) => new Date(b.createdAt || 0).getTime() - new Date(a.createdAt || 0).getTime()),
);

const selectedTimetableDate = computed(() => dateFromKey(timetableViewer.value.selectedDate) || new Date());

const selectedTimetableItems = computed(() => {
  const selected = timetableViewer.value.selectedDate;
  if (!selected) return [];
  return memberScheduleItems.value.filter((item) => localDateKey(item.start) === selected);
});

const selectedTimetableLabel = computed(() =>
  selectedTimetableDate.value.toLocaleDateString(undefined, {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
  }),
);

const timetableMonthLabel = computed(() => {
  const visibleDate = dateFromKey(timetableViewer.value.visibleMonth) || selectedTimetableDate.value;
  return visibleDate.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
});

const timetableCalendarDays = computed(() => {
  const selected = timetableViewer.value.selectedDate || todayKey();
  const visibleDate = dateFromKey(timetableViewer.value.visibleMonth) || dateFromKey(selected) || new Date();
  const firstOfMonth = new Date(visibleDate.getFullYear(), visibleDate.getMonth(), 1);
  const gridStart = new Date(firstOfMonth);
  gridStart.setDate(firstOfMonth.getDate() - firstOfMonth.getDay());
  const today = todayKey();
  return Array.from({ length: 42 }, (_, index) => {
    const date = new Date(gridStart);
    date.setDate(gridStart.getDate() + index);
    const key = localDateKey(date);
    const items = memberScheduleItems.value.filter((item) => localDateKey(item.start) === key);
    return {
      key,
      dayNumber: date.getDate(),
      inMonth: date.getMonth() === visibleDate.getMonth(),
      isToday: key === today,
      isSelected: key === selected,
      ticketCount: items.filter((item) => item.type === 'ticket').length,
      itemCount: items.length,
    };
  });
});

const memberScheduleDays = computed(() => {
  const byDay = new Map();
  for (const item of memberScheduleItems.value) {
    const key = item.start.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
    if (!byDay.has(key)) byDay.set(key, []);
    byDay.get(key).push(item);
  }
  return Array.from(byDay.entries()).map(([label, items]) => ({ label, items }));
});

const selectTimetableDate = (key) => {
  timetableViewer.value = {
    ...timetableViewer.value,
    selectedDate: key,
    visibleMonth: key.slice(0, 7) + '-01',
  };
};

const shiftTimetableMonth = (offset) => {
  const visibleDate = dateFromKey(timetableViewer.value.visibleMonth) || selectedTimetableDate.value;
  const next = new Date(visibleDate.getFullYear(), visibleDate.getMonth() + offset, 1);
  timetableViewer.value = {
    ...timetableViewer.value,
    visibleMonth: localDateKey(next),
  };
};

const openMemberTimetable = async (member) => {
  const initialDate = todayKey();
  timetableViewer.value = { member, isLoading: true, selectedDate: initialDate, visibleMonth: `${initialDate.slice(0, 7)}-01` };
  const loads = [loadTabRecords('leads'), loadTabRecords('tickets')];
  if (showAppointmentsTab.value) loads.push(loadTabRecords('appointments'));
  await Promise.allSettled(loads);
  const todayHasWork = memberScheduleItems.value.some((item) => localDateKey(item.start) === initialDate);
  const selectedDate = todayHasWork ? initialDate : (memberScheduleItems.value[0] ? localDateKey(memberScheduleItems.value[0].start) : initialDate);
  timetableViewer.value = {
    member,
    isLoading: false,
    selectedDate,
    visibleMonth: `${selectedDate.slice(0, 7)}-01`,
  };
};

const closeMemberTimetable = () => {
  timetableViewer.value = { member: null, isLoading: false, selectedDate: '', visibleMonth: '' };
};

const addField = () => {
  fieldEditor.value.fields.push({
    key: `custom_${fieldEditor.value.fields.length + 1}`,
    label: 'New Field',
    type: 'text',
    required: false,
  });
};

const removeField = (index) => {
  if (fieldEditor.value.fields.length <= 1) return;
  fieldEditor.value.fields.splice(index, 1);
};

const saveFieldEdit = async () => {
  if (!fieldEditor.value.key) return;
  fieldEditor.value.isSaving = true;
  errorMsg.value = '';
  try {
    const fields = fieldEditor.value.fields.map((field) => ({
      key: (field.key || field.label || 'field').trim().toLowerCase().replace(/[\s-]+/g, '_').replace(/[^a-z0-9_]/g, ''),
      label: (field.label || '').trim(),
      type: field.type || 'text',
      required: Boolean(field.required),
    }));
    const { data } = await api.patch(
      `/business-template/schemas/${fieldEditor.value.key}`,
      { fields },
      { headers: authHeader() },
    );
    organizationBusinessTemplate.value = data;
    infoMsg.value = `${fieldEditor.value.title} updated.`;
    closeFieldEdit();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Fields could not be saved.');
  } finally {
    fieldEditor.value.isSaving = false;
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
    if (onboardingV2Enabled.value && setupToken.value) {
      const { data } = await api.post('/signup/skip-totp', { setup_token: setupToken.value });
      persistSession(data);
      infoMsg.value = 'Email verified. You can set up MFA later from Settings.';
      await enterWorkspaceAfterAuth();
      return;
    }
    const { data } = await api.post('/signup/totp/setup', { setup_token: setupToken.value });
    setupToken.value = data.setup_token;
    totpUri.value = data.uri;
    totpSecret.value = data.secret;
    mfaSetupMode.value = 'signup';
    authState.value = 'mfa_setup';
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Could not initialise TOTP.');
  } finally {
    isAuthenticating.value = false;
  }
};

const startSessionTotpSetup = async () => {
  errorMsg.value = '';
  infoMsg.value = '';
  isAuthenticating.value = true;
  try {
    const { data } = await api.post('/mfa/totp/setup', {}, { headers: authHeader() });
    setupToken.value = data.setup_token || '';
    totpUri.value = data.uri;
    totpSecret.value = data.secret;
    totpCode.value = '';
    mfaSetupMode.value = 'session_setup';
    authState.value = 'mfa_setup';
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Could not initialise MFA.');
  } finally {
    isAuthenticating.value = false;
  }
};

const startSessionTotpVerify = () => {
  errorMsg.value = '';
  infoMsg.value = 'Enter your authenticator code to unlock this action.';
  totpUri.value = '';
  totpSecret.value = '';
  totpCode.value = '';
  mfaSetupMode.value = 'session_verify';
  authState.value = 'mfa_setup';
};

const verifySignupTotp = async () => {
  errorMsg.value = '';
  isAuthenticating.value = true;
  try {
    if (mfaSetupMode.value === 'session_setup' || mfaSetupMode.value === 'session_verify') {
      const { data } = await api.post(
        '/mfa/totp/verify',
        { code: totpCode.value },
        { headers: authHeader() },
      );
      persistSession(data);
      totpCode.value = '';
      totpUri.value = '';
      totpSecret.value = '';
      mfaSetupMode.value = 'signup';
      infoMsg.value = 'MFA is active for this session.';
      await enterWorkspaceAfterAuth();
      return;
    }
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

const cancelTotpSetup = () => {
  if (mfaSetupMode.value === 'session_setup' || mfaSetupMode.value === 'session_verify') {
    totpCode.value = '';
    totpUri.value = '';
    totpSecret.value = '';
    mfaSetupMode.value = 'signup';
    authState.value = 'ready';
    return;
  }
  resetLoginState();
};

const handleMfaProtectedError = async (err) => {
  const detail = err?.response?.data?.detail;
  if (detail === 'Organization MFA required' || detail === 'MFA required') {
    startSessionTotpVerify();
    return true;
  }
  if (!detail || typeof detail !== 'object') return false;
  if (detail.code === 'mfa_setup_required') {
    await startSessionTotpSetup();
    return true;
  }
  if (detail.code === 'mfa_step_up_required') {
    startSessionTotpVerify();
    return true;
  }
  return false;
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
    totpCode.value = '';
    await enterWorkspaceAfterAuth();
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
    await loadWorkspace();
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Invite failed.');
  } finally {
    isSavingMember.value = false;
  }
};

const removingMemberId = ref(null);

const removeMember = async (member) => {
  if (!member?.id) return;
  if (member.id === currentUser.value?.id) {
    errorMsg.value = "You can't remove your own account.";
    return;
  }
  const label = member.full_name || member.email;
  // window.confirm is the cheapest UX for a low-frequency destructive
  // action; if this grows into batch removal we'll swap in a modal.
  const ok = window.confirm(
    `Remove ${label}? They'll lose access immediately. Pending invites for them will be revoked.`,
  );
  if (!ok) return;
  errorMsg.value = '';
  removingMemberId.value = member.id;
  try {
    await api.delete(`/members/${member.id}`, { headers: authHeader() });
    infoMsg.value = `Removed ${label}.`;
    const { data } = await api.get('/members/', { headers: authHeader() });
    members.value = data;
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Failed to remove member.');
  } finally {
    removingMemberId.value = null;
  }
};

const toggleAgentTool = (key) => {
  const idx = newAgent.value.tool_keys.indexOf(key);
  if (idx >= 0) newAgent.value.tool_keys.splice(idx, 1);
  else newAgent.value.tool_keys.push(key);
};

const toggleAgentToolGroup = (group) => {
  const groupKeys = (group?.tools || []).map((t) => t.key);
  if (!groupKeys.length) return;
  const allSelected = groupKeys.every((k) => newAgent.value.tool_keys.includes(k));
  if (allSelected) {
    newAgent.value.tool_keys = newAgent.value.tool_keys.filter((k) => !groupKeys.includes(k));
  } else {
    const next = new Set(newAgent.value.tool_keys);
    groupKeys.forEach((k) => next.add(k));
    newAgent.value.tool_keys = Array.from(next);
  }
};

const isAgentToolGroupAllOn = (group) => {
  const keys = (group?.tools || []).map((t) => t.key);
  return keys.length > 0 && keys.every((k) => newAgent.value.tool_keys.includes(k));
};

const selectDefaultAgentTools = () => {
  newAgent.value.tool_keys = [...toolCatalogDefaults.value];
};

const createAgent = async () => {
  errorMsg.value = '';
  try {
    const { data } = await api.post('/agents/', newAgent.value, { headers: authHeader() });
    agents.value.unshift(data);
    activeAgent.value = data;
    newAgent.value = {
      name: '',
      description: '',
      system_prompt: '',
      tool_keys: [...toolCatalogDefaults.value],
    };
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

const isAdmin = computed(() => currentUser.value?.role === 'admin');
// Members + viewers see a stripped-down dashboard: own timetable + the
// two actions they're allowed to perform (add buffer, mark unavailable).
// Everything else in the sidebar and main area is gated off for them.
const isMemberOnly = computed(() => ['member', 'viewer'].includes(currentUser.value?.role));

const myTimetable = ref(null);
const isLoadingMyTimetable = ref(false);
const isMutatingMyBlock = ref(false);
const bufferForm = ref({ start_time: '', duration_minutes: 30, reason: 'Buffer' });
const unavailableForm = ref({ start_time: '', end_time: '', reason: '' });
const bufferDurationOptions = [
  { minutes: 15, label: '15 min' },
  { minutes: 30, label: '30 min' },
  { minutes: 45, label: '45 min' },
  { minutes: 60, label: '1 hr' },
  { minutes: 90, label: '1.5 hr' },
];

const loadMyTimetable = async () => {
  isLoadingMyTimetable.value = true;
  try {
    const { data } = await api.get('/members/me/timetable', { headers: authHeader() });
    myTimetable.value = data;
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Failed to load your timetable.');
  } finally {
    isLoadingMyTimetable.value = false;
  }
};

const addBuffer = async () => {
  if (!bufferForm.value.start_time) {
    errorMsg.value = 'Pick a start time for the buffer.';
    return;
  }
  const start = new Date(bufferForm.value.start_time);
  const end = new Date(start.getTime() + Number(bufferForm.value.duration_minutes) * 60 * 1000);
  isMutatingMyBlock.value = true;
  try {
    await api.post(
      '/members/me/blocked-slots',
      {
        start_time: start.toISOString(),
        end_time: end.toISOString(),
        reason: bufferForm.value.reason || 'Buffer',
      },
      { headers: authHeader() },
    );
    infoMsg.value = 'Buffer added to your calendar.';
    bufferForm.value = { start_time: '', duration_minutes: 30, reason: 'Buffer' };
    await loadMyTimetable();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Failed to add buffer.');
  } finally {
    isMutatingMyBlock.value = false;
  }
};

const addUnavailability = async () => {
  if (!unavailableForm.value.start_time || !unavailableForm.value.end_time) {
    errorMsg.value = 'Pick both a start and end time for your unavailability.';
    return;
  }
  const start = new Date(unavailableForm.value.start_time);
  const end = new Date(unavailableForm.value.end_time);
  if (end <= start) {
    errorMsg.value = 'End time must be after start time.';
    return;
  }
  isMutatingMyBlock.value = true;
  try {
    await api.post(
      '/members/me/blocked-slots',
      {
        start_time: start.toISOString(),
        end_time: end.toISOString(),
        reason: unavailableForm.value.reason || 'Unavailable',
      },
      { headers: authHeader() },
    );
    infoMsg.value = 'Unavailability added.';
    unavailableForm.value = { start_time: '', end_time: '', reason: '' };
    await loadMyTimetable();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Failed to add unavailability.');
  } finally {
    isMutatingMyBlock.value = false;
  }
};

const removeMyBlock = async (id) => {
  if (!id) return;
  isMutatingMyBlock.value = true;
  try {
    await api.delete(`/members/me/blocked-slots/${id}`, { headers: authHeader() });
    infoMsg.value = 'Block removed.';
    await loadMyTimetable();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Failed to remove block.');
  } finally {
    isMutatingMyBlock.value = false;
  }
};

const formatSlotRange = (slot) => {
  if (!slot?.start_time || !slot?.end_time) return '';
  const start = new Date(slot.start_time);
  const end = new Date(slot.end_time);
  const sameDay = start.toDateString() === end.toDateString();
  const opts = { dateStyle: 'medium', timeStyle: 'short' };
  const tail = sameDay ? end.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : end.toLocaleString([], opts);
  return `${start.toLocaleString([], opts)} → ${tail}`;
};

const kbApi = axios.create({ baseURL: 'http://localhost:8000/api/nokvo-one/knowledge-base' });

const loadKnowledgeDocuments = async () => {
  isLoadingKb.value = true;
  kbError.value = '';
  try {
    const { data } = await kbApi.get('/documents', { headers: authHeader() });
    kbDocuments.value = data.documents || [];
  } catch (err) {
    kbError.value = extractErrorMessage(err, 'Failed to load Knowledge Base documents.');
  } finally {
    isLoadingKb.value = false;
  }
};

const loadSinglePromptAgent = async () => {
  try {
    const { data } = await kbApi.get('/single-prompt-agent', { headers: authHeader() });
    kbSinglePromptConfig.value = data;
    if (data?.prompt && !kbSinglePromptForm.value.prompt.trim()) {
      kbSinglePromptForm.value.prompt = data.prompt;
    }
  } catch (err) {
    kbSinglePromptConfig.value = null;
  }
};

// Bulk upload queue. Becomes non-empty when 2+ files are selected at once.
// Each entry tracks its own status so the user sees per-file progress.
const kbBulkQueue = ref([]);  // { file, name, status: 'queued'|'uploading'|'done'|'error', error?: string }
const isUploadingKbBulk = ref(false);

const _kbStripExt = (filename) => filename.replace(/\.[^/.]+$/, '');

const kbSinglePromptCanSubmit = computed(() => (
  kbSinglePromptForm.value.prompt.trim().length >= 20
));

const saveSinglePromptVoiceAgent = async () => {
  if (!kbSinglePromptForm.value.prompt.trim()) {
    kbError.value = 'Add the single prompt for the voice agent.';
    return;
  }
  if (kbSinglePromptForm.value.prompt.trim().length < 20) {
    kbError.value = 'Single prompt must be at least 20 characters.';
    return;
  }
  isSavingSinglePromptAgent.value = true;
  kbError.value = '';
  kbInfo.value = '';
  try {
    const { data } = await kbApi.post(
      '/single-prompt-agent',
      {
        prompt: kbSinglePromptForm.value.prompt.trim(),
      },
      { headers: authHeader() },
    );
    kbSinglePromptConfig.value = data;
    kbInfo.value = 'Single prompt voice agent configured.';
  } catch (err) {
    kbError.value = extractErrorMessage(err, 'Failed to configure single prompt voice agent.');
  } finally {
    isSavingSinglePromptAgent.value = false;
  }
};

const disableSinglePromptVoiceAgent = async () => {
  isDisablingSinglePromptAgent.value = true;
  kbError.value = '';
  kbInfo.value = '';
  try {
    const { data } = await kbApi.post('/single-prompt-agent/disable', {}, { headers: authHeader() });
    kbSinglePromptConfig.value = data;
    kbSinglePromptForm.value.prompt = '';
    kbInfo.value = 'Single prompt removed. The voice runtime will use the default system prompt with approved documents.';
  } catch (err) {
    kbError.value = extractErrorMessage(err, 'Failed to remove single prompt.');
  } finally {
    isDisablingSinglePromptAgent.value = false;
  }
};

const _acceptKbFiles = (files) => {
  const list = Array.from(files || []);
  if (!list.length) return;
  if (list.length === 1) {
    // Single-file path — keep existing form-driven flow.
    kbBulkQueue.value = [];
    kbForm.value.file = list[0];
    if (!kbForm.value.name) {
      kbForm.value.name = _kbStripExt(list[0].name);
    }
    return;
  }
  // Bulk path: ignore the form name/description (would be wrong for N files)
  // and queue every file with its filename as the doc name. document_type and
  // tags carry over from kbForm so the admin can set defaults before bulk upload.
  kbForm.value.file = null;
  kbBulkQueue.value = list.map((file) => ({
    file,
    name: _kbStripExt(file.name),
    status: 'queued',
    error: '',
  }));
};

const handleKbFileChange = (event) => {
  _acceptKbFiles(event.target.files);
};

const handleKbDrop = (event) => {
  _acceptKbFiles(event.dataTransfer?.files);
};

const clearKbFile = () => {
  kbForm.value.file = null;
  kbBulkQueue.value = [];
  if (kbUploadInputRef.value) kbUploadInputRef.value.value = '';
};

const removeBulkQueueItem = (index) => {
  if (isUploadingKbBulk.value) return;
  kbBulkQueue.value.splice(index, 1);
  if (kbBulkQueue.value.length === 1) {
    // Reverting back to single-file behavior so the admin can edit the name.
    const remaining = kbBulkQueue.value[0].file;
    kbBulkQueue.value = [];
    kbForm.value.file = remaining;
    if (!kbForm.value.name) {
      kbForm.value.name = _kbStripExt(remaining.name);
    }
  }
};

// Bulk uploader concurrency. Serialised (1 at a time) because Azure OpenAI
// embedding deployments on the S0 tier rate-limit aggressively — even 2 in
// parallel triggers 429s during a multi-doc upload. The backend retries with
// a brief sleep on 429, but spacing uploads here halves the chance of ever
// hitting it.
const BULK_UPLOAD_CONCURRENCY = 1;

const uploadKnowledgeDocumentsBulk = async () => {
  if (!kbBulkQueue.value.length) return;
  isUploadingKbBulk.value = true;
  kbError.value = '';
  kbInfo.value = '';

  const tagsValue = kbForm.value.tags.trim() || null;
  const docType = kbForm.value.document_type;
  const queue = kbBulkQueue.value;
  // Track next index to pick up; worker loop pulls until exhausted.
  let cursor = 0;

  const _workOne = async () => {
    while (cursor < queue.length) {
      const idx = cursor;
      cursor += 1;
      const entry = queue[idx];
      entry.status = 'uploading';
      try {
        const content_base64 = await fileToBase64(entry.file);
        await kbApi.post(
          '/documents/upload',
          {
            name: entry.name,
            document_type: docType,
            description: null,
            tags: tagsValue,
            filename: entry.file.name,
            content_type: entry.file.type || null,
            content_base64,
          },
          { headers: authHeader() },
        );
        entry.status = 'done';
      } catch (err) {
        entry.status = 'error';
        entry.error = extractErrorMessage(err, 'Upload failed.');
      }
    }
  };

  const workers = [];
  for (let i = 0; i < Math.min(BULK_UPLOAD_CONCURRENCY, queue.length); i += 1) {
    workers.push(_workOne());
  }
  await Promise.all(workers);

  const succeeded = queue.filter((q) => q.status === 'done').length;
  const failed = queue.filter((q) => q.status === 'error').length;
  if (failed === 0) {
    kbInfo.value = `Embedded ${succeeded} document${succeeded === 1 ? '' : 's'}.`;
    kbBulkQueue.value = [];
    if (kbUploadInputRef.value) kbUploadInputRef.value.value = '';
  } else {
    kbError.value = `${succeeded} of ${queue.length} uploaded. ${failed} failed — see the list below.`;
  }
  isUploadingKbBulk.value = false;
  await loadKnowledgeDocuments();
};

const formatRelativeDate = (iso) => {
  if (!iso) return '';
  const ts = new Date(iso).getTime();
  if (Number.isNaN(ts)) return '';
  const diff = (Date.now() - ts) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)}d ago`;
  return new Date(iso).toLocaleDateString();
};

const recordData = (record) => record?.data || {};

const firstRecordValue = (record, keys) => {
  const data = recordData(record);
  for (const key of keys) {
    const value = data[key] ?? record?.[key];
    if (value === null || value === undefined) continue;
    if (typeof value === 'string' && !value.trim()) continue;
    return value;
  }
  return '';
};

const formatRecordValue = (value, fallback = 'Not set') => {
  if (value === null || value === undefined || value === '') return fallback;
  if (Array.isArray(value)) return value.filter(Boolean).join(', ') || fallback;
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
};

const formatRecordDateTime = (value) => {
  if (!value) return 'Not set';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return formatRecordValue(value);
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
};

const recordPhone = (record) =>
  formatRecordValue(
    firstRecordValue(record, ['contact_phone', 'phone', 'phone_number', 'patient_phone', 'mobile', 'mobile_number']),
    '',
  );

const phoneHref = (phone) => {
  const value = String(phone || '').trim();
  if (!value) return '';
  const cleaned = value.replace(/[^\d+]/g, '');
  return cleaned ? `tel:${cleaned}` : '';
};

const appointmentRecordTitle = (record) =>
  formatRecordValue(
    firstRecordValue(record, ['patient_name', 'customer_name', 'contact_name', 'name'])
      || firstRecordValue(record, ['reason', 'appointment_reason', 'eye_concern', 'concern']),
    'Appointment request',
  );

const appointmentRecordSubtitle = (record) => {
  const phone = firstRecordValue(record, ['contact_phone', 'phone', 'phone_number', 'patient_phone']);
  const reason = firstRecordValue(record, ['reason', 'appointment_reason', 'eye_concern', 'concern', 'service']);
  return [phone, reason].map((item) => formatRecordValue(item, '')).filter(Boolean).join(' · ') || 'No extra details';
};

const appointmentRecordTime = (record) =>
  formatRecordDateTime(firstRecordValue(record, ['appointment_time', 'requested_time', 'preferred_time', 'scheduled_at']));

const appointmentAssignedLabel = (record) =>
  formatRecordValue(
    firstRecordValue(record, ['assigned_member_name', 'doctor', 'assigned_to', 'agent']),
    'Unassigned',
  );

// Ticket helpers — mirror the appointment helpers but pull from the ticket
// schema's keys (customer, issue_type, priority, property_id) with sensible
// fallbacks for records routed from leads (which still carry name/phone).
const ticketRecordTitle = (record) =>
  formatRecordValue(
    firstRecordValue(record, [
      'customer', 'customer_name', 'patient_name', 'guest_name', 'name', 'contact_name',
    ]) || firstRecordValue(record, ['subject', 'issue_type', 'reason', 'description']),
    'Support ticket',
  );

const ticketRecordSubtitle = (record) => {
  const phone = firstRecordValue(record, ['contact_phone', 'phone', 'phone_number']);
  const summary = firstRecordValue(record, ['issue_type', 'subject', 'reason', 'description', 'property_id', 'location']);
  return [phone, summary].map((item) => formatRecordValue(item, '')).filter(Boolean).join(' · ') || 'No extra details';
};

const ticketRecordPriority = (record) =>
  formatRecordValue(firstRecordValue(record, ['priority']), 'normal');

const ticketRecordOwner = (record) =>
  formatRecordValue(
    firstRecordValue(record, ['assigned_to', 'owner', 'assigned_member_name', 'agent']),
    'Unassigned',
  );

// Lead helpers — pull from lead schema's keys (name, phone, budget, location).
const leadRecordTitle = (record) =>
  formatRecordValue(
    firstRecordValue(record, ['name', 'customer_name', 'patient_name', 'guest_name', 'contact_name']),
    'New lead',
  );

const leadRecordSubtitle = (record) => {
  const phone = firstRecordValue(record, ['contact_phone', 'phone', 'phone_number']);
  const interest = firstRecordValue(record, [
    'property_type', 'looking_for', 'service', 'reason', 'care_need', 'subject',
  ]);
  return [phone, interest].map((item) => formatRecordValue(item, '')).filter(Boolean).join(' · ') || 'No extra details';
};

const leadRecordBudget = (record) =>
  formatRecordValue(firstRecordValue(record, ['budget', 'price_range']), 'Not set');

const leadRecordLocation = (record) =>
  formatRecordValue(firstRecordValue(record, ['location', 'area', 'city']), 'Not set');

const kbStats = computed(() => {
  const docs = kbDocuments.value || [];
  const total = docs.length;
  let approved = 0;
  let pending = 0;
  let chunks = 0;
  let vectors = 0;
  let bytes = 0;
  let errors = 0;
  for (const d of docs) {
    if (d.approval_status === 'approved') approved += 1;
    else pending += 1;
    chunks += Number(d.chunk_count || 0);
    vectors += Number(d.qdrant_point_count || 0);
    if (d.last_error) errors += 1;
    if (d.size_bytes) bytes += Number(d.size_bytes);
  }
  return { total, approved, pending, chunks, vectors, bytes, errors };
});

const fileToBase64 = (file) => new Promise((resolve, reject) => {
  const reader = new FileReader();
  reader.onload = () => {
    const result = String(reader.result || '');
    const idx = result.indexOf(',');
    resolve(idx >= 0 ? result.slice(idx + 1) : result);
  };
  reader.onerror = () => reject(reader.error);
  reader.readAsDataURL(file);
});

const uploadKnowledgeDocument = async () => {
  if (!kbForm.value.file) {
    kbError.value = 'Choose a file to upload first.';
    return;
  }
  if (!kbForm.value.name.trim()) {
    kbError.value = 'Document name is required.';
    return;
  }
  isUploadingKb.value = true;
  kbError.value = '';
  kbInfo.value = '';
  try {
    const content_base64 = await fileToBase64(kbForm.value.file);
    const payload = {
      name: kbForm.value.name.trim(),
      document_type: kbForm.value.document_type,
      description: kbForm.value.description.trim() || null,
      tags: kbForm.value.tags.trim() || null,
      filename: kbForm.value.file.name,
      content_type: kbForm.value.file.type || null,
      content_base64,
    };
    await kbApi.post('/documents/upload', payload, { headers: authHeader() });
    kbForm.value = { name: '', document_type: 'policy', description: '', tags: '', file: null };
    if (kbUploadInputRef.value) kbUploadInputRef.value.value = '';
    kbInfo.value = 'Document uploaded, chunked, and embedded.';
    await loadKnowledgeDocuments();
  } catch (err) {
    kbError.value = extractErrorMessage(err, 'Failed to upload document.');
  } finally {
    isUploadingKb.value = false;
  }
};

const approveKnowledgeDocument = async (documentId) => {
  kbError.value = '';
  try {
    await kbApi.post(`/documents/${documentId}/approve`, { notes: null }, { headers: authHeader() });
    await loadKnowledgeDocuments();
  } catch (err) {
    kbError.value = extractErrorMessage(err, 'Failed to approve document.');
  }
};

const rejectKnowledgeDocument = async (documentId) => {
  kbError.value = '';
  try {
    await kbApi.post(`/documents/${documentId}/reject`, { notes: null }, { headers: authHeader() });
    await loadKnowledgeDocuments();
  } catch (err) {
    kbError.value = extractErrorMessage(err, 'Failed to reject document.');
  }
};

// Chunk viewer: per-doc expanded panel showing the embedded chunks.
// Lazy-loaded — we don't blow up the list response with chunk text for
// every doc; we fetch on-demand when the admin clicks "View chunks".
const kbChunksByDoc = ref({});      // documentId -> {chunks, loading, error}
const kbExpandedDocs = ref({});     // documentId -> true when panel is open

const toggleKnowledgeChunks = async (documentId) => {
  const isOpen = !!kbExpandedDocs.value[documentId];
  if (isOpen) {
    kbExpandedDocs.value = { ...kbExpandedDocs.value, [documentId]: false };
    return;
  }
  kbExpandedDocs.value = { ...kbExpandedDocs.value, [documentId]: true };
  // Already loaded — don't refetch unless the user explicitly refreshes.
  if (kbChunksByDoc.value[documentId]?.chunks) return;
  kbChunksByDoc.value = {
    ...kbChunksByDoc.value,
    [documentId]: { loading: true, error: '', chunks: null },
  };
  try {
    const { data } = await kbApi.get(`/documents/${documentId}/chunks`, { headers: authHeader() });
    kbChunksByDoc.value = {
      ...kbChunksByDoc.value,
      [documentId]: { loading: false, error: '', chunks: data.chunks || [], info: data },
    };
  } catch (err) {
    kbChunksByDoc.value = {
      ...kbChunksByDoc.value,
      [documentId]: {
        loading: false,
        error: extractErrorMessage(err, 'Failed to load chunks.'),
        chunks: null,
      },
    };
  }
};

const reconcileKnowledgeDocuments = async () => {
  isReconcilingKb.value = true;
  kbError.value = '';
  kbInfo.value = '';
  try {
    const { data } = await kbApi.post('/documents/reconcile', {}, { headers: authHeader() });
    const count = data?.reconciled ?? 0;
    if (count > 0) {
      kbInfo.value = `Reconciled ${count} orphaned document${count === 1 ? '' : 's'} from Qdrant.`;
    } else {
      kbInfo.value = 'No orphaned documents found in Qdrant — registry is already in sync.';
    }
    await loadKnowledgeDocuments();
  } catch (err) {
    kbError.value = extractErrorMessage(err, 'Reconcile failed.');
  } finally {
    isReconcilingKb.value = false;
  }
};

const removeKnowledgeDocument = async (documentId, documentName) => {
  const label = documentName ? `"${documentName}"` : 'this document';
  if (!window.confirm(`Remove ${label}?\n\nThis deletes the document, its Qdrant embeddings, its source blob in Azure Storage, and any policy/answer cards it produced. The agent will stop using it immediately. This cannot be undone.`)) {
    return;
  }
  kbError.value = '';
  kbInfo.value = '';
  try {
    const { data } = await kbApi.delete(`/documents/${documentId}`, { headers: authHeader() });
    const parts = ['Document removed.'];
    if (data?.qdrant_deleted) parts.push('Qdrant vectors deleted.');
    else parts.push('Qdrant delete skipped (unreachable).');
    if (typeof data?.blobs_deleted === 'number') {
      parts.push(`${data.blobs_deleted} blob${data.blobs_deleted === 1 ? '' : 's'} deleted from storage.`);
    }
    kbInfo.value = parts.join(' ');
    await loadKnowledgeDocuments();
  } catch (err) {
    kbError.value = extractErrorMessage(err, 'Failed to remove document.');
  }
};

const testKnowledgeRetrieval = async () => {
  if (!kbQuery.value.trim()) {
    kbError.value = 'Type a query to search the knowledge base.';
    return;
  }
  isSearchingKb.value = true;
  kbError.value = '';
  kbResults.value = [];
  try {
    const { data } = await kbApi.post(
      '/test-retrieval',
      { query: kbQuery.value.trim(), top_k: 5 },
      { headers: authHeader() },
    );
    kbResults.value = data.chunks || [];
    if (!kbResults.value.length && data.refusal) {
      kbInfo.value = data.refusal;
    } else {
      kbInfo.value = '';
    }
  } catch (err) {
    kbError.value = extractErrorMessage(err, 'Retrieval test failed.');
  } finally {
    isSearchingKb.value = false;
  }
};

const formatBytes = (bytes) => {
  const n = Number(bytes);
  if (!n || Number.isNaN(n)) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
};

// ─────────────────────── Agent Studio: pipeline / phone / campaigns / voice ───────────────────────

const agentsApi = axios.create({ baseURL: 'http://localhost:8000/api/nokvo-one/agents' });

const loadRuntimeStatus = async () => {
  try {
    const { data } = await agentsApi.get('/runtime/status', { headers: authHeader() });
    runtimeStatus.value = data;
  } catch (err) {
    runtimeStatus.value = null;
  }
};

const loadPhoneLink = async () => {
  try {
    const { data } = await agentsApi.get('/phone-link', { headers: authHeader() });
    phoneLink.value = data;
    phoneLinkInput.value = data.link_id || '';
  } catch (err) {
    phoneLink.value = null;
  }
};

const savePhoneLink = async () => {
  isSavingPhoneLink.value = true;
  try {
    const { data } = await agentsApi.post(
      '/phone-link',
      { link_id: phoneLinkInput.value.trim() || null },
      { headers: authHeader() },
    );
    phoneLink.value = data;
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Failed to save phone link.');
  } finally {
    isSavingPhoneLink.value = false;
  }
};

const loadCampaigns = async () => {
  try {
    const { data } = await agentsApi.get('/campaigns', { headers: authHeader() });
    campaigns.value = data || [];
  } catch (err) {
    campaigns.value = [];
  }
};

const loadLeadConnections = async () => {
  const { data } = await agentsApi.get('/lead-sources/connections', { headers: authHeader() });
  leadConnections.value = data || [];
  const nextInputs = { ...connectionAccountInputs.value };
  for (const connection of leadConnections.value) {
    nextInputs[connection.id] = connection.provider_account_id || connection.metadata?.customer_id || '';
  }
  connectionAccountInputs.value = nextInputs;
};

const loadLeadForms = async () => {
  const { data } = await agentsApi.get('/lead-sources/forms', { headers: authHeader() });
  leadForms.value = data || [];
};

const loadOutgoingLeads = async () => {
  const { data } = await agentsApi.get('/lead-sources/leads', {
    headers: authHeader(),
    params: { limit: 300 },
  });
  outgoingLeads.value = data || [];
  selectedLeadIds.value = selectedLeadIds.value.filter((id) =>
    outgoingLeads.value.some((lead) => lead.id === id && lead.callable),
  );
};

const loadOutgoingAgentWorkspace = async () => {
  isLoadingLeadSources.value = true;
  try {
    await Promise.all([loadLeadConnections(), loadLeadForms(), loadOutgoingLeads()]);
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Failed to load outgoing lead sources.');
  } finally {
    isLoadingLeadSources.value = false;
  }
};

const leadOAuthMode = (provider, channel = null) => {
  if (provider === 'meta_ads') return channel || 'facebook_ads';
  return provider === 'google_forms' ? 'forms' : 'ads';
};

const requestLeadOAuth = (provider, channel = null) => {
  if (provider === 'meta_ads' && channel === 'instagram_ads') {
    pendingLeadOAuth.value = {
      provider,
      channel,
      title: 'Connect Instagram Ads',
      actionLabel: 'Continue with Meta',
    };
    return;
  }
  startLeadOAuth(provider, channel);
};

const closeLeadOAuthNotice = () => {
  pendingLeadOAuth.value = null;
};

const continuePendingLeadOAuth = async () => {
  const pending = pendingLeadOAuth.value;
  pendingLeadOAuth.value = null;
  if (!pending) return;
  await startLeadOAuth(pending.provider, pending.channel);
};

const startLeadOAuth = async (provider, channel = null) => {
  try {
    const { data } = await agentsApi.post(
      '/lead-sources/oauth/start',
      { provider, mode: leadOAuthMode(provider, channel) },
      { headers: authHeader() },
    );
    if (data.authorization_url) window.location.href = data.authorization_url;
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Could not start OAuth.');
  }
};

const saveConnectionAccount = async (connection) => {
  try {
    const accountId = (connectionAccountInputs.value[connection.id] || '').trim();
    await agentsApi.patch(
      `/lead-sources/connections/${connection.id}`,
      {
        provider_account_id: accountId || null,
        metadata: connection.provider === 'google_ads' ? { customer_id: accountId } : {},
      },
      { headers: authHeader() },
    );
    await loadLeadConnections();
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Could not update connection.');
  }
};

const syncLeadConnection = async (connectionId) => {
  isSyncingLeadConnection.value = connectionId;
  try {
    const { data } = await agentsApi.post(`/lead-sources/connections/${connectionId}/sync`, {}, { headers: authHeader() });
    await Promise.all([loadLeadConnections(), loadLeadForms(), loadOutgoingLeads()]);
    infoMsg.value = `Synced ${data.leads || 0} lead(s).`;
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Lead sync failed.');
  } finally {
    isSyncingLeadConnection.value = null;
  }
};

const createNokvoLeadForm = async () => {
  try {
    await agentsApi.post('/lead-sources/nokvo-forms', nokvoLeadForm.value, { headers: authHeader() });
    nokvoLeadForm.value = {
      name: '',
      consent_text: 'I agree to receive a phone call from this business about my enquiry.',
      fields: [{ key: 'email', label: 'Email', type: 'email', required: false }],
    };
    await loadLeadForms();
    infoMsg.value = 'Nokvo lead form created.';
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Could not create Nokvo form.');
  }
};

const registerExternalLeadForm = async () => {
  try {
    let mapping = {};
    if (externalLeadForm.value.field_mapping.trim()) {
      mapping = JSON.parse(externalLeadForm.value.field_mapping);
    }
    await agentsApi.post(
      '/lead-sources/forms',
      {
        provider: externalLeadForm.value.provider,
        name: externalLeadForm.value.name,
        provider_form_id: externalLeadForm.value.provider_form_id,
        source_connection_id: externalLeadForm.value.source_connection_id || null,
        field_mapping: mapping,
        consent_field_key: externalLeadForm.value.consent_field_key || null,
        consent_text: externalLeadForm.value.consent_text || null,
        default_call_consent: externalLeadForm.value.default_call_consent,
      },
      { headers: authHeader() },
    );
    externalLeadForm.value.name = '';
    externalLeadForm.value.provider_form_id = '';
    await loadLeadForms();
    infoMsg.value = 'External form registered.';
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Could not register form.');
  }
};

const toggleLeadSelection = (lead) => {
  if (!lead.callable) return;
  const idx = selectedLeadIds.value.indexOf(lead.id);
  if (idx >= 0) selectedLeadIds.value.splice(idx, 1);
  else selectedLeadIds.value.push(lead.id);
};

const onCampaignFile = (event, key) => {
  campaignForm.value[key] = event.target.files?.[0] || null;
};

const createCampaign = async () => {
  if (!campaignForm.value.name.trim() || !selectedCallableLeads.value.length || !campaignForm.value.doc_file) {
    errorMsg.value = 'Campaign name, at least one consented lead, and script document are required.';
    return;
  }
  isCreatingCampaign.value = true;
  try {
    const fd = new FormData();
    fd.append('name', campaignForm.value.name.trim());
    if (campaignForm.value.from_number.trim()) fd.append('from_number', campaignForm.value.from_number.trim());
    if (campaignForm.value.agent_prompt.trim()) fd.append('agent_prompt', campaignForm.value.agent_prompt.trim());
    if (campaignForm.value.objectives.trim()) fd.append('objectives', JSON.stringify(campaignForm.value.objectives.split('\n').map((item) => item.trim()).filter(Boolean)));
    if (campaignForm.value.exit_conditions.trim()) fd.append('exit_conditions', JSON.stringify(campaignForm.value.exit_conditions.split('\n').map((item) => item.trim()).filter(Boolean)));
    if (campaignForm.value.tone.trim()) fd.append('tone', campaignForm.value.tone.trim());
    fd.append('silence_timeout_seconds', String(campaignForm.value.silence_timeout_seconds || 5));
    fd.append('lead_ids', JSON.stringify(selectedCallableLeads.value.map((lead) => lead.id)));
    fd.append('doc_file', campaignForm.value.doc_file);
    await agentsApi.post('/campaigns', fd, { headers: { ...authHeader(), 'Content-Type': 'multipart/form-data' } });
    campaignForm.value = emptyCampaignForm();
    selectedLeadIds.value = [];
    await loadCampaigns();
    await loadOutgoingLeads();
    infoMsg.value = 'Campaign created and script ingested.';
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Failed to create campaign.');
  } finally {
    isCreatingCampaign.value = false;
  }
};

const launchCampaign = async (id) => {
  isLaunchingCampaign.value = id;
  try {
    await agentsApi.post(`/campaigns/${id}/launch`, {}, { headers: authHeader() });
    await loadCampaigns();
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Failed to launch campaign.');
  } finally {
    isLaunchingCampaign.value = null;
  }
};

const cancelCampaign = async (id) => {
  try {
    await agentsApi.post(`/campaigns/${id}/cancel`, {}, { headers: authHeader() });
    await loadCampaigns();
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Failed to cancel campaign.');
  }
};

// ───── Voice tester (browser mic ↔ WebSocket ↔ Sarvam STT ↔ pipeline ↔ Sarvam TTS) ─────

const downsampleTo16k = (input, inputRate) => {
  if (inputRate === 16000) return input;
  const ratio = inputRate / 16000;
  const newLen = Math.round(input.length / ratio);
  const out = new Float32Array(newLen);
  let pos = 0;
  let idx = 0;
  while (pos < newLen) {
    const nextIdx = Math.round((pos + 1) * ratio);
    let sum = 0;
    let count = 0;
    for (let i = idx; i < nextIdx && i < input.length; i++) {
      sum += input[i];
      count += 1;
    }
    out[pos] = count > 0 ? sum / count : 0;
    pos += 1;
    idx = nextIdx;
  }
  return out;
};

const floatToInt16 = (input) => {
  const buffer = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]));
    buffer[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return buffer;
};

// Gapless playback with generation-counter for barge-in safety.
// Approach (ported from agent_lab/useVoiceCall.ts):
//   - Each decoded buffer is scheduled at max(now, nextPlaybackTime) so
//     adjacent sentences play seamlessly with no inter-sentence click.
//   - playbackGeneration is bumped on barge-in / interrupt so any decode
//     still in flight is dropped instead of getting scheduled after the
//     user has already started a new turn.
//   - playbackChain serializes decodeAudioData() across chunks so they
//     land in arrival order even though decode is async.
const enqueueTtsAudio = (base64) => {
  if (!voice.value.audioCtx) {
    voice.value.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    voice.value.nextPlaybackTime = voice.value.audioCtx.currentTime;
  }
  const gen = voice.value.playbackGeneration;
  voice.value.pendingPlaybackChunks = (voice.value.pendingPlaybackChunks || 0) + 1;
  voice.value.playbackChain = (voice.value.playbackChain || Promise.resolve())
    .then(() => decodeAndSchedule(base64, gen))
    .catch((err) => console.warn('TTS decode/play failed', err))
    .finally(() => {
      voice.value.pendingPlaybackChunks = Math.max(0, (voice.value.pendingPlaybackChunks || 0) - 1);
      if (!hasPendingOrActiveAgentAudio()) {
        voice.value.playbackCaptureBlockedUntil = performance.now() + PLAYBACK_CAPTURE_GRACE_MS;
        if (voice.value.status === 'speaking') voice.value.status = 'listening';
      }
    });
};

const decodeAndSchedule = async (base64, gen) => {
  if (!voice.value.audioCtx || gen !== voice.value.playbackGeneration) return;
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  const buffer = await voice.value.audioCtx.decodeAudioData(bytes.buffer.slice(0));
  // Re-check after the await — barge-in may have invalidated this chunk.
  if (gen !== voice.value.playbackGeneration) return;
  const src = voice.value.audioCtx.createBufferSource();
  src.buffer = buffer;
  src.connect(voice.value.audioCtx.destination);
  const startAt = Math.max(voice.value.audioCtx.currentTime + 0.02, voice.value.nextPlaybackTime);
  src.start(startAt);
  voice.value.nextPlaybackTime = startAt + buffer.duration;
  voice.value.scheduledSources = voice.value.scheduledSources || [];
  voice.value.scheduledSources.push(src);
  voice.value.status = 'speaking';
  src.onended = () => {
    voice.value.scheduledSources = (voice.value.scheduledSources || []).filter((s) => s !== src);
    if (!hasPendingOrActiveAgentAudio()) {
      // Brief grace period so the VAD doesn't re-capture residual playback
      // echo. Matches agent_lab's PLAYBACK_CAPTURE_GRACE_MS.
      voice.value.playbackCaptureBlockedUntil = performance.now() + PLAYBACK_CAPTURE_GRACE_MS;
      if (voice.value.status === 'speaking') voice.value.status = 'listening';
    }
  };
};

const hasPendingOrActiveAgentAudio = () => {
  if (!voice.value.audioCtx) return (voice.value.pendingPlaybackChunks || 0) > 0;
  return (
    (voice.value.pendingPlaybackChunks || 0) > 0 ||
    (voice.value.scheduledSources || []).length > 0 ||
    voice.value.nextPlaybackTime > voice.value.audioCtx.currentTime + 0.05
  );
};

// Called on barge-in / interrupt: stop everything currently playing or
// pending and bump the generation so in-flight decode tasks bail out
// before scheduling.
const stopAllPlayback = () => {
  voice.value.playbackGeneration = (voice.value.playbackGeneration || 0) + 1;
  for (const src of voice.value.scheduledSources || []) {
    try { src.stop(); } catch (_e) {}
  }
  voice.value.scheduledSources = [];
  voice.value.pendingPlaybackChunks = 0;
  if (voice.value.audioCtx) {
    voice.value.nextPlaybackTime = voice.value.audioCtx.currentTime;
  }
};

// ── VAD-based recorder (browser-side end-of-utterance detection) ──
// We continuously capture raw PCM via a ScriptProcessorNode into a small
// rolling pre-roll buffer. When the VAD detects speech we copy the pre-roll
// into the utterance buffer and keep appending live frames; on silence we
// wrap the whole utterance as 16-kHz mono WAV and send it to the server.
//
// Why not MediaRecorder? Starting MediaRecorder *after* VAD says "speech"
// loses the first ~50-100ms of the utterance (one VAD poll cycle + recorder
// spin-up). That clips leading consonants like "h-", "y-", "I'd", which is
// the symptom users see as "STT is not picking up my words."
const VAD_SPEECH_THRESHOLD = 0.022;       // RMS over this counts as speech (was 0.04 — too high for soft-gain mics)
const VAD_BARGE_THRESHOLD = 0.07;         // higher threshold during agent playback to avoid echo bleeding
const VAD_END_SILENCE_MS = 750;           // silence that marks end of utterance — short enough to stay snappy, long enough that natural mid-thought pauses (≤500ms) don't fire early
const VAD_MIN_UTTERANCE_MS = 220;         // ignore taps / coughs shorter than this
const VAD_POLL_INTERVAL_MS = 30;          // faster polling so speech-start fires sooner
const PRE_ROLL_MS = 320;                  // how much audio *before* VAD detection we include in the utterance
const PRE_ROLL_RING_MS = 600;             // ring-buffer capacity (must exceed PRE_ROLL_MS)
const TARGET_STT_SAMPLE_RATE = 16000;     // backend / Sarvam STT expects 16-kHz mono
const PLAYBACK_CAPTURE_GRACE_MS = 350;    // ignore mic for this long after agent stops playing

const writeWavString = (view, offset, text) => {
  for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
};

const pcm16ToWav = (int16, sampleRate) => {
  const dataSize = int16.byteLength;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);
  writeWavString(view, 0, 'RIFF');
  view.setUint32(4, 36 + dataSize, true);
  writeWavString(view, 8, 'WAVE');
  writeWavString(view, 12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeWavString(view, 36, 'data');
  view.setUint32(40, dataSize, true);
  new Uint8Array(buffer, 44).set(new Uint8Array(int16.buffer, int16.byteOffset, int16.byteLength));
  return buffer;
};

const concatFloat32 = (chunks) => {
  let total = 0;
  for (const c of chunks) total += c.length;
  const out = new Float32Array(total);
  let offset = 0;
  for (const c of chunks) {
    out.set(c, offset);
    offset += c.length;
  }
  return out;
};

const tickVad = () => {
  if (!voice.value.analyser || !voice.value.analysisBuffer) return;
  voice.value.analyser.getByteTimeDomainData(voice.value.analysisBuffer);
  let sum = 0;
  for (let i = 0; i < voice.value.analysisBuffer.length; i++) {
    const v = (voice.value.analysisBuffer[i] - 128) / 128;
    sum += v * v;
  }
  const rms = Math.sqrt(sum / voice.value.analysisBuffer.length);
  voice.value.micLevel = rms;

  const now = performance.now();
  const s = voice.value.status;
  const playbackActive = hasPendingOrActiveAgentAudio() || now < (voice.value.playbackCaptureBlockedUntil || 0);

  // Barge-in: caller is speaking while the agent is producing audio.
  if ((s === 'speaking' || s === 'thinking') && rms > VAD_BARGE_THRESHOLD) {
    stopAllPlayback();
    if (voice.value.ws && voice.value.ws.readyState === WebSocket.OPEN) {
      try { voice.value.ws.send(JSON.stringify({ type: 'interrupt' })); } catch {}
    }
    voice.value.status = 'listening';
    return;
  }
  if (playbackActive) return;
  if (s !== 'listening' && s !== 'recording') return;

  if (rms > VAD_SPEECH_THRESHOLD) {
    voice.value.silenceStartTime = 0;
    if (!voice.value.isInSpeech) {
      voice.value.isInSpeech = true;
      voice.value.speechStartTime = now;
      beginUtteranceCapture();
      voice.value.status = 'recording';
    }
  } else if (voice.value.isInSpeech) {
    if (!voice.value.silenceStartTime) voice.value.silenceStartTime = now;
    if (now - voice.value.silenceStartTime > VAD_END_SILENCE_MS) {
      const duration = now - voice.value.speechStartTime;
      voice.value.isInSpeech = false;
      voice.value.silenceStartTime = 0;
      if (duration > VAD_MIN_UTTERANCE_MS) {
        finishUtteranceCaptureAndSend();
        voice.value.status = 'thinking';
      } else {
        discardUtteranceCapture();
        voice.value.status = 'listening';
      }
    }
  }
};

const setupPcmCapture = (audioCtx, micStream) => {
  const inputRate = audioCtx.sampleRate;
  const source = audioCtx.createMediaStreamSource(micStream);
  // ScriptProcessor must have a sink to keep firing onaudioprocess; we route
  // the captured signal through a zero-gain node so it never reaches the
  // speakers (otherwise the agent's TTS gets echo-mixed with the user's voice).
  const processor = audioCtx.createScriptProcessor(2048, 1, 1);
  const muteGain = audioCtx.createGain();
  muteGain.gain.value = 0;
  source.connect(processor);
  processor.connect(muteGain);
  muteGain.connect(audioCtx.destination);

  voice.value.pcmInputRate = inputRate;
  voice.value.pcmRing = [];
  voice.value.pcmRingDurationMs = 0;
  voice.value.utteranceChunks = [];

  processor.onaudioprocess = (e) => {
    const input = e.inputBuffer.getChannelData(0);
    const copy = new Float32Array(input.length);
    copy.set(input);
    const chunkMs = (copy.length / inputRate) * 1000;
    voice.value.pcmRing.push(copy);
    voice.value.pcmRingDurationMs += chunkMs;
    while (voice.value.pcmRingDurationMs > PRE_ROLL_RING_MS && voice.value.pcmRing.length > 1) {
      const removed = voice.value.pcmRing.shift();
      voice.value.pcmRingDurationMs -= (removed.length / inputRate) * 1000;
    }
    if (voice.value.isInSpeech && voice.value.utteranceChunks) {
      voice.value.utteranceChunks.push(copy);
    }
  };

  voice.value.pcmProcessor = processor;
  voice.value.pcmMuteGain = muteGain;
  voice.value.pcmSource = source;
};

const teardownPcmCapture = () => {
  if (voice.value.pcmProcessor) {
    try { voice.value.pcmProcessor.onaudioprocess = null; } catch {}
    try { voice.value.pcmProcessor.disconnect(); } catch {}
  }
  if (voice.value.pcmMuteGain) {
    try { voice.value.pcmMuteGain.disconnect(); } catch {}
  }
  if (voice.value.pcmSource) {
    try { voice.value.pcmSource.disconnect(); } catch {}
  }
  voice.value.pcmProcessor = null;
  voice.value.pcmMuteGain = null;
  voice.value.pcmSource = null;
  voice.value.pcmRing = [];
  voice.value.pcmRingDurationMs = 0;
  voice.value.utteranceChunks = [];
  voice.value.pcmInputRate = 0;
};

const beginUtteranceCapture = () => {
  // Seed the utterance buffer with the last PRE_ROLL_MS of audio so we never
  // miss the leading consonant of the user's first word.
  const inputRate = voice.value.pcmInputRate || 48000;
  const ring = voice.value.pcmRing || [];
  const wanted = (PRE_ROLL_MS / 1000) * inputRate;
  let total = 0;
  const fromEnd = [];
  for (let i = ring.length - 1; i >= 0 && total < wanted; i--) {
    fromEnd.unshift(ring[i]);
    total += ring[i].length;
  }
  // Trim oldest pre-roll samples down to exactly PRE_ROLL_MS worth.
  if (fromEnd.length > 0 && total > wanted) {
    const drop = total - wanted;
    fromEnd[0] = fromEnd[0].subarray(Math.floor(drop));
  }
  voice.value.utteranceChunks = fromEnd.length ? [...fromEnd] : [];
};

const finishUtteranceCaptureAndSend = () => {
  const inputRate = voice.value.pcmInputRate || 48000;
  const chunks = voice.value.utteranceChunks || [];
  voice.value.utteranceChunks = [];
  if (!chunks.length) return;
  if (!voice.value.ws || voice.value.ws.readyState !== WebSocket.OPEN) return;
  const merged = concatFloat32(chunks);
  const downsampled = inputRate === TARGET_STT_SAMPLE_RATE
    ? merged
    : downsampleTo16k(merged, inputRate);
  const int16 = floatToInt16(downsampled);
  const wav = pcm16ToWav(int16, TARGET_STT_SAMPLE_RATE);
  try {
    voice.value.ws.send(wav);
  } catch (err) {
    console.warn('Failed to send utterance WAV', err);
  }
};

const discardUtteranceCapture = () => {
  voice.value.utteranceChunks = [];
};

const startAmbienceBed = (audioCtx) => {
  const cfg = authConfig.value?.call_center_ambience;
  if (!cfg?.enabled || !audioCtx) return;
  const urls = Array.isArray(cfg.urls) ? cfg.urls : [];
  if (!urls.length) return;
  const url = urls[Math.floor(Math.random() * urls.length)];
  const apiOrigin = api.defaults?.baseURL ? new URL(api.defaults.baseURL).origin : '';
  const audioEl = new Audio(`${apiOrigin}${url}`);
  audioEl.crossOrigin = 'anonymous';
  audioEl.loop = true;
  audioEl.preload = 'auto';
  try {
    const source = audioCtx.createMediaElementSource(audioEl);
    const gain = audioCtx.createGain();
    gain.gain.value = Math.max(0, Math.min(1, Number(cfg.volume ?? 0.28)));
    source.connect(gain);
    gain.connect(audioCtx.destination);
    audioEl.play().catch(() => { /* autoplay can be blocked until user gesture; ignored */ });
    voice.value.ambienceAudio = audioEl;
    voice.value.ambienceGain = gain;
  } catch (err) {
    // CORS, decode errors, etc. — silently drop, ambience is best-effort.
    try { audioEl.pause(); } catch {}
    voice.value.ambienceAudio = null;
    voice.value.ambienceGain = null;
  }
};

const stopAmbienceBed = () => {
  const el = voice.value.ambienceAudio;
  if (el) {
    try { el.pause(); } catch {}
    try { el.src = ''; } catch {}
  }
  if (voice.value.ambienceGain) {
    try { voice.value.ambienceGain.disconnect(); } catch {}
  }
  voice.value.ambienceAudio = null;
  voice.value.ambienceGain = null;
};

const startVoiceCall = async () => {
  if (voice.value.status !== 'idle' && voice.value.status !== 'error') return;
  voice.value.status = 'connecting';
  voice.value.errorMsg = '';
  voice.value.turns = [];
  voice.value.liveTranscript = '';
  voice.value.playbackGeneration = 0;
  voice.value.scheduledSources = [];
  voice.value.pendingPlaybackChunks = 0;
  voice.value.playbackChain = Promise.resolve();
  voice.value.firstSentenceMs = null;
  voice.value.ttsFirstAudioMs = null;
  // VAD state
  voice.value.isInSpeech = false;
  voice.value.speechStartTime = 0;
  voice.value.silenceStartTime = 0;
  voice.value.playbackCaptureBlockedUntil = 0;
  voice.value.utteranceChunks = [];
  voice.value.pcmRing = [];
  voice.value.pcmRingDurationMs = 0;

  const token = localStorage.getItem(ACCESS_TOKEN_KEY);
  if (!token) {
    voice.value.status = 'error';
    voice.value.errorMsg = 'Not authenticated.';
    return;
  }

  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    voice.value.micStream = stream;
    voice.value.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (voice.value.audioCtx.state === 'suspended') {
      try { await voice.value.audioCtx.resume(); } catch {}
    }
    voice.value.nextPlaybackTime = voice.value.audioCtx.currentTime;

    // Mix a low-volume call-center ambience under the agent's voice so the
    // call feels like a real workspace. Audio is CC0 and pre-shipped at
    // /assets/audio/call_center_ambience/. Disabled if /config disabled it
    // or no files were found server-side.
    startAmbienceBed(voice.value.audioCtx);

    // Analyser feeds the VAD loop. NOT connected to destination so the mic
    // isn't echoed into the speakers.
    const source = voice.value.audioCtx.createMediaStreamSource(stream);
    const analyser = voice.value.audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    source.connect(analyser);
    voice.value.analyser = analyser;
    voice.value.analysisBuffer = new Uint8Array(analyser.fftSize);

    // Continuous PCM capture with rolling pre-roll buffer (replaces MediaRecorder).
    setupPcmCapture(voice.value.audioCtx, stream);

    const wsUrl = `ws://localhost:8000/api/nokvo-one/agents/voice/ws?token=${encodeURIComponent(token)}`;
    const ws = new WebSocket(wsUrl);
    ws.binaryType = 'arraybuffer';
    voice.value.ws = ws;

    ws.onopen = () => {
      // Tell the backend we're using browser VAD — it will treat each binary
      // frame as a complete utterance instead of streaming PCM.
      ws.send(JSON.stringify({ type: 'config', language: voice.value.language, mode: 'vad_blob' }));
    };

    ws.onmessage = (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch { return; }
      handleVoiceEvent(msg);
    };

    ws.onerror = () => {
      voice.value.status = 'error';
      voice.value.errorMsg = 'WebSocket error.';
    };

    ws.onclose = () => {
      cleanupVoiceCall();
    };

    // Start VAD loop
    if (voice.value.vadTimer) clearInterval(voice.value.vadTimer);
    voice.value.vadTimer = setInterval(tickVad, VAD_POLL_INTERVAL_MS);
  } catch (err) {
    voice.value.status = 'error';
    voice.value.errorMsg = err?.message || 'Failed to start microphone.';
  }
};

const handleVoiceEvent = (msg) => {
  switch (msg.type) {
    case 'voice_session_ready':
      voice.value.callId = msg.call_id;
      voice.value.status = 'listening';
      break;
    case 'runtime_status':
      runtimeStatus.value = msg;
      break;
    case 'stt_transcript':
      voice.value.liveTranscript = msg.text;
      voice.value.transcriptLang = msg.language || voice.value.language;
      break;
    case 'stt_finished': {
      voice.value.status = 'thinking';
      const turn = { id: msg.turn_id, query: msg.text, sentences: [], answer: '', latencyMs: null, cacheHit: false, citations: [] };
      voice.value.turns.push(turn);
      voice.value.liveTranscript = '';
      break;
    }
    case 'agent_sentence': {
      const turn = voice.value.turns.find(t => t.id === msg.turn_id);
      if (turn) {
        turn.sentences.push(msg.sentence);
        turn.cacheHit = msg.cache_hit;
        if (msg.first_sentence_ms != null) {
          turn.latencyMs = msg.first_sentence_ms;
          voice.value.firstSentenceMs = msg.first_sentence_ms;
        }
      }
      voice.value.status = 'speaking';
      break;
    }
    case 'tts_first_audio':
      voice.value.ttsFirstAudioMs = msg.first_audio_latency_ms;
      break;
    case 'tts_audio':
      enqueueTtsAudio(msg.audio_base64);
      break;
    case 'barge_in_detected':
      stopAllPlayback();
      voice.value.status = 'listening';
      break;
    case 'language_locked':
      voice.value.transcriptLang = msg.language;
      break;
    case 'agent_answer': {
      const turn = voice.value.turns.find(t => t.id === msg.turn_id);
      if (turn) {
        turn.answer = msg.answer;
        turn.citations = msg.citations || [];
      }
      break;
    }
    case 'turn_complete':
      if (voice.value.status === 'thinking') voice.value.status = 'listening';
      break;
    case 'agent_error':
      voice.value.errorMsg = msg.error;
      voice.value.status = 'error';
      break;
    case 'stt_error':
      // Prefer the human-friendly user_message when present (e.g. Sarvam
      // rate-limit). Falls back to the raw error_message for everything else.
      voice.value.errorMsg = msg.user_message || msg.error_message || 'STT failed';
      if (voice.value.status === 'thinking') voice.value.status = 'listening';
      break;
    default:
      break;
  }
};

const cleanupVoiceCall = () => {
  // Stop VAD polling FIRST so it can't try to start a new capture mid-cleanup.
  if (voice.value.vadTimer) {
    try { clearInterval(voice.value.vadTimer); } catch {}
    voice.value.vadTimer = null;
  }
  teardownPcmCapture();
  stopAmbienceBed();
  if (voice.value.micNode) try { voice.value.micNode.disconnect(); } catch {}
  if (voice.value.micStream) voice.value.micStream.getTracks().forEach(t => t.stop());
  if (voice.value.audioCtx) try { voice.value.audioCtx.close(); } catch {}
  voice.value.ws = null;
  voice.value.micStream = null;
  voice.value.micNode = null;
  voice.value.audioCtx = null;
  voice.value.analyser = null;
  voice.value.analysisBuffer = null;
  voice.value.isInSpeech = false;
  voice.value.silenceStartTime = 0;
  voice.value.scheduledSources = [];
  voice.value.pendingPlaybackChunks = 0;
  voice.value.playbackChain = Promise.resolve();
  voice.value.playbackGeneration = (voice.value.playbackGeneration || 0) + 1;
  if (voice.value.status !== 'error') voice.value.status = 'idle';
};

const endVoiceCall = () => {
  if (voice.value.ws && voice.value.ws.readyState === WebSocket.OPEN) {
    try { voice.value.ws.send(JSON.stringify({ type: 'stop' })); } catch {}
    try { voice.value.ws.close(); } catch {}
  } else {
    cleanupVoiceCall();
  }
};

const sendTextToVoiceAgent = () => {
  if (!voice.value.ws || voice.value.ws.readyState !== WebSocket.OPEN) return;
  const text = (chatInput.value || '').trim();
  if (!text) return;
  voice.value.ws.send(JSON.stringify({ type: 'text_query', text, language: voice.value.language }));
  chatInput.value = '';
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
  assignmentSettings.value = [];
  clinicScheduleSettings.value = {};
  blockedSlots.value = {};
  agents.value = [];
  predefinedTools.value = [];
  toolCatalogGroups.value = [];
  toolCatalogDefaults.value = [];
  customTabs.value = [];
  outcomeWizard.value = { outcomes: [], selected: {}, agentName: '', isSaving: false };
  sampleUpload.value = { mode: 'document', file: null, prompt: '', isUploading: false };
  organizationBusinessTemplate.value = null;
  kbDocuments.value = [];
  kbResults.value = [];
  kbQuery.value = '';
  kbInfo.value = '';
  kbError.value = '';
  runtimeStatus.value = null;
  phoneLink.value = null;
  campaigns.value = [];
  endVoiceCall();
  closeFieldEdit();
  closeAssignmentEdit();
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

watch(authConfig, async () => {
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

    <div
      v-if="authState !== 'ready' || !['nokvo_connect', 'nokvo_connect_step2'].includes(currentPage)"
      class="mode-bar"
    >
      <button type="button" class="mode-link" @click="toggleThemeMode">
        <SunMedium v-if="themeMode === 'dark'" :size="14" />
        <Moon v-else :size="14" />
        {{ themeToggleLabel }}
      </button>
      <button
        v-if="authState === 'ready' && !isMemberOnly"
        type="button"
        class="mode-link mode-link--icon"
        aria-label="Notifications"
        title="Notifications"
      >
        <Bell :size="14" />
      </button>
      <div
        v-if="authState === 'ready' && !isMemberOnly && onboardingV2Enabled"
        class="mode-link-wrap"
      >
        <button
          type="button"
          class="mode-link mode-link--icon"
          :class="{ active: settingsMenuOpen }"
          aria-label="Settings"
          title="Settings"
          @click="settingsMenuOpen = !settingsMenuOpen"
        >
          <Settings2 :size="14" />
        </button>
        <div v-if="settingsMenuOpen" class="nav-settings-menu mode-settings-menu">
          <button
            type="button"
            class="nav-settings-item"
            @click="settingsMenuOpen = false; scrollToDashboardMembers()"
          >
            <strong>Team &amp; assignment</strong>
            <small>Invite teammates, set working hours and request caps.</small>
          </button>
          <button
            type="button"
            class="nav-settings-item"
            @click="settingsMenuOpen = false; switchPage('dashboard')"
          >
            <strong>Business type &amp; fields</strong>
            <small>Adjust the field schemas for leads, tickets, and appointments.</small>
          </button>
          <button
            type="button"
            class="nav-settings-item"
            @click="settingsMenuOpen = false; switchPage('dashboard')"
          >
            <strong>Custom tabs</strong>
            <small>Define org-specific resource tabs.</small>
          </button>
          <button
            type="button"
            class="nav-settings-item"
            @click="settingsMenuOpen = false; switchPage('agent')"
          >
            <strong>Advanced tool config</strong>
            <small>Pick individual agent tools and edit prompts.</small>
          </button>
        </div>
      </div>
      <button
        v-else-if="authState === 'ready' && !isMemberOnly"
        type="button"
        class="mode-link mode-link--icon"
        aria-label="Settings"
        title="Settings"
      >
        <Settings2 :size="14" />
      </button>
      <button
        v-if="authState === 'ready'"
        type="button"
        class="mode-link"
        @click="handleLogout"
      >
        <LogOut :size="14" />
        Log Out
      </button>
    </div>

    <main v-if="authState !== 'ready'" class="login-layout">
      <div class="brand-block">
        <img class="brand-block-logo" :src="nokvoLogo" alt="Nokvo One" />
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
          <div class="google-action">
            <div v-if="authConfig?.google_login_enabled" ref="googleLoginButtonRef" class="google-button-host" :class="{ disabled: isAuthenticating }"></div>
            <button v-else type="button" class="google-fallback-button" disabled>
              <span class="google-mark">G</span>
              Continue with Google
            </button>
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
          <div class="google-action">
            <div v-if="authConfig?.google_login_enabled" ref="googleSignupButtonRef" class="google-button-host" :class="{ disabled: isAuthenticating }"></div>
            <button v-else type="button" class="google-fallback-button" disabled>
              <span class="google-mark">G</span>
              Continue with Google
            </button>
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
            <strong>{{ mfaSetupMode === 'session_verify' ? 'Verify MFA' : 'Link Your Authenticator' }}</strong>
            <span>{{ currentUser?.email || signup.admin_email || inviteContext?.email }}</span>
          </div>
          <p v-if="mfaSetupMode === 'session_verify'" class="login-help compact">
            Enter the 6-digit code from the authenticator already linked to this work email.
          </p>
          <p v-else class="login-help compact">
            Scan this QR with the authenticator for this work email. Your TOTP secret is encrypted at rest.
          </p>
          <div v-if="mfaSetupMode !== 'session_verify' && totpUri" class="qr-shell">
            <QrcodeVue :value="totpUri" :size="168" level="M" background="#ffffff" foreground="#111111" />
          </div>
          <div v-if="mfaSetupMode !== 'session_verify' && totpSecret" class="secret-note">
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
            <button type="button" class="ghost-button" :disabled="isAuthenticating" @click="cancelTotpSetup">Cancel</button>
            <button type="button" class="primary-button" :disabled="isAuthenticating" @click="verifySignupTotp">
              {{ isAuthenticating ? 'Verifying...' : (mfaSetupMode === 'session_verify' ? 'Verify MFA' : 'Verify & Continue') }}
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

        <!-- BUSINESS TYPE SETUP -->
        <div v-else-if="authState === 'business_type_setup'" class="mfa-panel">
          <div class="mfa-head">
            <strong>Select Business Type</strong>
            <span>{{ currentOrganization?.name }} needs a template before the workspace opens.</span>
          </div>
          <div class="provider-grid business-type-grid">
            <label
              v-for="option in businessTypeOptions"
              :key="option.value"
              class="provider-option"
              :class="{ active: selectedBusinessType === option.value }"
            >
              <input v-model="selectedBusinessType" type="radio" class="sr-only" :value="option.value" />
              <strong class="provider-name">{{ option.label }}</strong>
              <small>
                {{ option.tabs.includes('appointments') ? 'Leads, Tickets, and Appointments' : 'Leads and Tickets' }}
              </small>
            </label>
          </div>
          <div v-if="selectedBusinessType" class="schema-preview">
            <strong>Default workspace fields</strong>
            <div class="schema-preview-grid">
              <span
                v-for="field in (businessTypeOptions.find((option) => option.value === selectedBusinessType)?.schemas?.leads || []).slice(0, 4)"
                :key="field.key"
              >
                {{ field.label }}
              </span>
            </div>
          </div>
          <div class="mfa-actions">
            <button type="button" class="ghost-button" :disabled="isSavingBusinessType" @click="handleLogout">Log out</button>
            <button type="button" class="primary-button" :disabled="!selectedBusinessType || isSavingBusinessType" @click="saveBusinessType">
              {{ isSavingBusinessType ? 'Saving...' : 'Continue' }}
            </button>
          </div>
        </div>

        <!-- ONBOARDING V2: OUTCOME WIZARD -->
        <div v-else-if="authState === 'outcome_setup'" class="mfa-panel outcome-wizard">
          <div class="mfa-head">
            <strong>What should your agent do?</strong>
            <span>Pick the outcomes you want — you can refine everything later in Settings.</span>
          </div>
          <div class="outcome-list">
            <label
              v-for="outcome in outcomeWizard.outcomes"
              :key="outcome.slug"
              class="provider-option outcome-option"
              :class="{ active: outcomeWizard.selected[outcome.slug] }"
            >
              <input
                type="checkbox"
                class="sr-only"
                :checked="!!outcomeWizard.selected[outcome.slug]"
                @change="toggleWizardOutcome(outcome.slug)"
              />
              <strong class="provider-name">{{ outcome.label }}</strong>
              <small>{{ outcome.description }}</small>
            </label>
          </div>
          <div class="db-form-block outcome-name-row">
            <label class="db-label" for="outcome-agent-name">Agent name</label>
            <input
              id="outcome-agent-name"
              v-model="outcomeWizard.agentName"
              class="db-input"
              type="text"
              placeholder="e.g. Clinic Assistant"
            />
          </div>
          <div class="mfa-actions">
            <button type="button" class="ghost-button" :disabled="outcomeWizard.isSaving" @click="authState = 'ready'">
              Skip for now
            </button>
            <button
              type="button"
              class="primary-button"
              :disabled="outcomeWizard.isSaving"
              @click="submitOutcomeWizard"
            >
              {{ outcomeWizard.isSaving ? 'Creating agent...' : 'Create my agent →' }}
            </button>
          </div>
        </div>

        <!-- ONBOARDING V2: SAMPLE KNOWLEDGE-BASE UPLOAD -->
        <div v-else-if="authState === 'sample_upload'" class="mfa-panel">
          <div class="mfa-head">
            <strong>Your agent is live — give it a knowledge boost</strong>
            <span>
              {{ activeAgent?.name || 'Your agent' }} is already configured. Optionally drop a
              document for retrieval, or refine the auto-generated prompt below. Skip if you'd
              rather start calling now.
            </span>
          </div>
          <div class="sample-mode-toggle">
            <button
              type="button"
              class="sample-mode-tab"
              :class="{ active: sampleUpload.mode === 'document' }"
              :disabled="sampleUpload.isUploading"
              @click="setSampleUploadMode('document')"
            >
              <strong>Upload a document</strong>
              <small>Best for FAQs, brochures, price lists. Indexed for retrieval.</small>
            </button>
            <button
              type="button"
              class="sample-mode-tab"
              :class="{ active: sampleUpload.mode === 'prompt' }"
              :disabled="sampleUpload.isUploading"
              @click="setSampleUploadMode('prompt')"
            >
              <strong>Single prompt</strong>
              <small>Best when you'd rather hand-write the agent's instructions in one paragraph.</small>
            </button>
          </div>

          <div v-if="sampleUpload.mode === 'document'" class="sample-upload-zone">
            <input
              id="sample-upload-input"
              type="file"
              accept=".pdf,.txt,.md,.docx,.csv"
              class="sr-only"
              @change="handleSampleFileChange"
            />
            <label for="sample-upload-input" class="provider-option sample-upload-label">
              <strong class="provider-name">
                {{ sampleUpload.file ? sampleUpload.file.name : 'Choose a file...' }}
              </strong>
              <small v-if="!sampleUpload.file">PDF, TXT, MD, DOCX, or CSV — up to a few MB.</small>
              <small v-else>{{ Math.round((sampleUpload.file.size || 0) / 1024) }} KB</small>
            </label>
          </div>

          <div v-else class="sample-prompt-zone">
            <textarea
              v-model="sampleUpload.prompt"
              class="db-input sample-prompt-textarea"
              placeholder="Example: You are the assistant for Sunrise Dental Clinic. Greet callers, ask their name and reason, book appointments only between 9am–6pm Mon–Sat, and escalate emergencies to a human."
              :maxlength="8000"
              :disabled="sampleUpload.isUploading"
            ></textarea>
            <div class="sample-prompt-meta">
              <small>{{ (sampleUpload.prompt || '').length }} / 8000 characters</small>
              <small v-if="(sampleUpload.prompt || '').trim().length > 0 && (sampleUpload.prompt || '').trim().length < 20" class="agent-warning">
                Needs at least 20 characters.
              </small>
            </div>
          </div>

          <div class="mfa-actions">
            <button
              type="button"
              class="ghost-button"
              :disabled="sampleUpload.isUploading"
              @click="skipSampleUpload"
            >
              Skip
            </button>
            <button
              type="button"
              class="primary-button"
              :disabled="sampleUpload.isUploading || (sampleUpload.mode === 'document' && !sampleUpload.file) || (sampleUpload.mode === 'prompt' && (sampleUpload.prompt || '').trim().length < 20)"
              @click="submitSampleUpload"
            >
              {{
                sampleUpload.isUploading
                  ? (sampleUpload.mode === 'document' ? 'Uploading...' : 'Saving...')
                  : (sampleUpload.mode === 'document' ? 'Upload and finish' : 'Save prompt and finish')
              }}
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

    <main
      v-else-if="currentPage === 'nokvo_connect'"
      class="connect-layout"
    >
      <button
        type="button"
        class="connect-back-link"
        @click="switchPage('dashboard')"
      >
        <ChevronLeft :size="16" />
        <span>Go back to Nokvo One</span>
      </button>
      <h1 class="connect-title" aria-label="Nokvo Connect">
        <span
          v-for="(char, index) in 'Nokvo Connect'"
          :key="index"
          class="connect-title-char"
          :class="{ 'connect-title-space': char === ' ' }"
          :style="{ animationDelay: `${0.18 + index * 0.06}s` }"
          aria-hidden="true"
        >{{ char === ' ' ? ' ' : char }}</span>
      </h1>
      <button
        type="button"
        class="connect-continue-button"
        @click="switchPage('nokvo_connect_step2')"
      >
        <span>Continue</span>
        <ChevronRight :size="16" />
      </button>
    </main>

    <main
      v-else-if="currentPage === 'nokvo_connect_step2'"
      class="connect-layout connect-layout--scroll"
    >
      <button
        type="button"
        class="connect-back-link"
        @click="switchPage('nokvo_connect')"
      >
        <ChevronLeft :size="16" />
        <span>Go back to Nokvo Connect</span>
      </button>

      <div class="connect-panel">
        <header class="connect-panel-head">
          <h2>API keys</h2>
          <p>Mint a key, hand it to your app, and start streaming voice or text against your agent.</p>
        </header>

        <div v-if="connect.errorMsg" class="connect-alert error">{{ connect.errorMsg }}</div>

        <section v-if="connect.newKeySecret" class="connect-secret-callout">
          <strong>Your new API key</strong>
          <p>Copy this now — we cannot show it again.</p>
          <code class="connect-secret-value">{{ connect.newKeySecret }}</code>
          <div v-if="connect.newWebhookSecret" class="connect-secret-subline">
            <span>Webhook signing secret:</span>
            <code>{{ connect.newWebhookSecret }}</code>
          </div>
          <button type="button" class="connect-secret-dismiss" @click="dismissConnectSecret">I've saved it</button>
        </section>

        <form class="connect-create-form" @submit.prevent="createConnectKey">
          <div class="connect-form-row">
            <label>
              <span>Label</span>
              <input v-model="connect.draft.label" type="text" placeholder="Production web SDK" required />
            </label>
            <label>
              <span>Mode</span>
              <select v-model="connect.draft.mode">
                <option value="live">Live</option>
                <option value="test">Test</option>
              </select>
            </label>
          </div>
          <div class="connect-form-row">
            <label>
              <span>Rate limit (req/min)</span>
              <input v-model.number="connect.draft.rate_limit_rpm" type="number" min="1" max="10000" />
            </label>
            <label>
              <span>Max concurrent sessions</span>
              <input v-model.number="connect.draft.max_concurrent_sessions" type="number" min="1" max="200" />
            </label>
          </div>
          <label class="connect-form-fullwidth">
            <span>Allowed origins (one per line, leave empty for any)</span>
            <textarea v-model="connect.draft.allowed_origins_raw" rows="2" placeholder="https://app.yourcompany.com"></textarea>
          </label>
          <label class="connect-form-fullwidth">
            <span>Webhook URL (optional)</span>
            <input v-model="connect.draft.webhook_url" type="url" placeholder="https://yourcompany.com/webhooks/nokvo" />
          </label>
          <button type="submit" class="connect-continue-button" :disabled="connect.isCreating">
            <span>{{ connect.isCreating ? 'Creating…' : 'Mint API key' }}</span>
            <ChevronRight v-if="!connect.isCreating" :size="16" />
          </button>
        </form>

        <section class="connect-key-list">
          <h3>Existing keys</h3>
          <div v-if="connect.isLoadingList" class="connect-key-empty">Loading…</div>
          <div v-else-if="!connect.keys.length" class="connect-key-empty">No keys yet. Create one above.</div>
          <article v-for="key in connect.keys" :key="key.id" class="connect-key-card">
            <div class="connect-key-card-head">
              <div>
                <strong>{{ key.label }}</strong>
                <code>{{ key.key_prefix }}…</code>
              </div>
              <span class="connect-key-status" :class="key.status">{{ key.status }}</span>
            </div>
            <dl class="connect-key-card-meta">
              <div><dt>Mode</dt><dd>{{ key.mode }}</dd></div>
              <div><dt>Rate limit</dt><dd>{{ key.rate_limit_rpm }} rpm</dd></div>
              <div><dt>Concurrency</dt><dd>{{ key.max_concurrent_sessions }}</dd></div>
              <div><dt>Last used</dt><dd>{{ key.last_used_at ? new Date(key.last_used_at).toLocaleString() : '—' }}</dd></div>
            </dl>
            <button
              v-if="key.status === 'active'"
              type="button"
              class="connect-revoke-button"
              @click="revokeConnectKey(key.id)"
            >
              Revoke
            </button>
          </article>
        </section>
      </div>
    </main>

    <main v-else class="workspace-layout dashboard-layout">
      <div class="floating-top-nav">
        <nav class="dashboard-nav">
          <div class="dashboard-brand">
            <img class="brand-logo" :src="nokvoLogo" alt="Nokvo One" />
          </div>

          <div class="dashboard-nav-actions">
            <button
              v-if="isMemberOnly"
              type="button"
              class="nav-page-button"
              :class="{ active: currentPage === 'my_timetable' }"
              @click="switchPage('my_timetable')"
            >
              <CalendarDays :size="17" />
              <span>My Timetable</span>
            </button>
            <button v-if="!isMemberOnly" type="button" class="nav-page-button" :class="{ active: currentPage === 'dashboard' }" @click="switchPage('dashboard')">
              <Database :size="17" />
              <span>Dashboard</span>
            </button>
            <button v-if="!isMemberOnly" type="button" class="nav-page-button" :class="{ active: currentPage === 'tickets' }" @click="switchPage('tickets')">
              <MessageSquare :size="17" />
              <span>Tickets</span>
            </button>
            <button v-if="!isMemberOnly" type="button" class="nav-page-button" :class="{ active: currentPage === 'leads' }" @click="switchPage('leads')">
              <UserPlus :size="17" />
              <span>Leads</span>
            </button>
            <button
              v-if="!isMemberOnly && showAppointmentsTab"
              type="button"
              class="nav-page-button"
              :class="{ active: currentPage === 'appointments' }"
              @click="switchPage('appointments')"
            >
              <CalendarDays :size="17" />
              <span>Appointments</span>
            </button>
            <button
              v-if="!isMemberOnly && !onboardingV2Enabled"
              type="button"
              class="nav-page-button"
              :class="{ active: currentPage === 'agent' }"
              @click="switchPage('agent')"
            >
              <Bot :size="17" />
              <span>Agent Studio</span>
            </button>
            <button
              v-if="!isMemberOnly && onboardingV2Enabled"
              type="button"
              class="nav-page-button"
              :class="{ active: currentPage === 'agent' }"
              @click="switchPage('agent')"
            >
              <Bot :size="17" />
              <span>Try Agent</span>
            </button>
            <button
              v-if="!isMemberOnly"
              type="button"
              class="nav-page-button"
              :class="{ active: currentPage === 'outgoing_agent' }"
              @click="switchPage('outgoing_agent')"
            >
              <PhoneCall :size="17" />
              <span>Outgoing Agent</span>
            </button>
            <button
              v-if="!isMemberOnly"
              type="button"
              class="nav-page-button"
              :class="{ active: currentPage === 'knowledge_base' }"
              @click="switchPage('knowledge_base')"
            >
              <BookOpen :size="17" />
              <span>Knowledge Base</span>
            </button>
            <button
              v-if="!isMemberOnly"
              type="button"
              class="nav-page-button"
              :class="{ active: currentPage === 'nokvo_connect' }"
              @click="switchPage('nokvo_connect')"
            >
              <Plug :size="17" />
              <span>Nokvo Connect</span>
            </button>
            <button type="button" class="org-avatar-button" @click="handleLogout">
              <span class="org-avatar-initial">{{ organizationInitial }}</span>
              <span class="org-avatar-name">{{ currentOrganization?.name }}</span>
            </button>
          </div>
        </nav>
      </div>

      <section class="dashboard-header">
        <div>
          <span class="section-kicker">Nokvo One Workspace</span>
          <h2>{{ currentOrganization?.name }}</h2>
        </div>
        <div class="dashboard-header-actions">
          <span class="dashboard-context-pill">
            <Shield :size="15" />
            {{ currentUser?.role || 'Workspace' }}
          </span>
        </div>
      </section>

      <div v-if="errorMsg" class="message error dashboard-message">{{ errorMsg }}</div>
      <div v-else-if="infoMsg" class="message info dashboard-message">{{ infoMsg }}</div>

      <!-- DASHBOARD: MFA-pending banner -->
      <div
        v-if="onboardingV2Enabled && currentUser?.mfa_pending && currentPage === 'dashboard'"
        class="mfa-pending-banner"
      >
        <div class="mfa-pending-icon">
          <Shield :size="18" />
        </div>
        <div class="mfa-pending-copy">
          <strong>Secure advanced actions</strong>
          <span>Enable MFA to unlock calling, campaigns, invites, and integrations.</span>
        </div>
        <button type="button" class="primary-button compact" :disabled="isAuthenticating" @click="startSessionTotpSetup">
          Set up MFA
        </button>
      </div>

      <!-- DASHBOARD: v2 Try-Agent banner -->
      <section
        v-if="onboardingV2Enabled && currentPage === 'dashboard' && agents.length > 0"
        class="dashboard-section try-agent-banner"
      >
        <article class="dashboard-card try-agent-card">
          <div class="try-agent-copy">
            <span class="section-kicker">Try it</span>
            <h3>Test {{ activeAgent?.name || agents[0]?.name || 'your agent' }} right now</h3>
            <p>
              No phone number needed. Send a test message or tap-to-talk inside the workspace.
              Tool calls are sandboxed and audited.
            </p>
          </div>
          <div class="try-agent-actions">
            <button
              type="button"
              class="primary-button"
              @click="activeAgent = activeAgent || agents[0]; switchPage('agent')"
            >
              <MessageSquare :size="16" />
              Send a message
            </button>
            <button
              type="button"
              class="ghost-button"
              @click="activeAgent = activeAgent || agents[0]; switchPage('agent')"
            >
              <Mic :size="16" />
              Tap to talk
            </button>
          </div>
        </article>
      </section>

      <!-- MEMBER TIMETABLE — own schedule + buffer/unavailable actions -->
      <section v-if="currentPage === 'my_timetable'" class="dashboard-section member-timetable-section">
        <div class="dashboard-section-head">
          <div>
            <span class="section-kicker">Your schedule</span>
            <h3>My timetable</h3>
          </div>
          <p>View your working window, add a buffer, or mark yourself unavailable. Changes apply immediately.</p>
        </div>

        <div v-if="isLoadingMyTimetable && !myTimetable" class="empty-state">Loading your timetable…</div>

        <div v-else class="member-timetable-grid">
          <article class="dashboard-card member-timetable-card">
            <div class="dashboard-card-glow"></div>
            <div class="member-timetable-card-head">
              <h4>Working window</h4>
              <span class="readonly-tag">Set by admin</span>
            </div>
            <dl class="dashboard-detail-list">
              <div>
                <dt>Days</dt>
                <dd>{{ (myTimetable?.assignment?.working_days || []).join(', ') || 'No days configured yet' }}</dd>
              </div>
              <div>
                <dt>Hours</dt>
                <dd>
                  <template v-if="myTimetable?.assignment?.start_time && myTimetable?.assignment?.end_time">
                    {{ myTimetable.assignment.start_time }} – {{ myTimetable.assignment.end_time }}
                    ({{ myTimetable.assignment.timezone || 'Asia/Kolkata' }})
                  </template>
                  <template v-else>Not set</template>
                </dd>
              </div>
              <div>
                <dt>Status</dt>
                <dd>
                  <span v-if="myTimetable?.assignment?.is_assignable">Accepting work</span>
                  <span v-else>Not assignable yet</span>
                </dd>
              </div>
              <div v-if="(myTimetable?.assignment?.request_types || []).length">
                <dt>Handles</dt>
                <dd>{{ (myTimetable.assignment.request_types || []).join(', ') }}</dd>
              </div>
            </dl>
          </article>

          <article class="dashboard-card member-timetable-card member-action-card buffer-card">
            <div class="dashboard-card-glow"></div>
            <header class="member-action-head">
              <div class="member-action-icon">
                <Clock :size="20" />
              </div>
              <div class="member-action-title">
                <h4>Add buffer</h4>
                <p>Short break between requests — lunch, a quick call, or back-to-back recovery.</p>
              </div>
            </header>
            <form class="member-action-form" @submit.prevent="addBuffer">
              <label class="member-field member-field-wide">
                <span class="member-field-label">Start time</span>
                <input
                  v-model="bufferForm.start_time"
                  type="datetime-local"
                  class="member-field-input"
                  required
                />
              </label>
              <div class="member-field member-field-wide">
                <span class="member-field-label">Duration</span>
                <div class="member-duration-pills" role="radiogroup" aria-label="Buffer duration">
                  <button
                    v-for="opt in bufferDurationOptions"
                    :key="opt.minutes"
                    type="button"
                    role="radio"
                    :aria-checked="bufferForm.duration_minutes === opt.minutes"
                    class="member-duration-pill"
                    :class="{ active: bufferForm.duration_minutes === opt.minutes }"
                    @click="bufferForm.duration_minutes = opt.minutes"
                  >
                    {{ opt.label }}
                  </button>
                </div>
              </div>
              <label class="member-field member-field-wide">
                <span class="member-field-label">Note <small>(optional)</small></span>
                <input
                  v-model="bufferForm.reason"
                  type="text"
                  maxlength="240"
                  class="member-field-input"
                  placeholder="e.g. lunch break"
                />
              </label>
              <button
                type="submit"
                class="member-action-submit"
                :disabled="isMutatingMyBlock || !bufferForm.start_time"
              >
                <Plus :size="16" />
                {{ isMutatingMyBlock ? 'Adding…' : 'Add buffer' }}
              </button>
            </form>
          </article>

          <article class="dashboard-card member-timetable-card member-action-card unavailable-card">
            <div class="dashboard-card-glow"></div>
            <header class="member-action-head">
              <div class="member-action-icon unavailable-icon">
                <CalendarOff :size="20" />
              </div>
              <div class="member-action-title">
                <h4>Mark unavailable</h4>
                <p>Longer time off — leave, an offsite, or anything that takes you out of rotation.</p>
              </div>
            </header>
            <form class="member-action-form" @submit.prevent="addUnavailability">
              <label class="member-field">
                <span class="member-field-label">From</span>
                <input
                  v-model="unavailableForm.start_time"
                  type="datetime-local"
                  class="member-field-input"
                  required
                />
              </label>
              <label class="member-field">
                <span class="member-field-label">Until</span>
                <input
                  v-model="unavailableForm.end_time"
                  type="datetime-local"
                  class="member-field-input"
                  required
                />
              </label>
              <label class="member-field member-field-wide">
                <span class="member-field-label">Reason <small>(optional)</small></span>
                <input
                  v-model="unavailableForm.reason"
                  type="text"
                  maxlength="240"
                  class="member-field-input"
                  placeholder="e.g. annual leave, training, doctor's appointment"
                />
              </label>
              <button
                type="submit"
                class="member-action-submit unavailable-submit"
                :disabled="isMutatingMyBlock || !unavailableForm.start_time || !unavailableForm.end_time"
              >
                <CalendarOff :size="16" />
                {{ isMutatingMyBlock ? 'Saving…' : 'Mark unavailable' }}
              </button>
            </form>
          </article>

          <article class="dashboard-card member-timetable-card member-timetable-card-wide">
            <div class="dashboard-card-glow"></div>
            <h4>Upcoming blocks</h4>
            <p v-if="!(myTimetable?.blocked_slots || []).length" class="empty-state">
              No buffers or unavailability scheduled.
            </p>
            <ul v-else class="member-timetable-blocks">
              <li v-for="slot in myTimetable.blocked_slots" :key="slot.id" class="member-timetable-block">
                <div class="member-timetable-block-meta">
                  <strong>{{ slot.reason || 'Block' }}</strong>
                  <span>{{ formatSlotRange(slot) }}</span>
                </div>
                <button
                  type="button"
                  class="dashboard-inline-button"
                  :disabled="isMutatingMyBlock"
                  @click="removeMyBlock(slot.id)"
                >
                  <Trash2 :size="14" />
                  Remove
                </button>
              </li>
            </ul>
          </article>
        </div>
      </section>

      <!-- DASHBOARD -->
      <section v-if="currentPage === 'dashboard'" class="dashboard-section">
        <div class="dashboard-section-head">
          <div>
            <span class="section-kicker">Overview</span>
            <h3>Organization analytics</h3>
          </div>
          <p>Live readiness across security, agent setup, knowledge, runtime, outbound, and infrastructure.</p>
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

            <div class="health-tracker">
              <div class="health-score-card" :class="`health-${organizationHealth.state}`">
                <div class="health-ring" :style="{ background: organizationHealth.ringStyle }">
                  <div>
                    <strong>{{ organizationHealth.score }}</strong>
                    <span>/100</span>
                  </div>
                </div>
                <div>
                  <span class="micro-label">Workspace health</span>
                  <h4>{{ organizationHealth.label }}</h4>
                  <p>{{ organizationHealth.openIssues ? `${organizationHealth.openIssues} area${organizationHealth.openIssues === 1 ? '' : 's'} need attention.` : 'All critical areas are clear.' }}</p>
                </div>
              </div>

              <div class="health-check-grid">
                <div v-for="item in organizationHealth.checks" :key="item.key" class="health-check" :data-state="item.state">
                  <span class="health-check-dot"></span>
                  <div>
                    <strong>{{ item.label }}</strong>
                    <small>{{ item.detail }}</small>
                  </div>
                </div>
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
                <p>{{ agents.length ? 'Ready for testing' : 'No agent yet' }}</p>
              </div>
            </div>
            <dl class="dashboard-detail-list">
              <div>
                <dt>Primary Agent</dt>
                <dd>{{ activeAgent?.name || agents[0]?.name || 'Not created' }}</dd>
              </div>
              <div>
                <dt>Calling</dt>
                <dd>{{ currentOrganization?.calling_enabled ? 'Enabled' : 'Awaiting approval' }}</dd>
              </div>
              <div>
                <dt>Chat Mode</dt>
                <dd>Limited (no external sends)</dd>
              </div>
              <div>
                <dt>Business Type</dt>
                <dd>{{ businessTypeLabel }}</dd>
              </div>
            </dl>
            <button type="button" class="dashboard-inline-button" @click="switchPage('agent')">
              <Wrench :size="15" />
              Open Agent Studio
            </button>
          </article>
        </div>

        <div class="dashboard-section-head">
          <div>
            <span class="section-kicker">Controls</span>
            <h3>Quick Actions</h3>
          </div>
          <p>Invite teammates under your verified workspace domain.</p>
        </div>

        <div class="dashboard-grid control-grid">
          <article class="dashboard-card invite-card wide-card expanded-invite-card">
            <div class="invite-context-panel">
              <div class="compact-card-head">
                <div class="compact-icon-shell">
                  <UserPlus :size="18" />
                </div>
                <div>
                  <h3>Invite {{ memberPageLabel }}</h3>
                  <p>Email invite link. Invitee sets their own password and TOTP.</p>
                </div>
              </div>
              <div class="invite-domain-banner">
                <span>Allowed Domain</span>
                <strong>{{ currentOrganization?.email_domain }}</strong>
                <small>Invites are restricted to verified work email addresses from this domain.</small>
              </div>
            </div>
            <form class="invite-form dashboard-invite-form" @submit.prevent="inviteMember">
              <div class="invite-field-grid">
                <label class="invite-field invite-field-wide">
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
              </div>
              <div class="invite-action-block">
                <p :class="['invite-helper', { invalid: !isInviteDomainValid }]">
                  {{ inviteValidationMessage }}
                </p>
                <button type="submit" :disabled="isSavingMember || !inviteCanSubmit">
                  <UserPlus :size="15" />
                  {{ isSavingMember ? 'Inviting...' : 'Send Invite' }}
                </button>
              </div>
            </form>
          </article>
        </div>

        <article id="dashboard-members" class="dashboard-card wide-card members-card dashboard-team-card">
          <div class="members-card-head">
            <div>
              <span class="section-kicker">Access</span>
              <h3>{{ memberPageLabel }}</h3>
              <p>Manage access and assignment availability for incoming calls, leads, tickets, and appointments.</p>
            </div>
            <div class="members-summary">
              <span>{{ filteredMembers.length }} visible</span>
              <span>{{ members.length }} total</span>
            </div>
          </div>

          <div v-if="isLoadingMembers" class="empty-state">Loading members...</div>
          <div v-else-if="!filteredMembers.length" class="empty-state">No members match the current filter.</div>
          <div v-else class="team-card-grid">
            <div v-for="m in filteredMembers" :key="m.id" class="team-member-card">
              <div class="team-member-main">
                <div class="team-avatar">{{ (m.full_name || m.email || 'N').trim().charAt(0).toUpperCase() }}</div>
                <div class="team-identity">
                  <strong>{{ m.full_name || 'Unnamed member' }}</strong>
                  <small>{{ m.email }}</small>
                </div>
                <div class="team-badges">
                  <span>{{ m.role }}</span>
                  <span :class="{ active: m.status === 'active' }">{{ m.status }}</span>
                </div>
              </div>

              <div class="team-assignment-panel">
                <div>
                  <span class="micro-label">Availability</span>
                  <p>{{ assignmentForMember(m.id).availability_summary }}</p>
                </div>
                <div class="team-load-pill">
                  <span>Current load</span>
                  <strong>{{ assignmentForMember(m.id).active_request_count || 0 }}</strong>
                </div>
                <button type="button" class="ghost-button compact" @click="openMemberTimetable(m)">
                  <CalendarDays :size="15" />
                  Timetable
                </button>
                <button type="button" class="ghost-button compact" @click="startAssignmentEdit(m)">
                  <CalendarDays :size="15" />
                  Schedule
                </button>
                <button
                  v-if="isAdmin && m.id !== currentUser?.id"
                  type="button"
                  class="ghost-button compact danger"
                  :disabled="removingMemberId === m.id"
                  @click="removeMember(m)"
                >
                  <Trash2 :size="15" />
                  {{ removingMemberId === m.id ? 'Removing…' : 'Remove' }}
                </button>
              </div>
            </div>
          </div>
        </article>
      </section>

      <!-- MEMBERS -->
      <section v-if="false" class="dashboard-section">
        <div class="dashboard-section-head">
          <div>
            <span class="section-kicker">Access</span>
            <h3>{{ memberPageLabel }}</h3>
          </div>
          <p>Manage access and assignment availability for incoming calls, leads, tickets, and appointments.</p>
        </div>

        <div class="dashboard-grid control-grid">
          <article class="dashboard-card invite-card">
            <div class="compact-card-head">
              <div class="compact-icon-shell">
                <UserPlus :size="18" />
              </div>
              <div>
                <h3>Invite {{ memberPageLabel }}</h3>
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
                <h3>{{ memberPageLabel }}</h3>
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
                <span>{{ memberPageLabel }}</span>
                <span>Role</span>
                <span>Status</span>
                <span>Assignment</span>
              </div>
              <div v-for="m in filteredMembers" :key="m.id" class="member-row">
                <div class="member-meta">
                  <strong>{{ m.full_name || 'Unnamed member' }}</strong>
                  <small>{{ m.email }}</small>
                </div>
                <span class="readonly-tag">{{ m.role }}</span>
                <span class="readonly-tag">{{ m.status }}</span>
                <div class="assignment-summary-cell">
                  <span class="readonly-tag">{{ assignmentForMember(m.id).availability_summary }}</span>
                  <span class="readonly-tag">Current load: {{ assignmentForMember(m.id).active_request_count || 0 }}</span>
                  <button type="button" class="ghost-button compact" @click="startAssignmentEdit(m)">Schedule</button>
                </div>
              </div>
            </div>
          </article>
        </div>
      </section>

      <!-- TICKETS -->
      <section v-if="currentPage === 'tickets'" class="dashboard-section">
        <div class="dashboard-section-head">
          <div>
            <span class="section-kicker">{{ businessTypeLabel }}</span>
            <h3>Tickets</h3>
          </div>
          <p>Choose the details your team tracks for support requests.</p>
        </div>

        <div class="dashboard-grid control-grid">
          <article class="dashboard-card wide-card members-card">
            <div class="members-card-head">
              <div>
                <h3>Ticket Fields</h3>
                <p>These fields appear when your team captures or reviews a ticket.</p>
              </div>
              <div class="field-card-actions">
                <span class="status-chip">{{ schemaFor('tickets').length }} fields</span>
                <button type="button" class="ghost-button compact" @click="startFieldEdit('tickets', 'Ticket Fields')">Edit Fields</button>
              </div>
            </div>
            <div class="schema-field-grid">
              <div v-for="field in schemaFor('tickets')" :key="field.key" class="schema-field-row">
                <strong>{{ field.label }}</strong>
                <span>{{ field.type }}{{ field.required ? ' · required' : '' }}</span>
              </div>
            </div>
          </article>

          <article class="dashboard-card compact-card">
            <div class="compact-card-head">
              <div class="compact-icon-shell">
                <MessageSquare :size="18" />
              </div>
              <div>
                <h3>At A Glance</h3>
                <p>{{ businessTypeLabel }} ticket setup</p>
              </div>
            </div>
            <dl class="dashboard-detail-list">
              <div v-for="field in schemaFor('tickets').slice(0, 5)" :key="field.key">
                <dt>{{ field.label }}</dt>
                <dd>{{ field.required ? 'Required' : 'Optional' }}</dd>
              </div>
            </dl>
          </article>
        </div>

        <article class="dashboard-card wide-card members-card">
          <div class="members-card-head">
            <div>
              <h3>Ticket Records</h3>
              <p>Inbound calls handled by the voice or chat agent appear here as tickets.</p>
            </div>
            <div class="field-card-actions">
              <span class="status-chip">{{ (tabRecords.tickets || []).length }} records</span>
              <button
                type="button"
                class="ghost-button compact"
                :disabled="tabRecordsLoading.tickets"
                @click="loadTabRecords('tickets')"
              >
                {{ tabRecordsLoading.tickets ? 'Refreshing' : 'Refresh' }}
              </button>
            </div>
          </div>

          <div v-if="tabRecordsLoading.tickets" class="empty-state compact">Loading ticket records...</div>
          <div v-else-if="!(tabRecords.tickets || []).length" class="empty-state compact">
            No ticket records yet.
          </div>
          <div v-else class="tab-record-list">
            <div v-for="record in tabRecords.tickets" :key="record.id" class="tab-record-row">
              <div class="tab-record-primary">
                <strong>{{ ticketRecordTitle(record) }}</strong>
                <small>{{ ticketRecordSubtitle(record) }}</small>
                <a v-if="phoneHref(recordPhone(record))" class="record-call-link" :href="phoneHref(recordPhone(record))">
                  <PhoneCall :size="14" />
                  Call {{ recordPhone(record) }}
                </a>
              </div>
              <div class="tab-record-meta">
                <div>
                  <span>Priority</span>
                  <strong>{{ ticketRecordPriority(record) }}</strong>
                </div>
                <div>
                  <span>Owner</span>
                  <strong>{{ ticketRecordOwner(record) }}</strong>
                </div>
                <div>
                  <span>Status</span>
                  <strong>{{ record.status || 'open' }}</strong>
                </div>
                <div>
                  <span>Created</span>
                  <strong>{{ formatRelativeDate(record.created_at) || 'Unknown' }}</strong>
                </div>
              </div>
            </div>
          </div>
        </article>
      </section>

      <!-- LEADS -->
      <section v-if="currentPage === 'leads'" class="dashboard-section">
        <div class="dashboard-section-head">
          <div>
            <span class="section-kicker">{{ businessTypeLabel }}</span>
            <h3>Leads</h3>
          </div>
          <p>Choose the details your team needs to qualify new customers.</p>
        </div>

        <div class="dashboard-grid control-grid">
          <article class="dashboard-card wide-card members-card">
            <div class="members-card-head">
              <div>
                <h3>Lead Fields</h3>
                <p>These fields guide customer intake, follow-up, and agent-created leads.</p>
              </div>
              <div class="field-card-actions">
                <span class="status-chip">{{ schemaFor('leads').length }} fields</span>
                <button type="button" class="ghost-button compact" @click="startFieldEdit('leads', 'Lead Fields')">Edit Fields</button>
              </div>
            </div>
            <div class="schema-field-grid">
              <div v-for="field in schemaFor('leads')" :key="field.key" class="schema-field-row">
                <strong>{{ field.label }}</strong>
                <span>{{ field.type }}{{ field.required ? ' · required' : '' }}</span>
              </div>
            </div>
          </article>

          <article class="dashboard-card compact-card">
            <div class="compact-card-head">
              <div class="compact-icon-shell">
                <UserPlus :size="18" />
              </div>
              <div>
                <h3>At A Glance</h3>
                <p>{{ businessTypeLabel }} lead setup</p>
              </div>
            </div>
            <dl class="dashboard-detail-list">
              <div v-for="field in schemaFor('leads').slice(0, 5)" :key="field.key">
                <dt>{{ field.label }}</dt>
                <dd>{{ field.required ? 'Required' : 'Optional' }}</dd>
              </div>
            </dl>
          </article>
        </div>

        <article class="dashboard-card wide-card members-card">
          <div class="members-card-head">
            <div>
              <h3>Lead Records</h3>
              <p>Outbound campaign calls and any agent-created leads appear here.</p>
            </div>
            <div class="field-card-actions">
              <span class="status-chip">{{ (tabRecords.leads || []).length }} records</span>
              <button
                type="button"
                class="ghost-button compact"
                :disabled="tabRecordsLoading.leads"
                @click="loadTabRecords('leads')"
              >
                {{ tabRecordsLoading.leads ? 'Refreshing' : 'Refresh' }}
              </button>
            </div>
          </div>

          <div v-if="tabRecordsLoading.leads" class="empty-state compact">Loading lead records...</div>
          <div v-else-if="!(tabRecords.leads || []).length" class="empty-state compact">
            No lead records yet.
          </div>
          <div v-else class="tab-record-list">
            <div v-for="record in tabRecords.leads" :key="record.id" class="tab-record-row">
              <div class="tab-record-primary">
                <strong>{{ leadRecordTitle(record) }}</strong>
                <small>{{ leadRecordSubtitle(record) }}</small>
                <a v-if="phoneHref(recordPhone(record))" class="record-call-link" :href="phoneHref(recordPhone(record))">
                  <PhoneCall :size="14" />
                  Call {{ recordPhone(record) }}
                </a>
              </div>
              <div class="tab-record-meta">
                <div>
                  <span>Budget</span>
                  <strong>{{ leadRecordBudget(record) }}</strong>
                </div>
                <div>
                  <span>Location</span>
                  <strong>{{ leadRecordLocation(record) }}</strong>
                </div>
                <div>
                  <span>Status</span>
                  <strong>{{ record.status || 'new' }}</strong>
                </div>
                <div>
                  <span>Created</span>
                  <strong>{{ formatRelativeDate(record.created_at) || 'Unknown' }}</strong>
                </div>
              </div>
            </div>
          </div>
        </article>
      </section>

      <!-- APPOINTMENTS -->
      <section v-if="currentPage === 'appointments' && showAppointmentsTab" class="dashboard-section">
        <div class="dashboard-section-head">
          <div>
            <span class="section-kicker">Clinics</span>
            <h3>Appointments</h3>
          </div>
          <p>Choose the details needed to book and follow up on visits.</p>
        </div>

        <article class="dashboard-card wide-card members-card">
          <div class="members-card-head">
            <div>
              <h3>Appointment Fields</h3>
              <p>Clinic agents use these fields for scheduling and appointment follow-up.</p>
            </div>
            <div class="field-card-actions">
              <span class="status-chip">{{ schemaFor('appointments').length }} fields</span>
              <button type="button" class="ghost-button compact" @click="startFieldEdit('appointments', 'Appointment Fields')">Edit Fields</button>
            </div>
          </div>
          <div class="schema-field-grid">
            <div v-for="field in schemaFor('appointments')" :key="field.key" class="schema-field-row">
              <strong>{{ field.label }}</strong>
              <span>{{ field.type }}{{ field.required ? ' · required' : '' }}</span>
            </div>
          </div>
        </article>

        <article class="dashboard-card wide-card members-card">
          <div class="members-card-head">
            <div>
              <h3>Appointment Records</h3>
              <p>Requests created by the voice agent, chat agent, or team tools appear here.</p>
            </div>
            <div class="field-card-actions">
              <span class="status-chip">{{ (tabRecords.appointments || []).length }} records</span>
              <button
                type="button"
                class="ghost-button compact"
                :disabled="tabRecordsLoading.appointments"
                @click="loadTabRecords('appointments')"
              >
                {{ tabRecordsLoading.appointments ? 'Refreshing' : 'Refresh' }}
              </button>
            </div>
          </div>

          <div v-if="tabRecordsLoading.appointments" class="empty-state compact">Loading appointment records...</div>
          <div v-else-if="!(tabRecords.appointments || []).length" class="empty-state compact">
            No appointment records yet.
          </div>
          <div v-else class="tab-record-list appointment-record-list">
            <div v-for="record in tabRecords.appointments" :key="record.id" class="tab-record-row">
              <div class="tab-record-primary">
                <strong>{{ appointmentRecordTitle(record) }}</strong>
                <small>{{ appointmentRecordSubtitle(record) }}</small>
                <a v-if="phoneHref(recordPhone(record))" class="record-call-link" :href="phoneHref(recordPhone(record))">
                  <PhoneCall :size="14" />
                  Call {{ recordPhone(record) }}
                </a>
              </div>
              <div class="tab-record-meta">
                <div>
                  <span>Requested Slot</span>
                  <strong>{{ appointmentRecordTime(record) }}</strong>
                </div>
                <div>
                  <span>Assigned To</span>
                  <strong>{{ appointmentAssignedLabel(record) }}</strong>
                </div>
                <div>
                  <span>Status</span>
                  <strong>{{ record.status || 'requested' }}</strong>
                </div>
                <div>
                  <span>Created</span>
                  <strong>{{ formatRelativeDate(record.created_at) || 'Unknown' }}</strong>
                </div>
              </div>
            </div>
          </div>
        </article>

        <article class="dashboard-card agent-documents-card">
          <div class="members-card-head">
            <div>
              <h3>Custom Tabs</h3>
              <p>
                Define org-specific resource tabs (e.g. Deliveries, Properties) — agents
                automatically get 8 CRUD tools per tab.
              </p>
            </div>
            <span class="status-chip">{{ customTabs.length }} / 8</span>
          </div>

          <div class="custom-tab-list">
            <div v-if="!customTabs.length" class="empty-state compact">
              No custom tabs yet. Add one below to extend your agent's toolset.
            </div>
            <div v-for="tab in customTabs" :key="tab.slug" class="custom-tab-row">
              <div>
                <strong>{{ tab.label }}</strong>
                <small>slug: <code>{{ tab.slug }}</code> · {{ (tab.fields || []).length }} fields</small>
                <small>statuses: {{ ((tab.status_vocabulary || {}).all || []).join(', ') }}</small>
              </div>
              <button
                type="button"
                class="ghost-button compact danger"
                :disabled="customTabActionInProgress"
                @click="deleteCustomTab(tab.slug)"
              >
                Remove
              </button>
            </div>
          </div>

          <div class="db-form-block custom-tab-form">
            <label class="db-label">Add custom tab</label>
            <div class="custom-tab-form-row">
              <input
                v-model="newCustomTab.label"
                class="db-input"
                type="text"
                placeholder="Label (e.g. Deliveries)"
              />
              <input
                v-model="newCustomTab.slug"
                class="db-input"
                type="text"
                placeholder="Slug (e.g. deliveries)"
              />
            </div>
            <div class="custom-tab-form-row">
              <input
                v-model="newCustomTab.statusList"
                class="db-input"
                type="text"
                placeholder="Statuses (comma-separated, e.g. pending,in_transit,delivered)"
              />
            </div>
            <div class="custom-tab-fields">
              <div
                v-for="(field, idx) in newCustomTab.fields"
                :key="`cf-${idx}`"
                class="custom-tab-field-row"
              >
                <input v-model="field.key" class="db-input compact" placeholder="key" />
                <input v-model="field.label" class="db-input compact" placeholder="label" />
                <select v-model="field.type" class="db-input compact">
                  <option value="text">text</option>
                  <option value="textarea">textarea</option>
                  <option value="phone">phone</option>
                  <option value="email">email</option>
                  <option value="number">number</option>
                  <option value="currency">currency</option>
                  <option value="date">date</option>
                  <option value="datetime">datetime</option>
                  <option value="select">select</option>
                </select>
                <label class="custom-tab-required-toggle">
                  <input type="checkbox" v-model="field.required" />
                  required
                </label>
                <button
                  type="button"
                  class="link-button"
                  @click="removeCustomTabField(idx)"
                  :disabled="newCustomTab.fields.length <= 1"
                >
                  Remove
                </button>
              </div>
              <button type="button" class="link-button" @click="addCustomTabField">
                + Add field
              </button>
            </div>
            <div class="db-actions">
              <button
                type="button"
                class="primary-button compact"
                :disabled="customTabActionInProgress || !newCustomTab.label || !newCustomTab.slug"
                @click="submitCustomTab"
              >
                Create tab
              </button>
            </div>
          </div>
        </article>
      </section>

      <!-- AGENT STUDIO -->
      <section v-if="currentPage === 'agent'" class="dashboard-section agent-studio-section">
        <div class="agent-hero">
          <div class="agent-hero-copy">
            <span class="section-kicker">Agent Studio</span>
            <h3>Build, test, and run your voice agent.</h3>
            <p>
              Sarvam STT → Qdrant retrieval → Azure OpenAI <strong>gpt-4.1-mini</strong> → Sarvam TTS,
              all running on your tenant-isolated infra. Calls hit your own embedding deployment and
              Knowledge Base collection — nothing leaks across organizations.
            </p>
          </div>
          <div class="agent-pipeline-grid">
            <div class="pipeline-chip">
              <Mic :size="16" />
              <div>
                <span>STT</span>
                <strong>{{ runtimeStatus?.stt?.model || 'Sarvam saaras:v3' }}</strong>
                <small :class="`chip-status-${runtimeStatus?.stt?.status || 'unknown'}`">
                  {{ runtimeStatus?.stt?.status || 'unknown' }}
                </small>
              </div>
            </div>
            <div class="pipeline-chip">
              <Brain :size="16" />
              <div>
                <span>LLM</span>
                <strong>{{ runtimeStatus?.llm?.model || 'gpt-4.1-mini' }}</strong>
                <small>South India</small>
              </div>
            </div>
            <div class="pipeline-chip">
              <Volume2 :size="16" />
              <div>
                <span>TTS</span>
                <strong>{{ runtimeStatus?.tts?.model || 'Sarvam bulbul:v3' }}</strong>
                <small :class="`chip-status-${runtimeStatus?.tts?.status || 'unknown'}`">
                  {{ runtimeStatus?.tts?.status || 'unknown' }}
                </small>
              </div>
            </div>
            <div class="pipeline-chip">
              <Layers :size="16" />
              <div>
                <span>Embeddings</span>
                <strong>text-embedding-3-small</strong>
                <small>1536-dim · cosine</small>
              </div>
            </div>
            <div class="pipeline-chip">
              <Database :size="16" />
              <div>
                <span>Vector store</span>
                <strong>Qdrant</strong>
                <small>{{ runtimeStatus?.knowledge_scope?.replace(/_/g, ' ') || 'tenant collection' }}</small>
              </div>
            </div>
            <div class="pipeline-chip">
              <Activity :size="16" />
              <div>
                <span>Cache</span>
                <strong>{{ runtimeStatus?.optimization?.semantic_cache_enabled ? 'Redis on' : 'off' }}</strong>
                <small>top-k {{ runtimeStatus?.optimization?.qdrant_top_k || 3 }}</small>
              </div>
            </div>
          </div>
        </div>

        <div v-if="currentOrganization?.status === 'pending_approval'" class="message info dashboard-message">
          Your organization is awaiting Nokvo activation. Voice testing and agent CRUD work today; outbound
          calling unlocks after approval.
        </div>

        <!-- VOICE TESTER -->
        <article class="dashboard-card voice-tester-card">
          <div class="kb-card-head">
            <div class="kb-card-icon kb-card-icon-primary">
              <Mic :size="18" />
            </div>
            <div>
              <h4>Voice Tester</h4>
              <p>Talk to your agent live in the browser. Audio → Sarvam STT → RAG → LLM → Sarvam TTS, full pipeline.</p>
            </div>
            <div class="voice-status-badge" :class="`voice-status-${voice.status}`">
              <span class="voice-status-dot"></span>
              {{ voice.status }}
            </div>
          </div>

          <div class="voice-controls">
            <select v-model="voice.language" :disabled="voice.status !== 'idle' && voice.status !== 'error'">
              <option v-for="opt in voiceLanguageOptions" :key="opt.value" :value="opt.value">
                {{ opt.label }}
              </option>
            </select>
            <button
              v-if="voice.status === 'idle' || voice.status === 'error'"
              type="button"
              class="primary-button compact"
              @click="startVoiceCall"
            >
              <PhoneCall :size="15" />
              Start Call
            </button>
            <button
              v-else
              type="button"
              class="ghost-button compact"
              @click="endVoiceCall"
            >
              <Square :size="15" />
              End Call
            </button>
            <button
              type="button"
              class="ghost-button compact"
              :disabled="!voice.ws"
              @click="sendTextToVoiceAgent"
              title="Send a text-only query through the same pipeline"
            >
              <MessageSquare :size="15" />
              Send Text Turn
            </button>
            <input
              v-if="voice.ws"
              v-model="chatInput"
              type="text"
              class="voice-text-input"
              placeholder="Type a query to send through the pipeline"
              @keyup.enter="sendTextToVoiceAgent"
            />
          </div>

          <div v-if="voice.errorMsg" class="message error dashboard-message">{{ voice.errorMsg }}</div>

          <div class="voice-live-row" v-if="voice.liveTranscript">
            <Radio :size="14" />
            <em>{{ voice.liveTranscript }}</em>
            <span v-if="voice.transcriptLang" class="kb-pill kb-pill-soft">{{ voice.transcriptLang }}</span>
          </div>

          <div class="voice-latency-row" v-if="voice.firstSentenceMs || voice.ttsFirstAudioMs">
            <span v-if="voice.firstSentenceMs">LLM first sentence: <strong>{{ voice.firstSentenceMs }}ms</strong></span>
            <span v-if="voice.ttsFirstAudioMs">TTS first audio: <strong>{{ voice.ttsFirstAudioMs }}ms</strong></span>
            <span v-if="voice.callId">call: <code>{{ voice.callId.slice(0, 8) }}</code></span>
          </div>

          <div v-if="!voice.turns.length && voice.status === 'idle'" class="kb-empty voice-empty">
            <div class="kb-empty-icon">
              <Mic :size="24" />
            </div>
            <strong>Start a call to test the pipeline.</strong>
            <span>Choose a language, hit Start Call, allow mic access, and speak. Audio plays back in real time.</span>
          </div>

          <div v-if="voice.turns.length" class="voice-transcript">
            <div v-for="turn in voice.turns" :key="turn.id" class="voice-turn">
              <div class="voice-turn-user">
                <span class="kb-pill kb-pill-type">caller</span>
                <p>{{ turn.query }}</p>
              </div>
              <div class="voice-turn-agent">
                <span class="kb-pill kb-pill-status-approved">agent</span>
                <p>{{ turn.sentences.join(' ') || turn.answer || '…' }}</p>
                <div class="voice-turn-meta">
                  <span v-if="turn.latencyMs">first sentence {{ turn.latencyMs }}ms</span>
                  <span v-if="turn.cacheHit" class="kb-pill kb-pill-soft">cache hit</span>
                  <span v-if="turn.citations.length" class="kb-pill kb-pill-soft">{{ turn.citations.length }} citation{{ turn.citations.length === 1 ? '' : 's' }}</span>
                </div>
              </div>
            </div>
          </div>
        </article>

        <!-- PHONE LINK -->
        <article class="dashboard-card phone-link-card">
          <div class="kb-card-head">
            <div class="kb-card-icon">
              <PhoneCall :size="18" />
            </div>
            <div>
              <h4>Exotel Phone Link</h4>
              <p>Set a stable link ID, then configure the URLs below in your Exotel portal under the inbound number.</p>
            </div>
          </div>

          <div class="kb-form-grid">
            <label class="kb-field kb-field-wide">
              <span>Link ID (any unique string)</span>
              <input v-model="phoneLinkInput" type="text" placeholder="acme-india-line-1" :disabled="!isAdmin" />
            </label>
          </div>

          <div class="kb-card-actions" v-if="isAdmin">
            <button type="button" class="primary-button compact" :disabled="isSavingPhoneLink" @click="savePhoneLink">
              <CheckCircle2 :size="15" />
              {{ isSavingPhoneLink ? 'Saving…' : 'Save Link' }}
            </button>
            <span class="kb-card-hint" v-if="phoneLink?.status === 'linked'">linked · {{ phoneLink.link_id }}</span>
            <span class="kb-card-hint" v-else>not linked yet</span>
          </div>

          <div v-if="phoneLink?.exotel_webhook_url" class="phone-link-urls">
            <div class="phone-link-url-row">
              <span>Inbound webhook (HTTP POST)</span>
              <code>{{ phoneLink.exotel_webhook_url }}</code>
            </div>
            <div class="phone-link-url-row">
              <span>Inbound media stream (WSS)</span>
              <code>{{ phoneLink.exotel_media_url }}</code>
            </div>
          </div>
        </article>

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
              <textarea id="agent-prompt" v-model="newAgent.system_prompt" class="db-input toolkit-textarea" :placeholder="businessPromptPlaceholder"></textarea>
              <p class="form-hint">Global Nokvo rules and {{ businessTypeLabel }} template rules are injected by the backend at runtime.</p>
            </div>

            <div class="db-form-block">
              <div class="agent-tools-head">
                <label class="db-label">Enabled Tools</label>
                <button
                  type="button"
                  class="link-button"
                  :disabled="!toolCatalogDefaults.length"
                  @click="selectDefaultAgentTools"
                >
                  Reset to recommended
                </button>
              </div>
              <p class="form-hint">
                Tools are generated from your {{ businessTypeLabel }} tab schema. Adjust a tab's fields under
                Workspace → Business Type to change tool inputs.
              </p>
              <div v-if="!toolCatalogGroups.length" class="empty-state compact">
                No tools available yet. Pick a Business Type to populate the catalog.
              </div>
              <div
                v-for="group in toolCatalogGroups"
                :key="group.label"
                class="agent-tool-group"
              >
                <div class="agent-tool-group-head">
                  <strong>{{ group.label }}</strong>
                  <span class="status-chip">{{ group.tools.length }} tools</span>
                  <button
                    type="button"
                    class="link-button"
                    @click="toggleAgentToolGroup(group)"
                  >
                    {{ isAgentToolGroupAllOn(group) ? 'Deselect all' : 'Select all' }}
                  </button>
                </div>
                <div class="provider-grid provider-grid-dual">
                  <label
                    v-for="t in group.tools"
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

      <!-- OUTGOING AGENT -->
      <section v-if="currentPage === 'outgoing_agent'" class="dashboard-section outgoing-agent-section">
        <div class="dashboard-section-head">
          <div>
            <span class="section-kicker">Outgoing Agent</span>
            <h3>Call only consented leads.</h3>
            <p>Connect Facebook Ads, Instagram Ads, Google Ads, Google Forms, or share a Nokvo form. Campaigns can launch only from callable leads with consent evidence.</p>
          </div>
          <button type="button" class="dashboard-inline-button" :disabled="isLoadingLeadSources" @click="loadOutgoingAgentWorkspace">
            <Search :size="16" />
            Refresh
          </button>
        </div>

        <div class="outgoing-tabs">
          <button type="button" :class="{ active: outgoingTab === 'leads' }" @click="outgoingTab = 'leads'">
            <Users :size="15" />
            Leads
          </button>
          <button type="button" :class="{ active: outgoingTab === 'connections' }" @click="outgoingTab = 'connections'">
            <Globe :size="15" />
            Connections
          </button>
          <button type="button" :class="{ active: outgoingTab === 'forms' }" @click="outgoingTab = 'forms'">
            <FileText :size="15" />
            Forms
          </button>
          <button type="button" :class="{ active: outgoingTab === 'campaigns' }" @click="outgoingTab = 'campaigns'; loadCampaigns()">
            <PhoneCall :size="15" />
            Campaigns
          </button>
        </div>

        <section v-if="outgoingTab === 'connections'" class="dashboard-grid agent-page-grid">
          <article class="dashboard-card">
            <div class="members-card-head">
              <div>
                <h3>Ad &amp; form connections</h3>
                <p>OAuth connects source systems. Imported leads still need consent fields or form-level call consent before campaigns can use them.</p>
              </div>
            </div>
            <div class="provider-grid provider-grid-dual outgoing-provider-grid">
              <button type="button" class="provider-option" @click="requestLeadOAuth('meta_ads', 'facebook_ads')">
                <strong class="provider-name">Facebook Ads</strong>
                <small>Connect Facebook Page lead forms through Meta Lead Ads Retrieval.</small>
              </button>
              <button type="button" class="provider-option" @click="requestLeadOAuth('meta_ads', 'instagram_ads')">
                <strong class="provider-name">Instagram Ads</strong>
                <small>Connect Instagram instant-form leads through Meta Business access.</small>
              </button>
              <button type="button" class="provider-option" @click="requestLeadOAuth('google_ads')">
                <strong class="provider-name">Google Ads</strong>
                <small>Lead form submissions from a configured Google Ads customer ID.</small>
              </button>
              <button type="button" class="provider-option" @click="requestLeadOAuth('google_forms')">
                <strong class="provider-name">Google Forms</strong>
                <small>Read form responses from registered Google Forms.</small>
              </button>
            </div>
          </article>

          <article class="dashboard-card">
            <div class="members-card-head">
              <div>
                <h3>Connected sources</h3>
                <p>Sync pulls forms and leads into Nokvo's consent gate.</p>
              </div>
              <span class="status-chip">{{ leadConnections.length }} connected</span>
            </div>
            <div class="agent-document-list">
              <div v-if="!leadConnections.length" class="empty-state compact">No lead sources connected yet.</div>
              <div v-for="connection in leadConnections" :key="connection.id" class="agent-document-row">
                <div class="agent-document-main">
                  <div class="agent-document-icon">
                    <Globe :size="18" />
                  </div>
                  <div>
                    <strong>{{ connection.display_name }}</strong>
                    <small>{{ connection.provider }} · {{ connection.status }}<template v-if="connection.last_sync_at"> · synced {{ formatRelativeDate(connection.last_sync_at) }}</template></small>
                    <div v-if="connection.provider === 'google_ads'" class="outgoing-inline-editor">
                      <input v-model="connectionAccountInputs[connection.id]" class="db-input compact" type="text" placeholder="Google Ads customer ID" />
                      <button type="button" class="ghost-button compact" @click="saveConnectionAccount(connection)">Save</button>
                    </div>
                    <p v-if="connection.last_error" class="agent-warning">{{ connection.last_error }}</p>
                  </div>
                </div>
                <button
                  type="button"
                  class="primary-button compact"
                  :disabled="isSyncingLeadConnection === connection.id"
                  @click="syncLeadConnection(connection.id)"
                >
                  <Search :size="15" />
                  {{ isSyncingLeadConnection === connection.id ? 'Syncing…' : 'Sync' }}
                </button>
              </div>
            </div>
          </article>
        </section>

        <section v-else-if="outgoingTab === 'forms'" class="dashboard-grid agent-page-grid">
          <article class="dashboard-card">
            <div class="members-card-head">
              <div>
                <h3>Create Nokvo form</h3>
                <p>Creates a public lead form link with a required call-consent checkbox.</p>
              </div>
            </div>
            <div class="kb-form-grid">
              <label class="kb-field">
                <span>Form name</span>
                <input v-model="nokvoLeadForm.name" type="text" placeholder="Site visit enquiry" />
              </label>
              <label class="kb-field kb-field-wide">
                <span>Call consent text</span>
                <input v-model="nokvoLeadForm.consent_text" type="text" />
              </label>
            </div>
            <div class="kb-card-actions">
              <button type="button" class="primary-button compact" @click="createNokvoLeadForm">
                <Plus :size="15" />
                Create form link
              </button>
            </div>
          </article>

          <article class="dashboard-card">
            <div class="members-card-head">
              <div>
                <h3>Register external form</h3>
                <p>Map Google Forms or ad form fields and define where call consent is stored.</p>
              </div>
            </div>
            <div class="kb-form-grid">
              <label class="kb-field">
                <span>Provider</span>
                <select v-model="externalLeadForm.provider">
                  <option value="google_forms">Google Forms</option>
                  <option value="meta_ads">Meta Ads</option>
                  <option value="google_ads">Google Ads</option>
                </select>
              </label>
              <label class="kb-field">
                <span>Connection</span>
                <select v-model="externalLeadForm.source_connection_id">
                  <option value="">None</option>
                  <option v-for="connection in leadConnections" :key="connection.id" :value="connection.id">
                    {{ connection.display_name }}
                  </option>
                </select>
              </label>
              <label class="kb-field">
                <span>Form name</span>
                <input v-model="externalLeadForm.name" type="text" placeholder="Google Form leads" />
              </label>
              <label class="kb-field">
                <span>Provider form ID</span>
                <input v-model="externalLeadForm.provider_form_id" type="text" placeholder="1FAIpQL..." />
              </label>
              <label class="kb-field">
                <span>Consent field key</span>
                <input v-model="externalLeadForm.consent_field_key" type="text" placeholder="call_consent" />
              </label>
              <label class="kb-field">
                <span>Consent text</span>
                <input v-model="externalLeadForm.consent_text" type="text" placeholder="I agree to receive a call..." />
              </label>
              <label class="kb-field kb-field-wide">
                <span>Field mapping JSON</span>
                <textarea v-model="externalLeadForm.field_mapping" class="kb-prompt-textarea compact"></textarea>
              </label>
              <label class="custom-tab-required-toggle">
                <input type="checkbox" v-model="externalLeadForm.default_call_consent" />
                form submission itself is call consent
              </label>
            </div>
            <div class="kb-card-actions">
              <button type="button" class="primary-button compact" @click="registerExternalLeadForm">
                <CheckCircle2 :size="15" />
                Register form
              </button>
            </div>
          </article>

          <article class="dashboard-card wide-card">
            <div class="members-card-head">
              <div>
                <h3>Forms</h3>
                <p>Only active forms with consent mapping can produce callable leads.</p>
              </div>
              <span class="status-chip">{{ leadForms.length }} forms</span>
            </div>
            <div class="agent-document-list">
              <div v-if="!leadForms.length" class="empty-state compact">No forms registered yet.</div>
              <div v-for="form in leadForms" :key="form.id" class="agent-document-row">
                <div class="agent-document-main">
                  <div class="agent-document-icon">
                    <FileText :size="18" />
                  </div>
                  <div>
                    <strong>{{ form.name }}</strong>
                    <small>{{ form.provider }} · {{ form.status }}<template v-if="form.provider_form_id"> · {{ form.provider_form_id }}</template></small>
                    <p v-if="form.public_url" class="agent-warning">{{ form.public_url }}</p>
                  </div>
                </div>
                <span class="status-chip">{{ form.consent_field_key || form.default_call_consent ? 'Consent mapped' : 'Needs consent map' }}</span>
              </div>
            </div>
          </article>
        </section>

        <section v-else-if="outgoingTab === 'leads'" class="dashboard-card">
          <div class="members-card-head">
            <div>
              <h3>Consented leads</h3>
              <p>Select callable leads for the next campaign. Unknown-consent rows are visible but blocked.</p>
            </div>
            <span class="status-chip">{{ selectedCallableLeads.length }} selected</span>
          </div>

          <div class="outgoing-lead-list">
            <div v-if="!outgoingLeads.length" class="empty-state compact">No leads imported yet. Connect a source or publish a Nokvo form.</div>
            <article
              v-for="lead in outgoingLeads"
              :key="lead.id"
              role="button"
              tabindex="0"
              class="agent-document-row outgoing-lead-row"
              :class="{ active: selectedLeadIds.includes(lead.id), disabled: !lead.callable }"
              @click="toggleLeadSelection(lead)"
              @keydown.enter.prevent="toggleLeadSelection(lead)"
              @keydown.space.prevent="toggleLeadSelection(lead)"
            >
              <div class="agent-document-main">
                <div class="agent-document-icon">
                  <PhoneCall :size="18" />
                </div>
                <div>
                  <strong>{{ lead.name || lead.phone_e164 || 'Unnamed lead' }}</strong>
                  <small>{{ lead.source_provider }} · {{ lead.phone_e164 || 'no phone' }} · {{ lead.consent_status }}</small>
                </div>
              </div>
              <div class="outgoing-lead-actions">
                <a
                  v-if="phoneHref(lead.phone_e164 || lead.phone_raw)"
                  class="record-call-link"
                  :href="phoneHref(lead.phone_e164 || lead.phone_raw)"
                  @click.stop
                >
                  <PhoneCall :size="14" />
                  Call
                </a>
                <span class="status-chip">{{ lead.callable ? 'Callable' : 'Blocked' }}</span>
              </div>
            </article>
          </div>
        </section>

        <section v-else class="dashboard-card campaign-card">
          <div class="kb-card-head">
            <div class="kb-card-icon">
              <PhoneCall :size="18" />
            </div>
            <div>
              <h4>Outbound Campaigns</h4>
              <p>Create campaigns from selected consented leads. Excel contacts are no longer accepted for calling.</p>
            </div>
            <button type="button" class="ghost-button compact" @click="loadCampaigns">Refresh</button>
          </div>
          <div v-if="isAdmin" class="campaign-create">
            <div class="kb-form-grid">
              <label class="kb-field">
                <span>Campaign name</span>
                <input v-model="campaignForm.name" type="text" placeholder="Diwali outreach" />
              </label>
              <label class="kb-field">
                <span>From number (optional)</span>
                <input v-model="campaignForm.from_number" type="text" placeholder="+91XXXXXXXXXX" />
              </label>
              <label class="kb-field">
                <span>Agent prompt</span>
                <textarea v-model="campaignForm.agent_prompt" rows="3" placeholder="Role, tone, and call strategy"></textarea>
              </label>
              <label class="kb-field">
                <span>Objectives</span>
                <textarea v-model="campaignForm.objectives" rows="4" placeholder="One objective per line"></textarea>
              </label>
              <label class="kb-field">
                <span>Exit conditions</span>
                <textarea v-model="campaignForm.exit_conditions" rows="4" placeholder="One exit condition per line"></textarea>
              </label>
              <label class="kb-field">
                <span>Tone</span>
                <input v-model="campaignForm.tone" type="text" placeholder="warm, direct, and respectful" />
              </label>
              <label class="kb-field">
                <span>Silence nudge after seconds</span>
                <input v-model.number="campaignForm.silence_timeout_seconds" type="number" min="2" max="20" step="1" />
              </label>
              <label class="kb-field">
                <span>Script document (PDF/DOCX/TXT)</span>
                <input type="file" accept=".pdf,.docx,.txt,.md" @change="onCampaignFile($event, 'doc_file')" />
                <small v-if="campaignForm.doc_file">{{ campaignForm.doc_file.name }} · {{ formatBytes(campaignForm.doc_file.size) }}</small>
              </label>
              <div class="kb-field">
                <span>Selected leads</span>
                <strong>{{ selectedCallableLeads.length }} callable lead(s)</strong>
                <small>Go to Leads tab to change selection.</small>
              </div>
            </div>
            <div class="kb-card-actions">
              <button
                type="button"
                class="primary-button compact"
                :disabled="isCreatingCampaign || !selectedCallableLeads.length"
                @click="createCampaign"
              >
                <Plus :size="15" />
                {{ isCreatingCampaign ? 'Creating & ingesting…' : 'Create Campaign' }}
              </button>
              <span class="kb-card-hint">Script auto-indexes to Qdrant scoped to this campaign.</span>
            </div>
          </div>

          <div v-if="!campaigns.length" class="kb-empty">
            <div class="kb-empty-icon">
              <PhoneCall :size="24" />
            </div>
            <strong>No outbound campaigns yet.</strong>
            <span>Select consented leads and add a script to start.</span>
          </div>

          <div v-else class="kb-doc-list">
            <article v-for="c in campaigns" :key="c.id" class="kb-doc-card">
              <div class="kb-doc-icon">
                <PhoneCall :size="18" />
              </div>
              <div class="kb-doc-body">
                <div class="kb-doc-title-row">
                  <strong>{{ c.name }}</strong>
                  <span class="kb-pill" :class="`kb-pill-status-${c.status === 'running' ? 'approved' : c.status === 'draft' ? 'pending' : 'rejected'}`">
                    {{ c.status }}
                  </span>
                </div>
                <div class="kb-doc-meta">
                  <span><strong>{{ c.total_count }}</strong> contacts</span>
                  <span><strong>{{ c.answered_count }}</strong> answered</span>
                  <span v-if="c.failed_count"><strong>{{ c.failed_count }}</strong> failed</span>
                  <span v-if="c.from_number">from {{ c.from_number }}</span>
                  <span v-if="c.agent_config?.objectives?.length"><strong>{{ c.agent_config.objectives.length }}</strong> objectives</span>
                  <span v-if="c.agent_config?.silence_timeout_seconds">nudges after {{ c.agent_config.silence_timeout_seconds }}s</span>
                  <span v-if="c.created_at">created {{ formatRelativeDate(c.created_at) }}</span>
                </div>
              </div>
              <div class="kb-doc-actions" v-if="isAdmin">
                <button
                  v-if="c.status === 'draft'"
                  type="button"
                  class="primary-button compact"
                  :disabled="isLaunchingCampaign === c.id"
                  @click="launchCampaign(c.id)"
                >
                  <Play :size="15" />
                  {{ isLaunchingCampaign === c.id ? 'Launching…' : 'Launch' }}
                </button>
                <button
                  v-if="c.status === 'draft' || c.status === 'running'"
                  type="button"
                  class="ghost-button compact"
                  @click="cancelCampaign(c.id)"
                >
                  <XCircle :size="15" />
                  Cancel
                </button>
              </div>
            </article>
          </div>
        </section>
      </section>

      <!-- KNOWLEDGE BASE -->
      <section v-if="currentPage === 'knowledge_base'" class="dashboard-section kb-section">
        <div class="kb-hero">
          <div class="kb-hero-copy">
            <span class="section-kicker">Knowledge Base</span>
            <h3>Documents your agent answers from.</h3>
            <p>
              Uploads are stored in your tenant blob, chunked, and embedded with
              <strong>text-embedding-3-small</strong> on your dedicated Azure OpenAI
              deployment in <strong>South India</strong>. Only approved documents
              ever serve answers.
            </p>
            <div class="kb-hero-pill-row">
              <span class="kb-pill kb-pill-soft">
                <span class="kb-pill-dot"></span>
                Azure OpenAI · South India
              </span>
              <span class="kb-pill kb-pill-soft">text-embedding-3-small</span>
              <span class="kb-pill kb-pill-soft">1536-dim · cosine</span>
            </div>
          </div>
          <div class="kb-hero-stats">
            <div class="kb-stat-card">
              <span class="kb-stat-label">Documents</span>
              <strong class="kb-stat-value">{{ kbStats.total }}</strong>
              <span class="kb-stat-meta">{{ kbStats.approved }} approved · {{ kbStats.pending }} pending</span>
            </div>
            <div class="kb-stat-card">
              <span class="kb-stat-label">Chunks</span>
              <strong class="kb-stat-value">{{ kbStats.chunks }}</strong>
              <span class="kb-stat-meta">{{ kbStats.vectors }} vectors in Qdrant</span>
            </div>
            <div class="kb-stat-card">
              <span class="kb-stat-label">Storage</span>
              <strong class="kb-stat-value">{{ formatBytes(kbStats.bytes) || '—' }}</strong>
              <span class="kb-stat-meta">{{ kbStats.errors }} with errors</span>
            </div>
          </div>
        </div>

        <div v-if="kbError" class="message error dashboard-message">{{ kbError }}</div>
        <div v-else-if="kbInfo" class="message info dashboard-message">{{ kbInfo }}</div>

        <div class="kb-grid">
          <article v-if="isAdmin" class="dashboard-card kb-card kb-upload-card">
            <div class="kb-card-head">
              <div class="kb-card-icon kb-card-icon-primary">
                <Upload :size="18" />
              </div>
              <div>
                <h4>Upload Document</h4>
                <p>PDF, DOCX, TXT, or Markdown · embeds on save.</p>
              </div>
            </div>

            <label
              class="kb-dropzone"
              :class="{ 'has-file': kbForm.file || kbBulkQueue.length, 'is-busy': isUploadingKb || isUploadingKbBulk }"
              @dragover.prevent
              @drop.prevent="handleKbDrop"
            >
              <input
                ref="kbUploadInputRef"
                class="kb-dropzone-input"
                type="file"
                multiple
                accept=".pdf,.docx,.txt,.md,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain,text/markdown"
                @change="handleKbFileChange"
              />
              <div v-if="!kbForm.file && !kbBulkQueue.length" class="kb-dropzone-empty">
                <div class="kb-dropzone-icon">
                  <Upload :size="22" />
                </div>
                <strong>Drop files or click to browse</strong>
                <span>PDF, DOCX, TXT, MD · pick one for a named upload or many for bulk embed</span>
              </div>
              <div v-else-if="kbForm.file && !kbBulkQueue.length" class="kb-dropzone-filled">
                <div class="kb-dropzone-icon">
                  <FileText :size="20" />
                </div>
                <div class="kb-dropzone-file">
                  <strong>{{ kbForm.file.name }}</strong>
                  <span>{{ formatBytes(kbForm.file.size) }} · {{ kbForm.file.type || 'unknown type' }}</span>
                </div>
                <button
                  type="button"
                  class="kb-link-button"
                  @click.prevent.stop="clearKbFile"
                >
                  Replace
                </button>
              </div>
              <div v-else class="kb-dropzone-filled kb-dropzone-bulk">
                <div class="kb-dropzone-icon">
                  <FileText :size="20" />
                </div>
                <div class="kb-dropzone-file">
                  <strong>{{ kbBulkQueue.length }} files queued for bulk embed</strong>
                  <span>Tags &amp; type below apply to all. Filenames become document names.</span>
                </div>
                <button
                  type="button"
                  class="kb-link-button"
                  :disabled="isUploadingKbBulk"
                  @click.prevent.stop="clearKbFile"
                >
                  Clear
                </button>
              </div>
            </label>

            <ul v-if="kbBulkQueue.length" class="kb-bulk-queue">
              <li v-for="(entry, idx) in kbBulkQueue" :key="entry.file.name + idx" :class="`kb-bulk-row kb-bulk-${entry.status}`">
                <div class="kb-bulk-row-top">
                  <div class="kb-bulk-row-main">
                    <FileText :size="14" />
                    <span class="kb-bulk-name">{{ entry.name }}</span>
                    <span class="kb-bulk-size">{{ formatBytes(entry.file.size) }}</span>
                  </div>
                  <div class="kb-bulk-status">
                    <span v-if="entry.status === 'queued'">Queued</span>
                    <span v-else-if="entry.status === 'uploading'">Embedding…</span>
                    <span v-else-if="entry.status === 'done'" class="kb-bulk-done">Embedded</span>
                    <span v-else class="kb-bulk-error">Failed</span>
                    <button
                      v-if="entry.status === 'queued' && !isUploadingKbBulk"
                      type="button"
                      class="kb-link-button"
                      @click="removeBulkQueueItem(idx)"
                    >
                      Remove
                    </button>
                  </div>
                </div>
                <div v-if="entry.status === 'error' && entry.error" class="kb-bulk-error-detail">
                  {{ entry.error }}
                </div>
              </li>
            </ul>

            <div v-if="!kbBulkQueue.length" class="kb-form-grid">
              <label class="kb-field">
                <span>Name</span>
                <input v-model="kbForm.name" type="text" placeholder="Refund policy v2" />
              </label>
              <label class="kb-field">
                <span>Type</span>
                <select v-model="kbForm.document_type">
                  <option v-for="opt in kbDocumentTypes" :key="opt.value" :value="opt.value">
                    {{ opt.label }}
                  </option>
                </select>
              </label>
              <label class="kb-field kb-field-wide">
                <span>Description</span>
                <input v-model="kbForm.description" type="text" placeholder="Optional summary" />
              </label>
              <label class="kb-field kb-field-wide">
                <span>Tags</span>
                <input v-model="kbForm.tags" type="text" placeholder="refunds, returns, escalation" />
              </label>
            </div>

            <div v-else class="kb-form-grid">
              <label class="kb-field">
                <span>Type (applies to all)</span>
                <select v-model="kbForm.document_type" :disabled="isUploadingKbBulk">
                  <option v-for="opt in kbDocumentTypes" :key="opt.value" :value="opt.value">
                    {{ opt.label }}
                  </option>
                </select>
              </label>
              <label class="kb-field kb-field-wide">
                <span>Tags (applies to all)</span>
                <input v-model="kbForm.tags" type="text" placeholder="refunds, returns, escalation" :disabled="isUploadingKbBulk" />
              </label>
            </div>

            <div class="kb-card-actions">
              <button
                v-if="!kbBulkQueue.length"
                type="button"
                class="primary-button compact"
                :disabled="isUploadingKb || !kbForm.file"
                @click="uploadKnowledgeDocument"
              >
                <Upload :size="15" />
                {{ isUploadingKb ? 'Embedding…' : 'Upload & Embed' }}
              </button>
              <button
                v-else
                type="button"
                class="primary-button compact"
                :disabled="isUploadingKbBulk || !kbBulkQueue.length"
                @click="uploadKnowledgeDocumentsBulk"
              >
                <Upload :size="15" />
                {{ isUploadingKbBulk ? 'Embedding…' : `Upload &amp; Embed ${kbBulkQueue.length} Files` }}
              </button>
              <span class="kb-card-hint">
                <template v-if="isUploadingKb || isUploadingKbBulk">Chunking and calling Azure OpenAI…</template>
                <template v-else-if="kbBulkQueue.length">2 files embed in parallel to stay under Azure OpenAI rate limits.</template>
                <template v-else>Approved by default if text is extractable.</template>
              </span>
            </div>
          </article>

          <article v-if="isAdmin" class="dashboard-card kb-card kb-single-prompt-card">
            <div class="kb-card-head">
              <div class="kb-card-icon kb-card-icon-primary">
                <Mic :size="18" />
              </div>
              <div>
                <h4>Single Prompt Voice Agent</h4>
                <p>Give the live voice agent one operating prompt. It works alongside approved Knowledge Base documents.</p>
              </div>
            </div>

            <div v-if="kbSinglePromptConfig?.enabled" class="kb-single-status">
              <CheckCircle2 :size="16" />
              <div>
                <strong>Single prompt route active</strong>
                <span>
                  <template v-if="kbSinglePromptConfig.updated_at">Updated {{ formatRelativeDate(kbSinglePromptConfig.updated_at) }}</template>
                  <template v-else>Runtime prompt enabled</template>
                </span>
              </div>
              <button
                type="button"
                class="kb-link-button kb-single-disable"
                :disabled="isDisablingSinglePromptAgent"
                @click="disableSinglePromptVoiceAgent"
              >
                {{ isDisablingSinglePromptAgent ? 'Removing…' : 'Remove prompt' }}
              </button>
            </div>

            <label class="kb-field kb-field-wide">
              <span>Single Prompt</span>
              <textarea
                v-model="kbSinglePromptForm.prompt"
                class="kb-prompt-textarea"
                placeholder="You are a warm phone agent for the clinic. Greet callers briefly, answer only from approved Knowledge Base documents, ask for the missing detail when needed, and keep every reply under three sentences."
                :disabled="isSavingSinglePromptAgent"
              ></textarea>
            </label>

            <div class="kb-card-actions">
              <button
                type="button"
                class="primary-button compact"
                :disabled="isSavingSinglePromptAgent || !kbSinglePromptCanSubmit"
                @click="saveSinglePromptVoiceAgent"
              >
                <CheckCircle2 :size="15" />
                {{ isSavingSinglePromptAgent ? 'Configuring…' : 'Configure Voice Agent' }}
              </button>
              <span class="kb-card-hint">Applies this prompt to the live voice runtime. Knowledge still comes from approved documents.</span>
            </div>
          </article>

          <article v-else class="dashboard-card kb-card kb-readonly-card">
            <div class="kb-card-head">
              <div class="kb-card-icon">
                <Shield :size="18" />
              </div>
              <div>
                <h4>Read-only access</h4>
                <p>Only admins can upload or approve Knowledge Base documents. Ask an admin to add new sources.</p>
              </div>
            </div>
          </article>

          <article class="dashboard-card kb-card kb-search-card">
            <div class="kb-card-head">
              <div class="kb-card-icon">
                <Search :size="18" />
              </div>
              <div>
                <h4>Test Retrieval</h4>
                <p>Embed a query and inspect the chunks your agent would surface.</p>
              </div>
            </div>

            <div class="kb-search-bar">
              <Search :size="15" class="kb-search-bar-icon" />
              <input
                v-model="kbQuery"
                type="text"
                placeholder="What is the refund window?"
                @keyup.enter="testKnowledgeRetrieval"
              />
              <button
                type="button"
                class="primary-button compact kb-search-submit"
                :disabled="isSearchingKb || !kbQuery.trim()"
                @click="testKnowledgeRetrieval"
              >
                {{ isSearchingKb ? 'Searching…' : 'Search' }}
              </button>
            </div>

            <div v-if="kbResults.length" class="kb-results">
              <div v-for="(chunk, idx) in kbResults" :key="chunk.chunk_id" class="kb-result-card">
                <div class="kb-result-head">
                  <span class="kb-result-rank">#{{ idx + 1 }}</span>
                  <strong>{{ chunk.document_name }}</strong>
                  <span class="kb-pill kb-pill-score">{{ chunk.score.toFixed(3) }}</span>
                </div>
                <p class="kb-result-text">{{ chunk.text.slice(0, 320) }}{{ chunk.text.length > 320 ? '…' : '' }}</p>
              </div>
            </div>
            <div v-else-if="!isSearchingKb && kbQuery" class="kb-search-empty">
              No results yet — try a different phrasing or upload more documents.
            </div>
          </article>
        </div>

        <div class="kb-list-head">
          <div>
            <span class="section-kicker">Indexed</span>
            <h4>{{ kbDocuments.length }} document{{ kbDocuments.length === 1 ? '' : 's' }}</h4>
            <p>Pending uploads still embed; only approved documents serve answers in production.</p>
          </div>
          <div class="kb-list-actions">
            <button
              v-if="isAdmin"
              type="button"
              class="ghost-button compact"
              :disabled="isReconcilingKb"
              @click="reconcileKnowledgeDocuments"
              title="Rebuild this list from chunks that already exist in Qdrant (used when an earlier upload landed vectors but lost the metadata)"
            >
              <Database :size="15" />
              {{ isReconcilingKb ? 'Reconciling…' : 'Reconcile from Qdrant' }}
            </button>
            <button
              type="button"
              class="ghost-button compact"
              :disabled="isLoadingKb"
              @click="loadKnowledgeDocuments"
            >
              <Database :size="15" />
              {{ isLoadingKb ? 'Loading…' : 'Refresh' }}
            </button>
          </div>
        </div>

        <div v-if="!kbDocuments.length && !isLoadingKb" class="kb-empty">
          <div class="kb-empty-icon">
            <BookOpen :size="28" />
          </div>
          <strong>No documents yet.</strong>
          <span>Upload your first policy or FAQ to start grounding the agent. <template v-if="isAdmin">If you've already uploaded but the list is empty, click <em>Reconcile from Qdrant</em> to rebuild the registry from any orphaned embeddings.</template></span>
        </div>

        <div v-else class="kb-doc-list">
          <article
            v-for="doc in kbDocuments"
            :key="doc.id"
            class="kb-doc-card"
            :class="`kb-doc-status-${doc.approval_status}`"
          >
            <div class="kb-doc-icon">
              <FileText :size="18" />
            </div>
            <div class="kb-doc-body">
              <div class="kb-doc-title-row">
                <strong>{{ doc.name }}</strong>
                <span class="kb-pill kb-pill-type">{{ doc.document_type }}</span>
                <span
                  class="kb-pill"
                  :class="`kb-pill-status-${doc.approval_status}`"
                >
                  {{ doc.approval_status }}
                </span>
              </div>
              <p v-if="doc.description" class="kb-doc-desc">{{ doc.description }}</p>
              <div class="kb-doc-meta">
                <span><strong>{{ doc.chunk_count }}</strong> chunks</span>
                <span><strong>{{ doc.qdrant_point_count }}</strong> vectors</span>
                <span v-if="doc.created_at">added {{ formatRelativeDate(doc.created_at) }}</span>
                <span v-if="doc.tags && doc.tags.length" class="kb-doc-tags">
                  <span v-for="tag in doc.tags.slice(0, 4)" :key="tag" class="kb-tag">{{ tag }}</span>
                </span>
              </div>
              <p v-if="doc.last_error" class="kb-doc-error">{{ doc.last_error }}</p>

              <!-- Lazy-loaded chunk viewer. Lets the admin see exactly
                   what the agent is grounding on for this document. -->
              <div v-if="kbExpandedDocs[doc.id]" class="kb-doc-chunks">
                <div v-if="kbChunksByDoc[doc.id]?.loading" class="kb-doc-chunks-status">
                  Loading chunks…
                </div>
                <div v-else-if="kbChunksByDoc[doc.id]?.error" class="kb-doc-chunks-status kb-doc-chunks-error">
                  {{ kbChunksByDoc[doc.id].error }}
                </div>
                <div v-else-if="!kbChunksByDoc[doc.id]?.chunks?.length" class="kb-doc-chunks-status">
                  No chunks indexed for this document.
                </div>
                <ol v-else class="kb-doc-chunks-list">
                  <li
                    v-for="chunk in kbChunksByDoc[doc.id].chunks"
                    :key="chunk.index"
                    class="kb-chunk"
                  >
                    <div class="kb-chunk-head">
                      <span class="kb-chunk-index">#{{ chunk.index + 1 }}</span>
                      <span v-if="chunk.section_title" class="kb-chunk-section">{{ chunk.section_title }}</span>
                      <span class="kb-chunk-meta">{{ chunk.token_count }} tok · {{ chunk.char_end - chunk.char_start }} chars</span>
                    </div>
                    <p class="kb-chunk-text">{{ chunk.text }}</p>
                  </li>
                </ol>
              </div>
            </div>
            <div class="kb-doc-actions">
              <button
                type="button"
                class="ghost-button compact"
                @click="toggleKnowledgeChunks(doc.id)"
                :title="kbExpandedDocs[doc.id] ? 'Hide chunks' : 'View chunks the agent uses for retrieval'"
              >
                <FileText :size="15" />
                {{ kbExpandedDocs[doc.id] ? 'Hide chunks' : 'View chunks' }}
              </button>
              <template v-if="isAdmin">
                <button
                  v-if="doc.approval_status !== 'approved' && doc.chunk_count > 0"
                  type="button"
                  class="primary-button compact"
                  @click="approveKnowledgeDocument(doc.id)"
                >
                  <CheckCircle2 :size="15" />
                  Approve
                </button>
                <button
                  v-if="doc.approval_status === 'approved'"
                  type="button"
                  class="ghost-button compact"
                  @click="rejectKnowledgeDocument(doc.id)"
                >
                  <XCircle :size="15" />
                  Revoke
                </button>
                <button
                  type="button"
                  class="ghost-button compact danger"
                  @click="removeKnowledgeDocument(doc.id, doc.name)"
                  title="Delete document, embeddings, and generated cards"
                >
                  <Trash2 :size="15" />
                  Remove
                </button>
              </template>
            </div>
          </article>
        </div>
      </section>
    </main>

    <div v-if="pendingLeadOAuth" class="field-modal-shell">
      <div class="field-modal-backdrop" @click="closeLeadOAuthNotice"></div>
      <section class="field-modal lead-oauth-modal">
        <div class="members-card-head">
          <div>
            <h3>{{ pendingLeadOAuth.title }}</h3>
            <p>Instagram lead forms are retrieved through Meta Lead Ads access. Meta may show Facebook Login because the lead permissions are attached to the Meta business, ad account, and Page assets behind the Instagram ad.</p>
          </div>
          <button type="button" class="ghost-button compact" @click="closeLeadOAuthNotice">Close</button>
        </div>

        <div class="lead-oauth-note">
          <strong>Before continuing</strong>
          <p>The account authorizing this must be able to advertise on the ad account and access the Page or Meta business asset that owns the Instagram lead form. If the client only has an Instagram login with no Meta business/Page lead access, this connector cannot pull Lead Ads data for outbound calls.</p>
        </div>

        <div class="field-modal-actions">
          <button type="button" class="ghost-button compact" @click="outgoingTab = 'forms'; closeLeadOAuthNotice()">
            <FileText :size="15" />
            Use Nokvo form
          </button>
          <button type="button" class="primary-button compact" @click="continuePendingLeadOAuth">
            {{ pendingLeadOAuth.actionLabel }}
          </button>
        </div>
      </section>
    </div>

    <div v-if="timetableViewer.member" class="field-modal-shell">
      <div class="field-modal-backdrop" @click="closeMemberTimetable"></div>
      <section class="field-modal timetable-modal">
        <div class="members-card-head">
          <div>
            <span class="section-kicker">Calendar</span>
            <h3>{{ timetableViewer.member.full_name || timetableViewer.member.email }}</h3>
            <p>Scheduled meetings, ticket work, and blocked time from the assignment engine.</p>
          </div>
          <button type="button" class="ghost-button compact" @click="closeMemberTimetable">Close</button>
        </div>

        <div class="timetable-summary-row">
          <div>
            <span>Current load</span>
            <strong>{{ assignmentForMember(timetableViewer.member.id).active_request_count || 0 }}</strong>
          </div>
          <div>
            <span>Working hours</span>
            <strong>{{ formatSimpleTime(assignmentForMember(timetableViewer.member.id).start_time) }} - {{ formatSimpleTime(assignmentForMember(timetableViewer.member.id).end_time) }}</strong>
          </div>
          <div>
            <span>Duration</span>
            <strong>{{ assignmentForMember(timetableViewer.member.id).appointment_duration_minutes || 30 }} min</strong>
          </div>
        </div>

        <div v-if="timetableViewer.isLoading" class="empty-state compact">Loading timetable...</div>
        <template v-else>
          <div class="timetable-calendar-shell">
            <section class="timetable-calendar-card">
              <div class="timetable-calendar-head">
                <button type="button" class="ghost-button compact icon-only" aria-label="Previous month" @click="shiftTimetableMonth(-1)">
                  <ChevronLeft :size="16" />
                </button>
                <strong>{{ timetableMonthLabel }}</strong>
                <button type="button" class="ghost-button compact icon-only" aria-label="Next month" @click="shiftTimetableMonth(1)">
                  <ChevronRight :size="16" />
                </button>
              </div>
              <div class="timetable-weekdays">
                <span v-for="day in ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']" :key="day">{{ day }}</span>
              </div>
              <div class="timetable-month-grid">
                <button
                  v-for="day in timetableCalendarDays"
                  :key="day.key"
                  type="button"
                  class="timetable-date-cell"
                  :class="{ muted: !day.inMonth, today: day.isToday, active: day.isSelected, busy: day.itemCount }"
                  @click="selectTimetableDate(day.key)"
                >
                  <span>{{ day.dayNumber }}</span>
                  <small v-if="day.ticketCount">{{ day.ticketCount }} ticket{{ day.ticketCount === 1 ? '' : 's' }}</small>
                  <small v-else-if="day.itemCount">{{ day.itemCount }} item{{ day.itemCount === 1 ? '' : 's' }}</small>
                </button>
              </div>
            </section>

            <section class="timetable-selected-day">
              <div class="timetable-queue-head">
                <div>
                  <span class="section-kicker">Selected date</span>
                  <h4>{{ selectedTimetableLabel }}</h4>
                </div>
                <span class="status-chip">{{ selectedTimetableItems.length }} item{{ selectedTimetableItems.length === 1 ? '' : 's' }}</span>
              </div>

              <div v-if="!selectedTimetableItems.length" class="kb-empty compact">
                <div class="kb-empty-icon">
                  <CalendarDays :size="22" />
                </div>
                <strong>No tickets on this date.</strong>
                <span>Choose a highlighted date to see scheduled tickets, appointments, and blocked slots.</span>
              </div>

              <div v-else class="timetable-day-list">
                <article v-for="item in selectedTimetableItems" :key="`${item.type}-${item.id}`" class="timetable-event" :class="{ blocked: item.isBlocked }">
                  <div class="timetable-time">
                    <strong>{{ formatCalendarTime(item.start) }}</strong>
                    <span>{{ item.end ? formatCalendarTime(item.end) : '' }}</span>
                  </div>
                  <div class="timetable-event-main">
                    <div>
                      <strong>{{ item.title }}</strong>
                      <span>{{ item.detail || item.type }}</span>
                      <a v-if="item.phoneHref" class="record-call-link timetable-call-link" :href="item.phoneHref">
                        <PhoneCall :size="14" />
                        Call {{ item.phone }}
                      </a>
                    </div>
                    <small>{{ item.typeLabel || item.status }}</small>
                  </div>
                </article>
              </div>
            </section>
          </div>

          <div v-if="memberQueuedItems.length" class="timetable-queue">
            <div class="timetable-queue-head">
              <div>
                <span class="section-kicker">Assigned queue</span>
                <h4>Tickets and requests without a booked time</h4>
              </div>
              <span class="status-chip">{{ memberQueuedItems.length }} item{{ memberQueuedItems.length === 1 ? '' : 's' }}</span>
            </div>
            <div class="timetable-ticket-list">
              <article v-for="item in memberQueuedItems" :key="`${item.type}-${item.id}`" class="timetable-ticket-card">
                <div class="timetable-ticket-primary">
                  <span class="timetable-type-pill">{{ item.typeLabel }}</span>
                  <strong>{{ item.title }}</strong>
                  <small>{{ item.detail || 'No details captured yet.' }}</small>
                  <a v-if="item.phoneHref" class="record-call-link timetable-call-link" :href="item.phoneHref">
                    <PhoneCall :size="14" />
                    Call {{ item.phone }}
                  </a>
                </div>
                <div class="timetable-ticket-meta">
                  <div>
                    <span>Status</span>
                    <strong>{{ item.status || 'open' }}</strong>
                  </div>
                  <div v-if="item.priority">
                    <span>Priority</span>
                    <strong>{{ item.priority }}</strong>
                  </div>
                  <div v-if="item.owner">
                    <span>Owner</span>
                    <strong>{{ item.owner }}</strong>
                  </div>
                  <div>
                    <span>Created</span>
                    <strong>{{ formatRelativeDate(item.createdAt) || 'Unknown' }}</strong>
                  </div>
                </div>
              </article>
            </div>
          </div>
        </template>
      </section>
    </div>

    <div v-if="fieldEditor.key" class="field-modal-shell">
      <div class="field-modal-backdrop" @click="closeFieldEdit"></div>
      <section class="field-modal">
        <div class="members-card-head">
          <div>
            <h3>Edit {{ fieldEditor.title }}</h3>
            <p>Use plain names your team recognizes. Required fields appear as must-fill details.</p>
          </div>
          <button type="button" class="ghost-button compact" @click="closeFieldEdit">Close</button>
        </div>

        <div class="field-editor-list">
          <div v-for="(field, index) in fieldEditor.fields" :key="`${field.key}:${index}`" class="field-editor-row">
            <label>
              <span>Field Name</span>
              <input v-model="field.label" type="text" placeholder="Customer Name" />
            </label>
            <label>
              <span>Type</span>
              <select v-model="field.type">
                <option v-for="type in fieldTypes" :key="type" :value="type">{{ type }}</option>
              </select>
            </label>
            <label class="field-required-toggle">
              <input v-model="field.required" type="checkbox" />
              <span>Required</span>
            </label>
            <button type="button" class="nav-icon-button" :disabled="fieldEditor.fields.length <= 1" @click="removeField(index)">
              <Trash2 :size="16" />
            </button>
          </div>
        </div>

        <div class="field-modal-actions">
          <button type="button" class="ghost-button compact" @click="addField">
            <Plus :size="15" />
            Add Field
          </button>
          <button type="button" class="primary-button compact" :disabled="fieldEditor.isSaving" @click="saveFieldEdit">
            {{ fieldEditor.isSaving ? 'Saving...' : 'Save Fields' }}
          </button>
        </div>
      </section>
    </div>

    <div v-if="assignmentEditor.member" class="field-modal-shell">
      <div class="field-modal-backdrop" @click="closeAssignmentEdit"></div>
      <section class="field-modal assignment-modal">
        <div class="members-card-head">
          <div>
            <h3>Availability for {{ assignmentEditor.member.full_name || assignmentEditor.member.email }}</h3>
            <p>Set who can receive agent-assigned work, when they are available, and which requests they handle.</p>
          </div>
          <button type="button" class="ghost-button compact" @click="closeAssignmentEdit">Close</button>
        </div>

        <div v-if="assignmentEditor.settings" class="assignment-form-grid calendar-form-grid">
          <div class="calendar-toolbar">
            <label class="field-required-toggle assignment-toggle">
              <input v-model="assignmentEditor.settings.is_assignable" type="checkbox" @change="handleAssignableToggle" />
              <span>Can receive assignments</span>
            </label>
            <span class="schedule-status-pill" :class="{ inactive: !assignmentEditor.settings.is_assignable }">{{ assignmentEditorStatus }}</span>
            <button type="button" class="ghost-button compact" @click="applyRecommendedAssignmentSetup">
              <CheckCircle2 :size="15" />
              Recommended setup
            </button>
            <span class="calendar-timezone">IST</span>
          </div>

          <div class="schedule-calendar-card">
            <div class="calendar-card-head">
              <div>
                <strong>Weekly Availability</strong>
                <span>
                  {{ formatSimpleTime(assignmentEditor.settings.start_time) }} -
                  {{ formatSimpleTime(assignmentEditor.settings.end_time) }} ·
                  {{ timeRangeDuration(assignmentEditor.settings.start_time, assignmentEditor.settings.end_time) }}
                </span>
              </div>
              <CalendarDays :size="19" />
            </div>

            <div class="schedule-quick-actions">
              <button type="button" class="ghost-button compact" @click="setScheduleDays(scheduleDefaultForBusiness.days)">
                Use default days
              </button>
              <button type="button" class="ghost-button compact" @click="setScheduleDays(['mon', 'tue', 'wed', 'thu', 'fri'])">
                Weekdays
              </button>
              <button type="button" class="ghost-button compact" @click="setScheduleDays(allScheduleDayValues)">
                Every day
              </button>
            </div>

            <div class="week-calendar-grid">
              <button
                v-for="day in scheduleDays"
                :key="day.value"
                type="button"
                class="week-day-card"
                :aria-pressed="assignmentEditor.settings.working_days.includes(day.value)"
                :class="{ active: assignmentEditor.settings.working_days.includes(day.value) }"
                @click="toggleListValue(assignmentEditor.settings.working_days, day.value)"
              >
                <span>{{ day.label }}</span>
                <strong>{{ assignmentEditor.settings.working_days.includes(day.value) ? 'Available' : 'Off' }}</strong>
                <small v-if="assignmentEditor.settings.working_days.includes(day.value)">
                  {{ formatSimpleTime(assignmentEditor.settings.start_time) }} - {{ formatSimpleTime(assignmentEditor.settings.end_time) }}
                </small>
                <small v-else>No assignments</small>
              </button>
            </div>
          </div>

          <div class="time-planner-card">
            <div class="time-planner-head">
              <div>
                <span class="micro-label">Daily Time Window</span>
                <strong>{{ formatSimpleTime(assignmentEditor.settings.start_time) }} - {{ formatSimpleTime(assignmentEditor.settings.end_time) }}</strong>
              </div>
              <span>{{ timeRangeDuration(assignmentEditor.settings.start_time, assignmentEditor.settings.end_time) }}</span>
            </div>

            <div class="time-preset-row">
              <button
                v-for="preset in scheduleTimePresets"
                :key="preset.label"
                type="button"
                class="time-preset-button"
                :class="{ active: isScheduleTimePresetActive(preset) }"
                @click="applyScheduleTimePreset(preset)"
              >
                <span>{{ preset.label }}</span>
                <strong>{{ formatSimpleTime(preset.start) }} - {{ formatSimpleTime(preset.end) }}</strong>
              </button>
            </div>

            <div class="time-range-control">
              <label class="time-input-card">
                <span>From</span>
                <input v-model="assignmentEditor.settings.start_time" type="time" step="900" />
              </label>
              <label class="time-input-card">
                <span>To</span>
                <input v-model="assignmentEditor.settings.end_time" type="time" step="900" />
              </label>
            </div>
          </div>

          <div class="capacity-planner-card">
            <label>
              <span>MAX REQUESTS PER HOUR</span>
              <input v-model.number="assignmentEditor.settings.max_requests_per_hour" type="number" min="1" max="100" />
            </label>
            <label>
              <span>APPOINTMENT DURATION (MIN)</span>
              <input v-model.number="assignmentEditor.settings.appointment_duration_minutes" type="number" min="5" max="480" />
            </label>
            <p>Uses IST for every schedule and assignment decision. The scheduler combines duration with max requests per hour to allot the next free slot when the requested time is taken.</p>
          </div>

          <div class="db-form-block">
            <div class="db-label-row">
              <label class="db-label">Request Types Handled</label>
              <button type="button" class="ghost-button compact" @click="selectAllRequestTypes">Select all</button>
            </div>
            <div class="assignment-chip-grid request-types">
              <label v-for="type in requestTypeOptions" :key="type.value" class="assignment-chip">
                <input
                  type="checkbox"
                  :checked="assignmentEditor.settings.request_types.includes(type.value)"
                  @change="toggleListValue(assignmentEditor.settings.request_types, type.value)"
                />
                <span>{{ type.label }}</span>
              </label>
            </div>
          </div>

          <div class="field-modal-actions">
            <button type="button" class="primary-button compact" :disabled="assignmentEditor.isSaving || assignmentEditor.isSavingClinic" @click="saveMemberSchedule">
              {{ assignmentEditor.isSaving || assignmentEditor.isSavingClinic ? 'Saving...' : 'Save Availability' }}
            </button>
          </div>
        </div>

        <div v-if="isClinicTemplate && assignmentEditor.clinic" class="clinic-schedule-panel">
          <div class="members-card-head">
            <div>
              <h3>Clinic Schedule</h3>
              <p>Capacity and blocked slots prevent bookings during rounds, operations, breaks, or overload.</p>
            </div>
          </div>

          <div class="assignment-two-col">
            <label>
              <span>Appointment Duration</span>
              <input v-model.number="assignmentEditor.clinic.appointment_duration_minutes" type="number" min="5" />
            </label>
            <label>
              <span>Buffer Minutes</span>
              <input v-model.number="assignmentEditor.clinic.buffer_minutes" type="number" min="0" />
            </label>
            <label>
              <span>Max Patients / Hour</span>
              <input v-model.number="assignmentEditor.clinic.max_patients_per_hour" type="number" min="1" />
            </label>
            <label>
              <span>Max Patients / Day</span>
              <input v-model.number="assignmentEditor.clinic.max_patients_per_day" type="number" min="1" />
            </label>
          </div>

          <div class="db-form-block">
            <div class="db-label-row">
              <label class="db-label">Consultation Types</label>
              <button type="button" class="ghost-button compact" @click="selectAllConsultationTypes">Select all</button>
            </div>
            <div class="assignment-chip-grid request-types">
              <label v-for="type in consultationTypeOptions" :key="type.value" class="assignment-chip">
                <input
                  type="checkbox"
                  :checked="assignmentEditor.clinic.consultation_types.includes(type.value)"
                  @change="toggleListValue(assignmentEditor.clinic.consultation_types, type.value)"
                />
                <span>{{ type.label }}</span>
              </label>
            </div>
          </div>
        </div>

        <div class="blocked-slot-panel">
          <div class="members-card-head">
            <div>
              <h3>Blocked Calendar</h3>
              <p>Mark unavailable time. The assignment engine skips these slots for every business type.</p>
            </div>
          </div>

          <div class="blocked-slot-form easy-block-form">
            <label>
              <span>Date</span>
              <input v-model="assignmentEditor.blockedSlot.date" type="date" />
            </label>
            <label>
              <span>From</span>
              <input v-model="assignmentEditor.blockedSlot.start_time" type="time" step="900" />
            </label>
            <label>
              <span>To</span>
              <input v-model="assignmentEditor.blockedSlot.end_time" type="time" step="900" />
            </label>
            <label>
              <span>Reason</span>
              <input v-model="assignmentEditor.blockedSlot.reason" type="text" placeholder="Meeting, break, site visit" />
            </label>
            <button type="button" class="ghost-button compact" :disabled="assignmentEditor.isSavingBlock" @click="addBlockedSlot">
              <Plus :size="15" />
              Add Block
            </button>
          </div>

          <div class="blocked-slot-list calendar-event-list">
            <div v-if="!(blockedSlots[assignmentEditor.member.id] || []).length" class="empty-state compact">No blocked slots.</div>
            <div v-for="slot in blockedSlots[assignmentEditor.member.id] || []" :key="slot.id" class="blocked-slot-row calendar-event-card">
              <div class="event-date-badge">
                <span>{{ formatCalendarDate(slot.start_time) }}</span>
                <strong>{{ formatCalendarTime(slot.start_time) }}</strong>
              </div>
              <div class="event-main">
                <strong>{{ slot.reason || 'Unavailable' }}</strong>
                <span>{{ formatCalendarTime(slot.start_time) }} - {{ formatCalendarTime(slot.end_time) }}</span>
              </div>
              <button type="button" class="nav-icon-button" @click="deleteBlockedSlot(slot.id)">
                <Trash2 :size="16" />
              </button>
            </div>
          </div>
        </div>
      </section>
    </div>

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
  gap: 0.5rem;
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

.mode-link--icon {
  padding: 0.55rem;
  width: 2.35rem;
  height: 2.35rem;
  justify-content: center;
}

.mode-link--icon.active {
  background: rgba(27, 28, 21, 0.08);
}

.mode-link-wrap {
  position: relative;
  display: inline-flex;
}

.mode-settings-menu {
  top: calc(100% + 0.45rem);
  right: 0;
  left: auto;
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

.connect-layout {
  position: fixed;
  inset: 0;
  z-index: 10;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 2rem;
  background:
    radial-gradient(circle at 12% 18%, rgba(255, 196, 110, 0.55), transparent 55%),
    radial-gradient(circle at 88% 14%, rgba(124, 196, 255, 0.45), transparent 55%),
    radial-gradient(circle at 50% 95%, rgba(199, 130, 255, 0.4), transparent 55%),
    linear-gradient(135deg, #f4e8d2 0%, #f6c8a3 35%, #b3c8f0 70%, #d9b8f5 100%);
  background-size: 180% 180%, 180% 180%, 180% 180%, 200% 200%;
  background-position: 0% 0%, 100% 0%, 50% 100%, 0% 50%;
  color: #1b1c15;
  overflow: hidden;
  animation: connect-bg-drift 18s ease-in-out infinite alternate,
    connect-layout-in 0.55s ease-out both;
}

@keyframes connect-layout-in {
  0% { opacity: 0; transform: scale(0.985); }
  100% { opacity: 1; transform: scale(1); }
}

@keyframes connect-bg-drift {
  0%   { background-position: 0% 0%, 100% 0%, 50% 100%, 0% 50%; }
  100% { background-position: 15% 25%, 85% 30%, 45% 80%, 100% 50%; }
}

.connect-back-link {
  position: absolute;
  top: 1.5rem;
  left: 1.5rem;
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.65rem 1rem;
  border-radius: 999px;
  border: 1px solid rgba(27, 28, 21, 0.18);
  background: rgba(255, 255, 255, 0.72);
  color: #1b1c15;
  font-family: Manrope, sans-serif;
  font-size: 0.85rem;
  font-weight: 600;
  cursor: pointer;
  backdrop-filter: blur(6px);
  transition: background 0.15s ease, transform 0.15s ease;
}

.connect-back-link:hover {
  background: rgba(255, 255, 255, 0.92);
  transform: translateX(-2px);
}

.connect-title {
  font-family: 'Playfair Display', Manrope, serif;
  font-size: clamp(2.75rem, 6vw, 4.75rem);
  font-weight: 700;
  letter-spacing: -0.01em;
  text-align: center;
  margin: 0;
  text-shadow: 0 2px 24px rgba(255, 255, 255, 0.35);
  display: inline-flex;
  flex-wrap: wrap;
  justify-content: center;
}

.connect-title-char {
  display: inline-block;
  opacity: 0;
  transform: translateY(0.55em) rotate(-3deg);
  filter: blur(6px);
  animation: connect-char-in 0.7s cubic-bezier(0.22, 1, 0.36, 1) forwards;
}

.connect-title-space {
  width: 0.35em;
}

.connect-continue-button {
  margin-top: 2rem;
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.85rem 1.6rem;
  border-radius: 999px;
  border: 1px solid rgba(27, 28, 21, 0.18);
  background: rgba(27, 28, 21, 0.88);
  color: #f4f0e1;
  font-family: Manrope, sans-serif;
  font-size: 0.95rem;
  font-weight: 600;
  letter-spacing: 0.01em;
  cursor: pointer;
  backdrop-filter: blur(6px);
  box-shadow: 0 8px 24px rgba(27, 28, 21, 0.18);
  opacity: 0;
  transform: translateY(8px);
  animation: connect-continue-in 0.55s 1.1s cubic-bezier(0.22, 1, 0.36, 1) forwards;
  transition: transform 0.15s ease, box-shadow 0.15s ease, background 0.15s ease;
}

.connect-continue-button:hover {
  transform: translateY(-1px);
  box-shadow: 0 12px 30px rgba(27, 28, 21, 0.24);
  background: #1b1c15;
}

.connect-continue-button:active {
  transform: translateY(0);
}

@keyframes connect-continue-in {
  0%   { opacity: 0; transform: translateY(8px); }
  100% { opacity: 1; transform: translateY(0); }
}

.org-shell.dark .connect-continue-button {
  background: rgba(244, 240, 225, 0.92);
  color: #1b1c15;
  border-color: rgba(244, 240, 225, 0.3);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
}

.org-shell.dark .connect-continue-button:hover {
  background: #ffffff;
}

.connect-layout--scroll {
  justify-content: flex-start;
  padding-top: 5rem;
  padding-bottom: 3rem;
  overflow-y: auto;
  align-items: stretch;
}

.connect-panel {
  width: min(100%, 720px);
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
  background: rgba(255, 255, 255, 0.72);
  backdrop-filter: blur(10px);
  border: 1px solid rgba(27, 28, 21, 0.12);
  border-radius: 18px;
  padding: 1.75rem;
  box-shadow: 0 18px 48px rgba(27, 28, 21, 0.12);
  color: #1b1c15;
}

.connect-panel-head h2 {
  font-family: 'Playfair Display', Manrope, serif;
  font-size: 1.6rem;
  margin: 0 0 0.35rem;
}

.connect-panel-head p {
  color: #5f5f53;
  font-size: 0.92rem;
  margin: 0;
}

.connect-alert {
  border-radius: 10px;
  padding: 0.7rem 0.9rem;
  font-size: 0.85rem;
}

.connect-alert.error {
  background: rgba(220, 64, 64, 0.12);
  color: #8a1f1f;
}

.connect-secret-callout {
  background: #1b1c15;
  color: #f4f0e1;
  border-radius: 12px;
  padding: 1rem 1.1rem;
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
}

.connect-secret-callout strong {
  font-family: Manrope, sans-serif;
  font-size: 0.95rem;
}

.connect-secret-callout p {
  margin: 0;
  font-size: 0.85rem;
  color: rgba(244, 240, 225, 0.78);
}

.connect-secret-value {
  display: block;
  background: rgba(244, 240, 225, 0.12);
  border-radius: 8px;
  padding: 0.65rem 0.8rem;
  font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.8rem;
  word-break: break-all;
}

.connect-secret-subline {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  font-size: 0.78rem;
  color: rgba(244, 240, 225, 0.78);
}

.connect-secret-subline code {
  font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  color: #f4f0e1;
  word-break: break-all;
}

.connect-secret-dismiss {
  align-self: flex-end;
  background: rgba(244, 240, 225, 0.12);
  border: 1px solid rgba(244, 240, 225, 0.22);
  color: #f4f0e1;
  padding: 0.4rem 0.85rem;
  border-radius: 999px;
  cursor: pointer;
  font-size: 0.78rem;
}

.connect-create-form {
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
}

.connect-form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.85rem;
}

.connect-form-fullwidth {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.connect-create-form label {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  font-size: 0.82rem;
  color: #5f5f53;
}

.connect-create-form input,
.connect-create-form select,
.connect-create-form textarea {
  border: 1px solid rgba(27, 28, 21, 0.18);
  background: #fff;
  border-radius: 8px;
  padding: 0.6rem 0.75rem;
  font-size: 0.9rem;
  color: #1b1c15;
  font-family: inherit;
}

.connect-create-form textarea {
  resize: vertical;
  min-height: 60px;
}

.connect-create-form .connect-continue-button {
  align-self: flex-start;
  margin-top: 0.25rem;
  animation: none;
  opacity: 1;
  transform: none;
}

.connect-key-list {
  display: flex;
  flex-direction: column;
  gap: 0.65rem;
}

.connect-key-list h3 {
  font-family: Manrope, sans-serif;
  font-size: 0.95rem;
  margin: 0.5rem 0 0;
}

.connect-key-empty {
  color: #5f5f53;
  font-size: 0.88rem;
  padding: 0.75rem 0;
}

.connect-key-card {
  border: 1px solid rgba(27, 28, 21, 0.12);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.86);
  padding: 0.85rem 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
}

.connect-key-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.connect-key-card-head strong {
  display: block;
  font-family: Manrope, sans-serif;
  font-size: 0.92rem;
}

.connect-key-card-head code {
  font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.78rem;
  color: #5f5f53;
}

.connect-key-status {
  font-size: 0.7rem;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  padding: 0.15rem 0.5rem;
  border-radius: 999px;
  background: rgba(30, 130, 70, 0.12);
  color: #1a6a3b;
}

.connect-key-status.revoked {
  background: rgba(150, 80, 80, 0.12);
  color: #8a1f1f;
}

.connect-key-card-meta {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 0.45rem 1rem;
  margin: 0;
  font-size: 0.78rem;
  color: #5f5f53;
}

.connect-key-card-meta div {
  display: flex;
  justify-content: space-between;
}

.connect-key-card-meta dt {
  color: #5f5f53;
}

.connect-key-card-meta dd {
  margin: 0;
  color: #1b1c15;
  font-weight: 600;
}

.connect-revoke-button {
  align-self: flex-start;
  border: 1px solid rgba(220, 64, 64, 0.4);
  background: transparent;
  color: #8a1f1f;
  padding: 0.4rem 0.85rem;
  border-radius: 999px;
  font-size: 0.78rem;
  cursor: pointer;
}

.connect-revoke-button:hover {
  background: rgba(220, 64, 64, 0.08);
}

.org-shell.dark .connect-panel {
  background: rgba(28, 29, 24, 0.72);
  border-color: rgba(244, 240, 225, 0.16);
  color: #f4f0e1;
}

.org-shell.dark .connect-panel-head p,
.org-shell.dark .connect-create-form label,
.org-shell.dark .connect-key-card-meta,
.org-shell.dark .connect-key-card-head code,
.org-shell.dark .connect-key-empty {
  color: rgba(244, 240, 225, 0.7);
}

.org-shell.dark .connect-create-form input,
.org-shell.dark .connect-create-form select,
.org-shell.dark .connect-create-form textarea {
  background: rgba(28, 29, 24, 0.6);
  border-color: rgba(244, 240, 225, 0.2);
  color: #f4f0e1;
}

.org-shell.dark .connect-key-card {
  background: rgba(28, 29, 24, 0.7);
  border-color: rgba(244, 240, 225, 0.16);
}

.org-shell.dark .connect-key-card-meta dd {
  color: #f4f0e1;
}

@keyframes connect-char-in {
  0% {
    opacity: 0;
    transform: translateY(0.55em) rotate(-3deg);
    filter: blur(6px);
  }
  60% {
    opacity: 1;
    filter: blur(0);
  }
  100% {
    opacity: 1;
    transform: translateY(0) rotate(0);
    filter: blur(0);
  }
}

.org-shell.dark .connect-layout {
  color: #f4f0e1;
  background:
    radial-gradient(circle at 12% 18%, rgba(255, 168, 78, 0.4), transparent 55%),
    radial-gradient(circle at 88% 14%, rgba(80, 140, 220, 0.45), transparent 55%),
    radial-gradient(circle at 50% 95%, rgba(150, 90, 220, 0.45), transparent 55%),
    linear-gradient(135deg, #1c1d18 0%, #2a2620 40%, #1d2a3d 75%, #2c1d3d 100%);
}

.org-shell.dark .connect-back-link {
  border-color: rgba(244, 240, 225, 0.28);
  background: rgba(28, 29, 24, 0.7);
  color: #f4f0e1;
}

.org-shell.dark .connect-back-link:hover {
  background: rgba(28, 29, 24, 0.9);
}

.login-layout {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 100px;
  max-width: 460px;
}

.brand-block {
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.6rem;
}

.brand-block-logo {
  display: block;
  width: clamp(17rem, 34vw, 26rem);
  max-width: 100%;
  /* Source PNG is 500×500 with whitespace below the "ONE" wordmark.
     Force a 500/420 box and crop with object-fit so only the top portion
     (logo glyph + NOKVO + ONE) renders. */
  aspect-ratio: 500 / 325;
  height: auto;
  object-fit: cover;
  object-position: center top;
  transition: filter 0.2s ease;
}

.org-shell.dark .brand-block-logo,
.org-shell.dark .brand-logo {
  /* The PNG is dark ink on a light background. In dark mode the surrounding
     surface is dark, so the logo would vanish — invert the pixel values to
     keep contrast without shipping a second asset. */
  filter: invert(1) hue-rotate(180deg);
}

.brand-block h1 {
  font-family: Manrope, sans-serif;
  font-size: clamp(2.25rem, 4vw, 3rem);
  letter-spacing: -0.05em;
  font-weight: 700;
}

.brand-block p {
  margin: 0;
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

.google-fallback-button {
  width: 100%;
  min-height: 44px;
  border-radius: 999px;
  border: 1px solid #d9d8ce;
  background: #ffffff;
  color: #1b1c15;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.7rem;
  font-size: 0.95rem;
  font-weight: 700;
  opacity: 0.72;
}

.google-mark {
  width: 1.35rem;
  height: 1.35rem;
  border-radius: 999px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid #d9d8ce;
  font-family: Arial, sans-serif;
  font-weight: 700;
  color: #4285f4;
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

.ghost-button.danger {
  border-color: #d6a7a0;
  background: #fff5f3;
  color: #8a2a1a;
}
.ghost-button.danger:hover {
  background: #fde9e4;
  border-color: #c98577;
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

.business-type-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  margin: 0.4rem 0 1rem;
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

.schema-preview {
  border: 1px solid #e4e3d7;
  background: #fbfaee;
  border-radius: 0.95rem;
  padding: 1rem;
}

.schema-preview strong,
.schema-field-row strong {
  color: #1b1c15;
}

.schema-preview-grid,
.schema-field-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.7rem;
  margin-top: 0.85rem;
}

.schema-preview-grid span,
.schema-field-row {
  border: 1px solid #e4e3d7;
  background: rgba(255, 255, 255, 0.72);
  border-radius: 0.8rem;
  padding: 0.85rem;
}

.schema-field-row {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.schema-field-row span,
.form-hint {
  color: #5f5f53;
  font-size: 0.88rem;
}

.form-hint {
  margin: 0.55rem 0 0;
}

.field-card-actions {
  display: inline-flex;
  align-items: center;
  gap: 0.65rem;
  flex-wrap: wrap;
}

.field-modal-shell {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1.5rem;
}

.field-modal-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(18, 19, 16, 0.48);
  backdrop-filter: blur(10px);
}

.field-modal {
  position: relative;
  z-index: 1;
  width: min(920px, 100%);
  max-height: min(760px, 88vh);
  overflow: auto;
  border-radius: 1.1rem;
  border: 1px solid #e4e3d7;
  background: #fffef8;
  box-shadow: 0 28px 90px -34px rgba(27, 28, 21, 0.45);
  padding: 1.4rem;
}

.field-editor-list {
  display: grid;
  gap: 0.8rem;
  margin-top: 1rem;
}

.field-editor-row {
  display: grid;
  grid-template-columns: minmax(180px, 1fr) 150px 120px 42px;
  gap: 0.75rem;
  align-items: end;
  padding: 0.85rem;
  border-radius: 0.9rem;
  border: 1px solid #e4e3d7;
  background: #fbfaee;
}

.field-editor-row label {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.field-editor-row label span {
  color: #5f5f53;
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

.field-editor-row input[type="text"],
.field-editor-row select {
  width: 100%;
  border-radius: 0.75rem;
  border: 1px solid #d9d8ce;
  background: #ffffff;
  color: #1b1c15;
  padding: 0.75rem 0.8rem;
  font-size: 0.92rem;
}

.field-required-toggle {
  min-height: 42px;
  flex-direction: row !important;
  align-items: center;
}

.field-required-toggle input {
  width: 1rem;
  height: 1rem;
}

.field-modal-actions {
  display: flex;
  justify-content: space-between;
  gap: 0.8rem;
  margin-top: 1rem;
}

.assignment-summary-cell {
  display: grid;
  gap: 0.35rem;
}

.assignment-modal {
  width: min(1040px, 100%);
}

.lead-oauth-modal {
  width: min(620px, 100%);
}

.lead-oauth-note {
  display: grid;
  gap: 0.45rem;
  margin-top: 1rem;
  padding: 1rem;
  border: 1px solid #d9e2d1;
  border-radius: 0.85rem;
  background: #f7fbf1;
  color: #394232;
}

.lead-oauth-note strong {
  color: #1f2d1b;
}

.lead-oauth-note p {
  margin: 0;
  color: #586151;
  line-height: 1.55;
}

.assignment-form-grid,
.clinic-schedule-panel,
.blocked-slot-panel {
  display: grid;
  gap: 1rem;
  margin-top: 1rem;
}

.clinic-schedule-panel,
.blocked-slot-panel {
  border-top: 1px solid #e4e3d7;
  padding-top: 1rem;
}

.assignment-two-col {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.8rem;
}

.assignment-form-grid label,
.assignment-two-col label,
.blocked-slot-form label {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.assignment-form-grid label span,
.assignment-two-col label span,
.blocked-slot-form label span {
  color: #5f5f53;
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

.assignment-form-grid input[type="text"],
.assignment-form-grid input[type="number"],
.assignment-form-grid input[type="time"],
.assignment-two-col input,
.blocked-slot-form input {
  width: 100%;
  border-radius: 0.75rem;
  border: 1px solid #d9d8ce;
  background: #ffffff;
  color: #1b1c15;
  padding: 0.75rem 0.8rem;
  font-size: 0.92rem;
}

.assignment-toggle {
  align-items: center;
  justify-content: flex-start;
}

.calendar-toolbar {
  display: grid;
  grid-template-columns: auto minmax(220px, 1fr) auto auto;
  align-items: center;
  gap: 0.8rem;
  border: 1px solid #e4e3d7;
  border-radius: 0.8rem;
  background: #fbfaee;
  padding: 0.75rem 0.85rem;
}

.schedule-status-pill {
  min-width: 0;
  border: 1px solid #d4e2de;
  border-radius: 999px;
  background: #ecf4f1;
  color: #2f6f64;
  font-size: 0.78rem;
  font-weight: 800;
  line-height: 1.25;
  padding: 0.5rem 0.7rem;
}

.schedule-status-pill.inactive {
  border-color: #e4e3d7;
  background: #fff9e8;
  color: #7a5b1e;
}

.calendar-timezone {
  border: 1px solid #d9d8ce;
  border-radius: 999px;
  background: #ffffff;
  color: #5f5f53;
  font-size: 0.78rem;
  font-weight: 800;
  padding: 0.45rem 0.7rem;
  white-space: nowrap;
}

.schedule-calendar-card {
  overflow: hidden;
  border: 1px solid #d9d8ce;
  border-radius: 0.8rem;
  background: #ffffff;
  box-shadow: 0 14px 28px rgba(27, 28, 21, 0.08);
}

.calendar-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  border-bottom: 1px solid #e4e3d7;
  background: #f5f4e8;
  color: #1b1c15;
  padding: 0.9rem 1rem;
}

.calendar-card-head div {
  display: grid;
  gap: 0.2rem;
}

.calendar-card-head strong {
  font-size: 0.95rem;
}

.calendar-card-head span {
  color: #5f5f53;
  font-size: 0.82rem;
  font-weight: 700;
}

.schedule-quick-actions,
.db-label-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.55rem;
  flex-wrap: wrap;
}

.schedule-quick-actions {
  border-bottom: 1px solid #e4e3d7;
  background: #fffef8;
  padding: 0.75rem 1rem;
}

.db-label-row {
  margin-bottom: 0.45rem;
}

.week-calendar-grid {
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
}

.week-day-card {
  display: flex;
  min-height: 112px;
  flex-direction: column;
  align-items: flex-start;
  justify-content: flex-start;
  gap: 0.36rem;
  border: 0;
  border-right: 1px solid #e4e3d7;
  background: #fbfaee;
  color: #1b1c15;
  cursor: pointer;
  padding: 0.85rem 0.75rem;
  text-align: left;
  transition: background 0.18s ease, box-shadow 0.18s ease, transform 0.18s ease;
}

.week-day-card:last-child {
  border-right: 0;
}

.week-day-card:hover {
  background: #f1f4ea;
  transform: translateY(-1px);
}

.week-day-card:focus-visible {
  outline: 2px solid #34776c;
  outline-offset: -3px;
}

.week-day-card span {
  color: #6a6a5d;
  font-size: 0.78rem;
  font-weight: 900;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.week-day-card strong {
  color: #1b1c15;
  font-size: 0.92rem;
}

.week-day-card small {
  color: #66675e;
  font-size: 0.78rem;
  line-height: 1.25;
}

.week-day-card.active {
  background: #e8f3ef;
  box-shadow: inset 0 -4px 0 #34776c;
}

.week-day-card.active span {
  color: #34776c;
}

.time-planner-card {
  display: grid;
  gap: 0.9rem;
  border: 1px solid #d9d8ce;
  border-radius: 0.8rem;
  background: #ffffff;
  padding: 1rem;
}

.time-planner-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
}

.time-planner-head div {
  display: grid;
  gap: 0.18rem;
}

.micro-label {
  color: #5f5f53;
  font-size: 0.72rem;
  font-weight: 900;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.time-planner-head strong {
  color: #1b1c15;
  font-size: 1.28rem;
  line-height: 1.2;
}

.time-planner-head > span {
  border: 1px solid #d4e2de;
  border-radius: 999px;
  background: #ecf4f1;
  color: #34776c;
  font-size: 0.8rem;
  font-weight: 800;
  padding: 0.45rem 0.7rem;
  white-space: nowrap;
}

.time-preset-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.55rem;
}

.time-preset-button {
  display: grid;
  gap: 0.18rem;
  min-height: 66px;
  border: 1px solid #e4e3d7;
  border-radius: 0.75rem;
  background: #fbfaee;
  color: #1b1c15;
  cursor: pointer;
  padding: 0.72rem;
  text-align: left;
  transition: border-color 0.18s ease, background 0.18s ease, transform 0.18s ease;
}

.time-preset-button:hover {
  background: #f1f4ea;
  transform: translateY(-1px);
}

.time-preset-button.active {
  border-color: #34776c;
  background: #e8f3ef;
  box-shadow: inset 0 0 0 1px #34776c;
}

.time-preset-button span {
  color: #5f5f53;
  font-size: 0.72rem;
  font-weight: 900;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.time-preset-button strong {
  color: #1b1c15;
  font-size: 0.86rem;
}

.time-range-control {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.75rem;
}

.time-input-card {
  border: 1px solid #e4e3d7;
  border-radius: 0.8rem;
  background: #fbfaee;
  padding: 0.75rem;
}

.time-input-card input {
  border-radius: 0.7rem !important;
  font-size: 1.05rem !important;
  font-weight: 800;
}

.capacity-planner-card {
  display: grid;
  grid-template-columns: minmax(170px, 0.5fr) minmax(0, 1fr);
  gap: 0.9rem;
  align-items: center;
  border: 1px solid #d9d8ce;
  border-radius: 0.8rem;
  background: #ffffff;
  padding: 1rem;
}

.capacity-planner-card label {
  margin: 0;
}

.capacity-planner-card input {
  font-size: 1.25rem !important;
  font-weight: 900;
}

.capacity-planner-card p {
  margin: 0;
  color: #5f5f53;
  font-size: 0.88rem;
  line-height: 1.45;
}

.blocked-slot-form {
  align-items: end;
  border: 1px solid #e4e3d7;
  border-radius: 0.8rem;
  background: #ffffff;
  padding: 0.85rem;
}

.easy-block-form {
  display: grid;
  grid-template-columns: 1.15fr 0.8fr 0.8fr minmax(180px, 1.4fr) auto;
  gap: 0.75rem;
}

.blocked-slot-form .ghost-button {
  min-height: 45px;
  justify-content: center;
}

.calendar-event-list {
  gap: 0.75rem;
}

.calendar-event-card {
  grid-template-columns: 82px minmax(0, 1fr) 42px;
  border-left: 4px solid #34776c;
  background: #ffffff;
}

.event-date-badge {
  display: grid;
  min-height: 58px;
  place-items: center;
  border: 1px solid #d4e2de;
  border-radius: 0.7rem;
  background: #ecf4f1;
  text-align: center;
}

.event-date-badge span {
  color: #34776c;
  font-size: 0.72rem;
  font-weight: 900;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.event-date-badge strong {
  color: #1b1c15;
  font-size: 0.82rem;
}

.event-main {
  display: grid;
  min-width: 0;
  gap: 0.2rem;
}

.event-main strong {
  overflow: hidden;
  color: #1b1c15;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.event-main span {
  color: #5f5f53;
  font-size: 0.84rem;
}

.assignment-chip-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 0.55rem;
}

.assignment-chip {
  display: inline-flex !important;
  flex-direction: row !important;
  align-items: center;
  gap: 0.4rem;
  border: 1px solid #e4e3d7;
  background: #fbfaee;
  border-radius: 999px;
  padding: 0.55rem 0.75rem;
}

.assignment-chip span {
  color: #1b1c15 !important;
  font-size: 0.78rem !important;
  letter-spacing: 0 !important;
  text-transform: none !important;
}

.request-types .assignment-chip {
  border-radius: 0.8rem;
}

.blocked-slot-list {
  display: grid;
  gap: 0.6rem;
}

.blocked-slot-row {
  display: grid;
  grid-template-columns: 1fr auto 42px;
  gap: 0.7rem;
  align-items: center;
  border: 1px solid #e4e3d7;
  border-radius: 0.8rem;
  background: #fbfaee;
  padding: 0.75rem;
}

.blocked-slot-row span {
  color: #5f5f53;
  font-size: 0.85rem;
}

@media (max-width: 900px) {
  .calendar-toolbar {
    grid-template-columns: 1fr;
  }

  .week-calendar-grid,
  .time-preset-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .easy-block-form,
  .assignment-two-col,
  .capacity-planner-card {
    grid-template-columns: 1fr;
  }
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
  width: min(calc(100% - 320px), 1320px);
  margin-left: 300px;
  margin-right: auto;
  padding-top: 2rem;
  gap: 2rem;
}

.floating-top-nav {
  position: fixed;
  top: 1.25rem;
  left: 0;
  z-index: 20;
  display: flex;
  align-items: flex-start;
  justify-content: flex-start;
  width: 268px;
  padding: 0 1.25rem;
  pointer-events: none;
}

.dashboard-nav {
  width: 100%;
  max-height: calc(100vh - 2.5rem);
  overflow: auto;
  padding: 1rem;
  border-radius: 1.35rem;
  border: 1px solid rgba(196, 199, 199, 0.45);
  background: rgba(255, 255, 255, 0.82);
  backdrop-filter: blur(18px);
  box-shadow: 0 16px 45px -28px rgba(27, 28, 21, 0.28);
  display: flex;
  flex-direction: column;
  gap: 1rem;
  pointer-events: auto;
}

.dashboard-brand {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  border-bottom: 1px solid #e4e3d7;
  padding-bottom: 0.4rem;
}

.brand-logo {
  display: block;
  width: 9.5rem;
  max-width: 100%;
  height: auto;
  object-fit: contain;
}

.org-avatar-initial {
  width: 1.8rem;
  height: 1.8rem;
  flex: 0 0 1.8rem;
  border-radius: 999px;
  background: #1b1c15;
  color: #fffef8;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: Manrope, sans-serif;
  font-weight: 800;
  font-size: 0.78rem;
}

.org-avatar-name {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}

.dashboard-nav-actions .org-avatar-button {
  justify-content: flex-start;
  gap: 0.55rem;
  padding: 0.55rem 0.75rem;
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
  min-width: 0;
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

.dashboard-nav-actions,
.dashboard-header-actions {
  display: flex;
  align-items: center;
  gap: 0.7rem;
}

.dashboard-nav-actions {
  align-items: stretch;
  flex-direction: column;
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

.dashboard-nav-actions .nav-icon-button,
.dashboard-nav-actions .org-avatar-button {
  width: 100%;
  border-radius: 0.85rem;
}

.theme-toggle-button {
  display: inline-flex;
  align-items: center;
  justify-content: flex-start;
  gap: 0.45rem;
  padding: 0.75rem 0.9rem;
  font-size: 0.8rem;
  font-weight: 700;
  white-space: nowrap;
}

.nav-page-button {
  display: inline-flex;
  align-items: center;
  justify-content: flex-start;
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

.nav-icon-button:disabled,
.ghost-button:disabled,
.primary-button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
  transform: none;
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
.dashboard-context-pill,
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

.dashboard-context-pill {
  border-radius: 999px;
  border: 1px solid rgba(21, 58, 27, 0.14);
  background: rgba(255, 254, 248, 0.86);
  color: #172714;
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

.member-timetable-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1.25rem;
}

.member-timetable-card {
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
  padding: 1.2rem;
}

.member-timetable-card h4 {
  margin: 0;
  font-family: Manrope, sans-serif;
  font-size: 1.05rem;
  letter-spacing: -0.01em;
}

.member-timetable-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.member-timetable-card-wide {
  grid-column: span 3;
}

.member-timetable-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.7rem;
}

.member-timetable-form .invite-field-wide {
  grid-column: span 2;
}

.member-timetable-form button[type="submit"] {
  grid-column: span 2;
  justify-self: start;
}

/* ── Polished buffer / unavailable cards ───────────────────────────── */

.member-action-card {
  padding: 1.35rem 1.4rem 1.5rem;
}

.member-action-head {
  display: flex;
  align-items: flex-start;
  gap: 0.85rem;
  margin-bottom: 1.15rem;
}

.member-action-icon {
  width: 2.4rem;
  height: 2.4rem;
  flex: 0 0 2.4rem;
  border-radius: 0.85rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(145deg, rgba(23, 63, 29, 0.16), rgba(23, 63, 29, 0.05));
  color: #173f1d;
  box-shadow: inset 0 0 0 1px rgba(23, 63, 29, 0.18);
}

.member-action-icon.unavailable-icon {
  background: linear-gradient(145deg, rgba(193, 80, 56, 0.18), rgba(193, 80, 56, 0.05));
  color: #b1452a;
  box-shadow: inset 0 0 0 1px rgba(193, 80, 56, 0.22);
}

.member-action-title h4 {
  margin: 0 0 0.15rem;
  font-family: Manrope, sans-serif;
  font-size: 1.05rem;
  letter-spacing: -0.01em;
}

.member-action-title p {
  margin: 0;
  color: var(--nokvo-muted, #6c706a);
  font-size: 0.82rem;
  line-height: 1.4;
}

.member-action-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.85rem;
}

.member-field {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.member-field-wide {
  grid-column: 1 / -1;
}

.member-field-label {
  font-family: Manrope, sans-serif;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--nokvo-muted, #6c706a);
  display: inline-flex;
  align-items: baseline;
  gap: 0.35rem;
}

.member-field-label small {
  font-size: 0.7rem;
  font-weight: 500;
  letter-spacing: 0;
  text-transform: none;
  opacity: 0.7;
}

.member-field-input {
  width: 100%;
  padding: 0.7rem 0.85rem;
  border-radius: 0.75rem;
  border: 1px solid rgba(196, 199, 199, 0.55);
  background: rgba(255, 255, 255, 0.85);
  color: var(--nokvo-ink, #1b1c15);
  font-family: inherit;
  font-size: 0.92rem;
  line-height: 1.2;
  transition: border-color 0.15s ease, box-shadow 0.15s ease, background 0.15s ease;
}

.member-field-input::placeholder {
  color: rgba(108, 112, 106, 0.7);
}

.member-field-input:hover {
  border-color: rgba(23, 63, 29, 0.35);
}

.member-field-input:focus {
  outline: none;
  border-color: rgba(23, 63, 29, 0.6);
  background: #ffffff;
  box-shadow: 0 0 0 3px rgba(23, 63, 29, 0.12);
}

.member-duration-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}

.member-duration-pill {
  flex: 1 1 auto;
  min-width: 4rem;
  padding: 0.55rem 0.8rem;
  border-radius: 999px;
  border: 1px solid rgba(196, 199, 199, 0.55);
  background: rgba(255, 255, 255, 0.7);
  color: var(--nokvo-ink, #1b1c15);
  font-family: Manrope, sans-serif;
  font-size: 0.85rem;
  font-weight: 700;
  cursor: pointer;
  transition: border-color 0.15s ease, background 0.15s ease, color 0.15s ease, transform 0.15s ease, box-shadow 0.15s ease;
}

.member-duration-pill:hover {
  border-color: rgba(23, 63, 29, 0.4);
  background: rgba(255, 255, 255, 0.95);
  transform: translateY(-1px);
}

.member-duration-pill.active {
  background: linear-gradient(180deg, #1f4a26, #173f1d);
  border-color: #173f1d;
  color: #fffef8;
  box-shadow: 0 10px 22px -16px rgba(23, 63, 29, 0.75);
}

.member-duration-pill.active:hover {
  transform: translateY(-1px);
}

.member-action-submit {
  grid-column: 1 / -1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  padding: 0.85rem 1rem;
  border-radius: 0.85rem;
  border: 1px solid #173f1d;
  background: linear-gradient(180deg, #1f4a26, #173f1d);
  color: #fffef8;
  font-family: Manrope, sans-serif;
  font-size: 0.92rem;
  font-weight: 800;
  letter-spacing: 0.01em;
  cursor: pointer;
  margin-top: 0.25rem;
  transition: transform 0.15s ease, box-shadow 0.15s ease, opacity 0.15s ease;
  box-shadow: 0 14px 30px -22px rgba(23, 63, 29, 0.7);
}

.member-action-submit:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: 0 18px 36px -22px rgba(23, 63, 29, 0.78);
}

.member-action-submit:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.member-action-submit.unavailable-submit {
  border-color: #a93a1d;
  background: linear-gradient(180deg, #c15038, #a93a1d);
  box-shadow: 0 14px 30px -22px rgba(169, 58, 29, 0.7);
}

.member-action-submit.unavailable-submit:hover:not(:disabled) {
  box-shadow: 0 18px 36px -22px rgba(169, 58, 29, 0.78);
}

.member-timetable-blocks {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.member-timetable-block {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.9rem;
  padding: 0.75rem 0.9rem;
  border: 1px solid rgba(196, 199, 199, 0.45);
  border-radius: 0.85rem;
  background: rgba(255, 255, 255, 0.6);
}

.member-timetable-block-meta {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  min-width: 0;
}

.member-timetable-block-meta strong {
  font-family: Manrope, sans-serif;
  font-weight: 700;
}

.member-timetable-block-meta span {
  color: var(--nokvo-muted, #6c706a);
  font-size: 0.82rem;
}

@media (max-width: 1080px) {
  .member-timetable-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .member-timetable-card-wide {
    grid-column: span 2;
  }
}

@media (max-width: 720px) {
  .member-timetable-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .member-timetable-card-wide {
    grid-column: span 1;
  }
  .member-timetable-form,
  .member-action-form {
    grid-template-columns: minmax(0, 1fr);
  }
  .member-timetable-form .invite-field-wide,
  .member-timetable-form button[type="submit"],
  .member-field-wide,
  .member-action-submit {
    grid-column: span 1;
  }
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

.health-tracker {
  display: grid;
  gap: 1.15rem;
  margin-top: 1.25rem;
}

.health-score-card {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  gap: 1rem;
  padding: 1rem;
  border: 1px solid rgba(196, 199, 199, 0.34);
  border-radius: 1rem;
  background: linear-gradient(135deg, rgba(249, 251, 244, 0.96), rgba(239, 246, 232, 0.82));
}

.health-score-card.health-warn {
  background: linear-gradient(135deg, rgba(255, 250, 235, 0.96), rgba(249, 241, 216, 0.84));
}

.health-score-card.health-blocked {
  background: linear-gradient(135deg, rgba(255, 245, 243, 0.96), rgba(249, 226, 222, 0.84));
}

.health-ring {
  width: 5.4rem;
  height: 5.4rem;
  display: grid;
  place-items: center;
  border-radius: 999px;
  box-shadow: inset 0 0 0 1px rgba(27, 28, 21, 0.06);
}

.health-ring > div {
  display: grid;
  place-items: center;
  width: 4.15rem;
  height: 4.15rem;
  border-radius: 999px;
  background: #fffef8;
}

.health-ring strong {
  color: #1b1c15;
  font-family: Manrope, sans-serif;
  font-size: 1.35rem;
  line-height: 1;
}

.health-ring span {
  color: #77786d;
  font-size: 0.72rem;
  font-weight: 800;
}

.health-score-card h4 {
  margin: 0.16rem 0 0;
  color: #1b1c15;
  font-family: Manrope, sans-serif;
  font-size: 1.35rem;
}

.health-score-card p {
  margin-top: 0.22rem;
}

.health-check-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.75rem;
}

.health-check {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 0.65rem;
  min-height: 4.4rem;
  padding: 0.85rem;
  border: 1px solid rgba(196, 199, 199, 0.28);
  border-radius: 0.85rem;
  background: rgba(255, 255, 255, 0.62);
}

.health-check-dot {
  width: 0.62rem;
  height: 0.62rem;
  margin-top: 0.32rem;
  border-radius: 999px;
  background: #2f6d3a;
  box-shadow: 0 0 0 4px rgba(47, 109, 58, 0.12);
}

.health-check[data-state="warn"] .health-check-dot {
  background: #b7791f;
  box-shadow: 0 0 0 4px rgba(183, 121, 31, 0.14);
}

.health-check[data-state="blocked"] .health-check-dot {
  background: #b42318;
  box-shadow: 0 0 0 4px rgba(180, 35, 24, 0.12);
}

.health-check strong {
  display: block;
  color: #20231c;
  font-family: Manrope, sans-serif;
  font-size: 0.92rem;
}

.health-check small {
  display: block;
  margin-top: 0.18rem;
  color: #62665b;
  font-size: 0.78rem;
  line-height: 1.4;
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

.link-button {
  background: transparent;
  border: 0;
  padding: 0;
  color: #2f7a4a;
  font-size: 0.78rem;
  font-weight: 600;
  cursor: pointer;
  letter-spacing: 0.01em;
}

.link-button:hover:not(:disabled) {
  text-decoration: underline;
}

.link-button:disabled {
  color: #b1b1a3;
  cursor: not-allowed;
}

.agent-tools-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  margin-bottom: 0.25rem;
}

.agent-tool-group {
  margin-top: 0.85rem;
  padding-top: 0.55rem;
  border-top: 1px dashed rgba(95, 95, 83, 0.25);
}

.agent-tool-group:first-of-type {
  border-top: 0;
  padding-top: 0;
}

.agent-tool-group-head {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  margin-bottom: 0.45rem;
}

.agent-tool-group-head strong {
  font-family: Manrope, sans-serif;
  font-size: 0.92rem;
  letter-spacing: 0.01em;
}

.agent-tool-group-head .status-chip {
  font-size: 0.72rem;
}

.agent-tool-group-head .link-button {
  margin-left: auto;
}

.custom-tab-list {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
}

.custom-tab-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.85rem;
  padding: 0.65rem 0.85rem;
  border-radius: 10px;
  border: 1px solid rgba(95, 95, 83, 0.18);
  background: rgba(255, 255, 255, 0.55);
}

.custom-tab-row strong {
  font-family: Manrope, sans-serif;
  display: block;
}

.custom-tab-row small {
  display: block;
  color: #5f5f53;
  font-size: 0.74rem;
  margin-top: 0.1rem;
}

.custom-tab-row small code {
  background: rgba(95, 95, 83, 0.1);
  padding: 0 0.3rem;
  border-radius: 3px;
}

.custom-tab-form {
  border-top: 1px dashed rgba(95, 95, 83, 0.25);
  padding-top: 0.85rem;
}

.custom-tab-form-row {
  display: flex;
  gap: 0.55rem;
  margin-bottom: 0.55rem;
}

.custom-tab-form-row .db-input {
  flex: 1;
}

.custom-tab-fields {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  margin: 0.5rem 0 0.75rem;
}

.custom-tab-field-row {
  display: grid;
  grid-template-columns: 1fr 1fr 130px auto auto;
  gap: 0.45rem;
  align-items: center;
}

.custom-tab-field-row .db-input.compact {
  padding: 0.4rem 0.55rem;
  font-size: 0.84rem;
}

.custom-tab-required-toggle {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  font-size: 0.78rem;
  color: #5f5f53;
}

.ghost-button.danger {
  color: #a13b2c;
  border-color: rgba(161, 59, 44, 0.4);
}

.outcome-wizard .mfa-head {
  margin-bottom: 1rem;
}

.outcome-list {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  margin-bottom: 0.85rem;
}

.outcome-option {
  display: block;
  padding: 0.85rem 1rem;
}

.outcome-name-row {
  margin-top: 0.75rem;
}

.sample-upload-zone {
  margin: 0.85rem 0 1.1rem;
}

.sample-upload-label {
  display: block;
  text-align: center;
  padding: 1.2rem;
  cursor: pointer;
  border-style: dashed;
}

.sample-mode-toggle {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.55rem;
  margin: 0.5rem 0 1rem;
}

.sample-mode-tab {
  display: block;
  text-align: left;
  padding: 0.7rem 0.85rem;
  border-radius: 9px;
  border: 1px solid rgba(95, 95, 83, 0.2);
  background: rgba(255, 255, 255, 0.55);
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}

.sample-mode-tab:hover:not(:disabled) {
  border-color: rgba(47, 122, 74, 0.4);
}

.sample-mode-tab.active {
  border-color: rgba(47, 122, 74, 0.7);
  background: rgba(47, 122, 74, 0.08);
}

.sample-mode-tab strong {
  display: block;
  font-family: Manrope, sans-serif;
  font-size: 0.9rem;
  margin-bottom: 0.18rem;
}

.sample-mode-tab small {
  display: block;
  color: #5f5f53;
  font-size: 0.76rem;
  line-height: 1.4;
}

.sample-mode-tab:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.sample-prompt-zone {
  margin-bottom: 1.1rem;
}

.sample-prompt-textarea {
  width: 100%;
  min-height: 150px;
  resize: vertical;
  font-family: inherit;
  line-height: 1.5;
}

.sample-prompt-meta {
  display: flex;
  justify-content: space-between;
  margin-top: 0.4rem;
  color: #5f5f53;
  font-size: 0.76rem;
}

.nav-settings-wrap {
  position: relative;
}

.nav-settings-menu {
  position: absolute;
  top: calc(100% + 0.45rem);
  right: 0;
  min-width: 280px;
  background: #ffffff;
  border: 1px solid rgba(95, 95, 83, 0.2);
  border-radius: 10px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
  padding: 0.45rem;
  z-index: 50;
}

.nav-settings-item {
  display: block;
  width: 100%;
  text-align: left;
  background: transparent;
  border: 0;
  padding: 0.6rem 0.75rem;
  border-radius: 7px;
  cursor: pointer;
}

.nav-settings-item:hover {
  background: rgba(95, 95, 83, 0.06);
}

.nav-settings-item strong {
  display: block;
  font-family: Manrope, sans-serif;
  font-size: 0.88rem;
  margin-bottom: 0.15rem;
}

.nav-settings-item small {
  display: block;
  color: #5f5f53;
  font-size: 0.76rem;
  line-height: 1.35;
}

.try-agent-banner {
  margin-bottom: 1.25rem;
}

.try-agent-card {
  display: flex;
  flex-direction: column;
  gap: 1rem;
  padding: 1.5rem;
  background: linear-gradient(135deg, rgba(47, 122, 74, 0.08), rgba(47, 122, 74, 0.02));
  border: 1px solid rgba(47, 122, 74, 0.18);
}

.try-agent-card h3 {
  margin: 0.25rem 0 0.5rem;
  font-size: 1.25rem;
}

.try-agent-card p {
  margin: 0;
  color: #5f5f53;
  line-height: 1.5;
}

.try-agent-actions {
  display: flex;
  gap: 0.6rem;
  flex-wrap: wrap;
}

.outgoing-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 0.55rem;
}

.outgoing-tabs button {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  border: 1px solid rgba(196, 199, 199, 0.6);
  background: rgba(255, 255, 255, 0.88);
  color: #1b1c15;
  border-radius: 999px;
  padding: 0.6rem 0.85rem;
  font-size: 0.8rem;
  font-weight: 800;
  cursor: pointer;
}

.outgoing-tabs button.active {
  background: #1b1c15;
  border-color: #1b1c15;
  color: #ffffff;
}

.outgoing-provider-grid .provider-option {
  text-align: left;
  cursor: pointer;
}

.outgoing-inline-editor {
  display: flex;
  gap: 0.45rem;
  margin-top: 0.55rem;
  flex-wrap: wrap;
}

.outgoing-inline-editor .db-input {
  min-width: 12rem;
}

.outgoing-lead-list {
  display: grid;
  gap: 0.7rem;
  margin-top: 1rem;
}

.outgoing-lead-row {
  width: 100%;
  cursor: pointer;
  text-align: left;
}

.outgoing-lead-row.disabled {
  opacity: 0.62;
  cursor: not-allowed;
}

.outgoing-lead-row:hover {
  border-color: rgba(27, 28, 21, 0.28);
  background: rgba(255, 255, 255, 0.92);
}

.outgoing-lead-actions {
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.mfa-pending-banner {
  display: flex;
  align-items: center;
  gap: 0.9rem;
  padding: 0.8rem 0.9rem;
  border: 1px solid rgba(75, 116, 62, 0.18);
  border-radius: 0.85rem;
  background: linear-gradient(135deg, #f8fbf2 0%, #eef6e8 100%);
  box-shadow: 0 16px 44px -34px rgba(35, 54, 28, 0.45);
}

.mfa-pending-icon {
  display: grid;
  place-items: center;
  width: 2.35rem;
  height: 2.35rem;
  flex: 0 0 auto;
  border-radius: 0.75rem;
  background: #233a1d;
  color: #f8fbf2;
}

.mfa-pending-copy {
  flex: 1;
  min-width: 0;
  display: grid;
  gap: 0.1rem;
}

.mfa-pending-copy strong {
  font-family: Manrope, sans-serif;
  color: #1d2b19;
  font-size: 0.95rem;
}

.mfa-pending-copy span {
  color: #596652;
  font-size: 0.84rem;
  line-height: 1.35;
}

.dashboard-message.warning {
  background: rgba(159, 95, 16, 0.08);
  border-color: rgba(159, 95, 16, 0.25);
  color: #6b3f0a;
}

/* Nokvo Prime aesthetic */
.org-shell {
  --nokvo-ink: #151710;
  --nokvo-muted: #67685f;
  --nokvo-soft: #f6f4ea;
  --nokvo-panel: rgba(255, 254, 248, 0.88);
  --nokvo-panel-solid: #fffef8;
  --nokvo-line: rgba(26, 43, 23, 0.13);
  --nokvo-green: #173f1d;
  --nokvo-green-2: #23572a;
  --nokvo-green-soft: #e8f1e0;
  --nokvo-warn: #b66514;
  background:
    linear-gradient(180deg, rgba(255, 253, 245, 0.98), rgba(247, 245, 235, 0.98)),
    #f8f6ec;
  color: var(--nokvo-ink);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

.ambient-orb {
  display: none;
}

.dashboard-layout {
  width: min(calc(100% - 288px), 1360px);
  margin-left: 268px;
  padding-top: 2.25rem;
}

.floating-top-nav {
  width: 240px;
  padding: 0 1.15rem;
}

.dashboard-nav {
  min-height: calc(100vh - 2.5rem);
  border-radius: 1.25rem;
  border-color: var(--nokvo-line);
  background: rgba(255, 254, 248, 0.8);
  box-shadow: 0 22px 70px -46px rgba(21, 23, 16, 0.35);
}

.dashboard-brand {
  flex-direction: column;
  align-items: center;
  padding: 0.88rem 0.72rem 0.4rem;
  border-bottom-color: var(--nokvo-line);
}

.dashboard-brand .brand-copy span {
  display: block;
  max-width: 100%;
  overflow-wrap: anywhere;
}

.brand-mark,
.organization-mark,
.compact-icon-shell,
.mfa-pending-icon {
  border-radius: 0.75rem;
  background: linear-gradient(145deg, var(--nokvo-green), #0f2c15);
  color: #fffef8;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.18), 0 12px 28px -20px rgba(23, 63, 29, 0.7);
}

.brand-copy strong {
  color: var(--nokvo-ink);
  font-size: 0.95rem;
  letter-spacing: 0.02em;
}

.brand-copy span,
.dashboard-section-head p,
.compact-card-head p,
.members-card-head p,
.organization-card p,
.provider-option small,
.health-check small,
.mfa-pending-copy span,
.kb-field > span,
.dashboard-detail-list dt,
.invite-helper,
.empty-state,
.readonly-tag {
  color: var(--nokvo-muted);
}

.dashboard-nav-actions {
  gap: 0.46rem;
}

.nav-page-button,
.theme-toggle-button,
.dashboard-nav-actions .nav-icon-button,
.dashboard-nav-actions .org-avatar-button {
  min-height: 3rem;
  border-radius: 0.85rem;
  border-color: transparent;
  background: transparent;
  color: var(--nokvo-ink);
  font-size: 0.9rem;
  font-weight: 700;
}

.nav-page-button svg,
.theme-toggle-button svg {
  color: #1f241c;
}

.nav-page-button:hover,
.theme-toggle-button:hover,
.dashboard-nav-actions .nav-icon-button:hover,
.dashboard-nav-actions .org-avatar-button:hover {
  background: rgba(23, 63, 29, 0.06);
  box-shadow: none;
}

.nav-page-button.active {
  border-color: rgba(23, 63, 29, 0.1);
  background: linear-gradient(180deg, var(--nokvo-green-2), var(--nokvo-green));
  color: #fffef8;
  box-shadow: 0 14px 26px -20px rgba(23, 63, 29, 0.8);
}

.nav-page-button.active svg {
  color: #fffef8;
}

.dashboard-nav-actions > .nav-icon-button:first-of-type {
  margin-top: 0.85rem;
  border-top: 1px solid var(--nokvo-line);
  padding-top: 0.95rem;
}

.dashboard-header {
  align-items: center;
  min-height: 6.2rem;
}

.dashboard-header h2,
.brand-block h1,
.dashboard-section-head h3,
.try-agent-card h3,
.organization-card h3,
.compact-card-head h3,
.members-card-head h3,
.health-score-card h4 {
  font-family: Georgia, "Times New Roman", serif;
  font-weight: 700;
  letter-spacing: -0.035em;
}

.dashboard-header h2 {
  font-size: clamp(3rem, 5.4vw, 4.25rem);
}

.section-kicker,
.micro-label,
.summary-pill span,
.db-label,
.kb-field > span,
.dashboard-detail-list dt,
.tab-record-meta span {
  color: #77786f;
  font-size: 0.72rem;
  letter-spacing: 0.18em;
  font-weight: 800;
}

.dashboard-header-actions {
  align-items: center;
}

.dashboard-secondary-button,
.dashboard-context-pill,
.dashboard-chip-button,
.dashboard-inline-button,
.ghost-button {
  border-color: var(--nokvo-line);
  background: rgba(255, 254, 248, 0.9);
  color: var(--nokvo-ink);
  box-shadow: 0 12px 28px -24px rgba(21, 23, 16, 0.35);
}

.primary-button,
.dashboard-primary-button {
  background: linear-gradient(180deg, var(--nokvo-green-2), var(--nokvo-green));
  color: #fffef8;
  box-shadow: 0 16px 30px -22px rgba(23, 63, 29, 0.8);
}

.dashboard-card,
.login-card,
.field-modal {
  border-radius: 1.15rem;
  border-color: var(--nokvo-line);
  background: var(--nokvo-panel);
  box-shadow: 0 22px 70px -52px rgba(21, 23, 16, 0.36);
}

.dashboard-grid {
  gap: 1.35rem;
}

.dashboard-section {
  gap: 1.25rem;
}

.dashboard-section-head {
  padding: 0.25rem 0.15rem;
}

.dashboard-section-head h3 {
  font-size: 1.85rem;
}

.mfa-pending-banner {
  min-height: 5.6rem;
  padding: 1rem 1.25rem;
  border-color: var(--nokvo-line);
  border-radius: 1.1rem;
  background: linear-gradient(90deg, rgba(255, 254, 248, 0.92), rgba(240, 247, 234, 0.92));
}

.mfa-pending-copy strong {
  color: var(--nokvo-ink);
  font-size: 1.05rem;
}

.try-agent-card {
  position: relative;
  min-height: 15rem;
  justify-content: center;
  padding: 2rem;
  border-radius: 1.2rem;
  border-color: rgba(26, 43, 23, 0.13);
  background:
    linear-gradient(110deg, rgba(255, 254, 248, 0.94) 0%, rgba(255, 254, 248, 0.94) 48%, rgba(233, 242, 224, 0.78) 100%);
}

.try-agent-card::after {
  content: "...";
  position: absolute;
  right: 15%;
  top: 50%;
  transform: translateY(-50%);
  width: 5rem;
  height: 5rem;
  display: grid;
  place-items: center;
  border-radius: 999px;
  background: linear-gradient(180deg, var(--nokvo-green-2), var(--nokvo-green));
  color: #fffef8;
  font-family: Georgia, "Times New Roman", serif;
  font-size: 2.2rem;
  line-height: 1;
  letter-spacing: 0.12em;
  box-shadow: 0 22px 50px -28px rgba(23, 63, 29, 0.8);
}

.try-agent-copy,
.try-agent-actions {
  position: relative;
  z-index: 1;
  max-width: 34rem;
}

.try-agent-card h3 {
  font-size: 1.75rem;
}

.try-agent-actions .primary-button,
.try-agent-actions .ghost-button {
  min-width: 13rem;
}

.health-score-card,
.health-check,
.provider-option,
.schema-preview,
.agent-document-row,
.tab-record-row,
.member-row,
.invite-domain-banner,
.week-day-card,
.time-planner-card,
.schedule-calendar-card,
.capacity-planner-card,
.blocked-slot-row,
.kb-result-card,
.kb-doc-card,
.outgoing-tabs button {
  border-color: var(--nokvo-line);
  background: rgba(255, 254, 248, 0.78);
}

.health-score-card {
  background: linear-gradient(135deg, rgba(255, 254, 248, 0.94), rgba(232, 241, 224, 0.78));
}

.health-ring > div {
  background: var(--nokvo-panel-solid);
}

.status-chip {
  border: 1px solid rgba(182, 101, 20, 0.16);
  background: #eeead9;
  color: #4b3a17;
  border-radius: 999px;
}

.status-chip.active {
  border-color: rgba(47, 109, 58, 0.18);
  background: #e8f1e0;
  color: #173f1d;
}

.provider-option {
  border-radius: 1rem;
}

.provider-option:hover,
.agent-document-row.toolkit-list-row:hover,
.tab-record-row:hover {
  border-color: rgba(23, 63, 29, 0.24);
  background: rgba(255, 254, 248, 0.94);
  transform: translateY(-1px);
}

.provider-option.active,
.outgoing-tabs button.active {
  border-color: var(--nokvo-green);
  background: var(--nokvo-green);
  color: #fffef8;
  box-shadow: 0 12px 24px -20px rgba(23, 63, 29, 0.78);
}

.provider-option.active .provider-name,
.provider-option.active small {
  color: #fffef8;
}

.outgoing-tabs {
  gap: 0.65rem;
}

.outgoing-tabs button {
  min-height: 2.55rem;
  padding: 0.65rem 0.95rem;
}

.db-input,
.totp-input,
.field-editor-row input[type="text"],
.field-editor-row select,
.kb-field input,
.kb-field select,
.kb-field textarea,
.kb-search-bar input,
.invite-field input,
.invite-field select,
.assignment-two-col input,
.assignment-two-col select,
.blocked-slot-form input,
.time-input-card input,
.capacity-planner-card input {
  border-color: var(--nokvo-line);
  background: rgba(255, 254, 248, 0.88);
  color: var(--nokvo-ink);
  border-radius: 0.85rem;
}

.db-input:focus,
.totp-input:focus,
.kb-field input:focus,
.kb-field select:focus,
.kb-field textarea:focus,
.kb-search-bar input:focus,
.invite-field input:focus,
.invite-field select:focus {
  border-color: rgba(23, 63, 29, 0.5);
  box-shadow: 0 0 0 4px rgba(23, 63, 29, 0.09);
}

.organization-card {
  min-height: 26rem;
}

.organization-card-head,
.compact-card-head,
.members-card-head {
  align-items: center;
}

.dashboard-detail-list dd,
.workspace-profile-grid strong,
.organization-metrics strong,
.health-check strong,
.provider-name {
  color: var(--nokvo-ink);
}

.message {
  border: 1px solid var(--nokvo-line);
}

.message.info {
  background: rgba(232, 241, 224, 0.72);
  color: #18351b;
}

.message.error {
  background: #fff2ee;
  color: #8a2a1a;
  border-color: rgba(138, 42, 26, 0.18);
}

.portal-footer {
  color: var(--nokvo-muted);
}

@media (max-width: 1080px) {
  .dashboard-layout {
    width: 100%;
    margin-left: 0;
    padding-left: 6.5rem;
  }

  .try-agent-card::after {
    display: none;
  }
}

@media (max-width: 720px) {
  .dashboard-layout {
    padding-left: 5.7rem;
  }

  .dashboard-header h2 {
    font-size: 2.6rem;
  }

  .dashboard-context-pill {
    width: 100%;
  }
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
  display: block;
  font-family: Manrope, sans-serif;
  font-size: 1.3rem;
  overflow-wrap: anywhere;
}

.invite-domain-banner small {
  display: block;
  margin-top: 0.45rem;
  color: #5f5f53;
  font-size: 0.86rem;
  line-height: 1.45;
}

.invite-form {
  display: grid;
  gap: 1.1rem;
}

.dashboard-invite-form {
  margin-top: 1.15rem;
}

.invite-field-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(220px, 0.85fr);
  gap: 1rem;
}

.invite-field-wide {
  grid-column: 1 / -1;
}

.invite-field {
  display: flex;
  flex-direction: column;
}

.invite-action-block {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.95rem;
  border: 1px solid rgba(196, 199, 199, 0.34);
  border-radius: 1rem;
  background: rgba(246, 244, 234, 0.62);
}

.invite-card,
.workspace-card {
  min-height: 100%;
}

.expanded-invite-card {
  display: grid;
  grid-template-columns: minmax(260px, 0.6fr) minmax(0, 1.4fr);
  gap: 1.4rem;
  align-items: start;
  padding: 1.65rem;
}

.expanded-invite-card .compact-card-head {
  align-items: flex-start;
}

.expanded-invite-card .invite-domain-banner {
  margin-top: 1.1rem;
}

.expanded-invite-card .dashboard-invite-form {
  margin-top: 0;
}

.invite-context-panel {
  min-height: 100%;
  padding: 1rem;
  border: 1px solid rgba(196, 199, 199, 0.32);
  border-radius: 1rem;
  background: rgba(255, 254, 248, 0.72);
}

.invite-form input,
.invite-form select {
  width: 100%;
  min-height: 3.25rem;
  border-radius: 0.95rem;
  border: 1px solid #d9d8ce;
  background: rgba(255, 255, 255, 0.8);
  color: #1b1c15;
  padding: 1rem 1.05rem;
  font-size: 1rem;
}

.invite-form button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.45rem;
  min-width: 10.5rem;
  min-height: 3rem;
  border-radius: 0.95rem;
  border: none;
  background: #1d1c0f;
  color: #ffffff;
  padding: 0.95rem 1.2rem;
  font-weight: 800;
}

.invite-form button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.invite-helper {
  margin: 0;
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

.team-card-grid {
  display: grid;
  gap: 0.9rem;
  margin-top: 1.25rem;
}

.team-member-card {
  display: grid;
  gap: 0.9rem;
  padding: 1rem;
  border: 1px solid var(--nokvo-line);
  border-radius: 1rem;
  background: rgba(255, 254, 248, 0.82);
}

.team-member-main {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 0.85rem;
}

.team-avatar {
  width: 2.8rem;
  height: 2.8rem;
  display: grid;
  place-items: center;
  border-radius: 0.85rem;
  background: linear-gradient(145deg, #e9f1df, #f7f4e9);
  color: #173f1d;
  font-family: Manrope, sans-serif;
  font-weight: 900;
}

.team-identity {
  min-width: 0;
  display: grid;
  gap: 0.15rem;
}

.team-identity strong {
  color: var(--nokvo-ink);
  font-family: Manrope, sans-serif;
  font-size: 1rem;
}

.team-identity small {
  color: var(--nokvo-muted);
  overflow-wrap: anywhere;
}

.team-badges {
  display: inline-flex;
  gap: 0.45rem;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.team-badges span,
.team-load-pill {
  border: 1px solid var(--nokvo-line);
  border-radius: 999px;
  background: rgba(246, 244, 234, 0.86);
  color: #3f443a;
  padding: 0.42rem 0.62rem;
  font-size: 0.72rem;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.team-badges span.active {
  background: #e8f1e0;
  color: #173f1d;
}

.team-assignment-panel {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 0.85rem;
  padding: 0.85rem;
  border-radius: 0.85rem;
  background: rgba(246, 244, 234, 0.68);
}

.team-assignment-panel p {
  margin: 0.18rem 0 0;
  color: #3f443a;
  line-height: 1.5;
}

.team-load-pill {
  display: grid;
  place-items: center;
  min-width: 5.4rem;
  border-radius: 0.8rem;
  text-align: center;
  text-transform: none;
  letter-spacing: 0;
}

.team-load-pill span {
  color: var(--nokvo-muted);
  font-size: 0.68rem;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.team-load-pill strong {
  color: var(--nokvo-ink);
  font-size: 1.2rem;
}

.timetable-modal {
  width: min(980px, 100%);
}

.timetable-summary-row {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0.75rem;
  margin-top: 1rem;
}

.timetable-summary-row > div {
  padding: 0.85rem;
  border: 1px solid var(--nokvo-line);
  border-radius: 0.85rem;
  background: rgba(246, 244, 234, 0.72);
}

.timetable-summary-row span {
  display: block;
  color: var(--nokvo-muted);
  font-size: 0.7rem;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.timetable-summary-row strong {
  display: block;
  margin-top: 0.18rem;
  color: var(--nokvo-ink);
  font-family: Manrope, sans-serif;
}

.timetable-calendar-shell {
  display: grid;
  grid-template-columns: minmax(320px, 0.9fr) minmax(0, 1.1fr);
  gap: 1rem;
  align-items: start;
  margin-top: 1rem;
}

.timetable-calendar-card,
.timetable-selected-day {
  border: 1px solid var(--nokvo-line);
  border-radius: 0.9rem;
  background: rgba(255, 254, 248, 0.9);
}

.timetable-calendar-card {
  overflow: hidden;
}

.timetable-selected-day {
  display: grid;
  gap: 0.85rem;
  padding: 0.9rem;
}

.timetable-calendar-head {
  display: grid;
  grid-template-columns: 2.4rem minmax(0, 1fr) 2.4rem;
  align-items: center;
  gap: 0.6rem;
  border-bottom: 1px solid var(--nokvo-line);
  background: rgba(246, 244, 234, 0.72);
  padding: 0.75rem;
}

.timetable-calendar-head strong {
  color: var(--nokvo-ink);
  font-family: Manrope, sans-serif;
  text-align: center;
}

.ghost-button.icon-only {
  width: 2.35rem;
  height: 2.35rem;
  justify-content: center;
  padding: 0;
}

.timetable-weekdays,
.timetable-month-grid {
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
}

.timetable-weekdays {
  border-bottom: 1px solid var(--nokvo-line);
  background: rgba(255, 254, 248, 0.78);
}

.timetable-weekdays span {
  padding: 0.58rem 0.25rem;
  color: var(--nokvo-muted);
  font-size: 0.68rem;
  font-weight: 900;
  letter-spacing: 0.06em;
  text-align: center;
  text-transform: uppercase;
}

.timetable-date-cell {
  display: grid;
  align-content: start;
  gap: 0.25rem;
  min-height: 4.7rem;
  border: 0;
  border-right: 1px solid var(--nokvo-line);
  border-bottom: 1px solid var(--nokvo-line);
  background: rgba(255, 254, 248, 0.76);
  color: var(--nokvo-ink);
  cursor: pointer;
  padding: 0.58rem;
  text-align: left;
  transition: background 0.18s ease, box-shadow 0.18s ease, transform 0.18s ease;
}

.timetable-date-cell:nth-child(7n) {
  border-right: 0;
}

.timetable-date-cell:nth-last-child(-n + 7) {
  border-bottom: 0;
}

.timetable-date-cell:hover {
  background: rgba(241, 244, 234, 0.96);
}

.timetable-date-cell span {
  display: grid;
  place-items: center;
  width: 1.72rem;
  height: 1.72rem;
  border-radius: 999px;
  font-family: Manrope, sans-serif;
  font-size: 0.86rem;
  font-weight: 900;
}

.timetable-date-cell small {
  color: var(--nokvo-muted);
  font-size: 0.68rem;
  font-weight: 800;
  line-height: 1.25;
}

.timetable-date-cell.muted {
  background: rgba(246, 244, 234, 0.44);
  color: rgba(21, 23, 16, 0.38);
}

.timetable-date-cell.today span {
  background: rgba(23, 63, 29, 0.08);
  color: var(--nokvo-green);
}

.timetable-date-cell.busy {
  background: rgba(232, 243, 239, 0.72);
}

.timetable-date-cell.active {
  background: var(--nokvo-green);
  color: #fffef8;
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.16);
}

.timetable-date-cell.active span {
  background: rgba(255, 254, 248, 0.18);
  color: #fffef8;
}

.timetable-date-cell.active small {
  color: rgba(255, 254, 248, 0.88);
}

.timetable-day-list {
  display: grid;
  gap: 0.65rem;
}

.timetable-day-list .timetable-event {
  border-left: 4px solid var(--nokvo-green);
}

.timetable-day-list .timetable-event.blocked {
  border-left-color: var(--nokvo-warn);
}

.timetable-day-list .timetable-event::before {
  display: none;
}

.timetable-days {
  display: grid;
  gap: 1rem;
  margin-top: 1rem;
}

.timetable-day {
  display: grid;
  grid-template-columns: 8rem minmax(0, 1fr);
  gap: 1rem;
}

.timetable-day-label {
  color: var(--nokvo-ink);
  font-family: Manrope, sans-serif;
  font-weight: 900;
  padding-top: 0.35rem;
}

.timetable-track {
  position: relative;
  display: grid;
  gap: 0.65rem;
  padding-left: 1rem;
  border-left: 1px solid rgba(23, 63, 29, 0.18);
}

.timetable-event {
  position: relative;
  display: grid;
  grid-template-columns: 6rem minmax(0, 1fr);
  gap: 0.8rem;
  padding: 0.8rem;
  border: 1px solid var(--nokvo-line);
  border-radius: 0.9rem;
  background: rgba(255, 254, 248, 0.9);
}

.timetable-event::before {
  content: "";
  position: absolute;
  left: calc(-1rem - 5px);
  top: 1.05rem;
  width: 9px;
  height: 9px;
  border-radius: 999px;
  background: var(--nokvo-green);
}

.timetable-event.blocked::before {
  background: var(--nokvo-warn);
}

.timetable-event.blocked {
  background: rgba(255, 248, 235, 0.86);
}

.timetable-time {
  display: grid;
  align-content: start;
  gap: 0.15rem;
}

.timetable-time strong {
  color: var(--nokvo-ink);
  font-family: Manrope, sans-serif;
}

.timetable-time span,
.timetable-event-main span,
.timetable-event-main small {
  color: var(--nokvo-muted);
}

.timetable-event-main {
  display: flex;
  justify-content: space-between;
  gap: 0.85rem;
  min-width: 0;
}

.timetable-event-main strong {
  display: block;
  color: var(--nokvo-ink);
  font-family: Manrope, sans-serif;
}

.timetable-event-main small {
  flex: 0 0 auto;
  align-self: start;
  border: 1px solid var(--nokvo-line);
  border-radius: 999px;
  padding: 0.28rem 0.5rem;
  font-size: 0.72rem;
  font-weight: 800;
}

.timetable-queue {
  display: grid;
  gap: 0.85rem;
  margin-top: 1.15rem;
  padding-top: 1rem;
  border-top: 1px solid var(--nokvo-line);
}

.timetable-queue-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 1rem;
}

.timetable-queue-head h4 {
  margin: 0.12rem 0 0;
  color: var(--nokvo-ink);
  font-family: Manrope, sans-serif;
  font-size: 1rem;
  letter-spacing: 0;
}

.timetable-ticket-list {
  display: grid;
  gap: 0.7rem;
}

.timetable-ticket-card {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(0, 1.85fr);
  gap: 1rem;
  align-items: center;
  padding: 0.9rem;
  border: 1px solid var(--nokvo-line);
  border-radius: 0.9rem;
  background: rgba(255, 254, 248, 0.92);
}

.timetable-ticket-primary {
  display: grid;
  gap: 0.24rem;
  min-width: 0;
}

.timetable-ticket-primary strong {
  min-width: 0;
  color: var(--nokvo-ink);
  font-family: Manrope, sans-serif;
  overflow-wrap: anywhere;
}

.timetable-ticket-primary small {
  color: var(--nokvo-muted);
  line-height: 1.45;
}

.timetable-type-pill {
  width: fit-content;
  border: 1px solid rgba(52, 119, 108, 0.22);
  border-radius: 999px;
  background: rgba(232, 243, 239, 0.84);
  color: var(--nokvo-green);
  font-size: 0.68rem;
  font-weight: 900;
  letter-spacing: 0.06em;
  padding: 0.24rem 0.5rem;
  text-transform: uppercase;
}

.timetable-ticket-meta {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.7rem;
  min-width: 0;
}

.timetable-ticket-meta div {
  min-width: 0;
}

.timetable-ticket-meta span {
  display: block;
  margin-bottom: 0.18rem;
  color: var(--nokvo-muted);
  font-size: 0.68rem;
  font-weight: 900;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.timetable-ticket-meta strong {
  display: block;
  min-width: 0;
  color: var(--nokvo-ink);
  font-family: Manrope, sans-serif;
  font-size: 0.88rem;
  overflow-wrap: anywhere;
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

.tab-record-list {
  display: grid;
  gap: 0.8rem;
  margin-top: 1rem;
}

.tab-record-row {
  display: grid;
  grid-template-columns: minmax(0, 1.2fr) minmax(0, 2fr);
  gap: 1rem;
  align-items: center;
  padding: 0.95rem 0;
  border-top: 1px solid #ecebdd;
}

.tab-record-row:first-child {
  border-top: none;
}

.tab-record-primary {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.tab-record-primary strong,
.tab-record-meta strong {
  min-width: 0;
  overflow-wrap: anywhere;
}

.tab-record-primary small,
.tab-record-meta span {
  color: #5f5f53;
}

.tab-record-primary small {
  line-height: 1.45;
}

.record-call-link {
  display: inline-flex;
  width: fit-content;
  align-items: center;
  gap: 0.35rem;
  margin-top: 0.28rem;
  border: 1px solid rgba(52, 119, 108, 0.22);
  border-radius: 999px;
  background: rgba(232, 243, 239, 0.72);
  color: var(--nokvo-green);
  font-size: 0.78rem;
  font-weight: 900;
  padding: 0.32rem 0.56rem;
  text-decoration: none;
}

.record-call-link:hover {
  background: rgba(232, 243, 239, 0.96);
  transform: translateY(-1px);
}

.timetable-call-link {
  margin-top: 0.42rem;
}

.tab-record-meta {
  min-width: 0;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.8rem;
}

.tab-record-meta div {
  min-width: 0;
}

.tab-record-meta span {
  display: block;
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  margin-bottom: 0.18rem;
}

.tab-record-meta strong {
  display: block;
  font-size: 0.9rem;
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

.org-shell.dark .brand-logo {
  filter: invert(1);
}

.org-shell.dark .db-input,
.org-shell.dark .totp-input,
.org-shell.dark .invite-form input,
.org-shell.dark .invite-form select {
  background: rgba(17, 19, 15, 0.92);
  color: #f2f1e5;
  border-color: rgba(102, 108, 92, 0.4);
}

.org-shell.dark .google-fallback-button {
  background: rgba(17, 19, 15, 0.92);
  color: #f2f1e5;
  border-color: rgba(102, 108, 92, 0.4);
}

.org-shell.dark .google-mark {
  background: #ffffff;
  border-color: #ffffff;
}

.org-shell.dark .dashboard-brand {
  border-color: rgba(102, 108, 92, 0.45);
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
.org-shell.dark .usage-track,
.org-shell.dark .schema-preview,
.org-shell.dark .schema-preview-grid span,
.org-shell.dark .schema-field-row,
.org-shell.dark .field-modal,
.org-shell.dark .field-editor-row,
.org-shell.dark .assignment-chip,
.org-shell.dark .blocked-slot-row {
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

.org-shell.dark .health-score-card,
.org-shell.dark .health-check {
  background: rgba(30, 34, 28, 0.86);
  border-color: rgba(102, 108, 92, 0.38);
}

.org-shell.dark .health-ring > div {
  background: #171a15;
}

.org-shell.dark .health-ring strong,
.org-shell.dark .health-score-card h4,
.org-shell.dark .health-check strong {
  color: #f6f5ea;
}

.org-shell.dark .health-ring span,
.org-shell.dark .health-check small {
  color: #b8b7ab;
}

.org-shell.dark .schema-preview strong,
.org-shell.dark .schema-field-row strong,
.org-shell.dark .tab-record-primary strong,
.org-shell.dark .tab-record-meta strong {
  color: #f6f5ea;
}

.org-shell.dark .schema-field-row span,
.org-shell.dark .form-hint,
.org-shell.dark .field-editor-row label span,
.org-shell.dark .assignment-form-grid label span,
.org-shell.dark .assignment-two-col label span,
.org-shell.dark .blocked-slot-row span,
.org-shell.dark .tab-record-primary small,
.org-shell.dark .tab-record-meta span {
  color: #b8b7ab;
}

.org-shell.dark .field-editor-row input[type="text"],
.org-shell.dark .field-editor-row select,
.org-shell.dark .assignment-form-grid input[type="text"],
.org-shell.dark .assignment-form-grid input[type="number"],
.org-shell.dark .assignment-form-grid input[type="time"],
.org-shell.dark .assignment-two-col input,
.org-shell.dark .blocked-slot-form input {
  background: rgba(17, 19, 15, 0.92);
  color: #f2f1e5;
  border-color: rgba(102, 108, 92, 0.4);
}

.org-shell.dark .calendar-toolbar,
.org-shell.dark .schedule-calendar-card,
.org-shell.dark .time-planner-card,
.org-shell.dark .time-input-card,
.org-shell.dark .capacity-planner-card,
.org-shell.dark .blocked-slot-form,
.org-shell.dark .calendar-event-card {
  background: rgba(33, 37, 30, 0.92);
  border-color: rgba(102, 108, 92, 0.38);
}

.org-shell.dark .calendar-timezone,
.org-shell.dark .calendar-card-head,
.org-shell.dark .event-date-badge,
.org-shell.dark .time-preset-button {
  background: rgba(17, 19, 15, 0.92);
  border-color: rgba(102, 108, 92, 0.4);
}

.org-shell.dark .calendar-card-head,
.org-shell.dark .calendar-card-head strong,
.org-shell.dark .week-day-card strong,
.org-shell.dark .time-planner-head strong,
.org-shell.dark .time-preset-button strong,
.org-shell.dark .event-date-badge strong,
.org-shell.dark .event-main strong {
  color: #f6f5ea;
}

.org-shell.dark .calendar-card-head span,
.org-shell.dark .calendar-timezone,
.org-shell.dark .week-day-card span,
.org-shell.dark .week-day-card small,
.org-shell.dark .micro-label,
.org-shell.dark .time-preset-button span,
.org-shell.dark .capacity-planner-card p,
.org-shell.dark .event-main span {
  color: #b8b7ab;
}

.org-shell.dark .week-day-card {
  background: rgba(17, 19, 15, 0.78);
  border-color: rgba(102, 108, 92, 0.35);
}

.org-shell.dark .week-day-card:hover {
  background: rgba(42, 47, 38, 0.96);
}

.org-shell.dark .week-day-card.active {
  background: rgba(52, 119, 108, 0.22);
  box-shadow: inset 0 -4px 0 #86c5b6;
}

.org-shell.dark .time-preset-button:hover {
  background: rgba(42, 47, 38, 0.96);
}

.org-shell.dark .time-preset-button.active {
  border-color: #86c5b6;
  background: rgba(52, 119, 108, 0.22);
  box-shadow: inset 0 0 0 1px #86c5b6;
}

.org-shell.dark .time-planner-head > span {
  background: rgba(52, 119, 108, 0.22);
  border-color: rgba(134, 197, 182, 0.6);
  color: #9bd8ca;
}

.org-shell.dark .week-day-card.active span,
.org-shell.dark .event-date-badge span {
  color: #9bd8ca;
}

.org-shell.dark .assignment-chip span {
  color: #f2f1e5 !important;
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

  .expanded-invite-card {
    grid-template-columns: 1fr;
  }

  .invite-field-grid {
    grid-template-columns: 1fr;
  }

  .invite-action-block {
    align-items: stretch;
    flex-direction: column;
  }

  .timetable-calendar-shell {
    grid-template-columns: 1fr;
  }

  .overview-grid .organization-card,
  .overview-grid .access-card,
  .control-grid .invite-card,
  .control-grid .workspace-card {
    grid-column: auto;
  }

  .invite-form,
  .member-row,
  .team-member-main,
  .team-assignment-panel,
  .timetable-summary-row,
  .timetable-day,
  .timetable-event,
  .timetable-ticket-card,
  .tab-record-row {
    grid-template-columns: 1fr;
  }

  .provider-grid,
  .provider-grid-dual,
  .business-type-grid,
  .schema-preview-grid,
  .schema-field-grid,
  .field-editor-row,
  .assignment-two-col,
  .blocked-slot-row {
    grid-template-columns: 1fr 1fr;
  }

  .agent-page-grid,
  .agent-console-grid {
    grid-template-columns: 1fr;
  }

  .member-head {
    display: none;
  }

  .week-calendar-grid {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }

  .week-day-card:nth-child(4n) {
    border-right: 0;
  }

  .calendar-event-card {
    grid-template-columns: 82px minmax(0, 1fr) 42px;
  }

  .time-preset-row,
  .tab-record-meta,
  .timetable-ticket-meta,
  .capacity-planner-card,
  .easy-block-form {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .floating-top-nav {
    width: 5.4rem;
    padding: 0.75rem;
  }

  .dashboard-layout {
    width: 100%;
    margin-left: 0;
    padding-left: 6.2rem;
    padding-top: 1.2rem;
  }

  .dashboard-nav {
    max-height: calc(100vh - 1.5rem);
    padding: 0.7rem;
  }

  .dashboard-brand {
    justify-content: center;
    align-items: center;
    padding-bottom: 0.7rem;
  }

  .brand-logo {
    width: 2.7rem;
  }

  .brand-copy,
  .nav-page-button span,
  .theme-toggle-button span {
    display: none;
  }

  .dashboard-nav-actions {
    align-items: center;
  }

  .nav-page-button,
  .theme-toggle-button,
  .dashboard-nav-actions .nav-icon-button,
  .dashboard-nav-actions .org-avatar-button {
    width: 2.8rem;
    height: 2.8rem;
    justify-content: center;
    padding: 0;
    border-radius: 0.9rem;
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

  .login-layout,
  .workspace-layout {
    padding: 1.2rem 1rem 2rem;
  }

  .dashboard-layout {
    padding-left: 5.7rem;
    padding-top: 1rem;
  }

  .login-card,
  .dashboard-card {
    padding: 1.2rem;
  }

  .dashboard-header-actions,
  .dashboard-nav-actions,
  .members-card-head {
    width: 100%;
    justify-content: flex-start;
    flex-wrap: wrap;
  }

  .dashboard-section-head p {
    text-align: left;
  }

  .theme-toggle-button {
    width: 2.8rem;
  }

  .dashboard-nav {
    padding: 0.65rem;
  }

  .organization-metrics,
  .health-check-grid,
  .workspace-profile-grid,
  .business-type-grid,
  .schema-preview-grid,
  .schema-field-grid,
  .field-editor-row,
  .assignment-two-col,
  .tab-record-meta,
  .timetable-ticket-meta,
  .blocked-slot-row {
    grid-template-columns: 1fr;
  }

  .field-modal-shell {
    padding: 0.8rem;
  }

  .field-modal-actions {
    flex-direction: column;
  }

  .calendar-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .week-calendar-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .week-day-card:nth-child(2n) {
    border-right: 0;
  }

  .time-planner-head {
    flex-direction: column;
  }

  .time-planner-head > span {
    white-space: normal;
  }

  .time-preset-row,
  .time-range-control,
  .capacity-planner-card,
  .easy-block-form {
    grid-template-columns: 1fr;
  }

  .calendar-event-card {
    grid-template-columns: 1fr;
  }

  .event-date-badge {
    min-height: auto;
    justify-items: start;
    grid-template-columns: auto auto;
    gap: 0.55rem;
    padding: 0.65rem;
    text-align: left;
  }
}

/* ───────────────── Agent Studio (voice pipeline) ───────────────── */
.agent-studio-section {
  display: flex;
  flex-direction: column;
  gap: 1.4rem;
}

.agent-hero {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, 1.3fr);
  gap: 1.5rem;
  padding: 1.6rem 1.8rem;
  border-radius: 1.3rem;
  border: 1px solid rgba(196, 199, 199, 0.5);
  background:
    radial-gradient(120% 120% at 0% 0%, rgba(255, 255, 255, 0.9), rgba(245, 244, 232, 0.6) 55%, rgba(229, 226, 225, 0.4) 100%);
}

.agent-hero-copy h3 {
  font-family: Manrope, sans-serif;
  font-size: 1.55rem;
  line-height: 1.15;
  margin-top: 0.3rem;
}

.agent-hero-copy p {
  margin-top: 0.65rem;
  color: #4a4a3e;
  font-size: 0.92rem;
  line-height: 1.65;
}

.agent-pipeline-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0.6rem;
}

.pipeline-chip {
  display: flex;
  gap: 0.6rem;
  align-items: flex-start;
  padding: 0.7rem 0.8rem;
  border-radius: 0.85rem;
  background: rgba(255, 255, 255, 0.85);
  border: 1px solid rgba(196, 199, 199, 0.5);
}

.pipeline-chip > svg {
  margin-top: 0.18rem;
  color: #5f5f53;
  flex-shrink: 0;
}

.pipeline-chip > div {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.pipeline-chip span {
  font-size: 0.65rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #5f5f53;
}

.pipeline-chip strong {
  font-family: Manrope, sans-serif;
  font-size: 0.85rem;
  line-height: 1.2;
  margin-top: 0.1rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pipeline-chip small {
  font-size: 0.7rem;
  color: #5f5f53;
  margin-top: 0.18rem;
}

.chip-status-configured { color: #047857; }
.chip-status-missing_api_key,
.chip-status-unknown { color: #b45309; }

.voice-tester-card,
.phone-link-card,
.campaign-card {
  margin-bottom: 1.3rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.voice-status-badge {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.35rem 0.7rem;
  border-radius: 999px;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  border: 1px solid rgba(196, 199, 199, 0.6);
  background: rgba(255, 255, 255, 0.7);
  color: #5f5f53;
}

.voice-status-dot {
  width: 0.5rem;
  height: 0.5rem;
  border-radius: 999px;
  background: currentColor;
  opacity: 0.6;
}

.voice-status-idle { color: #5f5f53; }
.voice-status-connecting { color: #b45309; }
.voice-status-listening { color: #047857; background: rgba(5, 150, 105, 0.08); border-color: rgba(5, 150, 105, 0.3); }
.voice-status-thinking { color: #1d4ed8; background: rgba(59, 130, 246, 0.08); border-color: rgba(59, 130, 246, 0.3); }
.voice-status-speaking { color: #6d28d9; background: rgba(124, 58, 237, 0.08); border-color: rgba(124, 58, 237, 0.3); }
.voice-status-error { color: #b91c1c; background: rgba(220, 38, 38, 0.08); border-color: rgba(220, 38, 38, 0.35); }

.voice-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.6rem;
}

.voice-controls select {
  padding: 0.55rem 0.7rem;
  border-radius: 0.7rem;
  border: 1px solid rgba(196, 199, 199, 0.6);
  background: rgba(255, 255, 255, 0.85);
  font-size: 0.88rem;
}

.voice-text-input {
  flex: 1;
  min-width: 12rem;
  padding: 0.55rem 0.8rem;
  border-radius: 0.7rem;
  border: 1px solid rgba(196, 199, 199, 0.6);
  background: rgba(255, 255, 255, 0.85);
  font-size: 0.9rem;
}

.voice-live-row {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  padding: 0.65rem 0.85rem;
  border-radius: 0.8rem;
  background: rgba(59, 130, 246, 0.08);
  color: #1d4ed8;
}

.voice-live-row em {
  flex: 1;
  font-style: italic;
}

.voice-latency-row {
  display: flex;
  flex-wrap: wrap;
  gap: 0.9rem;
  font-size: 0.78rem;
  color: #5f5f53;
}

.voice-latency-row strong { color: #1b1c15; font-variant-numeric: tabular-nums; }
.voice-latency-row code {
  background: rgba(15, 23, 42, 0.06);
  padding: 0.1rem 0.35rem;
  border-radius: 0.35rem;
  font-size: 0.75rem;
}

.voice-empty { padding: 1.8rem 1.2rem; }

.voice-transcript {
  display: grid;
  gap: 0.7rem;
  max-height: 26rem;
  overflow-y: auto;
  padding-right: 0.4rem;
}

.voice-turn {
  display: grid;
  gap: 0.35rem;
  padding: 0.85rem 1rem;
  border-radius: 1rem;
  border: 1px solid rgba(196, 199, 199, 0.5);
  background: rgba(255, 255, 255, 0.7);
}

.voice-turn-user,
.voice-turn-agent {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 0.55rem;
  align-items: start;
}

.voice-turn-user p,
.voice-turn-agent p {
  margin: 0;
  color: #1b1c15;
  font-size: 0.9rem;
  line-height: 1.55;
}

.voice-turn-meta {
  grid-column: 2 / -1;
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  font-size: 0.72rem;
  color: #5f5f53;
}

.phone-link-urls {
  display: grid;
  gap: 0.5rem;
  padding-top: 0.5rem;
  border-top: 1px solid rgba(196, 199, 199, 0.5);
}

.phone-link-url-row {
  display: grid;
  gap: 0.2rem;
}

.phone-link-url-row span {
  font-size: 0.7rem;
  font-weight: 700;
  letter-spacing: 0.07em;
  text-transform: uppercase;
  color: #5f5f53;
}

.phone-link-url-row code {
  background: rgba(15, 23, 42, 0.06);
  padding: 0.5rem 0.7rem;
  border-radius: 0.55rem;
  font-size: 0.78rem;
  word-break: break-all;
}

.campaign-create {
  padding-bottom: 0.5rem;
  border-bottom: 1px solid rgba(196, 199, 199, 0.5);
  margin-bottom: 0.5rem;
}

@media (max-width: 980px) {
  .agent-hero {
    grid-template-columns: 1fr;
  }
  .agent-pipeline-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 640px) {
  .agent-pipeline-grid {
    grid-template-columns: 1fr;
  }
}

/* ───────────────── Knowledge Base ───────────────── */
.kb-section {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}

.kb-hero {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr);
  gap: 1.75rem;
  padding: 1.85rem 2rem;
  border-radius: 1.4rem;
  border: 1px solid rgba(196, 199, 199, 0.45);
  background:
    radial-gradient(120% 120% at 0% 0%, rgba(255, 255, 255, 0.85), rgba(245, 244, 232, 0.6) 55%, rgba(229, 226, 225, 0.4) 100%);
  box-shadow: 0 22px 56px -38px rgba(27, 28, 21, 0.28);
  overflow: hidden;
}

.kb-hero::after {
  content: "";
  position: absolute;
  inset: auto -6rem -6rem auto;
  width: 18rem;
  height: 18rem;
  border-radius: 999px;
  background: radial-gradient(circle, rgba(59, 130, 246, 0.18), rgba(59, 130, 246, 0) 70%);
  pointer-events: none;
}

.kb-hero-copy h3 {
  font-family: Manrope, sans-serif;
  font-size: 1.7rem;
  line-height: 1.15;
  margin-top: 0.4rem;
}

.kb-hero-copy p {
  margin-top: 0.7rem;
  max-width: 36rem;
  color: #4a4a3e;
  font-size: 0.95rem;
  line-height: 1.7;
}

.kb-hero-pill-row {
  margin-top: 1.1rem;
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.kb-pill {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.35rem 0.7rem;
  border-radius: 999px;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  background: #efeee3;
  color: #2a2a1f;
  border: 1px solid rgba(196, 199, 199, 0.55);
}

.kb-pill-soft {
  background: rgba(255, 255, 255, 0.7);
  color: #3a3a2c;
}

.kb-pill-dot {
  width: 0.45rem;
  height: 0.45rem;
  border-radius: 999px;
  background: #059669;
  box-shadow: 0 0 0 4px rgba(5, 150, 105, 0.18);
}

.kb-pill-type {
  background: rgba(15, 23, 42, 0.06);
  color: #1b1c15;
}

.kb-pill-status-approved {
  background: rgba(5, 150, 105, 0.12);
  color: #047857;
  border-color: rgba(5, 150, 105, 0.3);
}

.kb-pill-status-pending {
  background: rgba(217, 119, 6, 0.12);
  color: #b45309;
  border-color: rgba(217, 119, 6, 0.3);
}

.kb-pill-status-rejected {
  background: rgba(220, 38, 38, 0.12);
  color: #b91c1c;
  border-color: rgba(220, 38, 38, 0.3);
}

.kb-pill-score {
  background: rgba(59, 130, 246, 0.12);
  color: #1d4ed8;
  border-color: rgba(59, 130, 246, 0.3);
  font-variant-numeric: tabular-nums;
}

.kb-hero-stats {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0.75rem;
  align-self: stretch;
  position: relative;
}

.kb-stat-card {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  padding: 1rem 1.1rem;
  border-radius: 1rem;
  border: 1px solid rgba(196, 199, 199, 0.55);
  background: rgba(255, 255, 255, 0.78);
  min-height: 7.5rem;
}

.kb-stat-label {
  font-size: 0.72rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.07em;
  color: #5f5f53;
}

.kb-stat-value {
  font-family: Manrope, sans-serif;
  font-size: 1.85rem;
  line-height: 1;
  margin-top: 0.4rem;
  color: #1b1c15;
  font-variant-numeric: tabular-nums;
}

.kb-stat-meta {
  margin-top: 0.55rem;
  color: #5f5f53;
  font-size: 0.74rem;
}

.kb-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 1.25rem;
}

.kb-card {
  display: flex;
  flex-direction: column;
  gap: 1.1rem;
}

.kb-card-head {
  display: flex;
  gap: 0.85rem;
  align-items: flex-start;
}

.kb-card-icon {
  flex: 0 0 auto;
  width: 2.4rem;
  height: 2.4rem;
  border-radius: 0.8rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: #efeee3;
  border: 1px solid rgba(196, 199, 199, 0.55);
  color: #1b1c15;
}

.kb-card-icon-primary {
  background: #1d1c0f;
  color: #fffef8;
  border-color: #1d1c0f;
}

.kb-card-head h4 {
  font-family: Manrope, sans-serif;
  font-size: 1.05rem;
  line-height: 1.2;
  margin: 0;
}

.kb-card-head p {
  margin: 0.18rem 0 0;
  color: #5f5f53;
  font-size: 0.85rem;
  line-height: 1.55;
}

.kb-dropzone {
  position: relative;
  display: block;
  padding: 1.5rem;
  border-radius: 1rem;
  border: 1.5px dashed rgba(27, 28, 21, 0.25);
  background: rgba(239, 238, 227, 0.4);
  cursor: pointer;
  transition: border-color 0.2s ease, background 0.2s ease, transform 0.15s ease;
}

.kb-dropzone:hover {
  border-color: rgba(27, 28, 21, 0.55);
  background: rgba(239, 238, 227, 0.7);
}

.kb-dropzone.has-file {
  border-style: solid;
  border-color: rgba(27, 28, 21, 0.4);
  background: rgba(255, 255, 255, 0.9);
}

.kb-dropzone.is-busy {
  pointer-events: none;
  opacity: 0.7;
}

.kb-dropzone-input {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
}

.kb-dropzone-empty,
.kb-dropzone-filled {
  display: flex;
  align-items: center;
  gap: 0.9rem;
  text-align: left;
}

.kb-bulk-queue {
  list-style: none;
  margin: 0.85rem 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
  max-height: 240px;
  overflow-y: auto;
}
.kb-bulk-row {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  padding: 0.55rem 0.75rem;
  border-radius: 0.5rem;
  background: #fffef6;
  border: 1px solid #ece9d7;
  font-size: 0.85rem;
}
.kb-bulk-row-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.85rem;
  width: 100%;
}
.kb-bulk-error-detail {
  font-size: 0.78rem;
  color: #8a2a1a;
  background: #fff5f3;
  padding: 0.35rem 0.55rem;
  border-radius: 0.4rem;
  line-height: 1.35;
  word-break: break-word;
}

/* ── Per-document chunk viewer ─────────────────────────────────────── */
.kb-doc-chunks {
  margin-top: 0.85rem;
  padding: 0.8rem;
  border: 1px solid #ece9d7;
  border-radius: 0.6rem;
  background: #fefdf3;
}
.kb-doc-chunks-status {
  font-size: 0.85rem;
  color: #57544a;
  padding: 0.3rem 0;
}
.kb-doc-chunks-status.kb-doc-chunks-error {
  color: #8a2a1a;
}
.kb-doc-chunks-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
  max-height: 480px;
  overflow-y: auto;
}
.kb-chunk {
  padding: 0.6rem 0.7rem;
  border-radius: 0.45rem;
  background: #ffffff;
  border: 1px solid #ece9d7;
}
.kb-chunk-head {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  margin-bottom: 0.35rem;
  font-size: 0.74rem;
  color: #57544a;
  flex-wrap: wrap;
}
.kb-chunk-index {
  font-weight: 700;
  color: #1b1c15;
}
.kb-chunk-section {
  padding: 0.05rem 0.45rem;
  background: #f4f1de;
  border-radius: 0.35rem;
  font-weight: 600;
  color: #57544a;
}
.kb-chunk-meta {
  margin-left: auto;
  font-size: 0.7rem;
  color: #6e6c5b;
}
.kb-chunk-text {
  margin: 0;
  font-size: 0.83rem;
  line-height: 1.45;
  color: #1b1c15;
  white-space: pre-wrap;
  word-break: break-word;
}
.kb-bulk-row-main {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  min-width: 0;
  flex: 1;
}
.kb-bulk-name {
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.kb-bulk-size {
  color: #6e6c5b;
  font-size: 0.78rem;
  flex-shrink: 0;
}
.kb-bulk-status {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.8rem;
  color: #57544a;
}
.kb-bulk-uploading {
  background: #f4f1de;
  border-color: #d8d3aa;
}
.kb-bulk-done {
  color: #1f6f3a;
  font-weight: 600;
}
.kb-bulk-error {
  color: #8a2a1a;
  font-weight: 600;
  cursor: help;
}
.kb-bulk-row.kb-bulk-done {
  background: #f1f7ef;
  border-color: #c1d8bc;
}
.kb-bulk-row.kb-bulk-error {
  background: #fdf2ee;
  border-color: #d6a7a0;
}

.kb-dropzone-empty {
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 0.5rem;
  padding: 0.5rem 0;
}

.kb-dropzone-empty strong {
  font-family: Manrope, sans-serif;
  font-size: 0.98rem;
}

.kb-dropzone-empty span {
  color: #5f5f53;
  font-size: 0.8rem;
}

.kb-dropzone-icon {
  width: 2.6rem;
  height: 2.6rem;
  border-radius: 0.85rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: #fffef8;
  border: 1px solid rgba(196, 199, 199, 0.55);
  color: #1b1c15;
  flex: 0 0 auto;
}

.kb-dropzone-file {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 0.18rem;
}

.kb-dropzone-file strong {
  font-family: Manrope, sans-serif;
  font-size: 0.95rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kb-dropzone-file span {
  color: #5f5f53;
  font-size: 0.78rem;
}

.kb-link-button {
  position: relative;
  z-index: 1;
  background: transparent;
  border: none;
  color: #1d4ed8;
  font-weight: 700;
  font-size: 0.8rem;
  cursor: pointer;
  padding: 0.3rem 0.5rem;
  border-radius: 0.5rem;
}

.kb-link-button:hover {
  background: rgba(59, 130, 246, 0.08);
}

.kb-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.85rem 0.9rem;
}

.kb-field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.kb-field-wide {
  grid-column: 1 / -1;
}

.kb-field > span {
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.07em;
  text-transform: uppercase;
  color: #5f5f53;
}

.kb-field input,
.kb-field select,
.kb-field textarea,
.kb-search-bar input {
  width: 100%;
  padding: 0.7rem 0.85rem;
  border-radius: 0.7rem;
  border: 1px solid rgba(196, 199, 199, 0.6);
  background: rgba(255, 255, 255, 0.85);
  font-size: 0.92rem;
  color: #1b1c15;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.kb-field input:focus,
.kb-field select:focus,
.kb-field textarea:focus,
.kb-search-bar input:focus {
  outline: none;
  border-color: rgba(27, 28, 21, 0.6);
  box-shadow: 0 0 0 3px rgba(27, 28, 21, 0.08);
}

.kb-prompt-textarea {
  min-height: 9.5rem;
  resize: vertical;
  line-height: 1.5;
}

.kb-single-status {
  display: flex;
  align-items: flex-start;
  gap: 0.65rem;
  flex-wrap: wrap;
  padding: 0.75rem 0.85rem;
  border-radius: 0.75rem;
  border: 1px solid rgba(5, 150, 105, 0.25);
  background: rgba(5, 150, 105, 0.08);
  color: #1b1c15;
}

.kb-single-status svg {
  flex: 0 0 auto;
  color: #047857;
  margin-top: 0.1rem;
}

.kb-single-status div {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  min-width: 0;
}

.kb-single-status strong {
  font-family: Manrope, sans-serif;
  font-size: 0.9rem;
}

.kb-single-status span {
  color: #4d5b51;
  font-size: 0.78rem;
}

.kb-single-disable {
  flex: 0 0 auto;
  margin-left: auto;
}

.kb-card-actions {
  display: flex;
  align-items: center;
  gap: 0.85rem;
  flex-wrap: wrap;
}

.kb-card-hint {
  color: #5f5f53;
  font-size: 0.78rem;
}

.kb-readonly-card {
  align-items: flex-start;
}

.kb-search-bar {
  position: relative;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.4rem 0.4rem 0.4rem 2.4rem;
  border-radius: 0.9rem;
  border: 1px solid rgba(196, 199, 199, 0.6);
  background: rgba(255, 255, 255, 0.9);
}

.kb-search-bar-icon {
  position: absolute;
  left: 0.95rem;
  color: #5f5f53;
  pointer-events: none;
}

.kb-search-bar input {
  flex: 1;
  border: none;
  background: transparent;
  padding: 0.55rem 0.2rem;
}

.kb-search-bar input:focus {
  box-shadow: none;
}

.kb-search-submit {
  flex: 0 0 auto;
}

.kb-results {
  display: grid;
  gap: 0.65rem;
}

.kb-result-card {
  padding: 0.85rem 1rem;
  border-radius: 0.85rem;
  border: 1px solid rgba(196, 199, 199, 0.5);
  background: rgba(255, 255, 255, 0.78);
}

.kb-result-head {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 0.4rem;
}

.kb-result-rank {
  font-family: Manrope, sans-serif;
  font-weight: 800;
  font-size: 0.78rem;
  color: #5f5f53;
}

.kb-result-head strong {
  flex: 1;
  font-family: Manrope, sans-serif;
  font-size: 0.95rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kb-result-text {
  color: #444748;
  font-size: 0.86rem;
  line-height: 1.55;
  margin: 0;
}

.kb-search-empty {
  color: #5f5f53;
  font-size: 0.85rem;
  padding: 0.4rem 0.2rem;
}

.kb-list-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 1rem;
  flex-wrap: wrap;
}

.kb-list-head h4 {
  font-family: Manrope, sans-serif;
  font-size: 1.25rem;
  line-height: 1.2;
  margin: 0.3rem 0 0.2rem;
}

.kb-list-head p {
  margin: 0;
  color: #5f5f53;
  font-size: 0.85rem;
}

.kb-list-actions {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.kb-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.4rem;
  padding: 2.4rem 1.5rem;
  border-radius: 1.2rem;
  border: 1px dashed rgba(196, 199, 199, 0.7);
  background: rgba(255, 255, 255, 0.55);
  text-align: center;
}

.kb-empty-icon {
  width: 3.2rem;
  height: 3.2rem;
  border-radius: 1rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: #efeee3;
  color: #1b1c15;
  border: 1px solid rgba(196, 199, 199, 0.55);
  margin-bottom: 0.5rem;
}

.kb-empty strong {
  font-family: Manrope, sans-serif;
  font-size: 1rem;
}

.kb-empty span {
  color: #5f5f53;
  font-size: 0.85rem;
}

.kb-doc-list {
  display: grid;
  gap: 0.75rem;
}

.kb-doc-card {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 1.1rem;
  align-items: center;
  padding: 1rem 1.2rem;
  border-radius: 1.1rem;
  border: 1px solid rgba(196, 199, 199, 0.5);
  background: rgba(255, 255, 255, 0.78);
  transition: border-color 0.2s ease, box-shadow 0.2s ease, transform 0.15s ease;
}

.kb-doc-card:hover {
  border-color: rgba(27, 28, 21, 0.35);
  box-shadow: 0 14px 32px -28px rgba(27, 28, 21, 0.4);
}

.kb-doc-status-approved {
  border-left: 3px solid #059669;
}

.kb-doc-status-pending {
  border-left: 3px solid #d97706;
}

.kb-doc-status-rejected {
  border-left: 3px solid #dc2626;
}

.kb-doc-icon {
  width: 2.5rem;
  height: 2.5rem;
  border-radius: 0.85rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: #efeee3;
  border: 1px solid rgba(196, 199, 199, 0.55);
  color: #1b1c15;
}

.kb-doc-body {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.kb-doc-title-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.55rem;
}

.kb-doc-title-row strong {
  font-family: Manrope, sans-serif;
  font-size: 1rem;
  line-height: 1.3;
}

.kb-doc-desc {
  margin: 0;
  color: #4a4a3e;
  font-size: 0.85rem;
  line-height: 1.55;
}

.kb-doc-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.8rem;
  color: #5f5f53;
  font-size: 0.78rem;
}

.kb-doc-meta strong {
  color: #1b1c15;
  font-weight: 800;
}

.kb-doc-tags {
  display: inline-flex;
  gap: 0.35rem;
  flex-wrap: wrap;
}

.kb-tag {
  background: rgba(15, 23, 42, 0.06);
  padding: 0.18rem 0.5rem;
  border-radius: 999px;
  font-size: 0.7rem;
  color: #1b1c15;
}

.kb-doc-error {
  margin: 0.25rem 0 0;
  color: #b91c1c;
  font-size: 0.78rem;
}

.kb-doc-actions {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  justify-content: flex-end;
}

@media (max-width: 980px) {
  .kb-hero {
    grid-template-columns: 1fr;
    padding: 1.4rem 1.4rem;
  }
  .kb-hero-stats {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
  .kb-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 640px) {
  .kb-hero-stats {
    grid-template-columns: 1fr;
  }
  .kb-doc-card {
    grid-template-columns: auto minmax(0, 1fr);
  }
  .kb-doc-actions {
    grid-column: 1 / -1;
    justify-content: flex-start;
  }
  .kb-form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
