<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import {
  ArrowUpDown,
  Bell,
  Bot,
  CheckCircle2,
  Database,
  Filter,
  FileText,
  Globe2,
  LogOut,
  MessageSquare,
  Mic,
  Moon,
  Plus,
  Radio,
  Search,
  Settings2,
  Shield,

  SunMedium,
  Ticket,
  UploadCloud,
  UserPlus,
  Users,
  Wrench,
  XCircle,
} from 'lucide-vue-next';
import QrcodeVue from 'qrcode.vue';

const API_BASE_URL = 'http://localhost:8000/api';
const ORG_ACCESS_TOKEN_KEY = 'org_access_token';
const ORG_REFRESH_TOKEN_KEY = 'org_refresh_token';
const ORG_THEME_MODE_KEY = 'org_theme_mode';
const TOOLKIT_REGISTRY_PREVIEW_LIMIT = 4;

const authConfig = ref(null);
const orgShellRef = ref(null);
const authState = ref('loading');
const errorMsg = ref('');
const infoMsg = ref('');
const isAuthenticating = ref(false);
const googleButtonRef = ref(null);
const pendingCredential = ref('');
const pendingAccessToken = ref('');
const pendingUser = ref(null);
const pendingOrganization = ref(null);
const candidateOrganizations = ref([]);
const totpCode = ref('');
const totpUri = ref('');
const totpSecret = ref('');
const currentUser = ref(null);
const currentOrganization = ref(null);
const members = ref([]);
const databaseProviders = ref([]);
const databaseStatus = ref({ status: 'not_connected', provider: null, selected_sources: [], indexed_points: 0 });
const crmProviders = ref([]);
const crmStatus = ref({ status: 'not_connected', provider: null, indexed_points: 0, module_count: 0, action_count: 0 });
const erpProviders = ref([]);
const erpStatus = ref({ status: 'not_connected', provider: null, indexed_points: 0, module_count: 0, action_count: 0 });
const shippingProviders = ref([]);
const shippingStatus = ref({ status: 'not_connected', provider: null, indexed_points: 0, module_count: 0, action_count: 0 });
const toolkitDraft = ref(null);
const toolkitRegistry = ref({ tools: [], drafts: [] });
const toolkitBuilders = ref([]);
const agentDocuments = ref([]);
const agentUploadForm = ref({
  name: '',
  document_type: 'policy',
  description: '',
  tags: '',
});
const agentUploadFile = ref(null);
const agentTestQuery = ref('');
const agentTestRetrieval = ref(null);
const agentTestAnswer = ref(null);
const agentRuntimeStatus = ref(null);
const agentPhoneLink = ref({ status: 'not_linked', phone_number: '' });
const agentPhoneForm = ref({ phone_number: '' });
const agentLatencyResult = ref(null);
const agentCampaigns = ref([]);
const agentCampaignForm = ref({ name: '', from_number: '' });
const agentCampaignContactsFile = ref(null);
const agentCampaignScriptFile = ref(null);
const voiceSocket = ref(null);
const voiceRecorder = ref(null);
const voiceMediaStream = ref(null);
const isVoiceSessionActive = ref(false);
const voiceTranscript = ref('');
const voiceAgentAnswer = ref('');
const voiceAudioChunks = ref([]);
const voiceTtsFirstAudioMs = ref(null);
const voiceStreamingPlaybackStarted = ref(false);
const voiceStreamingPlaybackFailed = ref(false);
const voiceReceivedStreamingAudio = ref(false);
const voiceEvents = ref([]);
const agentVoiceLanguage = ref('en');
let voiceChunkQueue = [];
let voiceChunkAudio = null;
const zohoDeskStatus = ref({
  status: 'not_connected',
  account_name: null,
  org_id: null,
  indexed_points: 0,
  module_count: 0,
  action_count: 0,
  folder_path: null,
});
const databaseModalOpen = ref(false);
const crmModalOpen = ref(false);
const erpModalOpen = ref(false);
const shippingModalOpen = ref(false);
const databaseForm = ref({
  provider: 'postgresql',
  connection_string: '',
  row_limit: 50,
});
const crmForm = ref({
  provider: 'zoho',
  account_url: '',
  api_domain: 'https://www.zohoapis.com',
  access_token: '',
  refresh_token: '',
});
const erpForm = ref({
  provider: 'tally',
  base_url: 'http://localhost:9000',
  company_name: '',
  timeout_seconds: 20,
  max_items_per_module: 25,
});
const shippingForm = ref({
  provider: 'shiprocket',
  email: '',
  password: '',
  base_url: 'https://apiv2.shiprocket.in/v1/external',
});
const toolkitForm = ref({
  builder_key: '',
  integration_type: 'shipping',
  provider: 'shiprocket',
  nlp_prompt: '',
  system_prompt: '',
});
const databaseSchema = ref([]);
const isScanningDatabase = ref(false);
const isIndexingDatabase = ref(false);
const isConnectingCrm = ref(false);
const isConnectingErp = ref(false);
const isConnectingShipping = ref(false);
const isGeneratingToolkit = ref(false);
const isReviewingToolkit = ref(false);
const isLoadingAgentDocuments = ref(false);
const isUploadingAgentDocument = ref(false);
const isReviewingAgentDocument = ref(false);
const isTestingAgentRetrieval = ref(false);
const isTestingAgentAnswer = ref(false);
const isStartingVoiceSession = ref(false);
const isLinkingAgentPhone = ref(false);
const isTestingAgentLatency = ref(false);
const isLoadingAgentCampaigns = ref(false);
const isCreatingAgentCampaign = ref(false);
const launchingCampaignId = ref(null);
const isLoadingMembers = ref(false);
const isSavingMember = ref(false);
const updatingMemberId = ref(null);
const dashboardQuery = ref('');
const showInvitedOnly = ref(false);
const memberSortMode = ref('name');
const themeMode = ref('light');
const currentPage = ref('dashboard');
const inviteForm = ref({
  email: '',
  full_name: '',
  role: 'member',
});

const canManageMembers = computed(() => currentUser.value?.role === 'admin');
const canManageDatabase = computed(() => currentUser.value?.role === 'admin');
const normalizedOrganizationDomain = computed(() => (currentOrganization.value?.email_domain || '').toLowerCase());
const organizationInitial = computed(() => (currentOrganization.value?.name || 'N').slice(0, 1).toUpperCase());
const inviteEmailDomain = computed(() => {
  const rawEmail = (inviteForm.value.email || '').trim().toLowerCase();
  if (!rawEmail.includes('@')) {
    return '';
  }
  return rawEmail.split('@').pop() || '';
});
const isInviteDomainValid = computed(() => {
  if (!inviteForm.value.email) {
    return true;
  }
  return inviteEmailDomain.value === normalizedOrganizationDomain.value;
});
const inviteValidationMessage = computed(() => {
  if (!inviteForm.value.email) {
    return `Invites are restricted to ${normalizedOrganizationDomain.value || 'your organization domain'}.`;
  }
  if (!inviteEmailDomain.value) {
    return 'Enter a valid work email address.';
  }
  if (!isInviteDomainValid.value) {
    return `Invite email must use ${normalizedOrganizationDomain.value}.`;
  }
  return `This invite will be sent under ${normalizedOrganizationDomain.value}.`;
});
const inviteCanSubmit = computed(
  () => Boolean(inviteForm.value.email) && Boolean(inviteEmailDomain.value) && isInviteDomainValid.value,
);
const activeMemberCount = computed(() => members.value.filter((member) => member.status === 'active').length);
const invitedMemberCount = computed(() => members.value.filter((member) => member.status === 'invited').length);
const disabledMemberCount = computed(() => members.value.filter((member) => member.status === 'disabled').length);
const organizationUsageRatio = computed(() => {
  const sources = Number(databaseStatus.value.selected_sources?.length || 0);
  const indexed = Number(databaseStatus.value.indexed_points || 0);
  if (!sources && !indexed) {
    return 0.08;
  }
  return Math.min(0.96, 0.18 + sources * 0.16 + Math.min(indexed / 150, 0.48));
});
const organizationUsageWidth = computed(() => `${Math.round(organizationUsageRatio.value * 100)}%`);
const memberFilterLabel = computed(() => (showInvitedOnly.value ? 'Invited only' : 'All members'));
const memberSortLabel = computed(() => (memberSortMode.value === 'name' ? 'Sort: Name' : 'Sort: Role'));
const themeToggleLabel = computed(() => (themeMode.value === 'dark' ? 'Light mode' : 'Dark mode'));
const dashboardSearchPlaceholder = computed(() =>
  currentPage.value === 'tickets'
    ? 'Search tickets, Desk status, namespace...'
    : currentPage.value === 'toolkit'
      ? 'Search tools, integrations, registry...'
      : currentPage.value === 'agent'
        ? 'Search documents, policies, chunks...'
      : 'Search members, roles, statuses...',
);
const crmProviderLabel = computed(() => {
  const match = crmProviders.value.find((provider) => provider.value === crmStatus.value.provider);
  return match?.label || crmStatus.value.provider || 'Not connected';
});
const erpProviderLabel = computed(() => {
  const match = erpProviders.value.find((provider) => provider.value === erpStatus.value.provider);
  return match?.label || erpStatus.value.provider || 'Not connected';
});
const shippingProviderLabel = computed(() => {
  const match = shippingProviders.value.find((provider) => provider.value === shippingStatus.value.provider);
  return match?.label || shippingStatus.value.provider || 'Not connected';
});
const zohoDeskStatusLabel = computed(() => {
  if (zohoDeskStatus.value.status === 'indexed') {
    return 'Indexed';
  }
  if (zohoDeskStatus.value.status === 'not_connected') {
    return 'Not connected';
  }
  return zohoDeskStatus.value.status || 'Unknown';
});
const activeCrmProvider = computed(() => crmForm.value.provider);
const isZohoCrmProvider = computed(() => activeCrmProvider.value === 'zoho');
const connectedToolkitIntegrations = computed(() => {
  const options = [];
  if (['schema_scanned', 'indexed'].includes(databaseStatus.value.status) && databaseStatus.value.provider) {
    options.push({
      value: `database:${databaseStatus.value.provider}`,
      integration_type: 'database',
      provider: databaseStatus.value.provider,
      label: `Database - ${databaseStatus.value.provider}`,
      detail: databaseStatus.value.database_name || `${databaseStatus.value.selected_sources?.length || 0} sources`,
    });
  }
  if (crmStatus.value.status === 'indexed' && crmStatus.value.provider) {
    options.push({
      value: `crm:${crmStatus.value.provider}`,
      integration_type: 'crm',
      provider: crmStatus.value.provider,
      label: `CRM - ${crmProviderLabel.value}`,
      detail: crmStatus.value.account_name || `${crmStatus.value.module_count || 0} modules`,
    });
  }
  if (zohoDeskStatus.value.status === 'indexed') {
    options.push({
      value: 'zoho_desk:zoho',
      integration_type: 'zoho_desk',
      provider: 'zoho',
      label: 'Zoho Desk',
      detail: zohoDeskStatus.value.account_name || `${zohoDeskStatus.value.action_count || 0} actions`,
    });
  }
  if (erpStatus.value.status === 'indexed' && erpStatus.value.provider) {
    options.push({
      value: `erp:${erpStatus.value.provider}`,
      integration_type: 'erp',
      provider: erpStatus.value.provider,
      label: `ERP - ${erpProviderLabel.value}`,
      detail: erpStatus.value.account_name || `${erpStatus.value.module_count || 0} modules`,
    });
  }
  if (shippingStatus.value.status === 'indexed' && shippingStatus.value.provider) {
    options.push({
      value: `shipping:${shippingStatus.value.provider}`,
      integration_type: 'shipping',
      provider: shippingStatus.value.provider,
      label: `Shipping - ${shippingProviderLabel.value}`,
      detail: shippingStatus.value.account_name || `${shippingStatus.value.action_count || 0} actions`,
    });
  }
  return options;
});
const toolkitBuilderOptions = computed(() => {
  if (toolkitBuilders.value.length) {
    return toolkitBuilders.value;
  }
  return connectedToolkitIntegrations.value.map((integration) => ({
    builder_key: `${integration.integration_type}:${integration.provider}`,
    integration_type: integration.integration_type,
    provider: integration.provider,
    execution_backend: integration.integration_type === 'database' ? 'database_sql' : 'connector_action',
    resource_model: integration.integration_type,
    label: `${integration.label} Builder`,
    description: `Generate MCP tools scoped to ${integration.label}.`,
    detail: integration.detail,
    supported_operations: ['read', 'create', 'update', 'workflow'],
    connected: true,
    value: integration.value,
  }));
});
const selectedToolkitBuilder = computed(() =>
  toolkitBuilderOptions.value.find((builder) => builder.builder_key === toolkitForm.value.builder_key)
  || toolkitBuilderOptions.value.find((builder) => `${builder.integration_type}:${builder.provider}` === `${toolkitForm.value.integration_type}:${toolkitForm.value.provider}`)
  || null,
);
const selectedToolkitIntegrationValue = computed({
  get() {
    return toolkitForm.value.builder_key || `${toolkitForm.value.integration_type}:${toolkitForm.value.provider}`;
  },
  set(value) {
    const option = toolkitBuilderOptions.value.find((item) => item.builder_key === value || item.value === value);
    if (!option) {
      return;
    }
    toolkitForm.value.builder_key = option.builder_key;
    toolkitForm.value.integration_type = option.integration_type;
    toolkitForm.value.provider = option.provider;
    toolkitDraft.value = null;
    fetchToolkitRegistry();
  },
});
const filteredMembers = computed(() => {
  const query = dashboardQuery.value.trim().toLowerCase();
  let visibleMembers = [...members.value];

  if (showInvitedOnly.value) {
    visibleMembers = visibleMembers.filter((member) => member.status === 'invited');
  }

  if (query) {
    visibleMembers = visibleMembers.filter((member) =>
      [member.full_name, member.email, member.role, member.status]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(query)),
    );
  }

  visibleMembers.sort((left, right) => {
    if (memberSortMode.value === 'role') {
      return `${left.role}:${left.email}`.localeCompare(`${right.role}:${right.email}`);
    }
    return `${left.full_name || ''}:${left.email}`.localeCompare(`${right.full_name || ''}:${right.email}`);
  });

  return visibleMembers;
});
const filteredToolkitTools = computed(() => {
  const query = dashboardQuery.value.trim().toLowerCase();
  const tools = [...(toolkitRegistry.value.tools || [])];
  if (!query) {
    return tools;
  }
  return tools.filter((tool) =>
    [
      tool.name,
      tool.title,
      tool.description,
      tool.integration_type,
      tool.provider,
      tool?.mcp?.tool_name,
    ]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(query)),
  );
});
const filteredToolkitDrafts = computed(() => {
  const query = dashboardQuery.value.trim().toLowerCase();
  const drafts = [...(toolkitRegistry.value.drafts || [])];
  if (!query) {
    return drafts;
  }
  return drafts.filter((draft) =>
    [
      draft.status,
      draft.nlp_prompt,
      draft.tool?.name,
      draft.tool?.title,
      draft.tool?.description,
      draft.integration_type,
      draft.provider,
    ]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(query)),
  );
});
const visibleToolkitTools = computed(() => filteredToolkitTools.value.slice(0, TOOLKIT_REGISTRY_PREVIEW_LIMIT));
const visibleToolkitDrafts = computed(() => filteredToolkitDrafts.value.slice(0, TOOLKIT_REGISTRY_PREVIEW_LIMIT));
const hiddenToolkitToolCount = computed(() => Math.max(0, filteredToolkitTools.value.length - visibleToolkitTools.value.length));
const hiddenToolkitDraftCount = computed(() => Math.max(0, filteredToolkitDrafts.value.length - visibleToolkitDrafts.value.length));
const filteredAgentDocuments = computed(() => {
  const query = dashboardQuery.value.trim().toLowerCase();
  const documents = [...agentDocuments.value];
  if (!query) {
    return documents;
  }
  return documents.filter((document) =>
    [
      document.name,
      document.document_type,
      document.description,
      document.status,
      document.approval_status,
      ...(document.tags || []),
    ]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(query)),
  );
});
const approvedAgentDocumentCount = computed(() =>
  agentDocuments.value.filter((document) => document.approval_status === 'approved').length,
);
const pendingAgentDocumentCount = computed(() =>
  agentDocuments.value.filter((document) => document.approval_status === 'pending').length,
);
const agentKnowledgeChunkCount = computed(() =>
  agentDocuments.value.reduce((total, document) => total + Number(document.chunk_count || 0), 0),
);
const agentVoiceLanguageOptions = computed(() =>
  agentRuntimeStatus.value?.supported_indian_languages?.length
    ? agentRuntimeStatus.value.supported_indian_languages
    : [
        { code: 'en', label: 'English', native_label: 'English' },
        { code: 'hi', label: 'Hindi', native_label: 'हिन्दी' },
        { code: 'bn', label: 'Bengali', native_label: 'বাংলা' },
        { code: 'gu', label: 'Gujarati', native_label: 'ગુજરાતી' },
        { code: 'kn', label: 'Kannada', native_label: 'ಕನ್ನಡ' },
        { code: 'ml', label: 'Malayalam', native_label: 'മലയാളം' },
        { code: 'mr', label: 'Marathi', native_label: 'मराठी' },
        { code: 'pa', label: 'Punjabi', native_label: 'ਪੰਜਾਬੀ' },
        { code: 'ta', label: 'Tamil', native_label: 'தமிழ்' },
        { code: 'te', label: 'Telugu', native_label: 'తెలుగు' },
        { code: 'ur', label: 'Urdu', native_label: 'اُردُو' },
      ],
);
const schemaSelectionCount = computed(() =>
  databaseSchema.value.reduce(
    (total, table) => total + table.columns.filter((column) => column.selected).length,
    0,
  ),
);

const googleScriptPromise = (() => {
  let promise;
  return () => {
    if (window.google?.accounts?.id) {
      return Promise.resolve(window.google);
    }
    if (!promise) {
      promise = new Promise((resolve, reject) => {
        const existing = document.querySelector('script[data-google-identity="true"]');
        if (existing) {
          existing.addEventListener('load', () => resolve(window.google), { once: true });
          existing.addEventListener('error', reject, { once: true });
          return;
        }

        const script = document.createElement('script');
        script.src = 'https://accounts.google.com/gsi/client';
        script.async = true;
        script.defer = true;
        script.dataset.googleIdentity = 'true';
        script.onload = () => resolve(window.google);
        script.onerror = () => reject(new Error('Failed to load Google Identity Services'));
        document.head.appendChild(script);
      });
    }
    return promise;
  };
})();

let refreshInFlight = null;

async function refreshOrganizationSession() {
  if (refreshInFlight) {
    return refreshInFlight;
  }

  const refreshToken = localStorage.getItem(ORG_REFRESH_TOKEN_KEY);
  if (!refreshToken) {
    throw new Error('Missing refresh token');
  }

  refreshInFlight = fetch(`${API_BASE_URL}/org-auth/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: refreshToken }),
  })
    .then(async (response) => {
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || 'Session refresh failed');
      }
      return response.json();
    })
    .then((payload) => {
      localStorage.setItem(ORG_ACCESS_TOKEN_KEY, payload.access_token);
      localStorage.setItem(ORG_REFRESH_TOKEN_KEY, payload.refresh_token);
      return payload;
    })
    .finally(() => {
      refreshInFlight = null;
    });

  return refreshInFlight;
}

async function apiRequest(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!headers.has('Content-Type') && options.body && !(options.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
  }

  const token = options.authToken || localStorage.getItem(ORG_ACCESS_TOKEN_KEY);
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`);
  }

  let response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...options,
      headers,
    });
  } catch (error) {
    throw new Error(`API server unavailable at ${API_BASE_URL}. Start the backend and try again.`);
  }

  if (
    response.status === 401 &&
    !options.skipRefresh &&
    !options.authToken &&
    path !== '/org-auth/refresh'
  ) {
    try {
      await refreshOrganizationSession();
      return await apiRequest(path, { ...options, skipRefresh: true });
    } catch (_) {
      clearSession();
      authState.value = 'login';
      infoMsg.value = '';
      throw new Error('Session expired. Sign in again.');
    }
  }

  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = payload.detail;
    const error = new Error(typeof detail === 'string' ? detail : detail?.message || 'Request failed');
    error.payload = payload;
    error.detail = detail;
    error.code = typeof detail === 'object' ? detail?.code : undefined;
    throw error;
  }

  return response.json();
}

function persistSession(payload) {
  localStorage.setItem(ORG_ACCESS_TOKEN_KEY, payload.access_token);
  localStorage.setItem(ORG_REFRESH_TOKEN_KEY, payload.refresh_token);
  currentUser.value = payload.user;
  currentOrganization.value = payload.organization;
  pendingAccessToken.value = '';
  pendingUser.value = null;
  pendingOrganization.value = null;
  pendingCredential.value = '';
  candidateOrganizations.value = [];
  totpCode.value = '';
  totpUri.value = '';
  totpSecret.value = '';
}

function clearSession() {
  localStorage.removeItem(ORG_ACCESS_TOKEN_KEY);
  localStorage.removeItem(ORG_REFRESH_TOKEN_KEY);
  currentUser.value = null;
  currentOrganization.value = null;
  members.value = [];
  pendingAccessToken.value = '';
  pendingUser.value = null;
  pendingOrganization.value = null;
  pendingCredential.value = '';
  candidateOrganizations.value = [];
  totpCode.value = '';
  totpUri.value = '';
  totpSecret.value = '';
  databaseProviders.value = [];
  databaseStatus.value = { status: 'not_connected', provider: null, selected_sources: [], indexed_points: 0 };
  crmProviders.value = [];
  crmStatus.value = { status: 'not_connected', provider: null, indexed_points: 0, module_count: 0, action_count: 0 };
  erpProviders.value = [];
  erpStatus.value = { status: 'not_connected', provider: null, indexed_points: 0, module_count: 0, action_count: 0 };
  shippingProviders.value = [];
  shippingStatus.value = { status: 'not_connected', provider: null, indexed_points: 0, module_count: 0, action_count: 0 };
  toolkitDraft.value = null;
  toolkitRegistry.value = { tools: [], drafts: [] };
  toolkitBuilders.value = [];
  agentDocuments.value = [];
  agentUploadForm.value = { name: '', document_type: 'policy', description: '', tags: '' };
  agentUploadFile.value = null;
  agentTestQuery.value = '';
  agentTestRetrieval.value = null;
  agentTestAnswer.value = null;
  agentRuntimeStatus.value = null;
  agentPhoneLink.value = { status: 'not_linked', phone_number: '' };
  agentPhoneForm.value = { phone_number: '' };
  agentLatencyResult.value = null;
  agentCampaigns.value = [];
  agentCampaignForm.value = { name: '', from_number: '' };
  agentCampaignContactsFile.value = null;
  agentCampaignScriptFile.value = null;
  voiceSocket.value = null;
  voiceRecorder.value = null;
  voiceMediaStream.value = null;
  isVoiceSessionActive.value = false;
  voiceTranscript.value = '';
  voiceAgentAnswer.value = '';
  voiceAudioChunks.value = [];
  voiceTtsFirstAudioMs.value = null;
  voiceStreamingPlaybackStarted.value = false;
  voiceStreamingPlaybackFailed.value = false;
  voiceChunkQueue = [];
  if (voiceChunkAudio) {
    voiceChunkAudio.pause();
    voiceChunkAudio = null;
  }
  voiceEvents.value = [];
  agentVoiceLanguage.value = 'en';
  zohoDeskStatus.value = {
    status: 'not_connected',
    account_name: null,
    org_id: null,
    indexed_points: 0,
    module_count: 0,
    action_count: 0,
    folder_path: null,
  };
  databaseModalOpen.value = false;
  crmModalOpen.value = false;
  erpModalOpen.value = false;
  shippingModalOpen.value = false;
  databaseSchema.value = [];
  databaseForm.value = { provider: 'postgresql', connection_string: '', row_limit: 50 };
  crmForm.value = { provider: 'zoho', account_url: '', api_domain: 'https://www.zohoapis.com', access_token: '', refresh_token: '' };
  erpForm.value = { provider: 'tally', base_url: 'http://localhost:9000', company_name: '', timeout_seconds: 20, max_items_per_module: 25 };
  shippingForm.value = { provider: 'shiprocket', email: '', password: '', base_url: 'https://apiv2.shiprocket.in/v1/external' };
  toolkitForm.value = { builder_key: '', integration_type: 'shipping', provider: 'shiprocket', nlp_prompt: '', system_prompt: '' };
  dashboardQuery.value = '';
  showInvitedOnly.value = false;
  memberSortMode.value = 'name';
  currentPage.value = 'dashboard';
}

function applyTheme(mode) {
  themeMode.value = mode === 'dark' ? 'dark' : 'light';
  localStorage.setItem(ORG_THEME_MODE_KEY, themeMode.value);
}

function toggleThemeMode() {
  applyTheme(themeMode.value === 'dark' ? 'light' : 'dark');
}

function updateCursorGlow(event) {
  if (!orgShellRef.value) {
    return;
  }
  const rect = orgShellRef.value.getBoundingClientRect();
  orgShellRef.value.style.setProperty('--cursor-x', `${event.clientX - rect.left}px`);
  orgShellRef.value.style.setProperty('--cursor-y', `${event.clientY - rect.top}px`);
}

async function initializeOrganizationMfa(payload) {
  pendingAccessToken.value = payload.access_token;
  pendingUser.value = payload.user;
  pendingOrganization.value = payload.organization;
  candidateOrganizations.value = [];
  pendingCredential.value = '';

  try {
    const setupPayload = await apiRequest('/org-auth/mfa/totp/setup', {
      method: 'POST',
      authToken: payload.access_token,
    });
    totpSecret.value = setupPayload.secret;
    totpUri.value = setupPayload.uri;
    totpCode.value = '';
    authState.value = 'mfa_setup';
    infoMsg.value = `Scan the authenticator QR for ${setupPayload.email}.`;
  } catch (error) {
    if (error.message === 'TOTP already set up') {
      totpSecret.value = '';
      totpUri.value = '';
      totpCode.value = '';
      authState.value = 'mfa_verify';
      infoMsg.value = `Enter the 6-digit code for ${payload.user.email}.`;
      return;
    }
    clearSession();
    authState.value = 'login';
    throw error;
  }
}

async function handleTotpVerify() {
  if (!pendingAccessToken.value || !totpCode.value || totpCode.value.length < 6) {
    return;
  }

  isAuthenticating.value = true;
  errorMsg.value = '';
  infoMsg.value = 'Verifying organization TOTP...';

  try {
    const payload = await apiRequest('/org-auth/mfa/totp/verify', {
      method: 'POST',
      authToken: pendingAccessToken.value,
      body: JSON.stringify({ token: totpCode.value }),
    });
    persistSession(payload);
    authState.value = 'ready';
    infoMsg.value = `${payload.organization.name} verified`;
    await fetchMembers();
    await fetchDatabaseProviders();
    await fetchDatabaseStatus();
    await fetchCrmProviders();
    await fetchCrmStatus();
    await fetchErpProviders();
    await fetchErpStatus();
    await fetchShippingProviders();
    await fetchShippingStatus();
    await fetchZohoDeskStatus();
    await fetchToolkitBuilders();
  } catch (error) {
    errorMsg.value = error.message;
    infoMsg.value = '';
  } finally {
    isAuthenticating.value = false;
  }
}

async function resetLoginState() {
  clearSession();
  authState.value = 'login';
  infoMsg.value = '';
  errorMsg.value = '';
  await initializeGoogleButton();
}

async function fetchMembers() {
  if (!canManageMembers.value) {
    members.value = [];
    return;
  }
  isLoadingMembers.value = true;
  try {
    members.value = await apiRequest('/org-auth/members');
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isLoadingMembers.value = false;
  }
}

async function fetchDatabaseProviders() {
  if (!canManageDatabase.value) {
    databaseProviders.value = [];
    return;
  }
  try {
    databaseProviders.value = await apiRequest('/org-auth/database/providers');
  } catch (error) {
    errorMsg.value = error.message;
  }
}

async function fetchCrmProviders() {
  if (!canManageDatabase.value) {
    crmProviders.value = [];
    return;
  }
  try {
    crmProviders.value = await apiRequest('/org-auth/crm/providers');
  } catch (error) {
    errorMsg.value = error.message;
  }
}

async function fetchErpProviders() {
  if (!canManageDatabase.value) {
    erpProviders.value = [];
    return;
  }
  try {
    erpProviders.value = await apiRequest('/org-auth/erp/providers');
  } catch (error) {
    errorMsg.value = error.message;
  }
}

async function fetchShippingProviders() {
  if (!canManageDatabase.value) {
    shippingProviders.value = [];
    return;
  }
  try {
    shippingProviders.value = await apiRequest('/org-auth/shipping/providers');
  } catch (error) {
    errorMsg.value = error.message;
  }
}

async function fetchDatabaseStatus() {
  if (!canManageDatabase.value) {
    databaseStatus.value = { status: 'not_connected', provider: null, selected_sources: [], indexed_points: 0 };
    return;
  }
  try {
    databaseStatus.value = await apiRequest('/org-auth/database/status');
  } catch (error) {
    errorMsg.value = error.message;
  }
}

async function fetchCrmStatus() {
  if (!canManageDatabase.value) {
    crmStatus.value = { status: 'not_connected', provider: null, indexed_points: 0, module_count: 0, action_count: 0 };
    return;
  }
  try {
    crmStatus.value = await apiRequest('/org-auth/crm/status');
  } catch (error) {
    errorMsg.value = error.message;
  }
}

async function fetchErpStatus() {
  if (!canManageDatabase.value) {
    erpStatus.value = { status: 'not_connected', provider: null, indexed_points: 0, module_count: 0, action_count: 0 };
    return;
  }
  try {
    erpStatus.value = await apiRequest('/org-auth/erp/status');
  } catch (error) {
    errorMsg.value = error.message;
  }
}

async function fetchShippingStatus() {
  if (!canManageDatabase.value) {
    shippingStatus.value = { status: 'not_connected', provider: null, indexed_points: 0, module_count: 0, action_count: 0 };
    return;
  }
  try {
    shippingStatus.value = await apiRequest('/org-auth/shipping/status');
  } catch (error) {
    errorMsg.value = error.message;
  }
}

async function fetchZohoDeskStatus() {
  if (!canManageDatabase.value) {
    zohoDeskStatus.value = {
      status: 'not_connected',
      account_name: null,
      org_id: null,
      indexed_points: 0,
      module_count: 0,
      action_count: 0,
      folder_path: null,
    };
    return;
  }
  try {
    zohoDeskStatus.value = await apiRequest('/org-auth/crm/zoho-desk/status');
  } catch (error) {
    errorMsg.value = error.message;
  }
}

function openDatabaseModal() {
  if (!canManageDatabase.value) {
    return;
  }
  databaseModalOpen.value = true;
  errorMsg.value = '';
  infoMsg.value = '';
}

function closeDatabaseModal() {
  databaseModalOpen.value = false;
}

function openCrmModal() {
  if (!canManageDatabase.value) {
    return;
  }
  crmModalOpen.value = true;
  errorMsg.value = '';
  infoMsg.value = '';
}

function closeCrmModal() {
  crmModalOpen.value = false;
}

function openErpModal() {
  if (!canManageDatabase.value) {
    return;
  }
  erpModalOpen.value = true;
  errorMsg.value = '';
  infoMsg.value = '';
}

function closeErpModal() {
  erpModalOpen.value = false;
}

function openShippingModal() {
  if (!canManageDatabase.value) {
    return;
  }
  shippingModalOpen.value = true;
  errorMsg.value = '';
  infoMsg.value = '';
}

function closeShippingModal() {
  shippingModalOpen.value = false;
}

function ensureToolkitIntegrationSelection() {
  if (!canManageDatabase.value) {
    return false;
  }
  const options = toolkitBuilderOptions.value;
  if (!options.length) {
    toolkitRegistry.value = { tools: [], drafts: [] };
    toolkitDraft.value = null;
    return false;
  }
  const current = selectedToolkitIntegrationValue.value;
  const hasCurrent = options.some((option) => option.builder_key === current || option.value === current);
  if (!hasCurrent) {
    const firstOption = options[0];
    toolkitForm.value.builder_key = firstOption.builder_key;
    toolkitForm.value.integration_type = firstOption.integration_type;
    toolkitForm.value.provider = firstOption.provider;
    toolkitDraft.value = null;
  }
  return true;
}

function switchPage(page) {
  currentPage.value = page;
  if (page !== 'dashboard') {
    dashboardQuery.value = '';
  }
  if (page === 'toolkit') {
    if (ensureToolkitIntegrationSelection()) {
      fetchToolkitBuilders();
      fetchToolkitRegistry();
    }
  }
  if (page === 'agent') {
    fetchAgentDocuments();
    fetchAgentRuntimeStatus();
    fetchAgentPhoneLink();
    fetchAgentCampaigns();
  }
}

async function fetchToolkitBuilders() {
  if (!canManageDatabase.value) {
    toolkitBuilders.value = [];
    return;
  }
  try {
    const payload = await apiRequest('/org-auth/toolkit/builders');
    toolkitBuilders.value = payload.builders || [];
    ensureToolkitIntegrationSelection();
  } catch (_) {
    toolkitBuilders.value = [];
  }
}

function openZohoCrmModal() {
  crmForm.value.provider = 'zoho';
  openCrmModal();
}

function toggleMemberFilter() {
  showInvitedOnly.value = !showInvitedOnly.value;
}

function cycleMemberSort() {
  memberSortMode.value = memberSortMode.value === 'name' ? 'role' : 'name';
}

function normalizeSchema(schema) {
  return (schema || []).map((table) => ({
    ...table,
    selected: false,
    columns: (table.columns || []).map((column) => ({
      ...column,
      selected: false,
    })),
  }));
}

function toggleTableSelection(table, checked) {
  table.selected = checked;
  table.columns = table.columns.map((column) => ({ ...column, selected: checked }));
}

async function scanDatabaseConnection() {
  if (!databaseForm.value.connection_string) {
    return;
  }
  isScanningDatabase.value = true;
  errorMsg.value = '';
  infoMsg.value = '';
  try {
    const payload = await apiRequest('/org-auth/database/connect', {
      method: 'POST',
      body: JSON.stringify({
        provider: databaseForm.value.provider,
        connection_string: databaseForm.value.connection_string,
      }),
    });
    databaseSchema.value = normalizeSchema(payload.schema);
    databaseStatus.value = {
      ...databaseStatus.value,
      provider: payload.provider,
      status: payload.status,
      database_name: payload.database_name,
      secret_ref: payload.secret_ref,
    };
    infoMsg.value = `Schema scanned for ${payload.database_name || payload.provider}. Select columns to index.`;
    await fetchDatabaseStatus();
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isScanningDatabase.value = false;
  }
}

async function connectCrmIntegration() {
  if (!crmForm.value.access_token) {
    errorMsg.value = 'CRM access token is required.';
    return;
  }
  if (crmForm.value.provider === 'freshworks' && !crmForm.value.account_url) {
    errorMsg.value = 'Freshworks account URL is required.';
    return;
  }

  isConnectingCrm.value = true;
  errorMsg.value = '';
  infoMsg.value = '';
  try {
    const payload = await apiRequest('/org-auth/crm/connect', {
      method: 'POST',
      body: JSON.stringify(crmForm.value),
    });
    crmStatus.value = {
      ...crmStatus.value,
      provider: payload.provider,
      status: payload.status,
      account_name: payload.account_name,
      indexed_points: payload.indexed_points,
      module_count: payload.module_count,
      action_count: payload.action_count,
      folder_path: payload.folder_path,
      secret_ref: payload.secret_ref,
    };
    infoMsg.value = `CRM schema indexed for ${payload.account_name}.`;
    await fetchCrmStatus();
    closeCrmModal();
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isConnectingCrm.value = false;
  }
}

async function connectErpIntegration() {
  if (!erpForm.value.base_url) {
    errorMsg.value = 'Tally URL is required.';
    return;
  }

  isConnectingErp.value = true;
  errorMsg.value = '';
  infoMsg.value = '';
  try {
    const payload = await apiRequest('/org-auth/erp/connect', {
      method: 'POST',
      body: JSON.stringify({
        provider: erpForm.value.provider,
        base_url: erpForm.value.base_url,
        company_name: erpForm.value.company_name || null,
        timeout_seconds: erpForm.value.timeout_seconds,
        max_items_per_module: erpForm.value.max_items_per_module,
      }),
    });
    erpStatus.value = {
      ...erpStatus.value,
      provider: payload.provider,
      status: payload.status,
      account_name: payload.account_name,
      indexed_points: payload.indexed_points,
      module_count: payload.module_count,
      action_count: payload.action_count,
      folder_path: payload.folder_path,
      secret_ref: payload.secret_ref,
      last_error: null,
    };
    infoMsg.value = `ERP schema indexed for ${payload.account_name}.`;
    await fetchErpStatus();
    closeErpModal();
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isConnectingErp.value = false;
  }
}

async function connectShippingIntegration() {
  if (!shippingForm.value.email || !shippingForm.value.password) {
    errorMsg.value = 'Shiprocket API email and password are required.';
    return;
  }

  isConnectingShipping.value = true;
  errorMsg.value = '';
  infoMsg.value = '';
  try {
    const payload = await apiRequest('/org-auth/shipping/connect', {
      method: 'POST',
      body: JSON.stringify(shippingForm.value),
    });
    shippingStatus.value = {
      ...shippingStatus.value,
      provider: payload.provider,
      status: payload.status,
      account_name: payload.account_name,
      indexed_points: payload.indexed_points,
      module_count: payload.module_count,
      action_count: payload.action_count,
      folder_path: payload.folder_path,
      secret_ref: payload.secret_ref,
      last_error: null,
    };
    infoMsg.value = `Shipping integration indexed for ${payload.account_name}.`;
    await fetchShippingStatus();
    closeShippingModal();
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isConnectingShipping.value = false;
  }
}

async function fetchToolkitRegistry() {
  if (!ensureToolkitIntegrationSelection() || !toolkitForm.value.integration_type || !toolkitForm.value.provider) {
    return;
  }
  try {
    const query = new URLSearchParams({
      integration_type: toolkitForm.value.integration_type,
      provider: toolkitForm.value.provider,
    });
    toolkitRegistry.value = await apiRequest(`/org-auth/toolkit/registry?${query.toString()}`);
  } catch (error) {
    errorMsg.value = error.message;
  }
}

async function generateToolkitDraft() {
  if (!ensureToolkitIntegrationSelection()) {
    errorMsg.value = 'Connect an integration before generating a toolkit draft.';
    return;
  }
  if (!toolkitForm.value.nlp_prompt) {
    errorMsg.value = 'Describe the tool you want to generate.';
    return;
  }
  isGeneratingToolkit.value = true;
  errorMsg.value = '';
  infoMsg.value = '';
  try {
    toolkitDraft.value = await apiRequest('/org-auth/toolkit/generate', {
      method: 'POST',
      body: JSON.stringify({
        builder_key: toolkitForm.value.builder_key || null,
        integration_type: toolkitForm.value.integration_type,
        provider: toolkitForm.value.provider,
        nlp_prompt: toolkitForm.value.nlp_prompt,
        system_prompt: toolkitForm.value.system_prompt || null,
      }),
    });
    infoMsg.value = 'Toolkit draft generated. Review it before approval.';
    await fetchToolkitRegistry();
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isGeneratingToolkit.value = false;
  }
}

async function reviewToolkitDraft(action) {
  if (!toolkitDraft.value?.id) {
    return;
  }
  isReviewingToolkit.value = true;
  errorMsg.value = '';
  infoMsg.value = '';
  try {
    toolkitDraft.value = await apiRequest(`/org-auth/toolkit/drafts/${toolkitDraft.value.id}/${action}`, {
      method: 'POST',
      body: JSON.stringify({ notes: action === 'approve' ? 'Approved from organization portal.' : 'Rejected from organization portal.' }),
    });
    infoMsg.value = action === 'approve' ? 'Tool added to MCP registry.' : 'Toolkit draft rejected.';
    await fetchToolkitRegistry();
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isReviewingToolkit.value = false;
  }
}

function handleAgentFileChange(event) {
  const [file] = Array.from(event.target.files || []);
  agentUploadFile.value = file || null;
  if (file && !agentUploadForm.value.name) {
    agentUploadForm.value.name = file.name.replace(/\.[^.]+$/, '').replace(/[-_]+/g, ' ');
  }
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || '');
      resolve(result.includes(',') ? result.split(',').pop() : result);
    };
    reader.onerror = () => reject(new Error('Unable to read selected file.'));
    reader.readAsDataURL(file);
  });
}

async function fetchAgentDocuments() {
  if (!canManageDatabase.value) {
    agentDocuments.value = [];
    return;
  }
  isLoadingAgentDocuments.value = true;
  try {
    const payload = await apiRequest('/org-auth/agent/documents');
    agentDocuments.value = payload.documents || [];
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isLoadingAgentDocuments.value = false;
  }
}

async function fetchAgentRuntimeStatus() {
  if (!canManageDatabase.value) {
    agentRuntimeStatus.value = null;
    return;
  }
  try {
    agentRuntimeStatus.value = await apiRequest('/org-auth/agent/runtime/status');
  } catch (error) {
    errorMsg.value = error.message;
  }
}

async function fetchAgentPhoneLink() {
  if (!canManageDatabase.value) {
    agentPhoneLink.value = { status: 'not_linked', phone_number: '' };
    return;
  }
  try {
    agentPhoneLink.value = await apiRequest('/org-auth/agent/phone-link');
    if (agentPhoneLink.value?.phone_number) {
      agentPhoneForm.value.phone_number = agentPhoneLink.value.phone_number;
    }
  } catch (error) {
    errorMsg.value = error.message;
  }
}

async function fetchAgentCampaigns() {
  if (!canManageDatabase.value) {
    agentCampaigns.value = [];
    return;
  }
  isLoadingAgentCampaigns.value = true;
  try {
    agentCampaigns.value = await apiRequest('/org-auth/agent/campaigns');
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isLoadingAgentCampaigns.value = false;
  }
}

function handleCampaignContactsFile(event) {
  const [file] = Array.from(event.target.files || []);
  agentCampaignContactsFile.value = file || null;
}

function handleCampaignScriptFile(event) {
  const [file] = Array.from(event.target.files || []);
  agentCampaignScriptFile.value = file || null;
  if (file && !agentCampaignForm.value.name) {
    agentCampaignForm.value.name = file.name.replace(/\.[^.]+$/, '').replace(/[-_]+/g, ' ');
  }
}

async function createAgentCampaign() {
  if (!agentCampaignForm.value.name.trim()) {
    errorMsg.value = 'Campaign name is required.';
    return;
  }
  if (!agentCampaignContactsFile.value || !agentCampaignScriptFile.value) {
    errorMsg.value = 'Upload both the contact sheet and campaign script.';
    return;
  }
  isCreatingAgentCampaign.value = true;
  errorMsg.value = '';
  infoMsg.value = '';
  try {
    const formData = new FormData();
    formData.append('name', agentCampaignForm.value.name.trim());
    if (agentCampaignForm.value.from_number.trim()) {
      formData.append('from_number', agentCampaignForm.value.from_number.trim());
    }
    formData.append('contacts_file', agentCampaignContactsFile.value);
    formData.append('script_file', agentCampaignScriptFile.value);
    await apiRequest('/org-auth/agent/campaigns', {
      method: 'POST',
      body: formData,
    });
    infoMsg.value = 'Campaign created and script indexed in Qdrant.';
    agentCampaignForm.value = { name: '', from_number: '' };
    agentCampaignContactsFile.value = null;
    agentCampaignScriptFile.value = null;
    await fetchAgentCampaigns();
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isCreatingAgentCampaign.value = false;
  }
}

async function launchAgentCampaign(campaign) {
  if (!campaign?.id) {
    return;
  }
  launchingCampaignId.value = campaign.id;
  errorMsg.value = '';
  infoMsg.value = '';
  try {
    await apiRequest(`/org-auth/agent/campaigns/${campaign.id}/launch`, { method: 'POST' });
    infoMsg.value = 'Outbound campaign launch requested through Exotel.';
    await fetchAgentCampaigns();
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    launchingCampaignId.value = null;
  }
}

async function cancelAgentCampaign(campaign) {
  if (!campaign?.id) {
    return;
  }
  launchingCampaignId.value = campaign.id;
  errorMsg.value = '';
  infoMsg.value = '';
  try {
    await apiRequest(`/org-auth/agent/campaigns/${campaign.id}/cancel`, { method: 'POST' });
    infoMsg.value = 'Campaign cancelled.';
    await fetchAgentCampaigns();
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    launchingCampaignId.value = null;
  }
}

async function linkAgentPhoneNumber() {
  if (!agentPhoneForm.value.phone_number.trim()) {
    errorMsg.value = 'Enter an Exotel phone number to link.';
    return;
  }
  isLinkingAgentPhone.value = true;
  errorMsg.value = '';
  infoMsg.value = '';
  try {
    agentPhoneLink.value = await apiRequest('/org-auth/agent/phone-link', {
      method: 'POST',
      body: JSON.stringify({ phone_number: agentPhoneForm.value.phone_number.trim() }),
    });
    infoMsg.value = 'Phone number linked to the Agent runtime.';
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isLinkingAgentPhone.value = false;
  }
}

async function unlinkAgentPhoneNumber() {
  isLinkingAgentPhone.value = true;
  errorMsg.value = '';
  infoMsg.value = '';
  try {
    agentPhoneLink.value = await apiRequest('/org-auth/agent/phone-link', {
      method: 'DELETE',
    });
    infoMsg.value = 'Phone number unlinked from the Agent runtime.';
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isLinkingAgentPhone.value = false;
  }
}

async function uploadAgentDocument() {
  if (!agentUploadForm.value.name.trim()) {
    errorMsg.value = 'Document name is required.';
    return;
  }
  if (!agentUploadFile.value) {
    errorMsg.value = 'Choose a document or policy file first.';
    return;
  }
  isUploadingAgentDocument.value = true;
  errorMsg.value = '';
  infoMsg.value = '';
  try {
    const contentBase64 = await readFileAsBase64(agentUploadFile.value);
    await apiRequest('/org-auth/agent/documents/upload', {
      method: 'POST',
      body: JSON.stringify({
        name: agentUploadForm.value.name.trim(),
        document_type: agentUploadForm.value.document_type,
        description: agentUploadForm.value.description || null,
        tags: agentUploadForm.value.tags || null,
        filename: agentUploadFile.value.name,
        content_type: agentUploadFile.value.type || 'application/octet-stream',
        content_base64: contentBase64,
      }),
    });
    infoMsg.value = 'Document uploaded, chunked, embedded, and indexed for the agent.';
    agentUploadForm.value = { name: '', document_type: 'policy', description: '', tags: '' };
    agentUploadFile.value = null;
    await fetchAgentDocuments();
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isUploadingAgentDocument.value = false;
  }
}

async function reviewAgentDocument(document, action) {
  if (!document?.id) {
    return;
  }
  isReviewingAgentDocument.value = true;
  errorMsg.value = '';
  infoMsg.value = '';
  try {
    await apiRequest(`/org-auth/agent/documents/${document.id}/${action}`, {
      method: 'POST',
      body: JSON.stringify({ notes: `${action === 'approve' ? 'Approved' : 'Rejected'} from Agent Studio.` }),
    });
    infoMsg.value = action === 'approve'
      ? 'Document approved. Its active chunks are now available to the agent.'
      : 'Document rejected. Its chunks are blocked from agent retrieval.';
    await fetchAgentDocuments();
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isReviewingAgentDocument.value = false;
  }
}

async function testAgentLatency() {
  if (!agentTestQuery.value.trim()) {
    errorMsg.value = 'Enter a test question first.';
    return;
  }
  isTestingAgentLatency.value = true;
  agentLatencyResult.value = null;
  errorMsg.value = '';
  try {
    agentLatencyResult.value = await apiRequest('/org-auth/agent/runtime/latency-test', {
      method: 'POST',
      body: JSON.stringify({
        query: agentTestQuery.value,
        response_language: agentVoiceLanguage.value,
        target_ms: 800,
      }),
    });
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isTestingAgentLatency.value = false;
  }
}

function appendVoiceEvent(event) {
  voiceEvents.value = [
    {
      id: `${Date.now()}-${Math.random()}`,
      created_at: new Date().toLocaleTimeString(),
      ...event,
    },
    ...voiceEvents.value,
  ].slice(0, 12);
}

function base64ToBytes(value) {
  const binary = window.atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function playVoiceAudio(force = false) {
  if (!voiceAudioChunks.value.length || (voiceReceivedStreamingAudio.value && !force)) {
    return;
  }
  const audioBlob = new Blob(voiceAudioChunks.value.map(base64ToBytes), {
    type: `audio/${agentRuntimeStatus.value?.tts?.audio_format || 'wav'}`,
  });
  const audio = new Audio(URL.createObjectURL(audioBlob));
  audio.play().catch(() => {
    appendVoiceEvent({ type: 'audio_blocked', text: 'Browser blocked autoplay. Click Test Voice again or allow audio playback.' });
  });
  voiceAudioChunks.value = [];
}

function resetVoiceAudioPlayback() {
  voiceAudioChunks.value = [];
  voiceTtsFirstAudioMs.value = null;
  voiceStreamingPlaybackStarted.value = false;
  voiceStreamingPlaybackFailed.value = false;
  voiceReceivedStreamingAudio.value = false;
  voiceChunkQueue = [];
  if (voiceChunkAudio) {
    voiceChunkAudio.pause();
    voiceChunkAudio = null;
  }
}

function playNextVoiceChunk() {
  if (voiceChunkAudio || voiceStreamingPlaybackFailed.value || !voiceChunkQueue.length) {
    return;
  }
  const chunk = voiceChunkQueue.shift();
  const audioBlob = new Blob([base64ToBytes(chunk)], {
    type: `audio/${agentRuntimeStatus.value?.tts?.audio_format || 'wav'}`,
  });
  const objectUrl = URL.createObjectURL(audioBlob);
  const audio = new Audio(objectUrl);
  voiceChunkAudio = audio;
  audio.onplay = () => {
    voiceStreamingPlaybackStarted.value = true;
  };
  audio.onended = () => {
    URL.revokeObjectURL(objectUrl);
    voiceChunkAudio = null;
    playNextVoiceChunk();
  };
  audio.onerror = () => {
    URL.revokeObjectURL(objectUrl);
    voiceChunkAudio = null;
    voiceStreamingPlaybackFailed.value = true;
    voiceChunkQueue = [];
  };
  audio.play().catch(() => {
    URL.revokeObjectURL(objectUrl);
    voiceChunkAudio = null;
    voiceStreamingPlaybackFailed.value = true;
    voiceChunkQueue = [];
  });
}

function enqueueVoiceAudioChunk(base64Audio) {
  if (voiceStreamingPlaybackFailed.value) {
    return;
  }
  voiceChunkQueue.push(base64Audio);
  playNextVoiceChunk();
}

function handleVoiceRuntimeMessage(raw) {
  const message = JSON.parse(raw.data);
  if (message.type === 'runtime_status') {
    agentRuntimeStatus.value = message;
  }
  if (message.type === 'stt_transcript') {
    voiceTranscript.value = message.is_final
      ? `${voiceTranscript.value} ${message.text}`.trim()
      : message.text;
  }
  if (message.type === 'stt_finished') {
    resetVoiceAudioPlayback();
    voiceTranscript.value = message.text || voiceTranscript.value;
    appendVoiceEvent({ type: 'TRANSCRIPT', text: voiceTranscript.value || '(empty transcript)' });
  }
  if (message.type === 'agent_answer') {
    voiceAgentAnswer.value = message.answer || '';
    // Show intent classification debug info
    const intent = message.intent || {};
    if (intent.type) {
      appendVoiceEvent({
        type: 'INTENT',
        text: `${intent.type} (${Math.round((intent.confidence || 0) * 100)}%) — ${intent.reason || ''}`,
        detail: intent.should_retrieve ? 'Retrieval: used' : `Retrieval: skipped — ${intent.retrieval_skipped_reason || 'not needed'}`,
        classifier: intent.classifier || 'unknown',
      });
    }
    appendVoiceEvent({ type: message.refused ? 'REFUSAL' : 'ANSWER', text: message.answer || '' });
  }
  if (message.type === 'agent_sentence') {
    appendVoiceEvent({
      type: 'ANSWER_SENTENCE',
      text: message.sentence || '',
      detail: message.cache_hit ? 'Served from semantic cache' : `First sentence: ${message.first_sentence_ms ?? 'n/a'}ms`,
    });
  }
  if (message.type === 'tts_warmed') {
    const purpose = message.purpose || 'answer';
    appendVoiceEvent({
      type: 'TTS_WARMED',
      text: `${purpose} TTS warmed in ${message.connected_ms ?? 'n/a'}ms`,
      detail: `Language: ${message.language || agentVoiceLanguage.value}`,
    });
  }
  if (message.type === 'tts_started') {
    const purpose = message.purpose || 'answer';
    appendVoiceEvent({
      type: 'TTS_STARTED',
      text: `${purpose} TTS request sent in ${message.request_sent_ms ?? 'n/a'}ms`,
      detail: message.warm_connection
        ? `Using warmed Sarvam TTS stream. Initial warmup: ${message.warm_connected_ms ?? 'n/a'}ms`
        : `Sarvam TTS request opened in ${message.connected_ms ?? 'n/a'}ms`,
    });
  }
  if (message.type === 'tts_first_audio') {
    const purpose = message.purpose || 'answer';
    if (purpose === 'answer') {
      voiceTtsFirstAudioMs.value = message.first_audio_latency_ms ?? null;
    }
    appendVoiceEvent({
      type: 'TTS_FIRST_AUDIO',
      text: `${purpose} first TTS audio in ${message.first_audio_latency_ms ?? 'n/a'}ms`,
      detail: purpose === 'answer'
        ? (Number(message.first_audio_latency_ms) <= 800 ? 'Within 800ms target' : 'Over 800ms target')
        : 'Filler audio does not count as final answer latency',
    });
  }
  if (message.type === 'tts_audio' && message.audio_base64) {
    voiceReceivedStreamingAudio.value = true;
    voiceAudioChunks.value.push(message.audio_base64);
    enqueueVoiceAudioChunk(message.audio_base64);
  }
  if (message.type === 'tts_done') {
    if (message.done_reason === 'idle_after_audio') {
      appendVoiceEvent({
        type: 'TTS_SEGMENT_DONE',
        text: `${message.purpose || 'answer'} TTS segment released after audio idle`,
      });
    }
    if (voiceStreamingPlaybackFailed.value) {
      playVoiceAudio(true);
    } else {
      voiceAudioChunks.value = [];
    }
  }
  if (message.type === 'tts_error') {
    appendVoiceEvent({ type: 'TTS_ERROR', text: message.error_message || 'Voice synthesis unavailable' });
  }
  if (message.type === 'tts_warm_error') {
    appendVoiceEvent({ type: 'TTS_WARM_ERROR', text: message.error_message || 'Unable to warm TTS stream' });
  }
  if (message.type === 'barge_in_detected') {
    resetVoiceAudioPlayback();
    appendVoiceEvent({ type: 'BARGE_IN', text: 'User interrupted — agent stopped speaking' });
  }
  if (message.type === 'turn_complete') {
    const filler = message.filler_played ? ' (filler played)' : '';
    appendVoiceEvent({ type: 'TURN_COMPLETE', text: `Turn done in ${message.total_ms ?? 'n/a'}ms${filler}`, detail: `Context: ${message.context_source ?? 'unknown'}` });
  }
  if (message.type?.endsWith('_error') && message.type !== 'tts_error') {
    appendVoiceEvent({ type: message.type.toUpperCase(), text: message.error || message.error_message || 'Voice runtime error' });
  }
}

function stopVoiceSession() {
  if (voiceRecorder.value && voiceRecorder.value.state !== 'inactive') {
    voiceRecorder.value.stop();
  }
  if (voiceMediaStream.value) {
    voiceMediaStream.value.getTracks().forEach((track) => track.stop());
  }
  if (voiceSocket.value && voiceSocket.value.readyState === WebSocket.OPEN) {
    voiceSocket.value.send(JSON.stringify({ type: 'stop' }));
  }
  isVoiceSessionActive.value = false;
  voiceRecorder.value = null;
  voiceMediaStream.value = null;
}

async function startVoiceSession() {
  const token = localStorage.getItem(ORG_ACCESS_TOKEN_KEY);
  if (!token) {
    errorMsg.value = 'Sign in again before starting voice runtime.';
    return;
  }
  isStartingVoiceSession.value = true;
  errorMsg.value = '';
  voiceTranscript.value = '';
  voiceAgentAnswer.value = '';
  resetVoiceAudioPlayback();
  try {
    const wsUrl = `${API_BASE_URL.replace(/^http/, 'ws')}/org-auth/agent/voice/ws?token=${encodeURIComponent(token)}`;
    const socket = new WebSocket(wsUrl);
    voiceSocket.value = socket;
    socket.onmessage = handleVoiceRuntimeMessage;
    socket.onerror = () => {
      errorMsg.value = 'Voice runtime socket failed.';
      isVoiceSessionActive.value = false;
    };
    socket.onclose = () => {
      isVoiceSessionActive.value = false;
    };
    await new Promise((resolve, reject) => {
      socket.onopen = resolve;
      socket.onerror = reject;
    });
    socket.send(JSON.stringify({ type: 'config', language: agentVoiceLanguage.value }));
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    voiceMediaStream.value = stream;
    const recorder = new MediaRecorder(stream);
    voiceRecorder.value = recorder;
    recorder.ondataavailable = async (event) => {
      if (event.data.size && socket.readyState === WebSocket.OPEN) {
        socket.send(await event.data.arrayBuffer());
      }
    };
    recorder.start(250);
    isVoiceSessionActive.value = true;
    appendVoiceEvent({ type: 'voice_started', text: 'Voice session started.' });
  } catch (error) {
    errorMsg.value = error.message || 'Unable to start voice session.';
    stopVoiceSession();
  } finally {
    isStartingVoiceSession.value = false;
  }
}

async function sendTextToVoiceRuntime() {
  if (!agentTestQuery.value.trim()) {
    errorMsg.value = 'Enter a test question first.';
    return;
  }
  resetVoiceAudioPlayback();
  voiceTranscript.value = agentTestQuery.value;
  voiceAgentAnswer.value = '';
  let socket = voiceSocket.value;
  if (!socket || socket.readyState === WebSocket.CLOSED || socket.readyState === WebSocket.CLOSING) {
    const token = localStorage.getItem(ORG_ACCESS_TOKEN_KEY);
    const wsUrl = `${API_BASE_URL.replace(/^http/, 'ws')}/org-auth/agent/voice/ws?token=${encodeURIComponent(token)}`;
    socket = new WebSocket(wsUrl);
    voiceSocket.value = socket;
    socket.onmessage = handleVoiceRuntimeMessage;
    socket.onclose = () => {
      isVoiceSessionActive.value = false;
      if (voiceSocket.value === socket) {
        voiceSocket.value = null;
      }
    };
    await new Promise((resolve, reject) => {
      socket.onopen = resolve;
      socket.onerror = reject;
    });
    socket.send(JSON.stringify({ type: 'config', language: agentVoiceLanguage.value }));
    isVoiceSessionActive.value = true;
  }
  socket.send(JSON.stringify({ type: 'text_query', text: agentTestQuery.value, language: agentVoiceLanguage.value }));
}

async function testAgentRetrieval() {
  if (!agentTestQuery.value.trim()) {
    errorMsg.value = 'Enter a test question first.';
    return;
  }
  isTestingAgentRetrieval.value = true;
  agentTestRetrieval.value = null;
  errorMsg.value = '';
  try {
    agentTestRetrieval.value = await apiRequest('/org-auth/agent/test-retrieval', {
      method: 'POST',
      body: JSON.stringify({ query: agentTestQuery.value, top_k: 5 }),
    });
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isTestingAgentRetrieval.value = false;
  }
}

async function testAgentAnswer() {
  if (!agentTestQuery.value.trim()) {
    errorMsg.value = 'Enter a test question first.';
    return;
  }
  isTestingAgentAnswer.value = true;
  agentTestAnswer.value = null;
  errorMsg.value = '';
  try {
    agentTestAnswer.value = await apiRequest('/org-auth/agent/test-answer', {
      method: 'POST',
      body: JSON.stringify({ query: agentTestQuery.value, top_k: 5, response_language: agentVoiceLanguage.value }),
    });
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isTestingAgentAnswer.value = false;
  }
}

async function beginZohoOAuth() {
  isConnectingCrm.value = true;
  errorMsg.value = '';
  infoMsg.value = 'Redirecting to Zoho...';
  try {
    const payload = await apiRequest('/org-auth/crm/zoho/authorize');
    window.location.href = payload.auth_url;
  } catch (error) {
    errorMsg.value = error.message;
    infoMsg.value = '';
    isConnectingCrm.value = false;
  }
}

async function indexSelectedColumns() {
  const selections = databaseSchema.value
    .map((table) => ({
      table: table.name,
      schema: table.schema,
      columns: table.columns.filter((column) => column.selected).map((column) => column.name),
    }))
    .filter((table) => table.columns.length);

  if (!selections.length) {
    errorMsg.value = 'Select at least one column to index.';
    return;
  }

  isIndexingDatabase.value = true;
  errorMsg.value = '';
  infoMsg.value = '';
  try {
    const payload = await apiRequest('/org-auth/database/index', {
      method: 'POST',
      body: JSON.stringify({
        provider: databaseForm.value.provider,
        row_limit: databaseForm.value.row_limit,
        selections,
      }),
    });
    infoMsg.value = `Indexed ${payload.indexed_points} records from ${payload.tables.length} table(s).`;
    await fetchDatabaseStatus();
    closeDatabaseModal();
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isIndexingDatabase.value = false;
  }
}

async function handleCredentialResponse(response) {
  if (!response?.credential) {
    errorMsg.value = 'Google did not return an ID token';
    return;
  }

  isAuthenticating.value = true;
  errorMsg.value = '';
  infoMsg.value = 'Verifying your organization access...';
  pendingCredential.value = response.credential;
  candidateOrganizations.value = [];

  try {
    const payload = await apiRequest('/org-auth/google/login', {
      method: 'POST',
      body: JSON.stringify({ id_token: response.credential }),
    });
    await initializeOrganizationMfa(payload);
  } catch (error) {
    clearSession();
    authState.value = 'login';
    if (error.code === 'organization_selection_required') {
      candidateOrganizations.value = error.detail.organizations || [];
      pendingCredential.value = response.credential;
      errorMsg.value = error.message;
      infoMsg.value = 'Choose your organization to complete sign-in.';
    } else {
      errorMsg.value = error.message;
      infoMsg.value = '';
    }
  } finally {
    isAuthenticating.value = false;
  }
}

async function selectOrganization(organizationId) {
  if (!pendingCredential.value) {
    errorMsg.value = 'Google sign-in expired. Try again.';
    return;
  }

  isAuthenticating.value = true;
  errorMsg.value = '';
  infoMsg.value = 'Verifying your organization access...';

  try {
    const payload = await apiRequest('/org-auth/google/login', {
      method: 'POST',
      body: JSON.stringify({
        organization_id: organizationId,
        id_token: pendingCredential.value,
      }),
    });
    await initializeOrganizationMfa(payload);
  } catch (error) {
    errorMsg.value = error.message;
    infoMsg.value = '';
  } finally {
    isAuthenticating.value = false;
  }
}

async function initializeGoogleButton() {
  if (!authConfig.value?.google_client_id || !googleButtonRef.value) {
    return;
  }

  await googleScriptPromise();
  await nextTick();

  window.google.accounts.id.initialize({
    client_id: authConfig.value.google_client_id,
    callback: handleCredentialResponse,
    auto_select: false,
    cancel_on_tap_outside: true,
    context: 'signin',
  });

  googleButtonRef.value.innerHTML = '';
  window.google.accounts.id.renderButton(googleButtonRef.value, {
    theme: 'outline',
    size: 'large',
    type: 'standard',
    shape: 'pill',
    text: 'continue_with',
    width: Math.min(392, Math.max(280, googleButtonRef.value.clientWidth || 392)),
    logo_alignment: 'left',
  });
}

async function bootstrapOrganizationSession() {
  const token = localStorage.getItem(ORG_ACCESS_TOKEN_KEY);
  if (!token) {
    authState.value = 'login';
    return;
  }

  try {
    const payload = await apiRequest('/org-auth/me');
    currentUser.value = payload.user;
    currentOrganization.value = payload.organization;
    authState.value = 'ready';
    await fetchMembers();
    await fetchDatabaseProviders();
    await fetchDatabaseStatus();
    await fetchCrmProviders();
    await fetchCrmStatus();
    await fetchErpProviders();
    await fetchErpStatus();
    await fetchShippingProviders();
    await fetchShippingStatus();
    await fetchZohoDeskStatus();
    await fetchToolkitBuilders();
  } catch (_) {
    clearSession();
    authState.value = 'login';
  }
}

async function loadAuthConfig() {
  try {
    authConfig.value = await apiRequest('/org-auth/config');
    if (!authConfig.value.google_login_enabled) {
      errorMsg.value = 'Google organization login is not configured yet.';
    }
  } catch (error) {
    errorMsg.value = error.message;
  }
}

async function hydrateCrmOauthResultFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const crmOauth = params.get('crm_oauth');
  const provider = params.get('provider');
  const message = params.get('message');
  if (!crmOauth || !provider) {
    return;
  }

  if (crmOauth === 'success') {
    infoMsg.value = `${provider} CRM connected successfully.`;
    await fetchCrmStatus();
  } else {
    errorMsg.value = message || `${provider} CRM connection failed.`;
  }

  params.delete('crm_oauth');
  params.delete('provider');
  params.delete('message');
  const nextQuery = params.toString();
  const nextUrl = `${window.location.pathname}${nextQuery ? `?${nextQuery}` : ''}${window.location.hash || ''}`;
  window.history.replaceState({}, '', nextUrl);
}

async function handleLogout() {
  try {
    await apiRequest('/org-auth/logout', { method: 'POST' });
  } catch (_) {
    // local logout should still proceed
  } finally {
    clearSession();
    authState.value = 'login';
    infoMsg.value = '';
    await initializeGoogleButton();
  }
}

async function inviteMember() {
  if (!inviteCanSubmit.value) {
    errorMsg.value = inviteValidationMessage.value;
    return;
  }

  isSavingMember.value = true;
  errorMsg.value = '';
  infoMsg.value = '';
  try {
    await apiRequest('/org-auth/members', {
      method: 'POST',
      body: JSON.stringify(inviteForm.value),
    });
    inviteForm.value = { email: '', full_name: '', role: 'member' };
    infoMsg.value = 'Member added. They can now sign in with Google and verify TOTP.';
    await fetchMembers();
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    isSavingMember.value = false;
  }
}

async function saveMember(member) {
  updatingMemberId.value = member.id;
  errorMsg.value = '';
  infoMsg.value = '';
  try {
    await apiRequest(`/org-auth/members/${member.id}`, {
      method: 'PATCH',
      body: JSON.stringify({
        role: member.role,
        status: member.status,
      }),
    });
    infoMsg.value = `Updated ${member.email}`;
    await fetchMembers();
  } catch (error) {
    errorMsg.value = error.message;
  } finally {
    updatingMemberId.value = null;
  }
}

onMounted(async () => {
  applyTheme(localStorage.getItem(ORG_THEME_MODE_KEY) || 'light');
  await loadAuthConfig();
  await bootstrapOrganizationSession();
  await hydrateCrmOauthResultFromUrl();
  if (authState.value !== 'ready') {
    await initializeGoogleButton();
  }
});

onBeforeUnmount(() => {
  stopVoiceSession();
  if (voiceSocket.value) {
    voiceSocket.value.close();
  }
  if (orgShellRef.value) {
    orgShellRef.value.style.removeProperty('--cursor-x');
    orgShellRef.value.style.removeProperty('--cursor-y');
  }
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

    <main v-if="authState !== 'ready'" class="login-layout">
      <div class="brand-block">
        <h1>NOKVO</h1>
        <p>Welcome back to effortless quality.</p>
      </div>

      <div class="login-card">
        <div v-if="errorMsg" class="message error">{{ errorMsg }}</div>
        <div v-else-if="infoMsg" class="message info">{{ infoMsg }}</div>

        <div v-if="authState === 'login' || authState === 'loading'" class="google-action">
          <div
            v-if="authConfig?.google_login_enabled"
            ref="googleButtonRef"
            class="google-button-host"
            :class="{ disabled: isAuthenticating }"
          ></div>
          <button v-else type="button" class="placeholder-button" disabled>
            Continue with Google
          </button>
        </div>

        <p v-if="authState === 'login' || authState === 'loading'" class="login-help">
          Access is restricted to organization members on an approved work email domain.
        </p>

        <div v-if="candidateOrganizations.length && authState === 'login'" class="org-selector">
          <div class="selector-head">
            <strong>Select your organization</strong>
            <span>{{ candidateOrganizations.length }} matches found for this domain</span>
          </div>
          <button
            v-for="organization in candidateOrganizations"
            :key="organization.id"
            type="button"
            class="org-option"
            :disabled="isAuthenticating"
            @click="selectOrganization(organization.id)"
          >
            <span class="org-option-main">
              <strong>{{ organization.name }}</strong>
              <small>{{ organization.environment }} · {{ organization.region }}</small>
            </span>
            <span class="org-option-side">{{ organization.admin_email }}</span>
          </button>
        </div>

        <div v-else-if="authState === 'mfa_setup'" class="mfa-panel">
          <div class="mfa-head">
            <strong>Link Your Authenticator</strong>
            <span>{{ pendingUser?.email }}</span>
          </div>
          <p class="login-help compact">
            Scan this QR with an authenticator for your registered organization email only. Access is issued only after TOTP verification.
          </p>
          <div class="qr-shell">
            <QrcodeVue :value="totpUri" :size="168" level="M" background="#ffffff" foreground="#111111" />
          </div>
          <div class="secret-note">
            Manual entry key: <code>{{ totpSecret }}</code>
          </div>
          <label class="code-label" for="org-totp-setup">6-digit code</label>
          <input
            id="org-totp-setup"
            v-model="totpCode"
            class="totp-input"
            type="text"
            inputmode="numeric"
            maxlength="6"
            placeholder="000000"
          />
          <div class="mfa-actions">
            <button type="button" class="ghost-button" :disabled="isAuthenticating" @click="resetLoginState">Cancel</button>
            <button type="button" class="primary-button" :disabled="isAuthenticating" @click="handleTotpVerify">
              {{ isAuthenticating ? 'Verifying...' : 'Verify & Continue' }}
            </button>
          </div>
        </div>

        <div v-else-if="authState === 'mfa_verify'" class="mfa-panel">
          <div class="mfa-head">
            <strong>Enter Organization TOTP</strong>
            <span>{{ pendingUser?.email }}</span>
          </div>
          <p class="login-help compact">
            Use the authenticator already linked to this registered organization email. A different email’s TOTP will not work here.
          </p>
          <label class="code-label" for="org-totp-verify">6-digit code</label>
          <input
            id="org-totp-verify"
            v-model="totpCode"
            class="totp-input"
            type="text"
            inputmode="numeric"
            maxlength="6"
            placeholder="000000"
          />
          <div class="mfa-actions">
            <button type="button" class="ghost-button" :disabled="isAuthenticating" @click="resetLoginState">Back</button>
            <button type="button" class="primary-button" :disabled="isAuthenticating" @click="handleTotpVerify">
              {{ isAuthenticating ? 'Verifying...' : 'Authenticate' }}
            </button>
          </div>
        </div>
      </div>

      <div class="footer-links">
        <p>
          Need access? Contact your organization admin to invite you before you sign in.
        </p>
      </div>
    </main>

    <main v-else class="workspace-layout dashboard-layout">
      <div class="floating-top-nav">
        <nav class="dashboard-nav">
          <div class="dashboard-brand">
            <div class="brand-mark">N</div>
            <div class="brand-copy">
              <strong>NOKVO</strong>
              <span>{{ currentOrganization?.name }}</span>
            </div>
          </div>

          <div class="nav-search">
            <Search :size="18" class="nav-search-icon" />
            <input
              v-model="dashboardQuery"
              type="text"
              :placeholder="dashboardSearchPlaceholder"
            />
          </div>

          <div class="dashboard-nav-actions">
            <button
              type="button"
              class="nav-page-button"
              :class="{ active: currentPage === 'dashboard' }"
              @click="switchPage('dashboard')"
            >
              <Database :size="17" />
              <span>Dashboard</span>
            </button>
            <button
              type="button"
              class="nav-page-button"
              :class="{ active: currentPage === 'tickets' }"
              @click="switchPage('tickets')"
            >
              <Ticket :size="17" />
              <span>Tickets</span>
            </button>
            <button
              type="button"
              class="nav-page-button"
              :class="{ active: currentPage === 'toolkit' }"
              @click="switchPage('toolkit')"
            >
              <Wrench :size="17" />
              <span>Toolkit</span>
            </button>
            <button
              type="button"
              class="nav-page-button"
              :class="{ active: currentPage === 'agent' }"
              @click="switchPage('agent')"
            >
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

      <section v-if="currentPage === 'dashboard'" class="dashboard-header">
        <div>
          <h2>Dashboard</h2>
          <p>Manage workspace access, database ingestion, and organization health from one place.</p>
        </div>
        <div class="dashboard-header-actions">
          <button
            v-if="canManageDatabase"
            type="button"
            class="dashboard-primary-button"
            @click="openDatabaseModal"
          >
            <Plus :size="18" />
            Connect Database
          </button>
          <button
            v-if="canManageDatabase"
            type="button"
            class="dashboard-secondary-button"
            @click="openCrmModal"
          >
            <Plus :size="16" />
            Connect CRM
          </button>
          <button
            v-if="canManageDatabase"
            type="button"
            class="dashboard-secondary-button"
            @click="openErpModal"
          >
            <Plus :size="16" />
            Connect ERP
          </button>
          <button
            v-if="canManageDatabase"
            type="button"
            class="dashboard-secondary-button"
            @click="openShippingModal"
          >
            <Plus :size="16" />
            Connect Shipping
          </button>
          <button
            type="button"
            class="dashboard-secondary-button"
            @click="handleLogout"
          >
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
            <span>Database</span>
            <strong>{{ databaseStatus.provider || 'Pending' }}</strong>
          </div>
          <div class="summary-pill">
            <span>ERP</span>
            <strong>{{ erpStatus.provider || 'Pending' }}</strong>
          </div>
          <div class="summary-pill">
            <span>Shipping</span>
            <strong>{{ shippingStatus.provider || 'Pending' }}</strong>
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

      <section v-if="currentPage === 'dashboard'" class="dashboard-section">
        <div class="dashboard-section-head">
          <div>
            <span class="section-kicker">Overview</span>
            <h3>Organization Snapshot</h3>
          </div>
          <p>Workspace status, database sync state, and access health in one glance.</p>
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
            <span class="status-chip active">
              <span class="status-dot"></span>
              Active
            </span>
          </div>

          <p class="organization-description">
            Signed in as {{ currentUser?.email }}. This workspace is scoped to {{ currentOrganization?.email_domain }} and keeps member access and knowledge sync under one organization boundary.
          </p>

          <div class="usage-block">
            <div class="usage-labels">
              <span>Knowledge Coverage</span>
              <strong>{{ databaseStatus.indexed_points || 0 }} indexed points</strong>
            </div>
            <div class="usage-track">
              <div class="usage-fill" :style="{ width: organizationUsageWidth }"></div>
            </div>
          </div>

          <div class="organization-metrics">
            <div>
              <span>Members</span>
              <strong>{{ members.length }}</strong>
            </div>
            <div>
              <span>Database</span>
              <strong>{{ databaseStatus.provider || 'Pending' }}</strong>
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
              <Database :size="18" />
            </div>
            <div>
              <h3>Database Sync</h3>
              <p>{{ databaseStatus.provider || 'No provider connected' }}</p>
            </div>
          </div>
          <dl class="dashboard-detail-list">
            <div>
              <dt>Status</dt>
              <dd>{{ databaseStatus.status || 'not_connected' }}</dd>
            </div>
            <div>
              <dt>Selected Sources</dt>
              <dd>{{ databaseStatus.selected_sources?.length || 0 }}</dd>
            </div>
            <div>
              <dt>Indexed Points</dt>
              <dd>{{ databaseStatus.indexed_points || 0 }}</dd>
            </div>
            <div>
              <dt>Database Name</dt>
              <dd>{{ databaseStatus.database_name || 'Not available' }}</dd>
            </div>
          </dl>
          <button
            v-if="canManageDatabase"
            type="button"
            class="dashboard-inline-button"
            @click="openDatabaseModal"
          >
            Connect DB
          </button>
        </article>

        <article class="dashboard-card compact-card access-card">
          <div class="compact-card-head">
            <div class="compact-icon-shell">
              <Shield :size="18" />
            </div>
            <div>
              <h3>Access Overview</h3>
              <p>{{ currentOrganization?.email_domain }}</p>
            </div>
          </div>
          <dl class="dashboard-detail-list">
            <div>
              <dt>Active Members</dt>
              <dd>{{ activeMemberCount }}</dd>
            </div>
            <div>
              <dt>Invited</dt>
              <dd>{{ invitedMemberCount }}</dd>
            </div>
            <div>
              <dt>Disabled</dt>
              <dd>{{ disabledMemberCount }}</dd>
            </div>
            <div>
              <dt>Your Status</dt>
              <dd>{{ currentUser?.status }}</dd>
            </div>
          </dl>
        </article>

        <article class="dashboard-card compact-card">
          <div class="compact-card-head">
            <div class="compact-icon-shell">
              <Globe2 :size="18" />
            </div>
            <div>
              <h3>ERP Sync</h3>
              <p>{{ erpProviderLabel }}</p>
            </div>
          </div>
          <dl class="dashboard-detail-list">
            <div>
              <dt>Status</dt>
              <dd>{{ erpStatus.status || 'not_connected' }}</dd>
            </div>
            <div>
              <dt>Modules</dt>
              <dd>{{ erpStatus.module_count || 0 }}</dd>
            </div>
            <div>
              <dt>Actions</dt>
              <dd>{{ erpStatus.action_count || 0 }}</dd>
            </div>
            <div>
              <dt>Indexed Points</dt>
              <dd>{{ erpStatus.indexed_points || 0 }}</dd>
            </div>
          </dl>
          <div class="crm-sync-emphasis">
            <span>Namespace</span>
            <strong>{{ erpStatus.folder_path || 'integrations/erp/pending' }}</strong>
          </div>
          <button
            v-if="canManageDatabase"
            type="button"
            class="dashboard-inline-button"
            @click="openErpModal"
          >
            Connect Tally
          </button>
        </article>

        <article class="dashboard-card compact-card crm-sync-card">
          <div class="compact-card-head">
            <div class="compact-icon-shell">
              <Globe2 :size="18" />
            </div>
            <div>
              <h3>CRM Sync</h3>
              <p>{{ crmProviderLabel }}</p>
            </div>
          </div>
          <dl class="dashboard-detail-list">
            <div>
              <dt>Status</dt>
              <dd>{{ crmStatus.status || 'not_connected' }}</dd>
            </div>
            <div>
              <dt>Modules</dt>
              <dd>{{ crmStatus.module_count || 0 }}</dd>
            </div>
            <div>
              <dt>Actions</dt>
              <dd>{{ crmStatus.action_count || 0 }}</dd>
            </div>
            <div>
              <dt>Indexed Points</dt>
              <dd>{{ crmStatus.indexed_points || 0 }}</dd>
            </div>
          </dl>
          <div class="crm-sync-emphasis">
            <span>Namespace</span>
            <strong>{{ crmStatus.folder_path || 'integrations/crm/pending' }}</strong>
          </div>
          <button
            v-if="canManageDatabase"
            type="button"
            class="dashboard-inline-button"
            @click="openCrmModal"
          >
              Connect CRM
          </button>
        </article>

        <article class="dashboard-card compact-card">
          <div class="compact-card-head">
            <div class="compact-icon-shell">
              <Globe2 :size="18" />
            </div>
            <div>
              <h3>Shipping Sync</h3>
              <p>{{ shippingProviderLabel }}</p>
            </div>
          </div>
          <dl class="dashboard-detail-list">
            <div>
              <dt>Status</dt>
              <dd>{{ shippingStatus.status || 'not_connected' }}</dd>
            </div>
            <div>
              <dt>Modules</dt>
              <dd>{{ shippingStatus.module_count || 0 }}</dd>
            </div>
            <div>
              <dt>Actions</dt>
              <dd>{{ shippingStatus.action_count || 0 }}</dd>
            </div>
            <div>
              <dt>Indexed Points</dt>
              <dd>{{ shippingStatus.indexed_points || 0 }}</dd>
            </div>
          </dl>
          <div class="crm-sync-emphasis">
            <span>Namespace</span>
            <strong>{{ shippingStatus.folder_path || 'integrations/shipping/pending' }}</strong>
          </div>
          <button
            v-if="canManageDatabase"
            type="button"
            class="dashboard-inline-button"
            @click="openShippingModal"
          >
            Connect Shiprocket
          </button>
        </article>

        </div>
      </section>

      <section v-if="currentPage === 'dashboard'" class="dashboard-section">
        <div class="dashboard-section-head">
          <div>
            <span class="section-kicker">Operations</span>
            <h3>Workspace Controls</h3>
          </div>
          <p>Invite members, adjust access, and curate indexed organization data.</p>
        </div>

        <div class="dashboard-grid control-grid">
        <article v-if="canManageMembers" class="dashboard-card invite-card">
          <div class="compact-card-head">
            <div class="compact-icon-shell">
              <UserPlus :size="18" />
            </div>
            <div>
              <h3>Invite Member</h3>
              <p>Add admins or members under the same verified organization domain.</p>
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
                {{ isSavingMember ? 'Inviting...' : 'Add Member' }}
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
              <Globe2 :size="18" />
            </div>
            <div>
              <h3>Workspace Profile</h3>
              <p>Current organization metadata and signed-in identity.</p>
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
              <span>Admin Email</span>
              <strong>{{ currentOrganization?.admin_email || 'Not set' }}</strong>
            </div>
            <div>
              <span>Signed In User</span>
              <strong>{{ currentUser?.full_name || currentUser?.email }}</strong>
            </div>
          </div>
        </article>

        <article class="dashboard-card wide-card members-card">
          <div class="members-card-head">
            <div>
              <h3>Organization Members</h3>
              <p>Searchable access roster for your current workspace.</p>
            </div>
            <div class="members-summary">
              <span>{{ filteredMembers.length }} visible</span>
              <span>{{ members.length }} total</span>
            </div>
          </div>

          <div v-if="isLoadingMembers" class="empty-state">Loading members...</div>
          <div v-else-if="!filteredMembers.length" class="empty-state">
            No members match the current filter.
          </div>
          <div v-else class="member-table dashboard-member-table">
            <div class="member-row member-head">
              <span>Member</span>
              <span>Role</span>
              <span>Status</span>
              <span>Action</span>
            </div>
            <div v-for="member in filteredMembers" :key="member.id" class="member-row">
              <div class="member-meta">
                <strong>{{ member.full_name || 'Unnamed member' }}</strong>
                <small>{{ member.email }}</small>
              </div>
              <select v-model="member.role" :disabled="!canManageMembers">
                <option value="member">Member</option>
                <option value="manager">Manager</option>
                <option value="viewer">Viewer</option>
                <option value="admin">Admin</option>
              </select>
              <select v-model="member.status" :disabled="!canManageMembers">
                <option value="invited">Invited</option>
                <option value="active">Active</option>
                <option value="disabled">Disabled</option>
              </select>
              <button
                v-if="canManageMembers"
                type="button"
                class="save-member"
                :disabled="updatingMemberId === member.id"
                @click="saveMember(member)"
              >
                {{ updatingMemberId === member.id ? 'Saving...' : 'Save' }}
              </button>
              <span v-else class="readonly-tag">Read only</span>
            </div>
          </div>
        </article>
        </div>
      </section>

      <section v-if="currentPage === 'tickets'" class="dashboard-section tickets-page">
        <div class="dashboard-section-head">
          <div>
            <span class="section-kicker">Zoho Desk</span>
            <h3>Tickets</h3>
          </div>
          <p>Ticket operations use the Zoho OAuth grant created when CRM is connected.</p>
        </div>

        <div class="dashboard-grid tickets-grid">
          <article class="dashboard-card ticket-console-card">
            <div class="compact-card-head">
              <div class="compact-icon-shell">
                <Ticket :size="18" />
              </div>
              <div>
                <h3>Desk Ticket Console</h3>
                <p>{{ crmStatus.provider === 'zoho' ? 'Zoho OAuth grant available' : 'Connect Zoho CRM to enable Desk tickets' }}</p>
              </div>
            </div>

            <dl class="dashboard-detail-list">
              <div>
                <dt>Desk Status</dt>
                <dd>{{ zohoDeskStatusLabel }}</dd>
              </div>
              <div>
                <dt>Account</dt>
                <dd>{{ zohoDeskStatus.account_name || crmStatus.account_name || 'Not connected' }}</dd>
              </div>
              <div>
                <dt>Indexed Actions</dt>
                <dd>{{ zohoDeskStatus.action_count || 0 }}</dd>
              </div>
              <div>
                <dt>Namespace</dt>
                <dd>{{ zohoDeskStatus.folder_path || 'integrations/zoho-desk' }}</dd>
              </div>
            </dl>

            <div class="ticket-page-note">
              <strong>{{ crmStatus.provider === 'zoho' ? 'Ready for ticket workflows' : 'Zoho CRM required' }}</strong>
              <p>
                {{ crmStatus.provider === 'zoho'
                  ? 'Create and update ticket actions are available through the Zoho Desk API using the connected CRM OAuth grant.'
                  : 'Use Connect CRM on the dashboard to authorize Zoho once, then return here for ticket workflows.' }}
              </p>
            </div>

            <button
              v-if="crmStatus.provider !== 'zoho' && canManageDatabase"
              type="button"
              class="dashboard-inline-button"
              @click="openZohoCrmModal"
            >
              Connect Zoho CRM
            </button>
          </article>
        </div>
      </section>

      <section v-if="currentPage === 'toolkit'" class="dashboard-section toolkit-page">
        <div class="dashboard-section-head">
          <div>
            <span class="section-kicker">MCP Registry</span>
            <h3>Toolkit Generator</h3>
          </div>
          <p>Generate draft MCP tools from indexed integration context, review the JSON, and register approved tools for the selected integration.</p>
        </div>

        <div v-if="canManageDatabase && toolkitBuilderOptions.length" class="dashboard-grid toolkit-page-grid">
          <article class="dashboard-card toolkit-builder-card">
            <div class="compact-card-head">
              <div class="compact-icon-shell">
                <Wrench :size="18" />
              </div>
              <div>
                <h3>Integration Toolkit Builder</h3>
                <p>Each builder is scoped to one connected integration and its provider capability.</p>
              </div>
            </div>

            <div class="db-form-block">
              <label class="db-label" for="toolkit-connected-integration">Toolkit Builder</label>
              <div class="toolkit-list">
                <button
                  v-for="builder in toolkitBuilderOptions"
                  :key="builder.builder_key"
                  type="button"
                  class="toolkit-list-row"
                  @click="selectedToolkitIntegrationValue = builder.builder_key"
                >
                  <span>
                    <strong>{{ builder.label }}</strong>
                    <small>{{ builder.detail }} · {{ builder.execution_backend }}</small>
                  </span>
                  <span class="status-chip">{{ builder.resource_model }}</span>
                </button>
              </div>
              <select id="toolkit-connected-integration" v-model="selectedToolkitIntegrationValue" class="db-input">
                <option
                  v-for="builder in toolkitBuilderOptions"
                  :key="builder.builder_key"
                  :value="builder.builder_key"
                >
                  {{ builder.label }} - {{ builder.detail }}
                </option>
              </select>
            </div>

            <div v-if="selectedToolkitBuilder" class="toolkit-registry-summary">
              <strong>{{ selectedToolkitBuilder.label }}</strong>
              <span>{{ selectedToolkitBuilder.execution_backend }} / {{ selectedToolkitBuilder.resource_model }}</span>
            </div>

            <div v-if="selectedToolkitBuilder" class="ticket-page-note">
              <strong>{{ selectedToolkitBuilder.description }}</strong>
              <p>Supported operations: {{ (selectedToolkitBuilder.supported_operations || []).join(', ') }}</p>
            </div>

            <div class="db-form-block">
              <label class="db-label" for="toolkit-prompt">Natural Language Tool Request</label>
              <textarea
                id="toolkit-prompt"
                v-model="toolkitForm.nlp_prompt"
                class="db-input toolkit-textarea"
                :placeholder="selectedToolkitBuilder?.execution_backend === 'database_sql'
                  ? 'Example: Build a tool to retrieve customer details using phone number.'
                  : 'Example: Create a tool that checks Shiprocket courier serviceability and returns the cheapest courier option.'"
              ></textarea>
            </div>

            <div class="db-form-block">
              <label class="db-label" for="toolkit-system-prompt">System Prompt Override (optional)</label>
              <textarea
                id="toolkit-system-prompt"
                v-model="toolkitForm.system_prompt"
                class="db-input toolkit-textarea compact"
                placeholder="Leave blank to use the default intelligent MCP tool creator prompt."
              ></textarea>
            </div>

            <div class="db-actions">
              <button type="button" class="primary-button" :disabled="isGeneratingToolkit" @click="generateToolkitDraft">
                {{ isGeneratingToolkit ? 'Generating...' : 'Generate Draft' }}
              </button>
            </div>
          </article>

          <article class="dashboard-card toolkit-registry-card">
            <div class="members-card-head">
              <div>
                <h3>Registry</h3>
                <p>{{ selectedToolkitBuilder?.label || `${toolkitForm.integration_type} / ${toolkitForm.provider}` }}</p>
              </div>
              <span class="status-chip">{{ visibleToolkitTools.length }} / {{ filteredToolkitTools.length }} tools</span>
            </div>

            <div class="toolkit-registry-summary">
              <strong>Pending Drafts</strong>
              <span>{{ visibleToolkitDrafts.length }} / {{ filteredToolkitDrafts.length }} shown</span>
            </div>

            <div class="toolkit-list">
              <div v-if="!filteredToolkitDrafts.length" class="empty-state compact">No pending drafts for this integration.</div>
              <button
                v-for="draft in visibleToolkitDrafts"
                :key="draft.id"
                type="button"
                class="toolkit-list-row"
                @click="toolkitDraft = draft"
              >
                <span>
                  <strong>{{ draft.tool?.title || draft.tool?.name }}</strong>
                  <small>{{ draft.nlp_prompt }}</small>
                </span>
                <span class="status-chip">{{ draft.status }}</span>
              </button>
              <div v-if="hiddenToolkitDraftCount" class="toolkit-list-more">
                {{ hiddenToolkitDraftCount }} more drafts hidden by the compact registry view.
              </div>
            </div>

            <div class="toolkit-list registered">
              <div v-if="!filteredToolkitTools.length" class="empty-state compact">No approved tools registered yet.</div>
              <div v-for="tool in visibleToolkitTools" :key="tool.name" class="toolkit-list-row static">
                <span>
                  <strong>{{ tool.title || tool.name }}</strong>
                  <small>{{ tool.description }}</small>
                </span>
                <span class="status-chip">{{ tool?.mcp?.tool_name || tool.name }}</span>
              </div>
              <div v-if="hiddenToolkitToolCount" class="toolkit-list-more">
                {{ hiddenToolkitToolCount }} more registered tools hidden by the compact registry view.
              </div>
            </div>
          </article>

          <article v-if="toolkitDraft" class="dashboard-card wide-card toolkit-review-card">
            <div class="members-card-head">
              <div>
                <h3>{{ toolkitDraft.tool?.title || toolkitDraft.tool?.name }}</h3>
                <p>{{ toolkitDraft.tool?.description }}</p>
              </div>
              <span class="status-chip">{{ toolkitDraft.status }}</span>
            </div>
            <pre>{{ JSON.stringify(toolkitDraft.tool, null, 2) }}</pre>
            <div v-if="toolkitDraft.status === 'draft'" class="db-actions">
              <button type="button" class="ghost-button" :disabled="isReviewingToolkit" @click="reviewToolkitDraft('reject')">Reject</button>
              <button type="button" class="primary-button" :disabled="isReviewingToolkit" @click="reviewToolkitDraft('approve')">
                {{ isReviewingToolkit ? 'Saving...' : 'Approve And Register' }}
              </button>
            </div>
          </article>
        </div>

        <article v-else class="dashboard-card ticket-console-card">
          <div class="compact-card-head">
            <div class="compact-icon-shell">
              <Shield :size="18" />
            </div>
            <div>
              <h3>{{ canManageDatabase ? 'Connect An Integration' : 'Admin Access Required' }}</h3>
              <p>
                {{ canManageDatabase
                  ? 'Toolkit generation is available after an integration-specific builder becomes available from a connected and indexed integration.'
                  : 'Toolkit generation and MCP registry changes are limited to organization admins.' }}
              </p>
            </div>
          </div>
        </article>
      </section>

      <section v-if="currentPage === 'agent'" class="dashboard-section agent-page">
        <div class="dashboard-section-head">
          <div>
            <span class="section-kicker">Nokvo One Agent</span>
            <h3>Agent Studio</h3>
          </div>
          <p>Prepare tenant knowledge off the call path, test grounded answers, and run Sarvam + Exotel voice sessions from one operator console.</p>
        </div>

        <div v-if="canManageDatabase" class="agent-hero-panel">
          <div>
            <span class="section-kicker">Ingestion Off Hot Path</span>
            <h2>Pre-indexed voice RAG agent</h2>
            <p>Raw files are stored first, then parsed, chunked, embedded, and indexed into the tenant Qdrant collection before any call uses them.</p>
          </div>
          <div class="agent-hero-stats">
            <span><strong>{{ approvedAgentDocumentCount }}</strong> approved docs</span>
            <span><strong>{{ pendingAgentDocumentCount }}</strong> pending review</span>
            <span><strong>{{ agentKnowledgeChunkCount }}</strong> chunks</span>
          </div>
        </div>

        <div v-if="canManageDatabase" class="dashboard-grid agent-page-grid">
          <article class="dashboard-card agent-upload-card">
            <div class="compact-card-head">
              <div class="compact-icon-shell">
                <UploadCloud :size="18" />
              </div>
              <div>
                <h3>Upload Knowledge</h3>
                <p>Name the document, store the raw file in blob, then embed chunks into the tenant collection.</p>
              </div>
            </div>

            <div class="agent-upload-drop">
              <input id="agent-document-file" type="file" @change="handleAgentFileChange" />
              <FileText :size="24" />
              <strong>{{ agentUploadFile?.name || 'Choose policy or document file' }}</strong>
              <span>TXT, Markdown, CSV, JSON, text-readable DOCX/PDF supported in this pass.</span>
            </div>

            <div class="agent-form-grid">
              <div class="db-form-block">
                <label class="db-label" for="agent-document-name">Document Name</label>
                <input id="agent-document-name" v-model="agentUploadForm.name" class="db-input" type="text" placeholder="Refund Policy v1" />
              </div>
              <div class="db-form-block">
                <label class="db-label" for="agent-document-type">Type</label>
                <select id="agent-document-type" v-model="agentUploadForm.document_type" class="db-input">
                  <option value="policy">Policy</option>
                  <option value="faq">FAQ</option>
                  <option value="script">Call Script</option>
                  <option value="compliance">Compliance</option>
                  <option value="product_docs">Product Docs</option>
                  <option value="training">Training</option>
                  <option value="other">Other</option>
                </select>
              </div>
            </div>

            <div class="db-form-block">
              <label class="db-label" for="agent-document-description">Description</label>
              <textarea id="agent-document-description" v-model="agentUploadForm.description" class="db-input toolkit-textarea compact" placeholder="What should agents use this document for?"></textarea>
            </div>

            <div class="db-form-block">
              <label class="db-label" for="agent-document-tags">Tags</label>
              <input id="agent-document-tags" v-model="agentUploadForm.tags" class="db-input" type="text" placeholder="refunds, shipping, escalation" />
            </div>

            <div class="db-actions">
              <button type="button" class="primary-button" :disabled="isUploadingAgentDocument" @click="uploadAgentDocument">
                {{ isUploadingAgentDocument ? 'Uploading...' : 'Upload & Index' }}
              </button>
            </div>
          </article>

          <article class="dashboard-card agent-documents-card">
            <div class="members-card-head">
              <div>
                <h3>Document Lifecycle</h3>
                <p>Documents move through pending, ok, empty, or error. Calls only use already indexed chunks.</p>
              </div>
              <span class="status-chip">{{ filteredAgentDocuments.length }} docs</span>
            </div>

            <div class="agent-document-list">
              <div v-if="isLoadingAgentDocuments" class="empty-state compact">Loading Agent Knowledge documents...</div>
              <div v-else-if="!filteredAgentDocuments.length" class="empty-state compact">No documents uploaded yet.</div>
              <div v-for="document in filteredAgentDocuments" :key="document.id" class="agent-document-row">
                <div class="agent-document-main">
                  <div class="agent-document-icon">
                    <FileText :size="18" />
                  </div>
                  <div>
                    <strong>{{ document.name }}</strong>
                    <small>{{ document.document_type }} · {{ document.chunk_count }} chunks · {{ document.blob_path }}</small>
                    <span v-if="document.last_error" class="agent-warning">{{ document.last_error }}</span>
                  </div>
                </div>
                <div class="agent-document-actions">
                  <span class="status-chip">{{ document.approval_status }}</span>
                  <button
                    v-if="document.approval_status === 'pending'"
                    type="button"
                    class="ghost-button compact"
                    :disabled="isReviewingAgentDocument"
                    @click="reviewAgentDocument(document, 'reject')"
                  >
                    <XCircle :size="15" />
                    Reject
                  </button>
                  <button
                    v-if="document.approval_status === 'pending'"
                    type="button"
                    class="primary-button compact"
                    :disabled="isReviewingAgentDocument"
                    @click="reviewAgentDocument(document, 'approve')"
                  >
                    <CheckCircle2 :size="15" />
                    Approve
                  </button>
                </div>
              </div>
            </div>
          </article>

          <article class="dashboard-card wide-card agent-test-card">
            <div class="members-card-head">
              <div>
                <h3>Agent Test Console</h3>
                <p>Test retrieval, semantic cache behavior, and grounded answers before routing Exotel calls.</p>
              </div>
              <span class="status-chip">RAG runtime</span>
            </div>

            <div class="agent-console-grid">
              <div class="db-form-block">
                <label class="db-label" for="agent-test-query">Test Question</label>
                <textarea id="agent-test-query" v-model="agentTestQuery" class="db-input toolkit-textarea compact" placeholder="Ask a question that should be answered only from approved documents."></textarea>
                <div class="db-actions">
                  <button type="button" class="ghost-button" :disabled="isTestingAgentRetrieval" @click="testAgentRetrieval">
                    {{ isTestingAgentRetrieval ? 'Retrieving...' : 'Test Retrieval' }}
                  </button>
                  <button type="button" class="primary-button" :disabled="isTestingAgentAnswer" @click="testAgentAnswer">
                    <MessageSquare :size="16" />
                    {{ isTestingAgentAnswer ? 'Answering...' : 'Test Answer' }}
                  </button>
                </div>
              </div>

              <div class="agent-console-results">
                <div class="agent-result-panel">
                  <strong>Retrieval</strong>
                  <p v-if="agentTestRetrieval?.refusal">{{ agentTestRetrieval.refusal }}</p>
                  <div v-else-if="agentTestRetrieval?.chunks?.length" class="agent-chunk-list">
                    <div v-for="chunk in agentTestRetrieval.chunks" :key="chunk.chunk_id" class="agent-chunk">
                      <span>{{ chunk.document_name }} · score {{ chunk.score.toFixed(3) }}</span>
                      <p>{{ chunk.text }}</p>
                    </div>
                  </div>
                  <p v-else>Run retrieval to see approved chunks.</p>
                </div>

                <div class="agent-result-panel answer">
                  <strong>Grounded Answer</strong>
                  <p v-if="agentTestAnswer?.answer">{{ agentTestAnswer.answer }}</p>
                  <p v-else>The answer endpoint refuses when no indexed tenant context is found.</p>
                  <div v-if="agentTestAnswer?.intent" class="agent-debug-grid">
                    <span>Intent</span>
                    <strong>{{ agentTestAnswer.intent.type || 'unknown' }}</strong>
                    <span>Retrieval</span>
                    <strong>{{ agentTestAnswer.retrieval?.used ? 'used' : 'skipped' }}</strong>
                    <span>Reason</span>
                    <strong>{{ agentTestAnswer.intent.reason || agentTestAnswer.retrieval?.skipped_reason || 'n/a' }}</strong>
                    <span>Relevant chunks</span>
                    <strong>{{ agentTestAnswer.retrieval?.relevant_count ?? agentTestAnswer.chunks?.length ?? 0 }}</strong>
                  </div>
                </div>
              </div>
            </div>

            <div class="agent-voice-panel">
              <div class="members-card-head">
                <div>
                  <h3>Voice Runtime</h3>
                  <p>Exotel media → Sarvam STT → streamed Azure OpenAI RAG → sentence-level Sarvam TTS.</p>
                </div>
                <span class="status-chip">{{ agentRuntimeStatus?.graph || 'nokvo_rag_pipeline' }}</span>
              </div>

              <div class="agent-runtime-strip">
                <span><Radio :size="15" /> STT {{ agentRuntimeStatus?.stt?.model || 'saaras:v3' }}</span>
                <span>LLM {{ agentRuntimeStatus?.llm?.model || 'gpt-4.1-mini' }}</span>
                <span>TTS {{ agentRuntimeStatus?.tts?.voice || 'shubh' }}</span>
              </div>

              <div class="db-form-block agent-language-control">
                <label class="db-label" for="agent-voice-language">Indian Language</label>
                <select id="agent-voice-language" v-model="agentVoiceLanguage" class="db-input">
                  <option
                    v-for="language in agentVoiceLanguageOptions"
                    :key="language.code"
                    :value="language.code"
                  >
                    {{ language.label }} - {{ language.native_label }}
                  </option>
                </select>
                <p class="db-note">This controls Sarvam STT hints, GPT-4.1-mini response language, and Sarvam TTS language.</p>
              </div>

              <div class="agent-phone-link-card">
                <div>
                  <span class="section-kicker">Exotel Phone Link</span>
                  <strong>{{ agentPhoneLink?.status === 'linked' ? agentPhoneLink.phone_number : 'No phone linked' }}</strong>
                  <small v-if="agentPhoneLink?.voice_url">{{ agentPhoneLink.voice_url }}</small>
                  <small v-else>Link an Exotel number to route inbound calls to the Nokvo One agent runtime.</small>
                </div>
                <div class="agent-phone-actions">
                  <input v-model="agentPhoneForm.phone_number" class="db-input" type="text" placeholder="+91XXXXXXXXXX" />
                  <button type="button" class="primary-button compact" :disabled="isLinkingAgentPhone" @click="linkAgentPhoneNumber">
                    {{ isLinkingAgentPhone ? 'Saving...' : 'Link Agent' }}
                  </button>
                  <button
                    type="button"
                    class="ghost-button compact"
                    :disabled="isLinkingAgentPhone || agentPhoneLink?.status !== 'linked'"
                    @click="unlinkAgentPhoneNumber"
                  >
                    Remove
                  </button>
                </div>
              </div>

              <div class="agent-campaign-card">
                <div class="members-card-head">
                  <div>
                    <h3>Outbound Campaign Test</h3>
                    <p>Upload contacts and a script document. The script is indexed under the campaign before Exotel calls are launched.</p>
                  </div>
                  <span class="status-chip">{{ agentCampaigns.length }} campaigns</span>
                </div>

                <div class="agent-campaign-form">
                  <input v-model="agentCampaignForm.name" class="db-input" type="text" placeholder="Campaign name" />
                  <input v-model="agentCampaignForm.from_number" class="db-input" type="text" placeholder="Caller ID (optional)" />
                  <label class="campaign-file-input">
                    <span>{{ agentCampaignContactsFile?.name || 'Contacts .xlsx' }}</span>
                    <input type="file" accept=".xlsx,.xls" @change="handleCampaignContactsFile" />
                  </label>
                  <label class="campaign-file-input">
                    <span>{{ agentCampaignScriptFile?.name || 'Script PDF/DOCX/TXT' }}</span>
                    <input type="file" accept=".pdf,.doc,.docx,.txt,.md" @change="handleCampaignScriptFile" />
                  </label>
                  <button type="button" class="primary-button compact" :disabled="isCreatingAgentCampaign" @click="createAgentCampaign">
                    {{ isCreatingAgentCampaign ? 'Indexing...' : 'Create Campaign' }}
                  </button>
                </div>

                <div class="agent-campaign-list">
                  <div v-if="isLoadingAgentCampaigns" class="empty-state compact">Loading campaigns...</div>
                  <div v-else-if="!agentCampaigns.length" class="empty-state compact">No outbound campaigns created yet.</div>
                  <div v-for="campaign in agentCampaigns" :key="campaign.id" class="agent-campaign-row">
                    <div>
                      <strong>{{ campaign.name }}</strong>
                      <small>{{ campaign.total_count }} contacts · {{ campaign.answered_count }} answered · {{ campaign.failed_count }} failed</small>
                    </div>
                    <span class="status-chip">{{ campaign.status }}</span>
                    <button
                      type="button"
                      class="primary-button compact"
                      :disabled="launchingCampaignId === campaign.id || campaign.status !== 'draft'"
                      @click="launchAgentCampaign(campaign)"
                    >
                      {{ launchingCampaignId === campaign.id ? 'Launching...' : 'Launch' }}
                    </button>
                    <button
                      type="button"
                      class="ghost-button compact"
                      :disabled="launchingCampaignId === campaign.id || !['draft', 'running'].includes(campaign.status)"
                      @click="cancelAgentCampaign(campaign)"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              </div>

              <div class="db-actions">
                <button
                  type="button"
                  class="primary-button"
                  :disabled="isStartingVoiceSession || isVoiceSessionActive"
                  @click="startVoiceSession"
                >
                  <Mic :size="16" />
                  {{ isStartingVoiceSession ? 'Starting...' : (isVoiceSessionActive ? 'Listening...' : 'Start Voice') }}
                </button>
                <button type="button" class="ghost-button" @click="sendTextToVoiceRuntime">
                  Test Query With TTS
                </button>
                <button type="button" class="ghost-button" :disabled="isTestingAgentLatency" @click="testAgentLatency">
                  {{ isTestingAgentLatency ? 'Testing...' : 'Check 800ms' }}
                </button>
              </div>

              <div v-if="agentLatencyResult" class="agent-latency-result" :class="{ pass: agentLatencyResult.latency?.passed, fail: !agentLatencyResult.latency?.passed }">
                <strong>
                  {{ agentLatencyResult.latency?.answer_text_to_first_tts_audio_ms ?? 'n/a' }}ms
                  {{ agentLatencyResult.latency?.passed ? 'within target' : 'over target' }}
                </strong>
                <span>
                  Target: {{ agentLatencyResult.latency?.target_ms }}ms from answer text to first TTS audio.
                  Text answer: {{ agentLatencyResult.latency?.final_transcript_to_answer_ms }}ms.
                  Transcript to first audio: {{ agentLatencyResult.latency?.final_transcript_to_first_tts_audio_ms ?? 'n/a' }}ms.
                </span>
              </div>

              <div class="agent-voice-grid">
                <div class="agent-result-panel">
                  <strong>Live Transcript</strong>
                  <p>{{ voiceTranscript || 'Start voice or send the test query to see transcript text.' }}</p>
                </div>
                <div class="agent-result-panel">
                  <strong>Agent Spoken Answer</strong>
                  <p>{{ voiceAgentAnswer || 'The voice answer appears here and plays through Sarvam TTS.' }}</p>
                  <small v-if="voiceTtsFirstAudioMs !== null">
                    First TTS audio: {{ voiceTtsFirstAudioMs }}ms
                    {{ voiceTtsFirstAudioMs <= 800 ? '(within 800ms)' : '(over 800ms)' }}
                  </small>
                </div>
              </div>

              <div class="agent-event-list">
                <div v-if="!voiceEvents.length" class="empty-state compact">No voice runtime events yet.</div>
                <div v-for="event in voiceEvents" :key="event.id" class="agent-event-row" :class="{
                  'event-intent': event.type === 'INTENT',
                  'event-answer': event.type === 'ANSWER' || event.type === 'ANSWER_SENTENCE',
                  'event-transcript': event.type === 'TRANSCRIPT',
                  'event-error': event.type === 'TTS_ERROR' || event.type?.includes('ERROR'),
                  'event-refusal': event.type === 'REFUSAL',
                }">
                  <span class="event-header">{{ event.created_at }} · <strong>{{ event.type }}</strong></span>
                  <p>{{ event.text }}</p>
                  <p v-if="event.detail" class="event-detail">{{ event.detail }}</p>
                </div>
              </div>
            </div>
          </article>
        </div>

        <article v-else class="dashboard-card ticket-console-card">
          <div class="compact-card-head">
            <div class="compact-icon-shell">
              <Shield :size="18" />
            </div>
            <div>
              <h3>Admin Access Required</h3>
              <p>Agent Knowledge uploads, approvals, and answer tests are limited to organization admins and managers.</p>
            </div>
          </div>
        </article>
      </section>
    </main>

    <footer class="portal-footer">
      <span>© 2024 NOKVO. All rights reserved.</span>
      <div class="footer-nav">
        <a href="#">Privacy Policy</a>
        <a href="#">Terms of Service</a>
        <a href="#">Security</a>
      </div>
    </footer>

    <div v-if="databaseModalOpen" class="db-modal-shell">
      <div class="db-modal-backdrop" @click="closeDatabaseModal"></div>
      <section class="db-modal">
        <div class="db-modal-header">
          <div>
            <h3>Connect Database</h3>
            <p>Select your database provider and provide the connection details to integrate with NOKVO.</p>
          </div>
          <button type="button" class="ghost-button compact" @click="closeDatabaseModal">Close</button>
        </div>

        <div class="db-form-block">
          <label class="db-label">Database Provider</label>
          <div class="provider-grid">
            <label
              v-for="provider in databaseProviders"
              :key="provider.value"
              class="provider-option"
              :class="{ active: databaseForm.provider === provider.value, muted: !provider.supported }"
            >
              <input v-model="databaseForm.provider" type="radio" :value="provider.value" class="sr-only" />
              <span class="provider-name">{{ provider.label }}</span>
              <small>{{ provider.group }}</small>
            </label>
          </div>
        </div>

        <div class="db-form-block">
          <label class="db-label" for="connection-string">Connection String</label>
          <input
            id="connection-string"
            v-model="databaseForm.connection_string"
            class="db-input"
            type="text"
            placeholder="postgresql://user:password@host:5432/dbname"
          />
          <p class="db-note">Your connection string is encrypted and stored in the shared Key Vault.</p>
        </div>

        <div class="db-actions">
          <button type="button" class="ghost-button" @click="closeDatabaseModal">Cancel</button>
          <button type="button" class="primary-button" :disabled="isScanningDatabase" @click="scanDatabaseConnection">
            {{ isScanningDatabase ? 'Scanning...' : 'Connect Database' }}
          </button>
        </div>

        <div v-if="databaseSchema.length" class="schema-panel">
          <div class="schema-panel-head">
            <div>
              <strong>Select Columns To Index</strong>
              <span>{{ schemaSelectionCount }} columns selected</span>
            </div>
            <label class="row-limit-control">
              <span>Rows per table</span>
              <input v-model.number="databaseForm.row_limit" type="number" min="1" max="200" />
            </label>
          </div>

          <div class="schema-table-list">
            <article v-for="table in databaseSchema" :key="`${table.schema || 'default'}:${table.name}`" class="schema-table-card">
              <div class="schema-table-head">
                <label>
                  <input type="checkbox" :checked="table.columns.every((column) => column.selected)" @change="toggleTableSelection(table, $event.target.checked)" />
                  <strong>{{ table.schema ? `${table.schema}.` : '' }}{{ table.name }}</strong>
                </label>
              </div>
              <div class="schema-column-grid">
                <label v-for="column in table.columns" :key="column.name" class="schema-column-option">
                  <input v-model="column.selected" type="checkbox" />
                  <span>{{ column.name }}</span>
                  <small>{{ column.type }}</small>
                </label>
              </div>
            </article>
          </div>

          <div class="db-actions">
            <button type="button" class="ghost-button" @click="closeDatabaseModal">Close</button>
            <button type="button" class="primary-button" :disabled="isIndexingDatabase" @click="indexSelectedColumns">
              {{ isIndexingDatabase ? 'Indexing...' : 'Connect And Index Selected Columns' }}
            </button>
          </div>
        </div>
      </section>
    </div>

    <div v-if="crmModalOpen" class="db-modal-shell">
      <div class="db-modal-backdrop" @click="closeCrmModal"></div>
      <section class="db-modal">
        <div class="db-modal-header">
          <div>
            <h3>Connect CRM</h3>
            <p>Connect Zoho or Freshworks CRM. Zoho OAuth also grants Desk access and indexes Desk metadata into its own namespace when available.</p>
          </div>
          <button type="button" class="ghost-button compact" @click="closeCrmModal">Close</button>
        </div>

        <div class="db-form-block">
          <label class="db-label">CRM Provider</label>
          <div class="provider-grid provider-grid-dual">
            <label
              v-for="provider in crmProviders"
              :key="provider.value"
              class="provider-option"
              :class="{ active: crmForm.provider === provider.value, muted: !provider.supported }"
            >
              <input v-model="crmForm.provider" type="radio" :value="provider.value" class="sr-only" />
              <span class="provider-name">{{ provider.label }}</span>
              <small>{{ provider.group }}</small>
            </label>
          </div>
        </div>

        <div class="crm-form-grid">
          <div v-if="isZohoCrmProvider" class="crm-oauth-panel">
            <div class="crm-oauth-copy">
              <strong>Zoho OAuth</strong>
              <p>Redirect the organization admin to Zoho and approve scopes once. NOKVO will exchange tokens, scan CRM metadata, and store the same OAuth grant for Zoho Desk sync.</p>
            </div>
            <div class="crm-oauth-scopes">
              <span>Scopes</span>
              <small>CRM modules, CRM settings, org read, secure search, Desk tickets, Desk basic read, Desk settings read</small>
            </div>
            <button type="button" class="primary-button crm-oauth-button" :disabled="isConnectingCrm" @click="beginZohoOAuth">
              {{ isConnectingCrm ? 'Redirecting...' : 'Continue with Zoho' }}
            </button>
          </div>

          <div v-if="activeCrmProvider === 'freshworks'" class="db-form-block">
            <label class="db-label" for="crm-account-url">Freshworks Account URL</label>
            <input
              id="crm-account-url"
              v-model="crmForm.account_url"
              class="db-input"
              type="text"
              placeholder="https://your-company.myfreshworks.com"
            />
          </div>

          <div v-if="activeCrmProvider === 'freshworks'" class="db-form-block">
            <label class="db-label" for="crm-access-token">Access Token / API Token</label>
            <input
              id="crm-access-token"
              v-model="crmForm.access_token"
              class="db-input"
              type="password"
              placeholder="Paste CRM token"
            />
          </div>
        </div>

        <p class="db-note">Credentials are encrypted in the shared Key Vault. The CRM scan indexes both module schema and supported actions into a CRM subfolder namespace within the tenant’s Qdrant collection.</p>

        <div v-if="!isZohoCrmProvider" class="db-actions">
          <button type="button" class="ghost-button" @click="closeCrmModal">Cancel</button>
          <button type="button" class="primary-button" :disabled="isConnectingCrm" @click="connectCrmIntegration">
            {{ isConnectingCrm ? 'Scanning CRM...' : 'Connect CRM' }}
          </button>
        </div>

        <div v-else class="db-actions">
          <button type="button" class="ghost-button" @click="closeCrmModal">Close</button>
        </div>
      </section>
    </div>

    <div v-if="erpModalOpen" class="db-modal-shell">
      <div class="db-modal-backdrop" @click="closeErpModal"></div>
      <section class="db-modal">
        <div class="db-modal-header">
          <div>
            <h3>Connect ERP</h3>
            <p>Connect TallyPrime or Tally ERP over its XML HTTP server. NOKVO will scan common masters, vouchers, and supported ERP actions into an ERP namespace.</p>
          </div>
          <button type="button" class="ghost-button compact" @click="closeErpModal">Close</button>
        </div>

        <div class="db-form-block">
          <label class="db-label">ERP Provider</label>
          <div class="provider-grid provider-grid-dual">
            <label
              v-for="provider in erpProviders"
              :key="provider.value"
              class="provider-option"
              :class="{ active: erpForm.provider === provider.value, muted: !provider.supported }"
            >
              <input v-model="erpForm.provider" type="radio" :value="provider.value" class="sr-only" />
              <span class="provider-name">{{ provider.label }}</span>
              <small>{{ provider.group }}</small>
            </label>
          </div>
        </div>

        <div class="crm-form-grid">
          <div class="db-form-block">
            <label class="db-label" for="erp-base-url">Tally HTTP URL</label>
            <input
              id="erp-base-url"
              v-model="erpForm.base_url"
              class="db-input"
              type="text"
              placeholder="http://localhost:9000"
            />
          </div>
          <div class="db-form-block">
            <label class="db-label" for="erp-company-name">Company Name (optional)</label>
            <input
              id="erp-company-name"
              v-model="erpForm.company_name"
              class="db-input"
              type="text"
              placeholder="Leave blank to use loaded company"
            />
          </div>
          <div class="db-form-block">
            <label class="db-label" for="erp-timeout">Timeout Seconds</label>
            <input
              id="erp-timeout"
              v-model.number="erpForm.timeout_seconds"
              class="db-input"
              type="number"
              min="3"
              max="60"
            />
          </div>
          <div class="db-form-block">
            <label class="db-label" for="erp-max-items">Sample Records Per Module</label>
            <input
              id="erp-max-items"
              v-model.number="erpForm.max_items_per_module"
              class="db-input"
              type="number"
              min="1"
              max="100"
            />
          </div>
        </div>

        <p class="db-note">TallyPrime must be running with HTTP server enabled, usually on port 9000, and a company must be loaded. The connection details are encrypted in Key Vault.</p>

        <div class="db-actions">
          <button type="button" class="ghost-button" @click="closeErpModal">Cancel</button>
          <button type="button" class="primary-button" :disabled="isConnectingErp" @click="connectErpIntegration">
            {{ isConnectingErp ? 'Scanning Tally...' : 'Connect Tally ERP' }}
          </button>
        </div>
      </section>
    </div>

    <div v-if="shippingModalOpen" class="db-modal-shell">
      <div class="db-modal-backdrop" @click="closeShippingModal"></div>
      <section class="db-modal">
        <div class="db-modal-header">
          <div>
            <h3>Connect Shipping</h3>
            <p>Connect Shiprocket using an API user from the Shiprocket panel. NOKVO will index shipping actions for serviceability, order creation, AWB, pickup, and tracking.</p>
          </div>
          <button type="button" class="ghost-button compact" @click="closeShippingModal">Close</button>
        </div>

        <div class="db-form-block">
          <label class="db-label">Shipping Provider</label>
          <div class="provider-grid provider-grid-dual">
            <label
              v-for="provider in shippingProviders"
              :key="provider.value"
              class="provider-option"
              :class="{ active: shippingForm.provider === provider.value, muted: !provider.supported }"
            >
              <input v-model="shippingForm.provider" type="radio" :value="provider.value" class="sr-only" />
              <span class="provider-name">{{ provider.label }}</span>
              <small>{{ provider.group }}</small>
            </label>
          </div>
        </div>

        <div class="crm-form-grid">
          <div class="db-form-block">
            <label class="db-label" for="shipping-email">Shiprocket API Email</label>
            <input
              id="shipping-email"
              v-model="shippingForm.email"
              class="db-input"
              type="email"
              placeholder="api-user@example.com"
            />
          </div>
          <div class="db-form-block">
            <label class="db-label" for="shipping-password">Shiprocket API Password</label>
            <input
              id="shipping-password"
              v-model="shippingForm.password"
              class="db-input"
              type="password"
              placeholder="API user password"
            />
          </div>
          <div class="db-form-block">
            <label class="db-label" for="shipping-base-url">API Base URL</label>
            <input
              id="shipping-base-url"
              v-model="shippingForm.base_url"
              class="db-input"
              type="text"
              placeholder="https://apiv2.shiprocket.in/v1/external"
            />
          </div>
        </div>

        <p class="db-note">Create a Shiprocket API user from Settings > API. Use that API user's email and password, not necessarily your normal panel login. Credentials are encrypted in Key Vault.</p>

        <div class="db-actions">
          <button type="button" class="ghost-button" @click="closeShippingModal">Cancel</button>
          <button type="button" class="primary-button" :disabled="isConnectingShipping" @click="connectShippingIntegration">
            {{ isConnectingShipping ? 'Connecting Shiprocket...' : 'Connect Shiprocket' }}
          </button>
        </div>
      </section>
    </div>

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

.login-card,
.info-card,
.workspace-hero {
  background: rgba(255, 255, 255, 0.82);
  border: 1px solid #e4e3d7;
  box-shadow: 0 20px 60px -30px rgba(27, 28, 21, 0.18);
  backdrop-filter: blur(16px);
}

.login-card {
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

.placeholder-button {
  width: 100%;
  min-height: 52px;
  border-radius: 999px;
  border: 1px solid #c4c7c7;
  background: #ffffff;
  color: #1b1c15;
  font-size: 1rem;
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

.org-selector {
  margin-top: 1.35rem;
  display: grid;
  gap: 0.75rem;
}

.selector-head {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  color: #1b1c15;
}

.selector-head span {
  color: #5f5f53;
  font-size: 0.9rem;
}

.org-option {
  width: 100%;
  border: 1px solid #e4e3d7;
  background: rgba(255, 255, 255, 0.92);
  border-radius: 1rem;
  padding: 1rem 1rem;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 1rem;
  text-align: left;
}

.org-option-main {
  display: flex;
  flex-direction: column;
  gap: 0.24rem;
}

.org-option-main small,
.org-option-side {
  color: #5f5f53;
}

.org-option-side {
  font-size: 0.86rem;
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
  letter-spacing: 0.18em;
  text-align: center;
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

.db-modal-shell {
  position: fixed;
  inset: 0;
  z-index: 30;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1.5rem;
}

.db-modal-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(27, 28, 21, 0.24);
  backdrop-filter: blur(8px);
}

.db-modal {
  position: relative;
  width: min(100%, 880px);
  max-height: calc(100vh - 3rem);
  overflow: auto;
  background: #ffffff;
  border-radius: 1.2rem;
  border: 1px solid #e4e3d7;
  box-shadow: 0 24px 70px -34px rgba(27, 28, 21, 0.3);
  padding: 2rem;
}

.db-modal-header {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  align-items: flex-start;
  margin-bottom: 1.5rem;
}

.db-modal-header h3 {
  font-family: Manrope, sans-serif;
  font-size: 2rem;
  letter-spacing: -0.03em;
}

.db-modal-header p {
  margin-top: 0.4rem;
  color: #444748;
  max-width: 36rem;
  line-height: 1.6;
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

.provider-option.muted small::after {
  content: " · driver pending";
}

.provider-name {
  font-weight: 700;
  color: #1b1c15;
}

.provider-option small {
  color: #5f5f53;
  text-transform: capitalize;
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

.db-note {
  margin-top: 0.65rem;
  color: #5f5f53;
  font-size: 0.86rem;
}

.db-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.8rem;
  padding-top: 0.25rem;
}

.crm-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem;
}

.crm-oauth-panel {
  grid-column: 1 / -1;
  display: grid;
  gap: 1rem;
  padding: 1.1rem;
  border-radius: 1rem;
  border: 1px solid rgba(196, 199, 199, 0.45);
  background: linear-gradient(180deg, rgba(245, 244, 232, 0.95), rgba(255, 255, 255, 0.9));
}

.crm-oauth-copy strong,
.crm-oauth-scopes span {
  display: block;
  margin-bottom: 0.3rem;
  font-family: Manrope, sans-serif;
  font-size: 1rem;
}

.crm-oauth-copy p,
.crm-oauth-scopes small {
  color: #5f5f53;
  line-height: 1.65;
}

.crm-oauth-button {
  width: 100%;
}

.schema-panel {
  margin-top: 1.5rem;
  padding-top: 1.5rem;
  border-top: 1px solid #e4e3d7;
}

.schema-panel-head {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  align-items: center;
  margin-bottom: 1rem;
}

.schema-panel-head span {
  display: block;
  margin-top: 0.2rem;
  color: #5f5f53;
  font-size: 0.9rem;
}

.row-limit-control {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  color: #5f5f53;
  font-size: 0.85rem;
}

.row-limit-control input {
  width: 100px;
  border-radius: 0.8rem;
  border: 1px solid #d9d8ce;
  padding: 0.7rem 0.85rem;
  background: #ffffff;
}

.schema-table-list {
  display: grid;
  gap: 1rem;
}

.schema-table-card {
  border: 1px solid #e4e3d7;
  border-radius: 1rem;
  background: #fbfaee;
  padding: 1rem;
}

.schema-table-head label {
  display: inline-flex;
  align-items: center;
  gap: 0.6rem;
}

.schema-column-grid {
  margin-top: 0.85rem;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0.7rem;
}

.schema-column-option {
  border: 1px solid #ecebdd;
  border-radius: 0.85rem;
  padding: 0.8rem 0.85rem;
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 0.45rem 0.65rem;
  align-items: start;
  background: rgba(255, 255, 255, 0.8);
}

.schema-column-option span {
  font-weight: 600;
}

.schema-column-option small {
  grid-column: 2;
  color: #5f5f53;
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

.nav-search,
.dashboard-filter-input {
  position: relative;
}

.nav-search input,
.dashboard-filter-input input {
  width: 100%;
  border-radius: 999px;
  border: 1px solid transparent;
  background: #efeee3;
  color: #1b1c15;
  padding: 0.85rem 1rem 0.85rem 2.8rem;
  font-size: 0.95rem;
  transition: border-color 0.2s ease, background 0.2s ease;
}

.nav-search input:focus,
.dashboard-filter-input input:focus {
  outline: none;
  border-color: rgba(116, 120, 120, 0.65);
  background: #ffffff;
}

.nav-search-icon,
.dashboard-filter-icon {
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

.dashboard-primary-button:disabled,
.dashboard-secondary-button:disabled,
.dashboard-chip-button:disabled,
.dashboard-inline-button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
  transform: none;
  box-shadow: none;
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

.overview-grid .crm-sync-card {
  grid-column: span 2;
  min-height: 18.5rem;
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

.crm-sync-actions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.75rem;
}

.crm-sync-emphasis {
  margin-top: 1.25rem;
  padding-top: 1rem;
  border-top: 1px solid rgba(196, 199, 199, 0.28);
}

.crm-sync-emphasis span {
  display: block;
  margin-bottom: 0.3rem;
  color: #5f5f53;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.crm-sync-emphasis strong {
  display: block;
  font-family: Manrope, sans-serif;
  font-size: 0.96rem;
  line-height: 1.55;
  word-break: break-word;
}

.tickets-grid {
  grid-template-columns: minmax(0, 1fr);
}

.toolkit-page-grid {
  grid-template-columns: minmax(0, 1.15fr) minmax(20rem, 0.85fr);
  align-items: start;
}

.toolkit-page .dashboard-section-head {
  padding: 1.1rem 1.2rem;
  border: 1px solid rgba(196, 199, 199, 0.38);
  border-radius: 1.2rem;
  background:
    radial-gradient(circle at 8% 20%, rgba(229, 226, 225, 0.92), transparent 34%),
    rgba(255, 255, 255, 0.7);
}

.toolkit-page .dashboard-card {
  border-color: rgba(173, 177, 169, 0.48);
}

.toolkit-builder-card,
.toolkit-registry-card {
  min-height: 34rem;
}

.ticket-console-card {
  max-width: 48rem;
}

.ticket-page-note {
  margin-top: 1.3rem;
  padding: 1rem;
  border-radius: 1rem;
  background: #efeee3;
  border: 1px solid rgba(196, 199, 199, 0.45);
}

.ticket-page-note strong {
  display: block;
  font-family: Manrope, sans-serif;
  margin-bottom: 0.35rem;
}

.ticket-page-note p {
  color: #5f5f53;
  font-size: 0.92rem;
  line-height: 1.65;
}

.toolkit-textarea {
  min-height: 8rem;
  resize: vertical;
}

.toolkit-textarea.compact {
  min-height: 5.5rem;
}

.toolkit-review-card {
  margin-top: 1.2rem;
  padding: 1rem;
  border-radius: 1rem;
  border: 1px solid rgba(196, 199, 199, 0.42);
  background: rgba(239, 238, 227, 0.72);
}

.toolkit-page .toolkit-review-card {
  margin-top: 0;
}

.toolkit-review-card pre {
  margin-top: 1rem;
  max-height: 22rem;
  overflow: auto;
  padding: 1rem;
  border-radius: 0.85rem;
  background: rgba(27, 28, 21, 0.92);
  color: #f8f7ee;
  font-size: 0.78rem;
  line-height: 1.55;
  white-space: pre-wrap;
}

.toolkit-registry-summary {
  margin-top: 1rem;
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  padding: 0.9rem 1rem;
  border-radius: 0.85rem;
  background: rgba(255, 255, 255, 0.72);
  border: 1px solid rgba(196, 199, 199, 0.35);
}

.toolkit-registry-summary span {
  color: #5f5f53;
  font-size: 0.9rem;
}

.toolkit-list {
  display: grid;
  gap: 0.75rem;
  margin-top: 1rem;
}

.toolkit-list.registered {
  padding-top: 1rem;
  border-top: 1px solid rgba(196, 199, 199, 0.35);
}

.toolkit-list-row {
  width: 100%;
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
  padding: 0.95rem;
  border: 1px solid rgba(196, 199, 199, 0.42);
  border-radius: 0.9rem;
  background: rgba(255, 255, 255, 0.68);
  color: inherit;
  text-align: left;
}

.toolkit-list-row:not(.static) {
  cursor: pointer;
}

.toolkit-list-row:not(.static):hover {
  border-color: rgba(27, 28, 21, 0.28);
  background: rgba(255, 255, 255, 0.92);
}

.toolkit-list-row strong,
.toolkit-list-row small {
  display: block;
}

.toolkit-list-row strong {
  font-family: Manrope, sans-serif;
  font-size: 0.95rem;
  line-height: 1.35;
}

.toolkit-list-row small {
  margin-top: 0.25rem;
  color: #5f5f53;
  font-size: 0.78rem;
  line-height: 1.45;
}

.toolkit-list-more {
  padding: 0.2rem 0.1rem;
  color: #5f5f53;
  font-size: 0.8rem;
  line-height: 1.4;
}

.empty-state.compact {
  padding: 0.7rem 0;
  font-size: 0.9rem;
}

.agent-page-grid {
  grid-template-columns: minmax(0, 0.95fr) minmax(0, 1.05fr);
  align-items: start;
}

.agent-hero-panel {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 1.5rem;
  align-items: end;
  padding: 1.4rem;
  border-radius: 1.35rem;
  border: 1px solid rgba(196, 199, 199, 0.45);
  background:
    radial-gradient(circle at 10% 15%, rgba(220, 233, 213, 0.9), transparent 32%),
    linear-gradient(135deg, rgba(255, 255, 255, 0.86), rgba(239, 238, 227, 0.72));
  box-shadow: 0 16px 42px -30px rgba(27, 28, 21, 0.24);
}

.agent-hero-panel h2 {
  font-family: Manrope, sans-serif;
  font-size: clamp(1.8rem, 3vw, 2.55rem);
  letter-spacing: -0.045em;
  line-height: 1.05;
}

.agent-hero-panel p {
  margin-top: 0.55rem;
  max-width: 47rem;
  color: #444748;
  line-height: 1.7;
}

.agent-hero-stats {
  display: grid;
  grid-template-columns: repeat(3, minmax(7rem, 1fr));
  gap: 0.75rem;
}

.agent-hero-stats span {
  padding: 0.9rem;
  border-radius: 1rem;
  background: rgba(255, 255, 255, 0.76);
  border: 1px solid rgba(196, 199, 199, 0.45);
  color: #5f5f53;
  font-size: 0.8rem;
  font-weight: 700;
}

.agent-hero-stats strong {
  display: block;
  color: #1b1c15;
  font-family: Manrope, sans-serif;
  font-size: 1.35rem;
}

.agent-upload-card,
.agent-documents-card {
  min-height: 34rem;
}

.agent-upload-drop {
  position: relative;
  margin-top: 1.25rem;
  min-height: 9rem;
  border: 1px dashed rgba(116, 120, 120, 0.6);
  border-radius: 1rem;
  background: rgba(239, 238, 227, 0.65);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.45rem;
  text-align: center;
  padding: 1rem;
  color: #444748;
}

.agent-upload-drop input {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
}

.agent-upload-drop strong {
  color: #1b1c15;
  font-family: Manrope, sans-serif;
}

.agent-upload-drop span {
  max-width: 24rem;
  color: #5f5f53;
  font-size: 0.82rem;
  line-height: 1.5;
}

.agent-form-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(12rem, 0.55fr);
  gap: 1rem;
  margin-top: 1rem;
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

.agent-chunk-list {
  display: grid;
  gap: 0.7rem;
  max-height: 18rem;
  overflow: auto;
}

.agent-chunk {
  padding: 0.75rem;
  border-radius: 0.8rem;
  background: rgba(255, 255, 255, 0.72);
}

.agent-chunk span {
  display: block;
  margin-bottom: 0.35rem;
  color: #5f5f53;
  font-size: 0.75rem;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.agent-voice-panel {
  margin-top: 1.1rem;
  padding-top: 1rem;
  border-top: 1px solid rgba(196, 199, 199, 0.35);
}

.agent-runtime-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 0.7rem;
  margin: 1rem 0;
}

.agent-runtime-strip span {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.55rem 0.75rem;
  border-radius: 999px;
  background: rgba(239, 238, 227, 0.75);
  border: 1px solid rgba(196, 199, 199, 0.42);
  color: #444748;
  font-size: 0.78rem;
  font-weight: 800;
}

.agent-language-control {
  max-width: 28rem;
}

.agent-phone-link-card {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(18rem, 0.8fr);
  gap: 1rem;
  align-items: end;
  margin: 1rem 0;
  padding: 1rem;
  border-radius: 1rem;
  background: rgba(255, 255, 255, 0.7);
  border: 1px solid rgba(196, 199, 199, 0.42);
}

.agent-phone-link-card strong,
.agent-phone-link-card small {
  display: block;
}

.agent-phone-link-card strong {
  font-family: Manrope, sans-serif;
  font-size: 1.05rem;
  line-height: 1.35;
}

.agent-phone-link-card small {
  margin-top: 0.3rem;
  color: #5f5f53;
  font-size: 0.8rem;
  line-height: 1.45;
  word-break: break-word;
}

.agent-phone-actions {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  gap: 0.6rem;
  align-items: center;
}

.agent-campaign-card {
  margin: 1rem 0;
  padding: 1rem;
  border-radius: 1rem;
  background: rgba(255, 255, 255, 0.7);
  border: 1px solid rgba(196, 199, 199, 0.42);
}

.agent-campaign-form {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 0.75fr);
  gap: 0.7rem;
  margin-top: 1rem;
}

.campaign-file-input {
  position: relative;
  display: inline-flex;
  align-items: center;
  min-height: 2.6rem;
  border: 1px dashed rgba(116, 120, 120, 0.55);
  border-radius: 0.85rem;
  background: rgba(239, 238, 227, 0.58);
  padding: 0.65rem 0.8rem;
  color: #444748;
  font-size: 0.82rem;
  font-weight: 800;
  overflow: hidden;
}

.campaign-file-input input {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
}

.agent-campaign-list {
  display: grid;
  gap: 0.65rem;
  margin-top: 0.9rem;
}

.agent-campaign-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto auto;
  gap: 0.65rem;
  align-items: center;
  padding: 0.75rem;
  border-radius: 0.85rem;
  background: rgba(255, 255, 255, 0.7);
  border: 1px solid rgba(196, 199, 199, 0.36);
}

.agent-campaign-row strong,
.agent-campaign-row small {
  display: block;
}

.agent-campaign-row small {
  margin-top: 0.18rem;
  color: #5f5f53;
  font-size: 0.78rem;
}

.agent-latency-result {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  align-items: center;
  margin-top: 0.85rem;
  padding: 0.85rem 1rem;
  border-radius: 0.9rem;
  border: 1px solid rgba(196, 199, 199, 0.42);
  background: rgba(239, 238, 227, 0.72);
}

.agent-latency-result.pass {
  background: rgba(220, 252, 231, 0.78);
  border-color: rgba(34, 197, 94, 0.25);
}

.agent-latency-result.fail {
  background: rgba(254, 226, 226, 0.78);
  border-color: rgba(239, 68, 68, 0.25);
}

.agent-latency-result strong {
  font-family: Manrope, sans-serif;
}

.agent-latency-result span {
  color: #5f5f53;
  font-size: 0.82rem;
}

.agent-voice-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem;
  margin-top: 1rem;
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

.agent-event-row.event-intent {
  border-left: 3px solid #6366f1;
  background: rgba(99, 102, 241, 0.06);
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
  border-left: 3px solid #f59e0b;
  background: rgba(245, 158, 11, 0.06);
}

.agent-event-row.event-refusal {
  border-left: 3px solid #ef4444;
  background: rgba(239, 68, 68, 0.06);
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
.invite-form select,
.member-row select {
  width: 100%;
  border-radius: 0.85rem;
  border: 1px solid #d9d8ce;
  background: rgba(255, 255, 255, 0.8);
  color: #1b1c15;
  padding: 0.9rem 1rem;
  font-size: 0.95rem;
}

.invite-form button,
.save-member {
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
  grid-template-columns: 2.2fr 1fr 1fr auto;
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

.message.in-card {
  margin-bottom: 1rem;
}

.empty-state {
  padding: 1rem 0;
  color: #5f5f53;
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

.org-shell.dark {
  background: #131511;
  color: #f2f1e5;
}

.agent-debug-grid {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 0.45rem 0.75rem;
  margin-top: 0.85rem;
  padding-top: 0.85rem;
  border-top: 1px solid rgba(51, 56, 45, 0.14);
  font-size: 0.78rem;
}

.agent-debug-grid span {
  color: #6b6f66;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.agent-debug-grid strong {
  display: block;
  margin: 0;
  color: #252923;
  font-family: Manrope, sans-serif;
  overflow-wrap: anywhere;
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
.org-shell.dark .db-modal,
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
.org-shell.dark .primary-button,
.org-shell.dark .connect-db-button {
  background: #f2f1e5;
  color: #131511;
  border-color: #f2f1e5;
}

.org-shell.dark .nav-search input,
.org-shell.dark .dashboard-filter-input input,
.org-shell.dark .db-input,
.org-shell.dark .totp-input,
.org-shell.dark .invite-form input,
.org-shell.dark .invite-form select,
.org-shell.dark .member-row select,
.org-shell.dark .row-limit-control input {
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
.org-shell.dark .db-note,
.org-shell.dark .db-modal-header p,
.org-shell.dark .schema-panel-head span,
.org-shell.dark .row-limit-control,
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
.org-shell.dark .schema-table-card,
.org-shell.dark .schema-column-option {
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

.org-shell.dark .provider-option.muted {
  opacity: 0.82;
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

.org-shell.dark .crm-oauth-panel {
  background: linear-gradient(180deg, rgba(36, 40, 33, 0.98), rgba(24, 27, 22, 0.96));
  border-color: rgba(144, 150, 136, 0.55);
}

.org-shell.dark .ticket-page-note {
  background: rgba(33, 37, 30, 0.92);
  border-color: rgba(102, 108, 92, 0.35);
}

.org-shell.dark .ticket-page-note p {
  color: #d0d0c3;
}

.org-shell.dark .toolkit-review-card,
.org-shell.dark .toolkit-registry-summary {
  background: rgba(33, 37, 30, 0.92);
  border-color: rgba(102, 108, 92, 0.35);
}

.org-shell.dark .toolkit-registry-summary span {
  color: #d0d0c3;
}

.org-shell.dark .toolkit-list-row {
  background: rgba(33, 37, 30, 0.84);
  border-color: rgba(102, 108, 92, 0.35);
}

.org-shell.dark .toolkit-list-row:not(.static):hover {
  border-color: rgba(221, 220, 205, 0.28);
  background: rgba(40, 45, 35, 0.95);
}

.org-shell.dark .toolkit-list-row small {
  color: #d0d0c3;
}

.org-shell.dark .toolkit-list-more {
  color: #d0d0c3;
}

.org-shell.dark .crm-oauth-copy p,
.org-shell.dark .crm-oauth-scopes small {
  color: #d0d0c3;
}

.org-shell.dark .message.info {
  background: rgba(51, 56, 45, 0.95);
  color: #f2f1e5;
}

.org-shell.dark .toolkit-page .dashboard-section-head,
.org-shell.dark .agent-hero-panel,
.org-shell.dark .agent-hero-stats span,
.org-shell.dark .agent-upload-drop,
.org-shell.dark .agent-document-row,
.org-shell.dark .agent-result-panel,
.org-shell.dark .agent-chunk,
.org-shell.dark .agent-runtime-strip span,
.org-shell.dark .agent-event-row,
.org-shell.dark .agent-phone-link-card,
.org-shell.dark .agent-campaign-card,
.org-shell.dark .agent-campaign-row,
.org-shell.dark .campaign-file-input,
.org-shell.dark .agent-latency-result {
  background: rgba(28, 31, 24, 0.78);
  color: #f2f1e5;
  border-color: rgba(102, 108, 92, 0.45);
}

.org-shell.dark .agent-hero-panel p,
.org-shell.dark .agent-upload-drop span,
.org-shell.dark .agent-document-main small,
.org-shell.dark .agent-result-panel p,
.org-shell.dark .agent-chunk span,
.org-shell.dark .agent-debug-grid span,
.org-shell.dark .agent-event-row span,
.org-shell.dark .agent-event-row p,
.org-shell.dark .agent-phone-link-card small,
.org-shell.dark .agent-campaign-row small,
.org-shell.dark .agent-latency-result span {
  color: #c8c7bc;
}

.org-shell.dark .agent-debug-grid strong {
  color: #f2f1e5;
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
  .overview-grid .crm-sync-card,
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
  .schema-column-grid {
    grid-template-columns: 1fr 1fr;
  }

  .crm-form-grid {
    grid-template-columns: 1fr;
  }

  .agent-hero-panel,
  .agent-page-grid,
  .agent-console-grid,
  .agent-form-grid,
  .agent-document-row,
  .agent-voice-grid,
  .agent-phone-link-card,
  .agent-phone-actions,
  .agent-campaign-form,
  .agent-campaign-row {
    grid-template-columns: 1fr;
  }

  .agent-hero-stats {
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

  .org-option {
    flex-direction: column;
    align-items: flex-start;
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

  .db-modal-header,
  .schema-panel-head,
  .db-actions {
    flex-direction: column;
    align-items: stretch;
  }

  .provider-grid,
  .schema-column-grid {
    grid-template-columns: 1fr;
  }

  .provider-grid-dual {
    grid-template-columns: 1fr;
  }
}
</style>
