<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import axios from 'axios';
import { pageKeyToRouteName } from '../router/pageKeys.js';
import { provideDashboardState } from '../composables/useDashboardState.js';
import QrcodeVue from 'qrcode.vue';
import nokvoLogo from '../assets/nokvo-one-logo.png';
import {
  Activity,
  Bell,
  Bot,
  Brain,
  CalendarDays,
  CalendarOff,
  CheckCircle2,
  ClipboardList,
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
  Repeat,
  Search,
  Settings2,
  Shield,
  Square,
  Stethoscope,
  SunMedium,
  SunMedium as Sun,
  Trash2,
  Upload,
  UserPlus,
  Users,
  Volume2,
  Wallet,
  Wrench,
  XCircle,
  ScrollText,
} from 'lucide-vue-next';

import {
  NOKVO_ONE_API_BASE,
  NOKVO_ONE_AGENTS_BASE,
  NOKVO_ONE_KB_BASE,
  NOKVO_ONE_SERVICES_BASE,
  NOKVO_CONNECT_API_BASE,
  wsUrl,
} from '../config.js';

const API_BASE_URL = NOKVO_ONE_API_BASE;
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
const route = useRoute();

const api = axios.create({ baseURL: API_BASE_URL });
const connectApi = axios.create({ baseURL: NOKVO_CONNECT_API_BASE });

const orgShellRef = ref(null);
const themeMode = ref(localStorage.getItem(THEME_KEY) || 'light');
const cursorTimer = ref(null);
const authConfig = ref(null);
const googleLoginButtonRef = ref(null);
const googleSignupButtonRef = ref(null);

const authState = ref(props.initialAuthState || 'login'); // login | signup | check_email | mfa_setup | mfa_verify | login_totp | accept_invite | business_type_setup | outcome_setup | sample_upload | ready
const onboardingV2Enabled = ref(false);
// Nokvo Connect is feature-flagged on the backend (settings.NOKVO_CONNECT_ENABLED).
// We mirror the flag here so the nav button + landing pages stay hidden until
// the operator turns it on in .env. Defaults to ``false`` so a /config fetch
// failure leaves Connect dormant instead of broken-and-visible.
const nokvoConnectEnabled = ref(false);
// Knowledge-base document upload is off by default and only surfaces
// when NOKVO_KB_DOCUMENT_UPLOAD_ENABLED=true in the backend .env. The
// /config endpoint reflects that flag; we hide the Upload card here
// rather than disabling the route, so we don't half-render a busted UI.
const kbDocumentUploadEnabled = ref(false);
const outcomeWizard = ref({
  outcomes: [],
  selected: {},
  agentName: '',
  isSaving: false,
});

// ── Real-estate setup wizard ────────────────────────────────────────────
// Real-estate orgs follow a custom onboarding path after the
// business-type screen:
//   projects → fields (site visits + leads) → single prompt →
//   tool selection → agent naming → ready
// This state machine drives every step and gets reset when the wizard
// completes or the user logs out.
const realEstateWizardSteps = ['projects', 'fields', 'prompt', 'tools', 'naming'];
const realEstateOnboardingStep = ref('projects');
const realEstateAgentDraft = ref({
  prompt: '',
  toolKeys: [],
  agentName: '',
  isSaving: false,
});

const _emptyProjectDraft = () => ({
  id: null,
  name: '',
  location: '',
  rera_number: '',
  property_type: '',
  price_min: '',
  price_max: '',
  price_display: '',
  configurations: '',
  amenities: '',
  description: '',
  possession_date: '',
  builder_name: '',
  brochure_url: '',
  contact_phone: '',
  // WhatsApp pre-set messages (Meta-approved template names). Location is
  // auto-sent on a site-visit booking; brochure is sent from whatsapp_mode.
  wa_location_template: '',
  wa_location_maps_url: '',
  wa_brochure_template: '',
  // Per-project WhatsApp Business sender — brochure + location send from this number.
  wa_sender_number: '',
});

const projects = ref([]);
const isLoadingProjects = ref(false);
const projectDraft = ref(_emptyProjectDraft());
const isSavingProject = ref(false);
const isDeletingProjectId = ref(null);
const realEstateFieldsEditor = ref({
  // Leads are fixed to name + phone + call notes — only the Site Visit (tickets)
  // schema is configurable during onboarding.
  tickets: [],
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
  // Feature-flagged off — backend has unregistered these routes and would
  // return 404, so don't bother calling.
  if (!nokvoConnectEnabled.value) return;
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
// Razorpay payment-gated onboarding.
const paymentToken = ref('');
const selectedPlan = ref('inbound_only');
const isPaying = ref(false);
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
// Outbound campaigns drive the per-campaign tab strip on the Leads page so
// each campaign's leads are separated. `activeLeadCampaignTab` is the selected
// campaign id, or null for the "All" tab.
const outboundCampaigns = ref([]);
const activeLeadCampaignTab = ref(null);
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
// The agent shown on the dedicated "Agent" tab — the active one, else the first.
const profileAgent = computed(() => activeAgent.value || agents.value[0] || null);
const renameDraft = ref('');
const isRenamingAgent = ref(false);
// Keep the rename field aligned with whichever agent is currently shown.
watch(() => profileAgent.value?.id, () => {
  renameDraft.value = profileAgent.value?.name || '';
});
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
// Per-organization conversation cost summary. Backed by
// GET /api/nokvo-one/cost-summary — today / this-month / all-time totals
// at ₹8 / minute. ``null`` until the first load resolves.
const costSummary = ref(null);
const isLoadingCostSummary = ref(false);
// Notifications — REST-loaded inbox + live WS fanout.
// ``unreadCount`` powers the bell badge. The WS is opened once on login
// and re-opened on token rotation; ``notificationsSocket`` holds the
// live instance so onBeforeUnmount + logout can close it cleanly.
const notifications = ref([]);
const unreadCount = ref(0);
const isNotificationsOpen = ref(false);
const isLoadingNotifications = ref(false);
const notificationsSocket = ref(null);
const phoneLink = ref(null);
const phoneLinkInput = ref('');
// Concierge WhatsApp onboarding (the client never sees Plivo): the portal shows
// not_requested → setting_up → connected; the operator fulfils it out-of-band.
const whatsappLink = ref(null);
const whatsappForm = ref({ business_name: '', contact_number: '', display_name: '' });
const isRequestingWhatsapp = ref(false);
// Transcripts page: the call list (from CallCost + has_transcript) for this org.
const transcripts = ref([]);
const transcriptsLoading = ref(false);
const isSavingPhoneLink = ref(false);
const campaigns = ref([]);
// Campaign-objective codes. Source of truth for the multi-select dropdown
// AND the FSM gate on the backend. Adding a new code = update both ends.
const CAMPAIGN_OBJECTIVE_OPTIONS = [
  { value: 'site_visit', label: 'Book a site visit', description: 'Drive the lead toward visiting one of your projects.' },
  { value: 'lead', label: 'Capture a lead', description: 'Record name + phone + your Lead Fields so the team can follow up.' },
];
const defaultCampaignExitConditions = [
  'Lead asks not to be called again.',
  'Lead says they are not interested.',
  'Lead says this is the wrong number.',
  'Lead is busy and asks for a callback.',
].join('\n');
const emptyCampaignForm = () => ({
  name: '',
  from_number: '',
  agent_prompt: 'You are making a consented outbound call. Be concise, explain the reason for the call, and guide the lead toward one clear next step.',
  // List of objective codes from CAMPAIGN_OBJECTIVE_OPTIONS. Defaults to
  // both selected so a fresh campaign is willing to do either next step.
  objectives: ['site_visit', 'lead'],
  exit_conditions: defaultCampaignExitConditions,
  tone: 'warm, direct, and respectful',
  silence_timeout_seconds: 5,
  // Follow-up rules block — mirrors DEFAULT_FOLLOWUP_RULES on the backend
  // (app/services/followup_scheduler_service.py). Stored alongside the
  // other campaign fields and serialised into agent_config.followup_rules
  // by the backend on create. Defaults: 3 attempts global, 9–7 IST window.
  followup_rules: {
    enabled: true,
    max_attempts_per_lead: 3,
    rules: {
      no_answer: { delay_minutes: 120, max_attempts: 3 },
      voicemail: { delay_minutes: 1440, max_attempts: 2 },
      failed: { delay_minutes: 240, max_attempts: 2 },
      partial: { delay_minutes: 4320, max_attempts: 1 },
    },
    call_window: {
      start_local: '09:00',
      end_local: '19:00',
      timezone: 'Asia/Kolkata',
    },
  },
});

const toggleCampaignObjective = (code) => {
  const idx = (campaignForm.value.objectives || []).indexOf(code);
  if (idx >= 0) {
    campaignForm.value.objectives.splice(idx, 1);
  } else {
    campaignForm.value.objectives = [...(campaignForm.value.objectives || []), code];
  }
};

const campaignObjectiveLabel = (code) => {
  const opt = CAMPAIGN_OBJECTIVE_OPTIONS.find((o) => o.value === code);
  return opt ? opt.label : code;
};
const campaignForm = ref(emptyCampaignForm());
const isCreatingCampaign = ref(false);
const isLaunchingCampaign = ref(null);
// The tenant's allotted outbound caller-ID number + plan flag (GET /outbound-number).
const outboundNumber = ref({ number: null, status: null, calling_enabled: false });
// When set, the create form builds the campaign from a whole form's callable
// leads and launches immediately ("Create & call this form"). Shape:
// { capture_form_id, formName, callableCount }.
const campaignFormPreset = ref(null);
const outgoingTab = ref('campaigns');
const leadConnections = ref([]);
const leadForms = ref([]);
const outgoingLeads = ref([]);
const selectedLeadIds = ref([]);
const isLoadingLeadSources = ref(false);
const isSyncingLeadConnection = ref(null);
const pendingLeadOAuth = ref(null);
const connectionAccountInputs = ref({});
const NOKVO_FORM_FIELD_TYPES = [
  { value: 'text', label: 'Text' },
  { value: 'email', label: 'Email' },
  { value: 'phone', label: 'Phone' },
  { value: 'textarea', label: 'Long text' },
  { value: 'select', label: 'Dropdown' },
  { value: 'number', label: 'Number' },
];

const _defaultNokvoLeadForm = () => ({
  name: '',
  consent_text: 'I agree to receive a phone call from this business about my enquiry.',
  fields: [
    { label: 'Email', type: 'email', required: false },
  ],
});

const nokvoLeadForm = ref(_defaultNokvoLeadForm());
const lastNokvoFormLink = ref(null); // Most recent public URL we minted, for one-click copy.

const addNokvoFormField = () => {
  nokvoLeadForm.value.fields.push({ label: '', type: 'text', required: false });
};

const removeNokvoFormField = (index) => {
  nokvoLeadForm.value.fields.splice(index, 1);
};

const copyNokvoFormLink = async () => {
  const link = lastNokvoFormLink.value;
  if (!link) return;
  try {
    await navigator.clipboard.writeText(link);
    infoMsg.value = 'Form link copied to clipboard.';
  } catch {
    // Clipboard access can be denied in test environments — fall through quietly.
  }
};
const selectedCallableLeads = computed(() =>
  outgoingLeads.value.filter((lead) => selectedLeadIds.value.includes(lead.id) && lead.callable),
);

// ── Leads sorted by their originating form (= campaign/project), Option A ──────
// null = "All forms". A sentinel for leads with no capture form (manual/legacy).
const LEAD_FORM_NONE = '__none__';
const activeLeadFormId = ref(null);

// Map capture_form_id → form name, so each lead row shows which form/project it
// came from. Falls back to the form's provider id when unnamed.
const formNameById = computed(() => {
  const map = {};
  for (const form of leadForms.value) map[form.id] = form.name || form.provider_form_id || 'Lead form';
  return map;
});
const leadFormName = (lead) =>
  (lead?.capture_form_id && formNameById.value[lead.capture_form_id]) || 'No form';

// Filter chips: "All" + one per form (with live counts from the API) + an
// "Uncategorized" bucket only when such leads exist.
const leadFormFilters = computed(() => {
  const chips = [{ id: null, name: 'All forms', total: outgoingLeads.value.length }];
  for (const form of leadForms.value) {
    chips.push({
      id: form.id,
      name: form.name || form.provider_form_id || 'Lead form',
      total: form.lead_count ?? 0,
      callable: form.callable_lead_count ?? 0,
    });
  }
  const noFormCount = outgoingLeads.value.filter((l) => !l.capture_form_id).length;
  if (noFormCount) chips.push({ id: LEAD_FORM_NONE, name: 'Uncategorized', total: noFormCount });
  return chips;
});

// The leads shown under the active filter.
const visibleLeads = computed(() => {
  if (activeLeadFormId.value === null) return outgoingLeads.value;
  if (activeLeadFormId.value === LEAD_FORM_NONE) return outgoingLeads.value.filter((l) => !l.capture_form_id);
  return outgoingLeads.value.filter((l) => l.capture_form_id === activeLeadFormId.value);
});

const setLeadFormFilter = (id) => {
  activeLeadFormId.value = id;
};

// Select every callable lead currently visible (i.e. in the active form) — the
// one-click "build a campaign from this form's leads" path.
const selectAllCallableInForm = () => {
  const ids = visibleLeads.value.filter((l) => l.callable).map((l) => l.id);
  const set = new Set(selectedLeadIds.value);
  for (const id of ids) set.add(id);
  selectedLeadIds.value = [...set];
};

const clearLeadSelection = () => {
  selectedLeadIds.value = [];
};
const OUTBOUND_OBJECTIVE_OPTIONS = [
  { value: 'lead_qualification', label: 'Lead qualification' },
  { value: 'demo_booking', label: 'Demo booking' },
  { value: 'info_outreach', label: 'Info outreach' },
  { value: 'survey', label: 'Survey' },
  { value: 'renewal', label: 'Renewal' },
];

// Outbound tester now drives off a real campaign's config — admins pick one
// of the org's saved campaigns and rehearse with the exact agent_prompt /
// objectives / doc_text / persona that the production dialler would use.
// (The old localStorage `nokvo_one_outbound_tester_preset_v1` workflow has
// been removed; the campaign row IS the preset.)
const _defaultOutboundTester = () => ({
  caller_name: 'Riya',
  company_name: '',
  pitch_summary: '',
  objective: 'lead_qualification',
  name: 'Outbound test call',
  goal: '',
  agent_prompt: '',
  objectives: '',
  exit_conditions: '',
  tone: 'warm, direct, and respectful',
  language: 'en',
});

const outboundTester = ref(_defaultOutboundTester());
// `selectedTesterCampaignId` is the id of the campaign whose config will be
// loaded server-side for the rehearsal. Null = fall back to the inline form.
const selectedTesterCampaignId = ref(null);
// Populated by the WS `outcome` event the server emits at end-of-call.
// Shape: { outcome, reason, lead_id, uncategorized, campaign_id, campaign_name }.
// Rendered as a banner inside the Tester card so the operator immediately
// sees where the test lead landed (or that it was discarded).
const outboundTesterOutcome = ref(null);

// Per-second billing rate matches the server (₹8/minute → ₹2/15 per
// second). We display the precise live total; the persisted ledger
// uses the same Decimal math on the backend, so the dashboard tile
// reconciles exactly with what the meter shows when the call ends.
const COST_RUPEES_PER_SECOND = 8 / 60;

const voice = ref({
  ws: null,
  audioCtx: null,
  micStream: null,
  micNode: null,
  mode: 'inbound',
  status: 'idle', // idle | connecting | listening | thinking | speaking | error
  callId: null,
  // Live cost meter. ``callStartedAt`` is the wall-clock anchor the
  // backend uses too (set when the WS opens); ``elapsedSeconds`` ticks
  // up via a 1s interval while the call is active. Cleared on cleanup.
  callStartedAt: null,
  elapsedSeconds: 0,
  costTimer: null,
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
  items: [],
  isLoading: false,
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

// Org-wide site-visit working hours (real estate). One window for the whole
// org — replaces per-member timings. Members inherit it; the voice agent
// refuses out-of-hours site visits against it.
const ORG_HOURS_DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
const orgWorkingHours = ref({
  working_days: ['mon', 'tue', 'wed', 'thu', 'fri'],
  start_time: '09:00',
  end_time: '19:00',
  working_hours_summary: '',
});
const isLoadingOrgHours = ref(false);
const isSavingOrgHours = ref(false);

const loadOrgWorkingHours = async () => {
  if (!isRealEstateTemplate.value) return;
  isLoadingOrgHours.value = true;
  try {
    const { data } = await api.get('/members/assignment-defaults', { headers: authHeader() });
    orgWorkingHours.value = {
      working_days: Array.isArray(data.working_days) && data.working_days.length
        ? data.working_days
        : ['mon', 'tue', 'wed', 'thu', 'fri'],
      start_time: data.start_time || '09:00',
      end_time: data.end_time || '19:00',
      working_hours_summary: data.working_hours_summary || '',
    };
  } catch (err) {
    // Non-fatal: keep the defaults already in the ref.
  } finally {
    isLoadingOrgHours.value = false;
  }
};

const toggleOrgHoursDay = (day) => {
  const days = new Set(orgWorkingHours.value.working_days || []);
  if (days.has(day)) days.delete(day);
  else days.add(day);
  orgWorkingHours.value.working_days = ORG_HOURS_DAYS.filter((d) => days.has(d));
};

const saveOrgWorkingHours = async () => {
  if (!isAdmin.value) return;
  const hrs = orgWorkingHours.value;
  if (!hrs.working_days?.length || !hrs.start_time || !hrs.end_time) {
    errorMsg.value = 'Pick at least one day and a start/end time.';
    return;
  }
  if (hrs.start_time >= hrs.end_time) {
    errorMsg.value = 'Start time must be before end time.';
    return;
  }
  isSavingOrgHours.value = true;
  try {
    const { data } = await api.put(
      '/members/assignment-defaults',
      {
        working_days: hrs.working_days,
        start_time: hrs.start_time,
        end_time: hrs.end_time,
      },
      { headers: authHeader() },
    );
    orgWorkingHours.value.working_hours_summary = data.working_hours_summary || '';
    infoMsg.value = 'Organization working hours saved.';
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Could not save working hours.');
  } finally {
    isSavingOrgHours.value = false;
  }
};

// Site-visit claim pool (real estate). Voice-created visits land unassigned;
// members claim the ones they want, which allots the visit to them.
const siteVisitFilter = ref('unclaimed'); // 'unclaimed' | 'mine' | 'all'
const claimableSiteVisits = ref([]);
const myAssignedTickets = ref([]);
const isLoadingClaimable = ref(false);
const claimingVisitId = ref(null);

const loadClaimableSiteVisits = async () => {
  if (!isRealEstateTemplate.value) return;
  isLoadingClaimable.value = true;
  try {
    const { data } = await api.get('/members/me/claimable-site-visits', { headers: authHeader() });
    claimableSiteVisits.value = Array.isArray(data) ? data : [];
  } catch (err) {
    claimableSiteVisits.value = [];
  } finally {
    isLoadingClaimable.value = false;
  }
};

const loadMyAssignedTickets = async () => {
  if (!isRealEstateTemplate.value) return;
  try {
    const { data } = await api.get('/members/me/assigned-records', {
      headers: authHeader(),
      params: { record_type: 'ticket' },
    });
    myAssignedTickets.value = Array.isArray(data) ? data : [];
  } catch (err) {
    myAssignedTickets.value = [];
  }
};

const refreshSiteVisitPool = async () => {
  await Promise.all([loadClaimableSiteVisits(), loadMyAssignedTickets()]);
};

const claimSiteVisit = async (record) => {
  if (!record?.id || claimingVisitId.value) return;
  claimingVisitId.value = record.id;
  try {
    await api.post(`/members/me/site-visits/${record.id}/claim`, {}, { headers: authHeader() });
    infoMsg.value = 'Site visit claimed — it is now assigned to you.';
    await refreshSiteVisitPool();
  } catch (err) {
    // Stale pool: another member claimed it first. Tell the user plainly and
    // refresh so the taken row drops out of their view.
    if (err?.response?.status === 409) {
      errorMsg.value = 'Sorry, another agent just claimed this visit.';
      await loadClaimableSiteVisits();
    } else {
      errorMsg.value = extractErrorMessage(err, 'Could not claim this site visit.');
    }
  } finally {
    claimingVisitId.value = null;
  }
};

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
const isRealEstateTemplate = computed(() => currentOrganization.value?.industry === 'real_estate');
const ticketsTabLabel = computed(() => (isRealEstateTemplate.value ? 'Site Visits' : 'Tickets'));
const ticketSingularLabel = computed(() => (isRealEstateTemplate.value ? 'site visit' : 'ticket'));
const ticketTitleLabel = computed(() => (isRealEstateTemplate.value ? 'Site Visit' : 'Ticket'));
const ticketFieldTitle = computed(() => `${ticketTitleLabel.value} Fields`);
const businessTypeTabsLabel = (option) => {
  const ticketsLabel = option?.value === 'real_estate' ? 'Site Visits' : 'Tickets';
  return option?.tabs?.includes('appointments')
    ? `Leads, ${ticketsLabel}, and Appointments`
    : `Leads and ${ticketsLabel}`;
};
const memberPageLabel = computed(() => currentBusinessTemplate.value?.member_label || 'Members');
const businessTypeRequired = computed(() => !currentOrganization.value?.industry);
const businessTemplateTabs = computed(() => currentBusinessTemplate.value?.tabs || []);
const showAppointmentsTab = computed(() => businessTemplateTabs.value.includes('appointments'));
// Config-driven, like appointments. Clinics drop the generic Tickets tab
// (appointments are their primary record), so this is false for clinics.
const showTicketsTab = computed(() => businessTemplateTabs.value.includes('tickets'));
const isClinicTemplate = computed(() => currentOrganization.value?.industry === 'clinics');
const schemaFor = (key) => currentBusinessTemplate.value?.schemas?.[key] || [];

// Field-type → label/icon mapping used by the beautified schema cards
// on the Site Visits and Leads tabs. Type strings come straight from
// the business template so the table covers everything the editor
// emits (text, phone, email, select, date, currency, textarea, …).
// Falling back to FileText + capitalised name keeps unknown types
// readable without crashing.
const fieldTypeIcon = (type) => {
  const normalised = String(type || '').toLowerCase();
  if (normalised === 'phone') return PhoneCall;
  if (normalised === 'email') return MessageSquare;
  if (normalised === 'select') return Layers;
  if (normalised === 'multiselect' || normalised === 'multi_select') return Layers;
  if (normalised === 'date' || normalised === 'datetime') return CalendarDays;
  if (normalised === 'currency' || normalised === 'number') return Wallet;
  if (normalised === 'url' || normalised === 'website') return Globe;
  if (normalised === 'textarea' || normalised === 'longtext') return FileText;
  if (normalised === 'user' || normalised === 'person') return Users;
  return FileText;
};

const fieldTypeLabel = (type) => {
  const s = String(type || '').replace(/_/g, ' ').trim();
  if (!s) return 'Text';
  return s.charAt(0).toUpperCase() + s.slice(1);
};
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
  if (page === 'tickets' && !showTicketsTab.value) return;
  // Ticket Board is a real-estate-only claim surface (members + admins).
  if (page === 'ticket_board' && !isRealEstateTemplate.value) return;
  // Clinics have no Leads tab (callers land in the Customer base instead),
  // and the Customer base only exists for clinics. Swallow deep-links /
  // programmatic navigation to the wrong surface.
  if (page === 'leads' && isClinicTemplate.value) return;
  if (page === 'customers' && !isClinicTemplate.value) return;
  // Nokvo Connect is feature-flagged. When disabled, swallow any attempt to
  // navigate to its landing pages (nav button is already hidden, but this
  // catches deep-links / programmatic calls) so the user can't reach a
  // broken view.
  if ((page === 'nokvo_connect' || page === 'nokvo_connect_step2') && !nokvoConnectEnabled.value) {
    return;
  }
  currentPage.value = page;
  // Drive Vue Router so the URL reflects the active page. The route watcher
  // below mirrors the reverse direction (URL → currentPage) so deep-links
  // and refreshes land on the right view.
  const targetRouteName = pageKeyToRouteName[page];
  if (targetRouteName && route.name !== targetRouteName) {
    router.push({ name: targetRouteName }).catch((err) => {
      // NavigationDuplicated is a no-op; swallow silently.
      if (err && err.name !== 'NavigationDuplicated') {
        // Don't break the legacy currentPage update on a routing miss.
        console.warn('[switchPage] router.push failed:', err);
      }
    });
  }
  errorMsg.value = '';
  infoMsg.value = '';
  if (page === 'my_timetable') {
    loadMyTimetable();
    return;
  }
  if (page === 'followups') {
    loadFollowupPipeline();
    return;
  }
  if (page === 'services') {
    loadClinicServices();
    return;
  }
  if (page === 'agent') {
    loadRuntimeStatus();
    loadPhoneLink();
    // The agent picker is hidden here, so make sure the tester has an agent
    // selected, and seed the rename field with the current name.
    if (!activeAgent.value && agents.value.length) {
      activeAgent.value = agents.value[0];
    }
    renameDraft.value = profileAgent.value?.name || '';
  }
  if (page === 'outgoing_agent') {
    loadOutgoingAgentWorkspace();
    loadCampaigns();
  }
  if (page === 'transcripts') {
    loadTranscripts();
  }
  if (['leads', 'tickets', 'appointments'].includes(page)) {
    loadTabRecords(page);
  }
  if ((page === 'tickets' || page === 'ticket_board') && isRealEstateTemplate.value) {
    refreshSiteVisitPool();
  }
  if (page === 'nokvo_connect_step2') {
    loadConnectKeys();
  }
  if (page === 'projects') {
    loadProjects();
    projectDraft.value = _emptyProjectDraft();
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
  plivo_telephony: 'Plivo telephony (subaccount + number)',
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
    case 'plivo_telephony':
      if (summary.exotel_status === 'pending_provisioning')
        return 'Reserved · Plivo not configured yet';
      return summary.exotel_status === 'linked'
        ? 'Subaccount + number provisioned'
        : summary.exotel_status || 'Number pending carrier verification';
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
      shape: 'rectangular',
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
    if (data.payment_required && data.payment_token) {
      // New org via Google — pay before resources provision. Keep the Google
      // session (the org is pending_payment so the dashboard stays gated until
      // payment flips it to active).
      paymentToken.value = data.payment_token;
      if (data.access_token && data.user) persistSession(data);
      signup.value.admin_email = data.email || signup.value.admin_email;
      if (!selectedPlan.value) selectedPlan.value = 'inbound_only';
      await resumePaymentScreen();
      return;
    }
    if (data.code === 'onboarding_required') {
      persistSession(data);
      await beginOnboardingWizard();
      return;
    }
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
    nokvoConnectEnabled.value = !!data?.nokvo_connect_enabled;
    kbDocumentUploadEnabled.value = !!data?.kb_document_upload_enabled;
  } catch (_) {
    authConfig.value = { google_client_id: '', google_login_enabled: false };
    onboardingV2Enabled.value = false;
    nokvoConnectEnabled.value = false;
    kbDocumentUploadEnabled.value = false;
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
  // Mid-onboarding (e.g. a reload while the org is still in the wizard) → reopen
  // the wizard at the saved step instead of the dashboard.
  if (currentOrganization.value?.status === 'onboarding') {
    await beginOnboardingWizard();
    return;
  }
  await loadBusinessTypeOptions();
  if (businessTypeRequired.value) {
    // Real estate is the only vertical — skip the business-type picker, assign
    // real_estate automatically, and go straight into the real-estate wizard.
    selectedBusinessType.value = 'real_estate';
    await saveBusinessType();
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
    // Cost summary is a small additional GET — fire it after the main
    // bundle so a billing-endpoint hiccup can't fail the whole load.
    // We don't await it; the card renders a skeleton until the data
    // arrives a few hundred ms later.
    loadCostSummary();
    // Follow-up agent: refresh the dashboard tile once at boot. Per-lead
    // chips load lazily via loadFollowupsForLead when the user expands a row.
    loadFollowupSummary();
    // Notifications: bulk inbox load + live WS subscription. Both are
    // fire-and-forget; failures are silent so a notifications outage
    // doesn't degrade the rest of the workspace.
    loadNotifications();
    connectNotificationsSocket();
    // Org-wide site-visit working hours (real estate only) — fire-and-forget.
    loadOrgWorkingHours();
    if (['leads', 'tickets', 'appointments'].includes(currentPage.value)) {
      await loadTabRecords(currentPage.value);
    }
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Failed to load workspace.');
  } finally {
    isLoadingMembers.value = false;
  }
};

// Render a cost-summary bucket's ``seconds`` (Decimal-as-string from the
// server) as a 1-dp minutes label. We keep the conversion on the client
// because the server already ships the raw seconds — re-quantising to
// minutes here means we never lie about the ledger duration, just round
// the display. ``—`` is the empty-state for "no data yet".
const formatMinutes = (seconds) => {
  if (seconds === null || seconds === undefined || seconds === '') return '—';
  const n = Number(seconds);
  if (!Number.isFinite(n) || n < 0) return '—';
  if (n === 0) return '0 min';
  if (n < 60) return `${Math.round(n)}s`;
  return `${(n / 60).toFixed(1)} min`;
};

const loadCostSummary = async () => {
  // Idempotent reloader for the dashboard cost card. The endpoint
  // returns today / this-month / all-time totals — we never recompute
  // from durations on the client because the server has the rate-lock
  // logic for tariff changes.
  if (isLoadingCostSummary.value) return;
  isLoadingCostSummary.value = true;
  try {
    const { data } = await api.get('/cost-summary', { headers: authHeader() });
    costSummary.value = data;
  } catch {
    // Silent failure — the card stays in its skeleton state, which is
    // less alarming than a red banner for a non-critical widget.
  } finally {
    isLoadingCostSummary.value = false;
  }
};

// ── Notifications ────────────────────────────────────────────────────
//
// Two-track design:
//   1. REST  — initial bulk load on login + a reload on bell open. This
//              is what the user sees on a fresh page open or after a
//              reconnect; it covers anything that arrived while the WS
//              was down.
//   2. WS    — live fanout on /api/nokvo-one/notifications/ws. We prepend
//              fresh notifications and bump the badge in real time.
//
// On logout / unmount we close the socket so the in-process bus on the
// server can drop our queue.

const loadNotifications = async () => {
  if (isLoadingNotifications.value) return;
  isLoadingNotifications.value = true;
  try {
    const [{ data: inbox }, { data: count }] = await Promise.all([
      api.get('/notifications', { headers: authHeader(), params: { limit: 50 } }),
      api.get('/notifications/unread-count', { headers: authHeader() }),
    ]);
    notifications.value = inbox?.items || [];
    unreadCount.value = Number(count?.unread || 0);
  } catch {
    // Silent — the bell stays in its current state. A failed inbox
    // shouldn't block the rest of the app.
  } finally {
    isLoadingNotifications.value = false;
  }
};

const openNotifications = async () => {
  isNotificationsOpen.value = !isNotificationsOpen.value;
  if (isNotificationsOpen.value) {
    // Refresh on open so the panel reflects anything the WS may have
    // missed during a brief disconnect.
    await loadNotifications();
  }
};

const markNotificationRead = async (notification) => {
  if (!notification || notification.read_at) return;
  try {
    await api.post(`/notifications/${notification.id}/read`, {}, { headers: authHeader() });
    // Optimistic local mutation — avoids a re-fetch round-trip for a
    // single-row state change the user just performed.
    notification.read_at = new Date().toISOString();
    unreadCount.value = Math.max(0, unreadCount.value - 1);
  } catch {
    // No-op — the next refresh will reconcile.
  }
};

const markAllNotificationsRead = async () => {
  try {
    await api.post('/notifications/read-all', {}, { headers: authHeader() });
    const now = new Date().toISOString();
    notifications.value = notifications.value.map((n) =>
      n.read_at ? n : { ...n, read_at: now }
    );
    unreadCount.value = 0;
  } catch {
    // No-op.
  }
};

const closeNotificationsSocket = () => {
  const sock = notificationsSocket.value;
  notificationsSocket.value = null;
  if (sock && sock.readyState !== WebSocket.CLOSED) {
    try { sock.close(); } catch { /* swallow */ }
  }
};

const connectNotificationsSocket = () => {
  // We open one WS per session. If a previous one is alive (e.g., the
  // user just rotated their token) close it first so the in-process
  // bus on the server doesn't hold a stale subscriber.
  closeNotificationsSocket();
  const token = localStorage.getItem(ACCESS_TOKEN_KEY);
  if (!token) return;
  const url = wsUrl(`/api/nokvo-one/notifications/ws?token=${encodeURIComponent(token)}`);
  let sock;
  try {
    sock = new WebSocket(url);
  } catch {
    return;
  }
  notificationsSocket.value = sock;
  sock.onmessage = (evt) => {
    let payload;
    try { payload = JSON.parse(evt.data); } catch { return; }
    if (payload?.type !== 'notification' || !payload?.data) return;
    const fresh = payload.data;
    // Drop dupes the REST inbox may already carry — the live fanout
    // and the REST load can race on a fresh page open.
    const existing = notifications.value.findIndex((n) => n.id === fresh.id);
    if (existing >= 0) {
      notifications.value.splice(existing, 1, fresh);
    } else {
      notifications.value = [fresh, ...notifications.value].slice(0, 100);
    }
    if (!fresh.read_at) unreadCount.value += 1;
  };
  sock.onclose = () => {
    if (notificationsSocket.value === sock) notificationsSocket.value = null;
  };
};

const loadTabRecords = async (tab) => {
  if (!tab) return;
  if (tab === 'appointments' && !showAppointmentsTab.value) return;
  // Clinics have no leads tab — single guard so every call site (mount,
  // timetable, WS refresh) skips the request instead of surfacing an error.
  if (tab === 'leads' && isClinicTemplate.value) return;
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
  // The Leads tab strip is driven by the org's outbound campaigns. Refresh the
  // list alongside the lead records so a newly-created campaign appears as a
  // tab without requiring a full page reload.
  if (tab === 'leads') {
    try {
      const { data: campaigns } = await api.get('/agents/campaigns', {
        headers: authHeader(),
      });
      outboundCampaigns.value = Array.isArray(campaigns) ? campaigns : [];
    } catch (err) {
      // Non-fatal: leads still render under "All" if campaigns can't load.
      outboundCampaigns.value = [];
    }
  }
};

// Sentinel tab id for the Uncategorized strip. Records land here when the
// outbound tester classifies the call as "partial" — soft yes, call-later,
// "send details" — and the backend persists them with data.uncategorized=true.
const UNCATEGORIZED_TAB_ID = '__uncategorized__';

const _isUncategorizedRecord = (record) => Boolean(record?.data?.uncategorized);

// Leads filtered to the active campaign tab. null = "All Leads",
// UNCATEGORIZED_TAB_ID = the global uncategorized bucket, otherwise filter
// by data.campaign_id.
const filteredLeadRecords = computed(() => {
  const all = tabRecords.value.leads || [];
  const active = activeLeadCampaignTab.value;
  if (!active) return all;
  if (active === UNCATEGORIZED_TAB_ID) {
    return all.filter(_isUncategorizedRecord);
  }
  return all.filter(
    (r) => !_isUncategorizedRecord(r)
      && String(r?.data?.campaign_id || '') === String(active),
  );
});

// Tab strip composition:
//   - "All Leads" first (always)
//   - One tab per existing campaign (always, even with zero records — matches
//     the product spec "a tab for that campaign is created in the leads page
//     as well" the moment the campaign exists)
//   - "Uncategorized" last (only when there are uncategorized records OR
//     campaigns exist — the operator should see the bucket exists)
const leadCampaignTabs = computed(() => {
  const all = tabRecords.value.leads || [];
  const counts = new Map();
  let uncategorizedCount = 0;
  for (const record of all) {
    if (_isUncategorizedRecord(record)) {
      uncategorizedCount += 1;
      continue;
    }
    const cid = record?.data?.campaign_id;
    if (!cid) continue;
    counts.set(String(cid), (counts.get(String(cid)) || 0) + 1);
  }
  const tabs = [
    { id: null, name: 'All Leads', count: all.length },
  ];
  for (const campaign of outboundCampaigns.value || []) {
    const cid = String(campaign.id);
    tabs.push({
      id: cid,
      name: campaign.name || `Campaign ${cid.slice(0, 6)}`,
      count: counts.get(cid) || 0,
    });
  }
  if (uncategorizedCount > 0 || (outboundCampaigns.value || []).length > 0) {
    tabs.push({
      id: UNCATEGORIZED_TAB_ID,
      name: 'Uncategorized',
      count: uncategorizedCount,
      uncategorized: true,
    });
  }
  return tabs;
});

const setActiveLeadCampaign = (id) => {
  activeLeadCampaignTab.value = id;
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
    // Real-estate orgs go through a custom project → fields → prompt → tools →
    // naming wizard regardless of the v2 flag. Other industries fall back to
    // the outcome wizard (v2) or the dashboard (v1).
    if (data.business_template?.value === 'real_estate' || data.organization?.industry === 'real_estate') {
      await loadWorkspace();
      await beginRealEstateWizard();
      return;
    }
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

// ── Real-estate setup wizard helpers ───────────────────────────────────
const _projectDraftFromRecord = (project) => ({
  id: project.id,
  name: project.name || '',
  location: project.location || '',
  rera_number: project.rera_number || '',
  property_type: project.property_type || '',
  price_min: project.price_min == null ? '' : String(project.price_min),
  price_max: project.price_max == null ? '' : String(project.price_max),
  price_display: project.price_display || '',
  configurations: Array.isArray(project.configurations) ? project.configurations.join(', ') : '',
  amenities: Array.isArray(project.amenities) ? project.amenities.join(', ') : '',
  description: project.description || '',
  possession_date: project.possession_date || '',
  builder_name: project.builder_name || '',
  brochure_url: project.brochure_url || '',
  contact_phone: project.contact_phone || '',
  wa_location_template: project.whatsapp?.location?.template || '',
  wa_location_maps_url: project.whatsapp?.location?.maps_url || '',
  wa_brochure_template: project.whatsapp?.brochure?.template || '',
  wa_sender_number: project.whatsapp?.sender_number || '',
});

const _projectPayloadFromDraft = (draft) => {
  const splitList = (value) => String(value || '')
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
  const numericOrNull = (value) => {
    if (value === null || value === undefined || value === '') return null;
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
  };
  const trimmed = (value) => {
    if (value === null || value === undefined) return null;
    const text = String(value).trim();
    return text.length ? text : null;
  };
  // Assemble the per-project WhatsApp config (Meta-approved templates). Only
  // include a sub-block when its template name is set, so an unconfigured
  // project sends nothing.
  const whatsapp = {};
  const locTemplate = trimmed(draft.wa_location_template);
  if (locTemplate) {
    whatsapp.location = { template: locTemplate, language: 'en', maps_url: trimmed(draft.wa_location_maps_url) || '' };
  }
  const brocTemplate = trimmed(draft.wa_brochure_template);
  if (brocTemplate) {
    whatsapp.brochure = { template: brocTemplate, language: 'en' };
  }
  // Per-project WhatsApp sender: the brochure + location go out FROM this number
  // (overrides the tenant default). Stored independently of the template names.
  const waSender = trimmed(draft.wa_sender_number);
  if (waSender) {
    whatsapp.sender_number = waSender;
  }
  return {
    name: (draft.name || '').trim(),
    location: trimmed(draft.location),
    rera_number: trimmed(draft.rera_number),
    property_type: trimmed(draft.property_type),
    price_min: numericOrNull(draft.price_min),
    price_max: numericOrNull(draft.price_max),
    price_display: trimmed(draft.price_display),
    configurations: splitList(draft.configurations),
    amenities: splitList(draft.amenities),
    description: trimmed(draft.description),
    possession_date: trimmed(draft.possession_date),
    builder_name: trimmed(draft.builder_name),
    brochure_url: trimmed(draft.brochure_url),
    contact_phone: trimmed(draft.contact_phone),
    whatsapp,
  };
};

const loadProjects = async () => {
  if (!isRealEstateTemplate.value) return;
  isLoadingProjects.value = true;
  try {
    const { data } = await api.get('/projects/', { headers: authHeader() });
    projects.value = Array.isArray(data) ? data : [];
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Failed to load projects.');
  } finally {
    isLoadingProjects.value = false;
  }
};

const startEditProject = (project) => {
  projectDraft.value = _projectDraftFromRecord(project);
};

const startNewProject = () => {
  projectDraft.value = _emptyProjectDraft();
};

const saveProject = async () => {
  const draft = projectDraft.value;
  if (!draft.name || !draft.name.trim()) {
    errorMsg.value = 'Project name is required.';
    return;
  }
  isSavingProject.value = true;
  errorMsg.value = '';
  try {
    const payload = _projectPayloadFromDraft(draft);
    let saved;
    if (draft.id) {
      const { data } = await api.patch(`/projects/${draft.id}`, payload, { headers: authHeader() });
      saved = data;
      const idx = projects.value.findIndex((p) => p.id === saved.id);
      if (idx >= 0) projects.value.splice(idx, 1, saved);
    } else {
      const { data } = await api.post('/projects/', payload, { headers: authHeader() });
      saved = data;
      projects.value = [saved, ...projects.value];
    }
    infoMsg.value = `Project "${saved.name}" saved.`;
    projectDraft.value = _emptyProjectDraft();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Project could not be saved.');
  } finally {
    isSavingProject.value = false;
  }
};

const isAnalyzingBrochure = ref(false);

// Brochure Analyzer: upload a brochure (PDF/DOCX/TXT) → a nano-pool LLM extracts
// the project fields and PREFILLS the form for review. The admin verifies, then
// the normal Save creates the project (description capped at 700 tokens server-
// side). The analyzer never creates the project itself.
const analyzeBrochure = async (file) => {
  if (!file) return;
  isAnalyzingBrochure.value = true;
  errorMsg.value = '';
  try {
    const form = new FormData();
    form.append('file', file);
    const { data } = await api.post('/projects/analyze-brochure', form, {
      headers: { ...authHeader(), 'Content-Type': 'multipart/form-data' },
    });
    // Prefill for review. Force a create (no id) — the analyzer returns none.
    projectDraft.value = { ..._projectDraftFromRecord(data || {}), id: undefined };
    infoMsg.value = 'Brochure analyzed — review the prefilled fields, then Save.';
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Could not analyze the brochure.');
  } finally {
    isAnalyzingBrochure.value = false;
  }
};

const deleteProject = async (project) => {
  if (!project?.id) return;
  if (!window.confirm(`Remove project "${project.name}"? This cannot be undone.`)) return;
  isDeletingProjectId.value = project.id;
  try {
    await api.delete(`/projects/${project.id}`, { headers: authHeader() });
    projects.value = projects.value.filter((p) => p.id !== project.id);
    if (projectDraft.value.id === project.id) {
      projectDraft.value = _emptyProjectDraft();
    }
    infoMsg.value = `Project "${project.name}" removed.`;
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Project could not be deleted.');
  } finally {
    isDeletingProjectId.value = null;
  }
};

const beginRealEstateWizard = async () => {
  realEstateOnboardingStep.value = 'projects';
  realEstateAgentDraft.value = {
    prompt: '',
    toolKeys: [...(toolCatalogDefaults.value || [])],
    agentName: 'Property Assistant',
    isSaving: false,
  };
  await loadProjects();
  authState.value = 'real_estate_setup';
};

const refreshRealEstateFieldsEditor = () => {
  const cloneSchema = (key) => schemaFor(key).map((field) => ({ ...field }));
  realEstateFieldsEditor.value = {
    // Only the Site Visit (tickets) schema is configurable; leads are fixed.
    tickets: cloneSchema('tickets'),
    isSaving: false,
  };
};

const advanceRealEstateStep = async () => {
  const current = realEstateOnboardingStep.value;
  if (current === 'projects') {
    if (!projects.value.length) {
      errorMsg.value = 'Add at least one project before continuing.';
      return;
    }
    refreshRealEstateFieldsEditor();
    realEstateOnboardingStep.value = 'fields';
    return;
  }
  if (current === 'fields') {
    realEstateFieldsEditor.value.isSaving = true;
    errorMsg.value = '';
    try {
      const sanitize = (fields) =>
        (fields || []).map((field) => ({
          key: _slugify(field.key || field.label || 'field'),
          label: (field.label || field.key || '').trim() || 'Field',
          type: field.type || 'text',
          required: !!field.required,
        }));
      // Leads are fixed to name + phone + call notes — only persist the Site
      // Visit (tickets) schema during onboarding.
      const tickets = sanitize(realEstateFieldsEditor.value.tickets);
      const ticketsRes = await api.patch(
        '/business-template/schemas/tickets', { fields: tickets }, { headers: authHeader() },
      );
      organizationBusinessTemplate.value = ticketsRes.data;
    } catch (err) {
      errorMsg.value = extractErrorMessage(err, 'Could not save field schemas.');
      realEstateFieldsEditor.value.isSaving = false;
      return;
    } finally {
      realEstateFieldsEditor.value.isSaving = false;
    }
    realEstateOnboardingStep.value = 'prompt';
    return;
  }
  if (current === 'prompt') {
    const prompt = (realEstateAgentDraft.value.prompt || '').trim();
    if (prompt.length < 20) {
      errorMsg.value = 'Prompt must be at least 20 characters.';
      return;
    }
    realEstateOnboardingStep.value = 'tools';
    return;
  }
  if (current === 'tools') {
    realEstateOnboardingStep.value = 'naming';
    return;
  }
  if (current === 'naming') {
    await finishRealEstateWizard();
  }
};

const goBackRealEstateStep = () => {
  const idx = realEstateWizardSteps.indexOf(realEstateOnboardingStep.value);
  if (idx > 0) {
    realEstateOnboardingStep.value = realEstateWizardSteps[idx - 1];
  }
};

const toggleRealEstateTool = (key) => {
  const list = realEstateAgentDraft.value.toolKeys;
  const idx = list.indexOf(key);
  if (idx >= 0) list.splice(idx, 1);
  else list.push(key);
};

const addRealEstateField = (tab) => {
  realEstateFieldsEditor.value[tab].push({
    key: `field_${realEstateFieldsEditor.value[tab].length + 1}`,
    label: 'New field',
    type: 'text',
    required: false,
  });
};

const removeRealEstateField = (tab, index) => {
  if (realEstateFieldsEditor.value[tab].length <= 1) return;
  realEstateFieldsEditor.value[tab].splice(index, 1);
};

const finishRealEstateWizard = async () => {
  const prompt = (realEstateAgentDraft.value.prompt || '').trim();
  const agentName = (realEstateAgentDraft.value.agentName || '').trim() || 'Property Assistant';
  const toolKeys = [...new Set(realEstateAgentDraft.value.toolKeys || [])];
  realEstateAgentDraft.value.isSaving = true;
  errorMsg.value = '';
  try {
    const { data: agent } = await api.post(
      '/agents/',
      {
        name: agentName,
        description: 'Configured during real-estate onboarding.',
        system_prompt: prompt,
        tool_keys: toolKeys,
      },
      { headers: authHeader() },
    );
    agents.value.unshift(agent);
    activeAgent.value = agent;
    try {
      await kbApi.post(
        '/business-facts',
        { business_facts: prompt },
        { headers: authHeader() },
      );
    } catch {
      // Business-facts write is best-effort; the agent runs on the curated
      // per-vertical prompt regardless of whether facts were captured here.
    }
    infoMsg.value = `Your agent "${agent.name}" is ready.`;
    authState.value = 'ready';
    await loadWorkspace();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Could not finish setup.');
  } finally {
    realEstateAgentDraft.value.isSaving = false;
  }
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
  // mode === 'prompt' — captured as the org's Business facts (the agent's
  // persona is the curated per-vertical prompt; this is just org specifics).
  const prompt = (sampleUpload.value.prompt || '').trim();
  if (prompt.length < 20) {
    errorMsg.value = 'Please add at least 20 characters of business details.';
    return;
  }
  sampleUpload.value.isUploading = true;
  errorMsg.value = '';
  try {
    await kbApi.post(
      '/business-facts',
      { business_facts: prompt },
      { headers: authHeader() },
    );
    infoMsg.value = 'Your business details are saved. You can refine them later in Advanced Settings.';
    skipSampleUpload();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Could not save your business details.');
  } finally {
    sampleUpload.value.isUploading = false;
  }
};

const cloneFields = (key) => schemaFor(key).map((field) => ({ ...field }));

const startFieldEdit = async (key, title) => {
  // Catalog-driven picker: fetch the selectable field palette for this record and
  // let the admin choose exactly which fields the agent collects. The agent then
  // asks ONLY the selected fields. Falls back to the current schema if the catalog
  // endpoint is unavailable.
  fieldEditor.value = { key, title, items: [], isLoading: true, isSaving: false };
  try {
    const { data } = await api.get(`/business-template/field-catalog/${key}`, { headers: authHeader() });
    fieldEditor.value.items = (data.fields || []).map((f) => ({ ...f }));
  } catch (err) {
    fieldEditor.value.items = cloneFields(key).map((f) => ({
      ...f,
      selected: true,
      kind: 'generic',
      agent_question: '',
      custom: true,
    }));
  } finally {
    if (fieldEditor.value.key === key) fieldEditor.value.isLoading = false;
  }
};

const closeFieldEdit = () => {
  fieldEditor.value = { key: null, title: '', items: [], isLoading: false, isSaving: false };
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

// Real-estate site visits store the day and the clock separately
// (visit_date="2026-06-18" + visit_time="20:00" or "8:00 PM"). A plain
// new Date(visit_date) lands at midnight UTC — i.e. an 8 PM visit shows at
// ~5:30 AM IST. Combine the two into a real local datetime so the calendar
// places the visit at the hour it was actually booked. Falls back to the
// generic chain (which already handles full-timestamp fields + created_at).
const recordEventStart = (record) => {
  const dateOnly = firstRecordValue(record, ['visit_date', 'preferred_date']);
  const dm = dateOnly && /^(\d{4})-(\d{2})-(\d{2})/.exec(String(dateOnly));
  // Only special-case when there's a date-only string AND no full timestamp.
  const hasFullTimestamp = !!firstRecordValue(record, [
    'scheduled_time', 'appointment_time', 'appointment_start', 'visit_at',
    'callback_at', 'requested_time', 'scheduled_at',
  ]);
  if (dm && !hasFullTimestamp) {
    const timeRaw = firstRecordValue(record, ['visit_time', 'preferred_time']);
    let hh = 0;
    let mm = 0;
    // Check AM/PM FIRST: "8:00 PM" must not be read as 24-hour "8:00" (8 AM).
    // visit_time is stored either as 24h ("20:00", from the scheduler/LLM) or
    // 12h ("8:00 PM" / "8 p.m.", from the structured booking flow).
    const tAmPm = timeRaw && /(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?/i.exec(String(timeRaw));
    const t24 = timeRaw && /^(\d{1,2}):(\d{2})/.exec(String(timeRaw));
    if (tAmPm) {
      hh = (Number(tAmPm[1]) % 12) + (/p/i.test(tAmPm[3]) ? 12 : 0);
      mm = Number(tAmPm[2] || 0);
    } else if (t24) {
      hh = Number(t24[1]); mm = Number(t24[2]);
    }
    return new Date(Number(dm[1]), Number(dm[2]) - 1, Number(dm[3]), hh, mm);
  }
  return recordCalendarDate(record);
};

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
      const start = recordEventStart(record);
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
  fieldEditor.value.items.push({
    key: `custom_${fieldEditor.value.items.length + 1}`,
    label: 'New Field',
    type: 'text',
    kind: 'generic',
    agent_question: '',
    selected: true,
    required: false,
    custom: true,
  });
};

const removeField = (index) => {
  fieldEditor.value.items.splice(index, 1);
};

const saveFieldEdit = async () => {
  if (!fieldEditor.value.key) return;
  // Only the SELECTED fields are saved → the agent asks exactly these, in order.
  const fields = fieldEditor.value.items
    .filter((it) => it.selected)
    .map((it) => ({
      key: (it.key || it.label || 'field').trim().toLowerCase().replace(/[\s-]+/g, '_').replace(/[^a-z0-9_]/g, ''),
      label: (it.label || '').trim(),
      type: it.type || 'text',
      required: Boolean(it.required),
    }))
    .filter((f) => f.key && f.label);
  if (!fields.length) {
    errorMsg.value = 'Select at least one field for the agent to collect.';
    return;
  }
  fieldEditor.value.isSaving = true;
  errorMsg.value = '';
  try {
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
  'plivo_telephony',
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
  try {
    // Account only — NO resources provisioned yet. The org is pending_payment;
    // resources provision after the Razorpay payment succeeds.
    const { data } = await api.post('/signup', signup.value);
    paymentToken.value = data.payment_token;
    selectedPlan.value = 'inbound_only';
    authState.value = 'payment';
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Sign up failed.');
    authState.value = 'signup';
  } finally {
    isAuthenticating.value = false;
  }
};

// ── Razorpay payment (monthly subscription) ──────────────────────────────────
const loadRazorpayCheckout = () =>
  new Promise((resolve, reject) => {
    if (window.Razorpay) return resolve();
    const existing = document.getElementById('razorpay-checkout-js');
    if (existing) {
      existing.addEventListener('load', () => resolve());
      existing.addEventListener('error', () => reject(new Error('Failed to load Razorpay')));
      return;
    }
    const s = document.createElement('script');
    s.id = 'razorpay-checkout-js';
    s.src = 'https://checkout.razorpay.com/v1/checkout.js';
    s.onload = () => resolve();
    s.onerror = () => reject(new Error('Failed to load Razorpay'));
    document.head.appendChild(s);
  });

const continueAfterPayment = async (orgStatus) => {
  // Post-payment → the onboarding wizard (business KYC → compliance + number →
  // hours → projects → agent → ToS). No email-verify / MFA gate.
  if (orgStatus === 'onboarding') {
    await beginOnboardingWizard();
  } else if (orgStatus === 'active') {
    await enterWorkspaceAfterAuth();
  } else {
    // Legacy fallback (older orgs that still email-verify).
    infoMsg.value = `Verification link sent to ${signup.value.admin_email || 'your email'}. Click it to continue.`;
    authState.value = 'check_email';
  }
};

// ── Post-payment onboarding wizard ───────────────────────────────────────────
const ONBOARDING_STEPS = ['business_details', 'documents', 'working_hours', 'projects', 'agent', 'terms'];
const onboardingStep = ref('business_details');
const onboardingState = ref(null);
const isOnboardingSaving = ref(false);
const onboardingBusiness = ref({ legal_name: '', alias_name: '', business_pan: '', cin: '' });
const onboardingDocs = ref({ incorporation: null, gst_or_pan: null });
const onboardingHours = ref({ working_days: [], start_time: '09:00', end_time: '18:00', timezone: 'Asia/Kolkata' });
const onboardingAgentName = ref('Property Assistant');
const onboardingTerms = ref({ terms: false, privacy: false });
const onboardingNumber = ref({ number: null, number_status: null });

const onboardingStepIndex = computed(() => ONBOARDING_STEPS.indexOf(onboardingStep.value));

const loadOnboardingState = async () => {
  const { data } = await api.get('/onboarding/state', { headers: authHeader() });
  onboardingState.value = data;
  onboardingStep.value = data.onboarding_step && data.onboarding_step !== 'done' ? data.onboarding_step : 'terms';
  const b = data.business_details || {};
  onboardingBusiness.value = {
    legal_name: b.legal_name || '',
    alias_name: b.alias_name || '',
    business_pan: b.business_pan || '',
    cin: b.cin || '',
  };
  const wh = data.working_hours || {};
  onboardingHours.value = {
    working_days: wh.working_days || [],
    start_time: wh.start_time || '09:00',
    end_time: wh.end_time || '18:00',
    timezone: wh.timezone || 'Asia/Kolkata',
  };
  if (data.agent_name) onboardingAgentName.value = data.agent_name;
  onboardingNumber.value = { number: data.number, number_status: data.number_status };
};

const beginOnboardingWizard = async () => {
  try {
    await loadOnboardingState();
    if (onboardingStep.value === 'projects') await loadProjects();
  } catch (e) {
    errorMsg.value = extractErrorMessage(e, 'Could not load your onboarding progress.');
  }
  authState.value = 'onboarding';
};

const saveOnboardingBusiness = async () => {
  if (!onboardingBusiness.value.legal_name.trim()) {
    errorMsg.value = 'Company legal name is required.';
    return;
  }
  isOnboardingSaving.value = true;
  errorMsg.value = '';
  try {
    const { data } = await api.post('/onboarding/business-details', onboardingBusiness.value, { headers: authHeader() });
    onboardingStep.value = data.onboarding_step;
  } catch (e) {
    errorMsg.value = extractErrorMessage(e, 'Could not save business details.');
  } finally {
    isOnboardingSaving.value = false;
  }
};

const submitOnboardingDocs = async () => {
  if (!onboardingDocs.value.incorporation || !onboardingDocs.value.gst_or_pan) {
    errorMsg.value = 'Upload both the certificate of incorporation and your GST or business PAN.';
    return;
  }
  isOnboardingSaving.value = true;
  errorMsg.value = '';
  try {
    const fd = new FormData();
    fd.append('incorporation', onboardingDocs.value.incorporation);
    fd.append('gst_or_pan', onboardingDocs.value.gst_or_pan);
    const { data } = await api.post('/onboarding/documents', fd, {
      headers: { ...authHeader(), 'Content-Type': 'multipart/form-data' },
    });
    onboardingNumber.value = { number: data.number, number_status: data.number_status };
    onboardingStep.value = data.onboarding_step;
  } catch (e) {
    errorMsg.value = extractErrorMessage(e, 'Could not submit your documents.');
  } finally {
    isOnboardingSaving.value = false;
  }
};

const saveOnboardingHours = async () => {
  isOnboardingSaving.value = true;
  errorMsg.value = '';
  try {
    const { data } = await api.post('/onboarding/working-hours', onboardingHours.value, { headers: authHeader() });
    onboardingStep.value = data.onboarding_step;
  } catch (e) {
    errorMsg.value = extractErrorMessage(e, 'Could not save working hours.');
  } finally {
    isOnboardingSaving.value = false;
  }
};

const finishOnboardingProjects = async () => {
  isOnboardingSaving.value = true;
  errorMsg.value = '';
  try {
    const { data } = await api.post('/onboarding/projects/done', {}, { headers: authHeader() });
    onboardingStep.value = data.onboarding_step;
  } catch (e) {
    errorMsg.value = extractErrorMessage(e, 'Add at least one project before continuing.');
  } finally {
    isOnboardingSaving.value = false;
  }
};

const saveOnboardingAgent = async () => {
  isOnboardingSaving.value = true;
  errorMsg.value = '';
  try {
    const { data } = await api.post('/onboarding/agent', { name: onboardingAgentName.value }, { headers: authHeader() });
    onboardingStep.value = data.onboarding_step;
  } catch (e) {
    errorMsg.value = extractErrorMessage(e, 'Could not save the agent.');
  } finally {
    isOnboardingSaving.value = false;
  }
};

const acceptOnboardingTerms = async () => {
  if (!onboardingTerms.value.terms || !onboardingTerms.value.privacy) {
    errorMsg.value = 'Please accept both the Terms of Service and the Privacy Policy.';
    return;
  }
  isOnboardingSaving.value = true;
  errorMsg.value = '';
  try {
    await api.post('/onboarding/terms', { terms_accepted: true, privacy_accepted: true }, { headers: authHeader() });
    if (currentOrganization.value) currentOrganization.value.status = 'active';
    infoMsg.value = 'Welcome to Nokvo One!';
    await enterWorkspaceAfterAuth();
  } catch (e) {
    errorMsg.value = extractErrorMessage(e, 'Could not finish onboarding.');
  } finally {
    isOnboardingSaving.value = false;
  }
};

const onboardingPickFile = (slot, event) => {
  const file = event?.target?.files?.[0] || null;
  onboardingDocs.value[slot] = file;
};

const ONBOARDING_DAYS = [
  { key: 'mon', label: 'Mon' }, { key: 'tue', label: 'Tue' }, { key: 'wed', label: 'Wed' },
  { key: 'thu', label: 'Thu' }, { key: 'fri', label: 'Fri' }, { key: 'sat', label: 'Sat' }, { key: 'sun', label: 'Sun' },
];
const toggleOnboardingDay = (day) => {
  const days = onboardingHours.value.working_days || [];
  onboardingHours.value.working_days = days.includes(day) ? days.filter((d) => d !== day) : [...days, day];
};
const onboardingStepLabel = (step) => ({
  business_details: 'Business', documents: 'Documents', working_hours: 'Hours',
  projects: 'Projects', agent: 'Agent', terms: 'Terms',
}[step] || step);

// Entering the payment screen on a RESUME (returning user logs back in while
// pending_payment). Reconcile with Razorpay first: if they already paid but the
// verify call AND webhook were both lost, /payments/status finishes provisioning
// server-side and we skip straight ahead instead of charging them again.
const resumePaymentScreen = async () => {
  authState.value = 'payment';
  if (!paymentToken.value) return;
  try {
    const { data } = await api.get('/payments/status', {
      params: { payment_token: paymentToken.value },
    });
    if (data && data.needs_payment === false) {
      await continueAfterPayment(data.org_status);
    }
  } catch (e) {
    // Non-fatal — fall back to showing the plan cards so they can pay.
  }
};

const startPayment = async (plan) => {
  errorMsg.value = '';
  infoMsg.value = '';
  if (!paymentToken.value) {
    errorMsg.value = 'Your payment session expired. Please sign in again.';
    return;
  }
  selectedPlan.value = plan;
  isPaying.value = true;
  try {
    await loadRazorpayCheckout();
    const { data } = await api.post('/payments/create-subscription', {
      payment_token: paymentToken.value,
      plan,
    });
    const rzp = new window.Razorpay({
      key: data.key_id,
      subscription_id: data.subscription_id,
      name: data.name,
      description: data.description,
      prefill: { email: signup.value.admin_email || '', name: signup.value.admin_name || '' },
      theme: { color: '#4f46e5' },
      handler: async (resp) => {
        try {
          const { data: vr } = await api.post('/payments/verify', {
            payment_token: paymentToken.value,
            razorpay_payment_id: resp.razorpay_payment_id,
            razorpay_subscription_id: resp.razorpay_subscription_id,
            razorpay_signature: resp.razorpay_signature,
          });
          // verify now returns a session so the (active) admin can drive the
          // onboarding wizard without an email round-trip.
          if (vr.session) persistSession(vr.session);
          await continueAfterPayment(vr.org_status);
        } catch (e) {
          errorMsg.value = extractErrorMessage(e, 'Payment verification failed. If you were charged, it will be reconciled automatically.');
        } finally {
          isPaying.value = false;
        }
      },
      modal: {
        ondismiss: () => {
          isPaying.value = false;
          infoMsg.value = 'Payment cancelled. Choose a plan to continue.';
        },
      },
    });
    rzp.on('payment.failed', (resp) => {
      errorMsg.value = resp?.error?.description || 'Payment failed. Please try again.';
      isPaying.value = false;
    });
    rzp.open();
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Could not start the payment. Please try again.');
    isPaying.value = false;
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
    infoMsg.value = 'TOTP enrolled. Your organization is active — sign in to continue.';
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
  infoMsg.value = '';
  isAuthenticating.value = true;
  try {
    const { data } = await api.post('/login', login.value);
    // Resume unfinished onboarding from wherever the user left off.
    if (data.code === 'payment_required' && data.payment_token) {
      paymentToken.value = data.payment_token;
      signup.value.admin_email = data.email || login.value.email;
      if (!selectedPlan.value) selectedPlan.value = 'inbound_only';
      await resumePaymentScreen();
      return;
    }
    if (data.code === 'onboarding_required') {
      persistSession(data);
      await beginOnboardingWizard();
      return;
    }
    if (data.code === 'email_verification_required') {
      signup.value.admin_email = data.email || login.value.email;
      infoMsg.value = `Verify your email (${data.email || login.value.email}) to continue — check your inbox for the link.`;
      authState.value = 'check_email';
      return;
    }
    if (data.code === 'totp_verify_required' || data.mfa_pending) {
      loginTempToken.value = data.access_token;
      authState.value = 'login_totp';
      return;
    }
    if (data.access_token && data.user) {
      persistSession(data);
      await enterWorkspaceAfterAuth();
      return;
    }
    // Legacy fallback: treat as a TOTP temp token.
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

const renameAgent = async () => {
  const agent = profileAgent.value;
  const name = renameDraft.value.trim();
  if (!agent || !name || name === agent.name) return;
  errorMsg.value = '';
  isRenamingAgent.value = true;
  try {
    const { data } = await api.patch(`/agents/${agent.id}`, { name }, { headers: authHeader() });
    // Keep the agents list and the active reference in sync with the rename.
    const idx = agents.value.findIndex((a) => a.id === data.id);
    if (idx >= 0) agents.value.splice(idx, 1, data);
    if (activeAgent.value?.id === data.id) activeAgent.value = data;
    renameDraft.value = data.name;
    infoMsg.value = 'Agent name updated.';
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Rename failed.');
  } finally {
    isRenamingAgent.value = false;
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
// Records assigned to the current member (site visits / leads / tickets).
// Loaded alongside the timetable so the calendar can light up dates that
// have customer work, mirroring what the admin sees in the timetable
// modal — but scoped to the calling user only.
const myAssignedRecords = ref([]);
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
    // Pull the timetable + the member's assigned work in parallel so the
    // calendar page renders with both data sources in one round trip.
    // Assigned-records failure is non-fatal — the calendar still renders
    // the timetable bits even if the records call errors.
    const [timetable, assigned] = await Promise.allSettled([
      api.get('/members/me/timetable', { headers: authHeader() }),
      api.get('/members/me/assigned-records', { headers: authHeader() }),
    ]);
    if (timetable.status === 'fulfilled') myTimetable.value = timetable.value.data;
    else throw timetable.reason;
    myAssignedRecords.value = assigned.status === 'fulfilled' ? (assigned.value.data || []) : [];
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Failed to load your timetable.');
  } finally {
    isLoadingMyTimetable.value = false;
  }
};

// ── Member calendar view ─────────────────────────────────────────────
//
// The admin already gets a month-grid timetable in the member-roster
// modal. Members get the same shape here, driven by their own
// /members/me/timetable payload (assignment + blocked_slots).
//
// We don't fetch the member's assigned tickets/appointments yet — the
// /me/timetable endpoint doesn't expose them. The calendar will surface
// the recurring working window + one-off buffer/unavailable blocks,
// which is the contract for what "their timetable" means today.

const myCalendarSelectedDate = ref(todayKey());
const myCalendarVisibleMonth = ref(`${todayKey().slice(0, 7)}-01`);

// Map server weekday names → JS getDay() indexes so the calendar grid
// can tell which dates fall on the member's configured working days.
const _WEEKDAY_INDEX = {
  sunday: 0, sun: 0,
  monday: 1, mon: 1,
  tuesday: 2, tue: 2,
  wednesday: 3, wed: 3,
  thursday: 4, thu: 4,
  friday: 5, fri: 5,
  saturday: 6, sat: 6,
};

const myWorkingDayIndexes = computed(() => {
  const days = myTimetable.value?.assignment?.working_days || [];
  const out = new Set();
  for (const d of days) {
    const idx = _WEEKDAY_INDEX[String(d).trim().toLowerCase()];
    if (idx !== undefined) out.add(idx);
  }
  return out;
});

// Customer work assigned to the calling member, in the same {start, end,
// title, ...} shape the calendar consumes. Each record's start comes from
// scheduleRecordDate which already knows where appointments / leads /
// tickets stash their scheduled timestamp.
const myAssignedScheduleItems = computed(() => {
  const duration = Number(myTimetable.value?.assignment?.appointment_duration_minutes || 30);
  return myAssignedRecords.value
    .map((record) => {
      const start = recordEventStart(record);
      const end = start ? new Date(start.getTime() + duration * 60000) : null;
      return {
        id: record.id,
        recordType: record.record_type,
        title: scheduleRecordLabel(record),
        typeLabel: scheduleRecordTypeLabel(record),
        detail: scheduleRecordDetail(record),
        status: record.status,
        phone: recordPhone(record),
        phoneHref: phoneHref(recordPhone(record)),
        createdAt: record.created_at,
        start,
        end,
        isBlocked: false,
      };
    });
});

const myAssignedQueuedItems = computed(() =>
  myAssignedScheduleItems.value
    .filter((item) => !item.start)
    .sort((a, b) => new Date(b.createdAt || 0).getTime() - new Date(a.createdAt || 0).getTime()),
);

const myCalendarBlocks = computed(() => {
  const slots = myTimetable.value?.blocked_slots || [];
  const blocks = slots
    .map((slot) => ({
      id: slot.id,
      recordType: 'blocked',
      title: slot.reason || 'Block',
      typeLabel: 'Blocked',
      isBlocked: true,
      start: parseRecordDate(slot.start_time),
      end: parseRecordDate(slot.end_time),
    }));
  // Merge blocked slots + scheduled customer work into a single ordered
  // stream — the calendar renders both kinds with their own visual.
  return [...blocks, ...myAssignedScheduleItems.value.filter((item) => item.start)]
    .filter((item) => item.start)
    .sort((a, b) => a.start.getTime() - b.start.getTime());
});

// Bucket every calendar event by its local day ONCE, so the 42-cell grid and
// the selected-day list are O(1) lookups instead of each re-scanning the full
// event list. Recomputes only when the underlying events change.
const myCalendarEventsByDay = computed(() => {
  const map = new Map();
  for (const item of myCalendarBlocks.value) {
    const key = localDateKey(item.start);
    if (!key) continue;
    const bucket = map.get(key);
    if (bucket) bucket.push(item);
    else map.set(key, [item]);
  }
  return map;
});

const myCalendarMonthLabel = computed(() => {
  const visible = dateFromKey(myCalendarVisibleMonth.value) || new Date();
  return visible.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
});

const myCalendarDays = computed(() => {
  // Same 42-cell month grid as the admin timetable modal. We don't reuse
  // ``timetableCalendarDays`` because that one is scoped to the admin
  // viewer state (timetableViewer.member) and the member view needs its
  // own independent selection.
  const selected = myCalendarSelectedDate.value;
  const visibleDate = dateFromKey(myCalendarVisibleMonth.value) || new Date();
  const firstOfMonth = new Date(visibleDate.getFullYear(), visibleDate.getMonth(), 1);
  const gridStart = new Date(firstOfMonth);
  gridStart.setDate(firstOfMonth.getDate() - firstOfMonth.getDay());
  const today = todayKey();
  const workingDays = myWorkingDayIndexes.value;
  return Array.from({ length: 42 }, (_, index) => {
    const date = new Date(gridStart);
    date.setDate(gridStart.getDate() + index);
    const key = localDateKey(date);
    const itemsOnDay = myCalendarEventsByDay.value.get(key) || [];
    // Separate visit count from block count so the cell badge can read
    // "1 visit + 1 block" cleanly without lumping them together.
    const visitCount = itemsOnDay.filter((i) => !i.isBlocked).length;
    const blockCount = itemsOnDay.filter((i) => i.isBlocked).length;
    return {
      key,
      dayNumber: date.getDate(),
      inMonth: date.getMonth() === visibleDate.getMonth(),
      isToday: key === today,
      isSelected: key === selected,
      isWorkingDay: workingDays.has(date.getDay()),
      itemCount: itemsOnDay.length,
      visitCount,
      blockCount,
    };
  });
});

const myCalendarSelectedLabel = computed(() => {
  const date = dateFromKey(myCalendarSelectedDate.value) || new Date();
  return date.toLocaleDateString(undefined, {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
  });
});

const myCalendarSelectedIsWorking = computed(() => {
  const date = dateFromKey(myCalendarSelectedDate.value) || new Date();
  return myWorkingDayIndexes.value.has(date.getDay());
});

const myCalendarSelectedItems = computed(
  () => myCalendarEventsByDay.value.get(myCalendarSelectedDate.value) || [],
);

const selectMyCalendarDate = (key) => {
  myCalendarSelectedDate.value = key;
  myCalendarVisibleMonth.value = `${key.slice(0, 7)}-01`;
};

const shiftMyCalendarMonth = (offset) => {
  const visible = dateFromKey(myCalendarVisibleMonth.value) || new Date();
  const next = new Date(visible.getFullYear(), visible.getMonth() + offset, 1);
  myCalendarVisibleMonth.value = localDateKey(next);
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

const kbApi = axios.create({ baseURL: NOKVO_ONE_KB_BASE });

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
const ticketRecordTitle = (record) => {
  const primary = firstRecordValue(record, [
    'customer', 'customer_name', 'patient_name', 'guest_name', 'name', 'contact_name',
  ]);
  if (primary) return formatRecordValue(primary, '');

  const secondary = firstRecordValue(record, ['subject', 'issue_type', 'reason', 'description']);
  if (secondary && secondary !== 'site_visit') return formatRecordValue(secondary, '');

  return 'No Name';
};

const ticketRecordSubtitle = (record) => {
  const phone = firstRecordValue(record, ['contact_phone', 'phone', 'phone_number']);
  let summary = firstRecordValue(record, ['issue_type', 'subject', 'reason', 'description', 'property_id', 'location']);
  if (summary === 'site_visit') summary = null;
  
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

const agentsApi = axios.create({ baseURL: NOKVO_ONE_AGENTS_BASE });

// ── Clinic services (catalog + doctor mapping) ───────────────────────────
const servicesApi = axios.create({ baseURL: NOKVO_ONE_SERVICES_BASE });
const clinicServices = ref([]);
const clinicDoctors = ref([]);
const isLoadingServices = ref(false);

const loadClinicServices = async () => {
  isLoadingServices.value = true;
  try {
    const [svc, docs] = await Promise.all([
      servicesApi.get('/', { headers: authHeader() }),
      servicesApi.get('/doctors', { headers: authHeader() }),
    ]);
    clinicServices.value = Array.isArray(svc.data) ? svc.data : [];
    clinicDoctors.value = Array.isArray(docs.data) ? docs.data : [];
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Failed to load services.');
  } finally {
    isLoadingServices.value = false;
  }
};

const saveClinicService = async (payload) => {
  // payload may include an id (update) or not (create). doctor_ids on create;
  // mapping is set separately via setServiceDoctors for updates.
  if (payload.id) {
    const { id, doctor_ids, ...patch } = payload;
    await servicesApi.patch(`/${id}`, patch, { headers: authHeader() });
    if (Array.isArray(doctor_ids)) {
      await servicesApi.put(`/${id}/doctors`, { doctor_ids }, { headers: authHeader() });
    }
  } else {
    await servicesApi.post('/', payload, { headers: authHeader() });
  }
  await loadClinicServices();
};

const setServiceDoctors = async (serviceId, doctorIds) => {
  await servicesApi.put(`/${serviceId}/doctors`, { doctor_ids: doctorIds }, { headers: authHeader() });
  await loadClinicServices();
};

const deleteClinicService = async (serviceId) => {
  await servicesApi.delete(`/${serviceId}`, { headers: authHeader() });
  await loadClinicServices();
};

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
    // The tenant enters THEIR own number to forward to the assigned Plivo DID.
    phoneLinkInput.value = data.forward_from_number || '';
  } catch (err) {
    phoneLink.value = null;
  }
};

const savePhoneLink = async () => {
  isSavingPhoneLink.value = true;
  try {
    const { data } = await agentsApi.post(
      '/phone-link',
      { forward_from_number: phoneLinkInput.value.trim() || null },
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

const loadWhatsAppLink = async () => {
  try {
    const { data } = await agentsApi.get('/whatsapp-link', { headers: authHeader() });
    whatsappLink.value = data;
  } catch (err) {
    whatsappLink.value = null;
  }
};

const requestWhatsApp = async () => {
  isRequestingWhatsapp.value = true;
  try {
    const { data } = await agentsApi.post(
      '/whatsapp-link/request',
      {
        business_name: (whatsappForm.value.business_name || '').trim(),
        contact_number: (whatsappForm.value.contact_number || '').trim(),
        display_name: (whatsappForm.value.display_name || '').trim() || null,
      },
      { headers: authHeader() },
    );
    whatsappLink.value = data;
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Failed to request WhatsApp setup.');
  } finally {
    isRequestingWhatsapp.value = false;
  }
};

const cancelWhatsApp = async () => {
  try {
    const { data } = await agentsApi.delete('/whatsapp-link', { headers: authHeader() });
    whatsappLink.value = data;
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Failed to cancel WhatsApp setup.');
  }
};

const loadTranscripts = async () => {
  transcriptsLoading.value = true;
  try {
    const { data } = await api.get('/transcripts', { headers: authHeader() });
    transcripts.value = data?.transcripts || [];
  } catch (err) {
    transcripts.value = [];
  } finally {
    transcriptsLoading.value = false;
  }
};

const loadTranscript = async (callId) => {
  try {
    const { data } = await api.get(`/transcripts/${encodeURIComponent(callId)}`, { headers: authHeader() });
    return data;
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Failed to load transcript.');
    return null;
  }
};

const downloadTranscript = async (callId) => {
  // The .txt endpoint needs the Bearer header, so a plain <a href> won't do —
  // fetch as a blob, then click a synthetic link.
  try {
    const res = await api.get(`/transcripts/${encodeURIComponent(callId)}/download`, {
      headers: authHeader(),
      responseType: 'blob',
    });
    const url = window.URL.createObjectURL(new Blob([res.data], { type: 'text/plain' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = `transcript_${callId}.txt`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);
  } catch (err) {
    errorMsg.value = extractErrorMessage(err, 'Failed to download transcript.');
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

const loadOutboundNumber = async () => {
  try {
    const { data } = await agentsApi.get('/outbound-number', { headers: authHeader() });
    outboundNumber.value = data || { number: null, status: null, calling_enabled: false };
  } catch {
    // Non-fatal — UI falls back to "no number provisioned" / gated state.
  }
};

const loadOutgoingAgentWorkspace = async () => {
  isLoadingLeadSources.value = true;
  try {
    await Promise.all([loadLeadConnections(), loadLeadForms(), loadOutgoingLeads(), loadOutboundNumber()]);
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

const isDisconnectingLeadConnection = ref(null);

const disconnectLeadConnection = async (connectionId, displayName) => {
  const label = displayName ? `the connection "${displayName}"` : 'this connection';
  if (!window.confirm(`Disconnect ${label}? You'll need to re-authorize to sync again.`)) return;
  isDisconnectingLeadConnection.value = connectionId;
  try {
    await agentsApi.post(`/lead-sources/connections/${connectionId}/disconnect`, {}, { headers: authHeader() });
    await loadLeadConnections();
    infoMsg.value = 'Connection disconnected.';
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Could not disconnect the lead source.');
  } finally {
    isDisconnectingLeadConnection.value = null;
  }
};

const createNokvoLeadForm = async () => {
  // Trim labels and synthesize stable `key`s for the backend so admins
  // never have to think about identifiers. The backend dedupes and
  // injects name/phone/consent — see OutgoingLeadService.create_nokvo_form.
  const cleanedFields = (nokvoLeadForm.value.fields || [])
    .map((field) => ({
      label: (field.label || '').trim(),
      type: field.type || 'text',
      required: Boolean(field.required),
    }))
    .filter((field) => field.label.length > 0)
    .map((field) => ({
      ...field,
      key: field.label.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, ''),
    }));

  const payload = {
    name: (nokvoLeadForm.value.name || '').trim(),
    consent_text: (nokvoLeadForm.value.consent_text || '').trim(),
    fields: cleanedFields,
  };
  if (!payload.name) {
    errorMsg.value = 'Give the form a name first.';
    return;
  }
  if (!payload.consent_text) {
    errorMsg.value = 'Call-consent text is required.';
    return;
  }

  try {
    const { data } = await agentsApi.post(
      '/lead-sources/nokvo-forms',
      payload,
      { headers: authHeader() },
    );
    lastNokvoFormLink.value = data?.public_url || data?.public_link || null;
    nokvoLeadForm.value = _defaultNokvoLeadForm();
    await loadLeadForms();
    infoMsg.value = lastNokvoFormLink.value
      ? 'Nokvo lead form created — copy the public link below.'
      : 'Nokvo lead form created.';
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Could not create Nokvo form.');
  }
};

const toggleLeadSelection = (lead) => {
  if (!lead.callable) return;
  const idx = selectedLeadIds.value.indexOf(lead.id);
  if (idx >= 0) selectedLeadIds.value.splice(idx, 1);
  else selectedLeadIds.value.push(lead.id);
};

const createCampaign = async () => {
  // The agent_prompt IS the agent's knowledge — it's required. No separate
  // doc upload. Leads are optional here; the operator can attach them later
  // from the campaign card.
  if (!campaignForm.value.name.trim() || !campaignForm.value.agent_prompt.trim()) {
    errorMsg.value = 'Campaign name and an agent prompt are required — the prompt is what the agent reads on the call.';
    return;
  }
  const preset = campaignFormPreset.value;
  if (preset && !outboundNumber.value.calling_enabled) {
    errorMsg.value = 'Outbound calling isn\'t on your plan. Upgrade to Inbound + Outbound to call leads.';
    return;
  }
  isCreatingCampaign.value = true;
  try {
    const fd = new FormData();
    fd.append('name', campaignForm.value.name.trim());
    // Caller ID is always the tenant's allotted number (resolved server-side at
    // launch) — no free-text from_number.
    fd.append('agent_prompt', campaignForm.value.agent_prompt.trim());
    // Objectives is now a list of structured codes (site_visit / lead) from
    // the dropdown. Always send the JSON array (even when empty) so the
    // backend knows the operator explicitly chose to skip side flows.
    fd.append('objectives', JSON.stringify(Array.isArray(campaignForm.value.objectives) ? campaignForm.value.objectives : []));
    if (campaignForm.value.exit_conditions.trim()) fd.append('exit_conditions', JSON.stringify(campaignForm.value.exit_conditions.split('\n').map((item) => item.trim()).filter(Boolean)));
    if (campaignForm.value.tone.trim()) fd.append('tone', campaignForm.value.tone.trim());
    fd.append('silence_timeout_seconds', String(campaignForm.value.silence_timeout_seconds || 5));
    // Follow-up rules — serialised into agent_config.followup_rules by the
    // backend so the scheduler can read them. Only sent if the operator
    // has actually configured them (always present in defaults, so we
    // always send them — keeps the contract simple).
    if (campaignForm.value.followup_rules) {
      fd.append('followup_rules', JSON.stringify(campaignForm.value.followup_rules));
    }
    // "Create & call this form": the server builds the campaign from the form's
    // callable leads and launches it. Otherwise, attach the hand-picked selection.
    if (preset?.capture_form_id) {
      fd.append('capture_form_id', preset.capture_form_id);
      fd.append('launch', 'true');
    } else if (selectedCallableLeads.value.length) {
      fd.append('lead_ids', JSON.stringify(selectedCallableLeads.value.map((lead) => lead.id)));
    }
    const { data: created } = await agentsApi.post('/campaigns', fd, { headers: { ...authHeader(), 'Content-Type': 'multipart/form-data' } });
    const launched = preset?.capture_form_id;
    campaignForm.value = emptyCampaignForm();
    selectedLeadIds.value = [];
    campaignFormPreset.value = null;
    showCampaignCreateForm.value = false;
    await loadCampaigns();
    await loadOutgoingLeads();
    if (launched) {
      infoMsg.value = `Calling ${created?.total_count ?? ''} lead(s) from ${created?.from_number || 'your number'} — campaign is live.`;
    } else {
      infoMsg.value = selectedCallableLeads.value.length
        ? 'Campaign created with attached leads.'
        : 'Campaign created. Attach consented leads from the card to launch later.';
    }
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Failed to create campaign.');
  } finally {
    isCreatingCampaign.value = false;
  }
};

// "Create & call this form's leads": open the create panel preset to build +
// launch a campaign from one form's callable leads (Option A → outbound).
const createAndCallFromForm = (form) => {
  if (!outboundNumber.value.calling_enabled) {
    errorMsg.value = 'Outbound calling isn\'t on your plan. Upgrade to Inbound + Outbound to call leads.';
    return;
  }
  if (!outboundNumber.value.number) {
    errorMsg.value = 'No outbound number is provisioned for your account yet.';
    return;
  }
  campaignFormPreset.value = {
    capture_form_id: form.id,
    formName: form.name,
    callableCount: form.callable ?? 0,
  };
  campaignForm.value = emptyCampaignForm();
  campaignForm.value.name = `${form.name} — outbound`;
  outgoingTab.value = 'campaigns';
  showCampaignCreateForm.value = true;
};

const cancelCampaignPreset = () => {
  campaignFormPreset.value = null;
};

// Toggle for the (collapsed-by-default) inline create form. Keeps the
// Campaigns sub-tab visually clean when the operator just wants to look at
// existing campaigns.
const showCampaignCreateForm = ref(false);

// Per-campaign expanded state. Holds the campaign id whose detail panel
// is currently open. null = all collapsed.
const expandedCampaignId = ref(null);

const toggleCampaignExpansion = (id) => {
  expandedCampaignId.value = expandedCampaignId.value === id ? null : id;
};

// Lead-attach UI state: which campaign card is currently picking leads,
// plus the working selection.
const campaignAttachLeadIds = ref([]);
const campaignAttachInFlight = ref(null); // campaign id during request

const startAttachLeadsToCampaign = async (campaignId) => {
  campaignAttachLeadIds.value = [];
  // Make sure the inventory list is fresh so the picker shows newly synced
  // leads from a recent ad-source sync.
  try {
    await loadOutgoingLeads();
  } catch (_) {}
};

const toggleCampaignAttachLead = (leadId) => {
  const idx = campaignAttachLeadIds.value.indexOf(leadId);
  if (idx >= 0) {
    campaignAttachLeadIds.value.splice(idx, 1);
  } else {
    campaignAttachLeadIds.value.push(leadId);
  }
};

const submitCampaignAttachLeads = async (campaignId) => {
  if (!campaignAttachLeadIds.value.length) {
    errorMsg.value = 'Pick at least one consented lead to attach.';
    return;
  }
  campaignAttachInFlight.value = campaignId;
  try {
    const fd = new FormData();
    fd.append('lead_ids', JSON.stringify(campaignAttachLeadIds.value));
    await agentsApi.post(`/campaigns/${campaignId}/leads`, fd, {
      headers: { ...authHeader(), 'Content-Type': 'multipart/form-data' },
    });
    campaignAttachLeadIds.value = [];
    await Promise.all([loadCampaigns(), loadOutgoingLeads()]);
    infoMsg.value = 'Leads attached to the campaign.';
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Failed to attach leads.');
  } finally {
    campaignAttachInFlight.value = null;
  }
};

const detachLeadFromCampaign = async (campaignId, leadId) => {
  if (!window.confirm('Remove this lead from the campaign?')) return;
  try {
    await agentsApi.delete(`/campaigns/${campaignId}/leads/${leadId}`, {
      headers: authHeader(),
    });
    await Promise.all([loadCampaigns(), loadOutgoingLeads()]);
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Failed to detach lead.');
  }
};

// "Test this campaign" entry-point on each campaign card. Switches the
// sub-tab to Tester, pre-selects the campaign in the picker, and clears
// any stale outcome banner from a prior session.
const testCampaign = (campaignId) => {
  selectedTesterCampaignId.value = campaignId;
  outboundTesterOutcome.value = null;
  outgoingTab.value = 'tester';
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

// Hard-delete a campaign. The backend refuses if the campaign is still running
// (cancel-first). We collapse the expanded panel so we don't read state that
// no longer exists.
const removeCampaign = async (id, name) => {
  const label = name ? `"${name}"` : 'this campaign';
  if (!window.confirm(
    `Remove ${label}? The campaign and its lead mappings are deleted permanently. ` +
    'Lead records and the cost ledger are preserved.'
  )) return;
  try {
    await agentsApi.delete(`/campaigns/${id}`, { headers: authHeader() });
    if (expandedCampaignId.value === id) expandedCampaignId.value = null;
    await loadCampaigns();
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Failed to remove campaign.');
  }
};

// ── Follow-up agent ───────────────────────────────────────────────────
//
// Surfaces the schedule + admin override controls in the UI. State lives
// here so any view can read it via useDashboardState; refresh is on-demand
// (after a campaign launch, after a manual schedule, after pause/resume,
// and on first paint of the LeadsView).

const followupSummary = ref({
  scheduled: 0,
  in_flight: 0,
  today: 0,
  exhausted: 0,
  paused: 0,
  completed: 0,
  cancelled: 0,
});

// Keyed by lead_id → list of follow-up rows (newest first). Populated
// on-demand from /followups?lead_id when the chip opens its detail panel.
const leadFollowupsByLeadId = ref({});
// Keyed by lead_id → { handoff_note, handoff_note_generated_at }. Filled
// by the same endpoint as the follow-up rows so the LeadsView drawer can
// render the manager-readable summary above the schedule list without an
// extra round-trip.
const leadHandoffByLeadId = ref({});

// Business facts: the org's free-text specifics (hours, services, prices,
// staff) appended to the curated per-vertical agent prompt. Replaces the old
// admin "single prompt". Edited in Advanced Settings.
const businessFacts = ref('');
const loadBusinessFacts = async () => {
  try {
    const { data } = await kbApi.get('/business-facts', { headers: authHeader() });
    businessFacts.value = String(data?.business_facts || '');
    return businessFacts.value;
  } catch (err) {
    if (await handleMfaProtectedError(err)) return '';
    errorMsg.value = extractErrorMessage(err, 'Failed to load business facts.');
    return '';
  }
};
const saveBusinessFacts = async (text) => {
  const { data } = await kbApi.post(
    '/business-facts',
    { business_facts: String(text || '') },
    { headers: authHeader() },
  );
  businessFacts.value = String(data?.business_facts || '');
  return businessFacts.value;
};

// Full Follow-Up Pipeline payload (counts + bucketed queue + today's queue
// with handoff notes + conversion ROI). Loaded on demand when the Follow-ups
// page is opened. null until first load so the view can show a skeleton.
const followupPipeline = ref(null);

const loadFollowupPipeline = async () => {
  try {
    const { data } = await agentsApi.get('/followups/pipeline', { headers: authHeader() });
    if (data && typeof data === 'object') {
      followupPipeline.value = data;
    }
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Failed to load the follow-up pipeline.');
  }
};

// Outgoing-lead list for the manual "Add follow-up" picker. Returns a light
// {id, name, phone} array; fetched on demand when the form opens (follow-ups
// schedule against OutgoingLead, not the inbound tool-records the tabs show).
const loadOutgoingLeadsForPicker = async () => {
  try {
    const { data } = await agentsApi.get('/lead-sources/leads', {
      headers: authHeader(),
      params: { limit: 200 },
    });
    if (!Array.isArray(data)) return [];
    return data.map((l) => ({
      id: l.id,
      name: l.name || l.phone_e164 || l.phone_raw || 'Lead',
      phone: l.phone_e164 || l.phone_raw || null,
    }));
  } catch (err) {
    if (await handleMfaProtectedError(err)) return [];
    errorMsg.value = extractErrorMessage(err, 'Failed to load leads for the picker.');
    return [];
  }
};

const loadFollowupSummary = async () => {
  try {
    const { data } = await agentsApi.get('/followups/summary', { headers: authHeader() });
    if (data && typeof data === 'object') {
      followupSummary.value = { ...followupSummary.value, ...data };
    }
  } catch (err) {
    // Silent — tile drops to skeleton; not worth a red banner.
  }
};

const loadFollowupsForLead = async (leadId) => {
  if (!leadId) return [];
  try {
    const { data } = await agentsApi.get('/followups', {
      headers: authHeader(),
      params: { lead_id: leadId },
    });
    const items = (data && Array.isArray(data.items)) ? data.items : [];
    leadFollowupsByLeadId.value = { ...leadFollowupsByLeadId.value, [leadId]: items };
    // The same endpoint returns the lead's handoff note alongside its
    // schedule rows. Stash it under the lead id so the drawer can render
    // it on the same render pass.
    const leadEnvelope = (data && typeof data.lead === 'object') ? data.lead : null;
    if (leadEnvelope) {
      leadHandoffByLeadId.value = {
        ...leadHandoffByLeadId.value,
        [leadId]: {
          handoff_note: leadEnvelope.handoff_note || null,
          handoff_note_generated_at: leadEnvelope.handoff_note_generated_at || null,
        },
      };
    }
    return items;
  } catch (err) {
    if (await handleMfaProtectedError(err)) return [];
    errorMsg.value = extractErrorMessage(err, 'Failed to load follow-ups for lead.');
    return [];
  }
};

const pauseFollowup = async (followupId, leadId) => {
  try {
    await agentsApi.post(`/followups/${followupId}/pause`, {}, { headers: authHeader() });
    if (leadId) await loadFollowupsForLead(leadId);
    await loadFollowupSummary();
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Failed to pause follow-up.');
  }
};

const resumeFollowup = async (followupId, leadId) => {
  try {
    await agentsApi.post(`/followups/${followupId}/resume`, {}, { headers: authHeader() });
    if (leadId) await loadFollowupsForLead(leadId);
    await loadFollowupSummary();
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Failed to resume follow-up.');
  }
};

const cancelFollowupsForLead = async (leadId) => {
  if (!leadId) return;
  if (!window.confirm(
    'Cancel every pending follow-up for this lead? '
    + 'The lead remains active — you can schedule a new follow-up manually later.'
  )) return;
  try {
    await agentsApi.delete('/followups', {
      headers: authHeader(),
      params: { lead_id: leadId },
    });
    await loadFollowupsForLead(leadId);
    await loadFollowupSummary();
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Failed to cancel follow-ups.');
  }
};

const scheduleManualFollowup = async (leadId, scheduledAtIso, campaignId = null) => {
  try {
    const body = { lead_id: leadId, scheduled_at: scheduledAtIso };
    if (campaignId) body.campaign_id = campaignId;
    await agentsApi.post('/followups/manual', body, { headers: authHeader() });
    await loadFollowupsForLead(leadId);
    await loadFollowupSummary();
    infoMsg.value = 'Follow-up scheduled.';
  } catch (err) {
    if (await handleMfaProtectedError(err)) return;
    errorMsg.value = extractErrorMessage(err, 'Failed to schedule follow-up.');
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
        // Streaming STT: every PCM chunk has already been pushed to the
        // server during speech. Tell it we're done so it flushes Sarvam
        // and fires the turn immediately — no REST roundtrip.
        if (voice.value.ws && voice.value.ws.readyState === WebSocket.OPEN) {
          try {
            voice.value.ws.send(JSON.stringify({ type: 'end_of_utterance' }));
          } catch {
            // Closed; cleanup will handle the rest.
          }
        }
        voice.value.utteranceChunks = [];
        voice.value.status = 'thinking';
      } else {
        discardUtteranceCapture();
        voice.value.status = 'listening';
      }
    }
  }
};

const _streamPcmChunkToServer = (chunk) => {
  // Send a single PCM frame as a binary WS message during streaming STT.
  // The server (mode=stream) forwards each frame to the Sarvam STT WS so
  // transcription happens IN PARALLEL with the user still speaking — by
  // the time end-of-speech fires, the transcript is almost ready.
  if (!voice.value.ws || voice.value.ws.readyState !== WebSocket.OPEN) return;
  const inputRate = voice.value.pcmInputRate || 48000;
  const downsampled = inputRate === TARGET_STT_SAMPLE_RATE
    ? chunk
    : downsampleTo16k(chunk, inputRate);
  const int16 = floatToInt16(downsampled);
  try {
    voice.value.ws.send(int16.buffer);
  } catch (err) {
    // Closed mid-send; the call-cleanup path will surface the error.
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
    if (voice.value.isInSpeech) {
      // Streaming STT path: push every chunk straight to the server as
      // it's captured. The server pipes them into Sarvam STT WS so we
      // never pay for a post-EOU REST roundtrip. ``utteranceChunks``
      // is still maintained as a fallback for vad_blob mode.
      _streamPcmChunkToServer(copy);
      if (voice.value.utteranceChunks) {
        voice.value.utteranceChunks.push(copy);
      }
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
  // Streaming-STT path: flush the pre-roll to the server up front so
  // Sarvam has the leading consonants by the time the user's voice
  // hits the mic threshold. Without this the first 60-100ms of audio
  // never reaches the transcriber.
  for (const chunk of voice.value.utteranceChunks) {
    _streamPcmChunkToServer(chunk);
  }
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

const _buildOutboundTesterWsUrl = (token, testerSessionToken) => {
  const tester = outboundTester.value || {};
  const params = new URLSearchParams({ token });
  if (tester.language) params.set('language', tester.language);
  // Preferred path — admin selected a real campaign. The backend loads its
  // agent_prompt / objectives / exit_conditions / doc_text / persona fields
  // straight from the DB and ignores the per-field params below.
  if (selectedTesterCampaignId.value) {
    params.set('campaign_id', String(selectedTesterCampaignId.value));
    return wsUrl(`/api/nokvo-one/agents/voice/outbound-tester/ws?${params.toString()}`);
  }
  // Fallback (no campaign selected) — legacy per-field rehearsal.
  if (tester.caller_name) params.set('caller_name', tester.caller_name.trim());
  if (tester.company_name) params.set('company_name', tester.company_name.trim());
  if (tester.pitch_summary) params.set('pitch_summary', tester.pitch_summary.trim());
  if (tester.objective) params.set('objective', tester.objective);
  if (tester.goal) params.set('goal', tester.goal.trim());
  if (testerSessionToken) params.set('tester_session_token', testerSessionToken);
  if (tester.objectives) params.set('objectives', tester.objectives.trim());
  if (tester.exit_conditions) params.set('exit_conditions', tester.exit_conditions.trim());
  if (tester.tone) params.set('tone', tester.tone.trim());
  if (tester.name) params.set('name', tester.name.trim());
  return wsUrl(`/api/nokvo-one/agents/voice/outbound-tester/ws?${params.toString()}`);
};

const _prepareOutboundTesterSession = async () => {
  // When a campaign is selected the backend ignores the per-field rehearsal
  // payload entirely, so don't bother round-tripping the prompt stash.
  if (selectedTesterCampaignId.value) {
    return { tester_session_token: null, has_prompt: false };
  }
  const tester = outboundTester.value || {};
  const prompt = (tester.agent_prompt || '').trim();
  if (!prompt) {
    // Allowed — caller wants the platform default outbound persona.
    return { tester_session_token: null, has_prompt: false };
  }
  const form = new FormData();
  form.append('agent_prompt', prompt);
  const { data } = await agentsApi.post(
    '/lead-sources/outbound-tester/prepare',
    form,
    { headers: { ...authHeader(), 'Content-Type': 'multipart/form-data' } },
  );
  return data || {};
};

const startVoiceCall = async (mode = 'inbound') => {
  if (voice.value.status !== 'idle' && voice.value.status !== 'error') return;
  voice.value.mode = mode;
  voice.value.status = 'connecting';
  // For the outbound tester we POST the optional prompt + doc to /prepare
  // first so the WS URL stays short. Failures here surface to the user
  // before we touch the mic — better than starting a call and getting a
  // confusing close code.
  let testerSessionToken = null;
  if (mode === 'outbound') {
    try {
      const prepared = await _prepareOutboundTesterSession();
      testerSessionToken = prepared?.tester_session_token || null;
    } catch (err) {
      if (await handleMfaProtectedError(err)) return;
      voice.value.status = 'error';
      voice.value.errorMsg = extractErrorMessage(err, 'Could not prepare the tester session.');
      return;
    }
  }
  voice.value.errorMsg = '';
  voice.value.turns = [];
  voice.value.liveTranscript = '';
  voice.value.playbackGeneration = 0;
  voice.value.scheduledSources = [];
  voice.value.pendingPlaybackChunks = 0;
  voice.value.playbackChain = Promise.resolve();
  voice.value.firstSentenceMs = null;
  voice.value.ttsFirstAudioMs = null;
  // Clear last call's outcome banner so the operator doesn't see a stale
  // "saved as lead" surfacing from the previous test.
  if (mode === 'outbound') outboundTesterOutcome.value = null;
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

    // Local var; shadows the imported wsUrl() helper from ../config.js.
    // Renaming to ``socketUrl`` avoids that collision so we can still call
    // wsUrl() for the inbound fallback path below.
    const socketUrl = mode === 'outbound'
      ? _buildOutboundTesterWsUrl(encodeURIComponent(token), testerSessionToken)
      : wsUrl(`/api/nokvo-one/agents/voice/ws?token=${encodeURIComponent(token)}`);
    const ws = new WebSocket(socketUrl);
    ws.binaryType = 'arraybuffer';
    voice.value.ws = ws;

    ws.onopen = () => {
      // Tell the backend we're using browser VAD — it will treat each binary
      // frame as a complete utterance instead of streaming PCM.
      // Streaming STT: PCM frames flow continuously while the user
      // speaks; Sarvam transcribes in parallel. Saves ~200-400ms per
      // turn vs the legacy vad_blob (full-utterance REST POST).
      ws.send(JSON.stringify({ type: 'config', language: voice.value.language, mode: 'stream', sample_rate: TARGET_STT_SAMPLE_RATE }));
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
      // Anchor the cost meter when the call actually starts (mic up,
      // STT live). Same anchor the backend uses for the ledger row.
      _startCostMeter();
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
    case 'outcome': {
      // End-of-call routing decision from the outbound-tester classifier.
      // Renders as a banner inside the Tester card so the operator can see
      // whether the test lead was filed under the campaign tab, the global
      // Uncategorized tab, or discarded.
      outboundTesterOutcome.value = {
        outcome: msg.outcome,
        reason: msg.reason || '',
        leadId: msg.lead_id || null,
        uncategorized: Boolean(msg.uncategorized),
        campaignId: msg.campaign_id || null,
        campaignName: msg.campaign_name || '',
      };
      // Refresh the Leads page data in the background so the new tab
      // count is correct the moment the operator switches over.
      loadCampaigns().catch(() => {});
      if (currentPage.value === 'leads') {
        loadTabRecords('leads').catch(() => {});
      }
      break;
    }
    default:
      break;
  }
};

// Cost meter helpers. Kept here next to cleanupVoiceCall because both
// touch the same voice ref and need to stay in lockstep — a leaked
// interval would keep ticking after the call ended, and forgetting to
// reset elapsedSeconds would leave a stale total on the badge for the
// next call.
const _startCostMeter = () => {
  if (voice.value.costTimer) {
    try { clearInterval(voice.value.costTimer); } catch {}
  }
  voice.value.callStartedAt = Date.now();
  voice.value.elapsedSeconds = 0;
  voice.value.costTimer = setInterval(() => {
    if (!voice.value.callStartedAt) return;
    voice.value.elapsedSeconds = (Date.now() - voice.value.callStartedAt) / 1000;
  }, 1000);
};

const _stopCostMeter = () => {
  if (voice.value.costTimer) {
    try { clearInterval(voice.value.costTimer); } catch {}
    voice.value.costTimer = null;
  }
  voice.value.callStartedAt = null;
};

// Display values for the in-call meter. Two-decimal rupees (₹0.13/s
// granularity is the tariff so a 1-decimal display would round
// awkwardly) + an HH:MM:SS clock so long calls don't pile into a
// single huge seconds count.
const liveCostRupees = computed(() => {
  const seconds = voice.value.elapsedSeconds || 0;
  return (seconds * COST_RUPEES_PER_SECOND).toFixed(2);
});

const liveCostDuration = computed(() => {
  const total = Math.max(0, Math.floor(voice.value.elapsedSeconds || 0));
  const hh = Math.floor(total / 3600);
  const mm = Math.floor((total % 3600) / 60);
  const ss = total % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return hh > 0 ? `${hh}:${pad(mm)}:${pad(ss)}` : `${pad(mm)}:${pad(ss)}`;
});

const cleanupVoiceCall = () => {
  // Stop VAD polling FIRST so it can't try to start a new capture mid-cleanup.
  if (voice.value.vadTimer) {
    try { clearInterval(voice.value.vadTimer); } catch {}
    voice.value.vadTimer = null;
  }
  // Drop the safety-net close timer set by endVoiceCall so it doesn't fire
  // against a null ws after the server already closed cleanly.
  if (voice.value.closeTimer) {
    try { clearTimeout(voice.value.closeTimer); } catch {}
    voice.value.closeTimer = null;
  }
  // Stop the cost meter and refresh the dashboard tile so the call
  // that just ended shows up in the persisted totals. Refresh runs
  // fire-and-forget — the backend may take a moment to commit the row,
  // so we re-pull after a short delay too.
  _stopCostMeter();
  try { loadCostSummary(); } catch {}
  setTimeout(() => { try { loadCostSummary(); } catch {} }, 1500);
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
    // Send `stop` and DON'T close client-side immediately. The server runs
    // post-session work (transcript classification + lead persistence) in
    // its `finally` block and sends a final {type:"outcome"} message before
    // closing the WS. If we close first, that message never arrives and
    // the outcome banner stays blank. Belt-and-braces: a 10s safety net
    // closes the socket if the server somehow doesn't.
    try { voice.value.ws.send(JSON.stringify({ type: 'stop' })); } catch {}
    voice.value.status = 'ending';
    if (voice.value.closeTimer) { try { clearTimeout(voice.value.closeTimer); } catch {} }
    voice.value.closeTimer = setTimeout(() => {
      try { voice.value.ws?.close(); } catch {}
    }, 10000);
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
  closeNotificationsSocket();
  notifications.value = [];
  unreadCount.value = 0;
  isNotificationsOpen.value = false;
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
  closeNotificationsSocket();
});

// ── Real routing wire-up ───────────────────────────────────────────────
// Mirror route.meta.pageKey into currentPage so deep-links, refreshes, and
// browser back/forward all land on the correct page. We route through
// switchPage() so the per-page data loaders (phoneLink, runtimeStatus,
// loadTabRecords, etc.) fire on every navigation — not only when the user
// clicks a nav button. Previously this watcher only updated currentPage,
// which left views empty after a back/forward because the loader hooks
// never ran. switchPage's router.push is a no-op when the route already
// matches, so the loop is bounded.
watch(
  () => route.meta?.pageKey,
  (pageKey) => {
    if (!pageKey) return;
    if (pageKey === currentPage.value) return;
    // Pre-auth: just sync the label so the right v-if branch shows when the
    // shell finishes initial render. switchPage() touches authed endpoints
    // and would 401 noisily during the login screen.
    if (authState.value !== 'ready') {
      currentPage.value = pageKey;
      return;
    }
    switchPage(pageKey);
  },
  { immediate: true },
);

// When auth resolves while sitting on a dashboard route, fire the page's
// loaders once. Without this, a user that lands on /tickets via a deep link,
// authenticates, and never clicks a nav button would see an empty page —
// because bootstrap only loads the leads/tickets/appointments tab the user
// happened to start on, and the route watcher above is bypassed pre-auth.
watch(authState, async (newState, oldState) => {
  if (newState !== 'ready' || oldState === 'ready') return;
  await nextTick();
  const pageKey = route.meta?.pageKey;
  if (pageKey && pageKey !== 'dashboard') {
    switchPage(pageKey);
  }
});

// Expose the shell's live refs to extracted view components. Refs are kept
// as Refs (not unwrapped) so destructuring in a view's <script setup>
// preserves reactivity — Vue's template compiler auto-unwraps top-level
// setup bindings that are Refs.
provideDashboardState({
  // Identity / org / theme
  currentUser,
  currentOrganization,
  organizationInitial,
  organizationHealth,
  nokvoConnectEnabled,
  themeMode,
  errorMsg,
  infoMsg,
  isAdmin,
  isMemberOnly,
  onboardingV2Enabled,
  isAuthenticating,
  switchPage,

  // Members / invites / dashboard
  costSummary,
  isLoadingCostSummary,
  loadCostSummary,
  formatMinutes,
  memberPageLabel,
  inviteForm,
  isInviteDomainValid,
  inviteValidationMessage,
  isSavingMember,
  inviteCanSubmit,
  inviteMember,
  filteredMembers,
  members,
  isLoadingMembers,
  assignmentForMember,
  openMemberTimetable,
  startAssignmentEdit,
  removingMemberId,
  removeMember,

  // Org-wide site-visit working hours (real estate)
  orgWorkingHours,
  isLoadingOrgHours,
  isSavingOrgHours,
  loadOrgWorkingHours,
  saveOrgWorkingHours,
  toggleOrgHoursDay,
  ORG_HOURS_DAYS,

  // Site-visit claim pool (real estate)
  siteVisitFilter,
  claimableSiteVisits,
  myAssignedTickets,
  isLoadingClaimable,
  claimingVisitId,
  loadClaimableSiteVisits,
  refreshSiteVisitPool,
  claimSiteVisit,

  // Tabs / records / business type
  businessTypeLabel,
  ticketsTabLabel,
  isRealEstateTemplate,
  isClinicTemplate,
  ticketFieldTitle,
  ticketTitleLabel,
  ticketSingularLabel,
  schemaFor,
  startFieldEdit,
  fieldTypeIcon,
  fieldTypeLabel,
  tabRecords,
  tabRecordsLoading,
  loadTabRecords,
  ticketRecordTitle,
  ticketRecordSubtitle,
  ticketRecordPriority,
  ticketRecordOwner,
  recordPhone,
  phoneHref,
  formatRelativeDate,

  // Leads
  leadCampaignTabs,
  activeLeadCampaignTab,
  setActiveLeadCampaign,
  UNCATEGORIZED_TAB_ID,
  filteredLeadRecords,
  leadRecordTitle,
  leadRecordSubtitle,
  leadRecordBudget,
  leadRecordLocation,

  // Appointments
  showAppointmentsTab,
  appointmentRecordTitle,
  appointmentRecordSubtitle,
  appointmentRecordTime,
  appointmentAssignedLabel,
  customTabs,
  customTabActionInProgress,
  deleteCustomTab,
  newCustomTab,
  addCustomTabField,
  removeCustomTabField,
  submitCustomTab,

  // Projects
  projectDraft,
  saveProject,
  startNewProject,
  isSavingProject,
  analyzeBrochure,
  isAnalyzingBrochure,
  projects,
  isLoadingProjects,
  startEditProject,
  deleteProject,
  isDeletingProjectId,

  // My timetable
  isLoadingMyTimetable,
  myTimetable,
  shiftMyCalendarMonth,
  myCalendarMonthLabel,
  myCalendarDays,
  selectMyCalendarDate,
  myCalendarSelectedLabel,
  myCalendarSelectedIsWorking,
  myCalendarSelectedItems,
  formatCalendarTime,
  isMutatingMyBlock,
  removeMyBlock,
  myAssignedQueuedItems,
  bufferDurationOptions,
  bufferForm,
  addBuffer,
  unavailableForm,
  addUnavailability,
  formatSlotRange,

  // Inbound agent
  agents,
  activeAgent,
  profileAgent,
  renameDraft,
  isRenamingAgent,
  renameAgent,
  runtimeStatus,
  voice,
  liveCostRupees,
  liveCostDuration,
  voiceLanguageOptions,
  startVoiceCall,
  endVoiceCall,
  sendTextToVoiceAgent,
  chatInput,
  chatLog,
  sendChat,
  newAgent,
  toolCatalogDefaults,
  selectDefaultAgentTools,
  businessPromptPlaceholder,
  toolCatalogGroups,
  toggleAgentToolGroup,
  isAgentToolGroupAllOn,
  toggleAgentTool,
  createAgent,
  phoneLinkInput,
  isSavingPhoneLink,
  savePhoneLink,
  phoneLink,
  whatsappLink,
  whatsappForm,
  isRequestingWhatsapp,
  loadWhatsAppLink,
  requestWhatsApp,
  cancelWhatsApp,
  transcripts,
  transcriptsLoading,
  loadTranscripts,
  loadTranscript,
  downloadTranscript,
  emailDrafts,
  loadEmailDrafts,
  discardDraft,

  // Outgoing agent
  isLoadingLeadSources,
  loadOutgoingAgentWorkspace,
  requestLeadOAuth,
  nokvoLeadForm,
  NOKVO_FORM_FIELD_TYPES,
  addNokvoFormField,
  removeNokvoFormField,
  createNokvoLeadForm,
  lastNokvoFormLink,
  copyNokvoFormLink,
  outgoingTab,
  loadCampaigns,
  leadForms,
  selectedCallableLeads,
  outgoingLeads,
  selectedLeadIds,
  toggleLeadSelection,
  activeLeadFormId,
  leadFormFilters,
  visibleLeads,
  setLeadFormFilter,
  selectAllCallableInForm,
  clearLeadSelection,
  leadFormName,
  outboundTester,
  OUTBOUND_OBJECTIVE_OPTIONS,
  selectedTesterCampaignId,
  campaigns,
  outboundTesterOutcome,
  showCampaignCreateForm,
  campaignForm,
  CAMPAIGN_OBJECTIVE_OPTIONS,
  toggleCampaignObjective,
  isCreatingCampaign,
  createCampaign,
  outboundNumber,
  campaignFormPreset,
  createAndCallFromForm,
  cancelCampaignPreset,
  expandedCampaignId,
  toggleCampaignExpansion,
  campaignObjectiveLabel,
  detachLeadFromCampaign,
  campaignAttachLeadIds,
  toggleCampaignAttachLead,
  submitCampaignAttachLeads,
  campaignAttachInFlight,
  testCampaign,
  isLaunchingCampaign,
  launchCampaign,
  cancelCampaign,
  removeCampaign,

  // Follow-up agent
  businessFacts,
  loadBusinessFacts,
  saveBusinessFacts,
  // Clinic services
  clinicServices,
  clinicDoctors,
  isLoadingServices,
  loadClinicServices,
  saveClinicService,
  setServiceDoctors,
  deleteClinicService,
  followupSummary,
  followupPipeline,
  loadFollowupPipeline,
  loadOutgoingLeadsForPicker,
  leadFollowupsByLeadId,
  leadHandoffByLeadId,
  loadFollowupSummary,
  loadFollowupsForLead,
  pauseFollowup,
  resumeFollowup,
  cancelFollowupsForLead,
  scheduleManualFollowup,

  // Knowledge base
  kbStats,
  formatBytes,
  kbError,
  kbInfo,
  kbDocumentUploadEnabled,
  kbForm,
  kbBulkQueue,
  isUploadingKb,
  isUploadingKbBulk,
  handleKbDrop,
  handleKbFileChange,
  clearKbFile,
  removeBulkQueueItem,
  kbDocumentTypes,
  uploadKnowledgeDocument,
  uploadKnowledgeDocumentsBulk,
  kbSinglePromptConfig,
  isDisablingSinglePromptAgent,
  disableSinglePromptVoiceAgent,
  kbSinglePromptForm,
  isSavingSinglePromptAgent,
  kbSinglePromptCanSubmit,
  saveSinglePromptVoiceAgent,
  kbQuery,
  testKnowledgeRetrieval,
  isSearchingKb,
  kbResults,
  kbDocuments,
  kbExpandedDocs,
  kbChunksByDoc,
  toggleKnowledgeChunks,
  approveKnowledgeDocument,
  rejectKnowledgeDocument,
  removeKnowledgeDocument,
  isLoadingKb,
  isReconcilingKb,
  reconcileKnowledgeDocuments,
  loadKnowledgeDocuments,
});
</script>

<template>
  <section
    ref="orgShellRef"
    :class="['org-shell', themeMode]"
    @pointermove="updateCursorGlow"
  >
    <div v-if="authState === 'ready'" class="ambient-layer" aria-hidden="true">
      <div class="ambient-orb orb-top"></div>
      <div class="ambient-orb orb-bottom"></div>
    </div>

    <!-- Pre-auth mode bar — theme toggle for the login/signup screens.
         Post-auth, theme + bell + settings live in the floating dock. -->
    <div
      v-if="authState !== 'ready'"
      class="mode-bar"
    >
      <button v-if="false" type="button" class="mode-link" @click="toggleThemeMode">
        <SunMedium v-if="themeMode === 'dark'" :size="14" />
        <Moon v-else :size="14" />
        {{ themeToggleLabel }}
      </button>
      <div
        v-if="authState === 'ready' && !isMemberOnly"
        class="mode-link-wrap notif-wrap"
      >
        <button
          type="button"
          class="mode-link mode-link--icon notif-button"
          :class="{ active: isNotificationsOpen, 'has-unread': unreadCount > 0 }"
          aria-label="Notifications"
          title="Notifications"
          @click="openNotifications"
        >
          <Bell :size="14" />
          <span v-if="unreadCount > 0" class="notif-badge" :aria-label="`${unreadCount} unread`">{{ unreadCount > 99 ? '99+' : unreadCount }}</span>
        </button>
        <div v-if="isNotificationsOpen" class="notif-panel">
          <div class="notif-panel-head">
            <strong>Notifications</strong>
            <button
              v-if="unreadCount > 0"
              type="button"
              class="notif-mark-all"
              @click="markAllNotificationsRead"
            >
              Mark all read
            </button>
          </div>
          <p v-if="!notifications.length && !isLoadingNotifications" class="notif-empty">
            Nothing new. Hot leads, integration issues, and outbound batch results will show up here.
          </p>
          <p v-else-if="isLoadingNotifications && !notifications.length" class="notif-empty">
            Loading…
          </p>
          <ul v-else class="notif-list">
            <li
              v-for="notif in notifications"
              :key="notif.id"
              class="notif-row"
              :class="[`severity-${notif.severity}`, { unread: !notif.read_at }]"
              @click="markNotificationRead(notif)"
            >
              <span class="notif-dot" :class="`severity-${notif.severity}`"></span>
              <div class="notif-row-body">
                <strong>{{ notif.title }}</strong>
                <small v-if="notif.body">{{ notif.body }}</small>
                <small class="notif-row-time">{{ notif.created_at ? new Date(notif.created_at).toLocaleString() : '' }}</small>
              </div>
            </li>
          </ul>
        </div>
      </div>
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
            @click="settingsMenuOpen = false; switchPage('organization_health')"
          >
            <strong>Organization Health</strong>
            <small>Workspace readiness — security, agent setup, knowledge, runtime, outbound, infra.</small>
          </button>
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

    <main v-if="authState !== 'ready'" class="auth-v2">
      <!-- LEFT: brand / marketing stage -->
      <aside class="auth-v2__stage" aria-hidden="true">
        <div class="auth-v2__stage-content">
          <div class="auth-v2__pitch">
            <div class="auth-v2__brand-name">Nokvo<br/>One</div>
            <h1 class="auth-v2__pitch-title">
              Voice agents<br />
              that close.
            </h1>
          </div>
        </div>
      </aside>

      <!-- RIGHT: form -->
      <section class="auth-v2__form-shell">
        <div class="auth-v2__brand auth-v2__brand--mobile">
          <span class="auth-v2__brand-mark">
            <span class="auth-v2__brand-glyph">N</span>
          </span>
          <span class="auth-v2__brand-name">Nokvo<span>/One</span></span>
        </div>

        <div class="auth-v2__card">
        <div v-if="errorMsg" class="message error">{{ errorMsg }}</div>
        <div v-else-if="infoMsg" class="message info">{{ infoMsg }}</div>

        <!-- LOGIN -->
        <div v-if="authState === 'login'" class="mfa-panel">
          <div class="mfa-head">
            <strong>Sign in to Nokvo One</strong>
            <span>Continue with Google or use your email + password</span>
          </div>
          <div class="google-action custom-google-overlay">
            <button type="button" class="google-fallback-button" :class="{ disabled: isAuthenticating }">
              <span class="google-mark">G</span>
              Continue with Google
            </button>
            <div v-if="authConfig?.google_login_enabled" ref="googleLoginButtonRef" class="google-button-host invisible-iframe"></div>
          </div>
          <div class="auth-divider"><span>or</span></div>
          <label class="code-label" for="nokvo-one-email">Email</label>
          <input id="nokvo-one-email" v-model="login.email" class="totp-input" type="email" placeholder="you@gmail.com" />
          <label class="code-label" for="nokvo-one-password">Password</label>
          <input id="nokvo-one-password" v-model="login.password" class="totp-input" type="password" placeholder="••••••••" />
          <div class="mfa-actions">
            <button type="button" class="ghost-button" @click="authState = 'signup'">Create org</button>
            <button type="button" class="primary-button" :disabled="isAuthenticating" @click="handleLogin">
              {{ isAuthenticating ? 'Continuing...' : 'Continue' }}
            </button>
          </div>
          <p class="login-help">
            Access is restricted to organization members on the organization's email domain.
          </p>
        </div>

        <!-- LOGIN TOTP -->
        <div v-else-if="authState === 'login_totp'" class="mfa-panel">
          <div class="mfa-head">
            <strong>Enter Organization TOTP</strong>
            <span>{{ login.email }}</span>
          </div>
          <p class="login-help compact">
            Use the authenticator linked to this email. A different email's TOTP will not work.
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
          <div class="google-action custom-google-overlay">
            <button type="button" class="google-fallback-button" :class="{ disabled: isAuthenticating }">
              <span class="google-mark">G</span>
              Continue with Google
            </button>
            <div v-if="authConfig?.google_login_enabled" ref="googleSignupButtonRef" class="google-button-host invisible-iframe"></div>
          </div>
          <div class="auth-divider"><span>or</span></div>
          <label class="code-label" for="signup-org">Organization name</label>
          <input id="signup-org" v-model="signup.org_name" class="totp-input" type="text" placeholder="Acme Inc." />
          <label class="code-label" for="signup-name">Your name</label>
          <input id="signup-name" v-model="signup.admin_name" class="totp-input" type="text" placeholder="Full name" />
          <label class="code-label" for="signup-email">Work email</label>
          <input id="signup-email" v-model="signup.admin_email" class="totp-input" type="email" placeholder="you@gmail.com" />
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

        <div v-else-if="authState === 'payment'" class="mfa-panel pay-panel">
          <div class="mfa-head">
            <strong>Choose your plan</strong>
            <span>Billed monthly · cancel anytime</span>
          </div>
          <p class="login-help compact">
            Your workspace is set up after payment. Voice minutes are billed monthly on top, at tiered usage rates.
          </p>
          <div class="pay-plans">
            <button
              type="button"
              class="pay-plan"
              :class="{ 'is-selected': selectedPlan === 'inbound_only' }"
              @click="selectedPlan = 'inbound_only'"
            >
              <span class="pay-plan__name">Inbound Only</span>
              <span class="pay-plan__price">₹4,499<small>/mo</small></span>
              <span class="pay-plan__desc">Answer inbound calls with your AI agent.</span>
            </button>
            <button
              type="button"
              class="pay-plan"
              :class="{ 'is-selected': selectedPlan === 'inbound_outbound' }"
              @click="selectedPlan = 'inbound_outbound'"
            >
              <span class="pay-plan__name">Inbound + Outbound</span>
              <span class="pay-plan__price">₹6,499<small>/mo</small></span>
              <span class="pay-plan__desc">Inbound + outbound calling campaigns.</span>
            </button>
          </div>
          <div class="pay-tiers">
            <span class="pay-tiers__head">Per-minute usage (monthly)</span>
            <ul>
              <li><span>0 – 1,000 min</span><strong>₹10/min</strong></li>
              <li><span>1,000 – 10,000 min</span><strong>₹9/min</strong></li>
              <li><span>10,000 – 25,000 min</span><strong>₹8/min</strong></li>
              <li><span>25,000+ min</span><strong>₹6.5/min</strong></li>
            </ul>
          </div>
          <button
            type="button"
            class="primary-button pay-cta"
            :disabled="isPaying"
            @click="startPayment(selectedPlan)"
          >
            {{ isPaying ? 'Opening secure checkout…' : `Subscribe & Pay — ${selectedPlan === 'inbound_outbound' ? '₹6,499' : '₹4,499'}/mo` }}
          </button>
          <p class="login-help compact">Payments are processed securely by Razorpay.</p>
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
            Enter the 6-digit code from the authenticator already linked to this email.
          </p>
          <p v-else class="login-help compact">
            Scan this QR with the authenticator for this email. Your TOTP secret is encrypted at rest.
          </p>
          <div v-if="mfaSetupMode !== 'session_verify' && totpUri" class="qr-shell">
            <QrcodeVue :value="totpUri" :size="168" level="M" background="var(--surface)" foreground="#111111" />
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
                {{ businessTypeTabsLabel(option) }}
              </small>
            </label>
          </div>
          <div v-if="selectedBusinessType" class="schema-preview">
            <strong>Default workspace fields</strong>
            <div class="schema-preview-grid">
              <span
                v-for="field in (businessTypeOptions.find((option) => option.value === selectedBusinessType)?.schemas?.tickets || []).slice(0, 4)"
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

        <!-- POST-PAYMENT ONBOARDING WIZARD -->
        <div v-else-if="authState === 'onboarding'" class="mfa-panel real-estate-wizard">
          <div class="mfa-head">
            <strong>
              <template v-if="onboardingStep === 'business_details'">Your business details</template>
              <template v-else-if="onboardingStep === 'documents'">Verify your business</template>
              <template v-else-if="onboardingStep === 'working_hours'">Set your working hours</template>
              <template v-else-if="onboardingStep === 'projects'">Add your projects</template>
              <template v-else-if="onboardingStep === 'agent'">Name your agent</template>
              <template v-else>Almost done</template>
            </strong>
            <span>
              <template v-if="onboardingStep === 'documents'">We file these with the carrier to allot your calling number.</template>
              <template v-else-if="onboardingStep === 'projects'">The agent uses these to answer questions and capture site visits + leads.</template>
              <template v-else>Step {{ onboardingStepIndex + 1 }} of {{ ONBOARDING_STEPS.length }}</template>
            </span>
          </div>

          <ol class="real-estate-stepper">
            <li v-for="(step, idx) in ONBOARDING_STEPS" :key="step" :class="{ active: onboardingStep === step, done: onboardingStepIndex > idx }">
              <span class="re-step-number">{{ idx + 1 }}</span>
              <span class="re-step-label">{{ onboardingStepLabel(step) }}</span>
            </li>
          </ol>

          <p v-if="errorMsg" class="login-error">{{ errorMsg }}</p>

          <!-- STEP 1: Business details -->
          <div v-if="onboardingStep === 'business_details'" class="re-wizard-step">
            <form class="re-project-form" @submit.prevent="saveOnboardingBusiness">
              <label class="re-form-fullwidth">
                <span>Company legal name *</span>
                <input v-model="onboardingBusiness.legal_name" type="text" placeholder="Acme Realty Private Limited" required />
              </label>
              <label class="re-form-fullwidth">
                <span>Alias / trading name</span>
                <input v-model="onboardingBusiness.alias_name" type="text" placeholder="Acme Realty" />
              </label>
              <div class="re-form-row">
                <label>
                  <span>Company PAN</span>
                  <input v-model="onboardingBusiness.business_pan" type="text" placeholder="ABCDE1234F" />
                </label>
                <label>
                  <span>CIN</span>
                  <input v-model="onboardingBusiness.cin" type="text" placeholder="U70100KA2020PTC123456" />
                </label>
              </div>
              <div class="mfa-actions">
                <button type="submit" class="primary-button" :disabled="isOnboardingSaving || !onboardingBusiness.legal_name.trim()">
                  {{ isOnboardingSaving ? 'Saving…' : 'Continue →' }}
                </button>
              </div>
            </form>
          </div>

          <!-- STEP 2: Documents + compliance -->
          <div v-else-if="onboardingStep === 'documents'" class="re-wizard-step">
            <p class="login-help compact">
              Required for telephony compliance. Upload clear PDFs or images.
            </p>
            <div class="re-project-form">
              <label class="re-form-fullwidth">
                <span>Certificate of incorporation *</span>
                <input type="file" accept=".pdf,.jpg,.jpeg,.png" @change="onboardingPickFile('incorporation', $event)" />
              </label>
              <label class="re-form-fullwidth">
                <span>GST certificate or business PAN *</span>
                <input type="file" accept=".pdf,.jpg,.jpeg,.png" @change="onboardingPickFile('gst_or_pan', $event)" />
              </label>
              <div class="mfa-actions">
                <button type="button" class="primary-button" :disabled="isOnboardingSaving || !onboardingDocs.incorporation || !onboardingDocs.gst_or_pan" @click="submitOnboardingDocs">
                  {{ isOnboardingSaving ? 'Submitting…' : 'Submit & allot number →' }}
                </button>
              </div>
            </div>
          </div>

          <!-- STEP 3: Working hours -->
          <div v-else-if="onboardingStep === 'working_hours'" class="re-wizard-step">
            <div v-if="onboardingNumber.number" class="login-help compact">
              📞 Your calling number <strong>{{ onboardingNumber.number }}</strong>
              <span v-if="onboardingNumber.number_status !== 'active'">— pending carrier verification</span>
            </div>
            <div class="re-project-form">
              <label class="re-form-fullwidth">
                <span>Working days</span>
                <div class="onb-days">
                  <button
                    v-for="d in ONBOARDING_DAYS"
                    :key="d.key"
                    type="button"
                    class="onb-day"
                    :class="{ 'is-on': (onboardingHours.working_days || []).includes(d.key) }"
                    @click="toggleOnboardingDay(d.key)"
                  >{{ d.label }}</button>
                </div>
              </label>
              <div class="re-form-row">
                <label>
                  <span>Start time</span>
                  <input v-model="onboardingHours.start_time" type="time" />
                </label>
                <label>
                  <span>End time</span>
                  <input v-model="onboardingHours.end_time" type="time" />
                </label>
              </div>
              <div class="mfa-actions">
                <button type="button" class="primary-button" :disabled="isOnboardingSaving" @click="saveOnboardingHours">
                  {{ isOnboardingSaving ? 'Saving…' : 'Continue →' }}
                </button>
              </div>
            </div>
          </div>

          <!-- STEP 4: Projects (with brochure analyzer) -->
          <div v-else-if="onboardingStep === 'projects'" class="re-wizard-step">
            <div v-if="projects.length" class="re-project-list">
              <article v-for="project in projects" :key="project.id" class="re-project-card">
                <div class="re-project-card-head">
                  <strong>{{ project.name }}</strong>
                  <div class="re-project-card-actions">
                    <button type="button" class="ghost-button compact" @click="startEditProject(project)">Edit</button>
                    <button type="button" class="ghost-button compact danger" :disabled="isDeletingProjectId === project.id" @click="deleteProject(project)">Remove</button>
                  </div>
                </div>
                <small v-if="project.location">📍 {{ project.location }}</small>
                <small v-if="project.price_display">💰 {{ project.price_display }}</small>
              </article>
            </div>
            <p v-else class="login-help compact">No projects yet. Analyze a brochure to auto-fill, or add one manually.</p>

            <label class="re-form-fullwidth onb-brochure">
              <span>📄 Auto-fill from a brochure</span>
              <input type="file" accept=".pdf,.docx,.doc,.txt" :disabled="isAnalyzingBrochure" @change="analyzeBrochure($event.target.files?.[0])" />
              <small v-if="isAnalyzingBrochure">Analyzing…</small>
            </label>

            <form class="re-project-form" @submit.prevent="saveProject">
              <h4>{{ projectDraft.id ? 'Edit project' : 'Add a project' }}</h4>
              <div class="re-form-row">
                <label>
                  <span>Name *</span>
                  <input v-model="projectDraft.name" type="text" placeholder="Skyline Heights" required />
                </label>
                <label>
                  <span>Location</span>
                  <input v-model="projectDraft.location" type="text" placeholder="HSR Layout, Bangalore" />
                </label>
              </div>
              <div class="re-form-row">
                <label>
                  <span>Property type</span>
                  <input v-model="projectDraft.property_type" type="text" placeholder="Apartments / Villas / Plots" />
                </label>
                <label>
                  <span>Price label</span>
                  <input v-model="projectDraft.price_display" type="text" placeholder="₹95L – ₹1.45Cr" />
                </label>
              </div>
              <label class="re-form-fullwidth">
                <span>Description / pitch</span>
                <textarea v-model="projectDraft.description" rows="3" placeholder="Premium gated community, RERA-approved…"></textarea>
              </label>
              <div class="mfa-actions">
                <button v-if="projectDraft.id" type="button" class="ghost-button" :disabled="isSavingProject" @click="startNewProject">Cancel edit</button>
                <button type="submit" class="primary-button" :disabled="isSavingProject">
                  {{ isSavingProject ? 'Saving…' : (projectDraft.id ? 'Update project' : 'Add project') }}
                </button>
              </div>
            </form>

            <div class="mfa-actions">
              <button type="button" class="primary-button" :disabled="!projects.length || isOnboardingSaving" @click="finishOnboardingProjects">Continue →</button>
            </div>
          </div>

          <!-- STEP 5: Name the agent -->
          <div v-else-if="onboardingStep === 'agent'" class="re-wizard-step">
            <p class="login-help compact">Pick a name your callers and team will hear.</p>
            <div class="re-project-form">
              <label class="re-form-fullwidth">
                <span>Agent name</span>
                <input v-model="onboardingAgentName" type="text" placeholder="Property Assistant" />
              </label>
              <div class="mfa-actions">
                <button type="button" class="primary-button" :disabled="isOnboardingSaving" @click="saveOnboardingAgent">
                  {{ isOnboardingSaving ? 'Saving…' : 'Continue →' }}
                </button>
              </div>
            </div>
          </div>

          <!-- STEP 6: Terms + Privacy -->
          <div v-else class="re-wizard-step">
            <div class="re-project-form">
              <label class="onb-check">
                <input v-model="onboardingTerms.terms" type="checkbox" />
                <span>I accept the <a href="https://nokvo.org/terms" target="_blank" rel="noopener">Terms of Service</a>.</span>
              </label>
              <label class="onb-check">
                <input v-model="onboardingTerms.privacy" type="checkbox" />
                <span>I accept the <a href="https://nokvo.org/privacy" target="_blank" rel="noopener">Privacy Policy</a>.</span>
              </label>
              <div class="mfa-actions">
                <button type="button" class="primary-button" :disabled="isOnboardingSaving || !onboardingTerms.terms || !onboardingTerms.privacy" @click="acceptOnboardingTerms">
                  {{ isOnboardingSaving ? 'Finishing…' : 'Finish & go to dashboard →' }}
                </button>
              </div>
            </div>
          </div>
        </div>

        <!-- REAL-ESTATE WIZARD -->
        <div v-else-if="authState === 'real_estate_setup'" class="mfa-panel real-estate-wizard">
          <div class="mfa-head">
            <strong>
              <template v-if="realEstateOnboardingStep === 'projects'">Add your projects</template>
              <template v-else-if="realEstateOnboardingStep === 'fields'">Tune site-visit + lead fields</template>
              <template v-else-if="realEstateOnboardingStep === 'prompt'">Write your agent prompt</template>
              <template v-else-if="realEstateOnboardingStep === 'tools'">Pick the tools your agent can use</template>
              <template v-else>Name your agent</template>
            </strong>
            <span>
              <template v-if="realEstateOnboardingStep === 'projects'">
                The inbound agent uses these projects to answer questions and to attach site visits + leads.
              </template>
              <template v-else-if="realEstateOnboardingStep === 'fields'">
                Site visits live under the "Tickets" record type. Add the fields your team tracks.
              </template>
              <template v-else-if="realEstateOnboardingStep === 'prompt'">
                One prompt is enough — describe how the agent should greet, qualify, and follow up.
              </template>
              <template v-else-if="realEstateOnboardingStep === 'tools'">
                Toggle the tools the agent can call during live conversations.
              </template>
              <template v-else>
                Pick a name your team will see in the workspace.
              </template>
            </span>
          </div>

          <ol class="real-estate-stepper">
            <li v-for="(step, idx) in realEstateWizardSteps" :key="step" :class="{ active: realEstateOnboardingStep === step, done: realEstateWizardSteps.indexOf(realEstateOnboardingStep) > idx }">
              <span class="re-step-number">{{ idx + 1 }}</span>
              <span class="re-step-label">{{ step === 'projects' ? 'Projects' : step === 'fields' ? 'Fields' : step === 'prompt' ? 'Prompt' : step === 'tools' ? 'Tools' : 'Name' }}</span>
            </li>
          </ol>

          <!-- STEP 1: Projects -->
          <div v-if="realEstateOnboardingStep === 'projects'" class="re-wizard-step">
            <div v-if="projects.length" class="re-project-list">
              <article v-for="project in projects" :key="project.id" class="re-project-card">
                <div class="re-project-card-head">
                  <strong>{{ project.name }}</strong>
                  <div class="re-project-card-actions">
                    <button type="button" class="ghost-button compact" @click="startEditProject(project)">Edit</button>
                    <button type="button" class="ghost-button compact danger" :disabled="isDeletingProjectId === project.id" @click="deleteProject(project)">Remove</button>
                  </div>
                </div>
                <small v-if="project.location">📍 {{ project.location }}</small>
                <small v-if="project.property_type">🏢 {{ project.property_type }}</small>
                <small v-if="project.price_display">💰 {{ project.price_display }}</small>
                <small v-if="project.rera_number">RERA: {{ project.rera_number }}</small>
              </article>
            </div>
            <p v-else class="login-help compact">No projects yet. Add your first one below.</p>

            <form class="re-project-form" @submit.prevent="saveProject">
              <h4>{{ projectDraft.id ? 'Edit project' : 'Add a project' }}</h4>
              <div class="re-form-row">
                <label>
                  <span>Name *</span>
                  <input v-model="projectDraft.name" type="text" placeholder="Skyline Heights" required />
                </label>
                <label>
                  <span>Location</span>
                  <input v-model="projectDraft.location" type="text" placeholder="HSR Layout, Bangalore" />
                </label>
              </div>
              <div class="re-form-row">
                <label>
                  <span>Property type</span>
                  <input v-model="projectDraft.property_type" type="text" placeholder="Apartments / Villas / Plots" />
                </label>
                <label>
                  <span>RERA number</span>
                  <input v-model="projectDraft.rera_number" type="text" placeholder="PRM/KA/RERA/1251/..." />
                </label>
              </div>
              <div class="re-form-row">
                <label>
                  <span>Price (min)</span>
                  <input v-model="projectDraft.price_min" type="number" min="0" step="1" placeholder="9500000" />
                </label>
                <label>
                  <span>Price (max)</span>
                  <input v-model="projectDraft.price_max" type="number" min="0" step="1" placeholder="14500000" />
                </label>
                <label>
                  <span>Price label</span>
                  <input v-model="projectDraft.price_display" type="text" placeholder="₹95L – ₹1.45Cr" />
                </label>
              </div>
              <div class="re-form-row">
                <label>
                  <span>Configurations</span>
                  <input v-model="projectDraft.configurations" type="text" placeholder="2 BHK, 3 BHK, 4 BHK Duplex" />
                </label>
                <label>
                  <span>Possession date</span>
                  <input v-model="projectDraft.possession_date" type="text" placeholder="Dec 2027" />
                </label>
              </div>
              <label class="re-form-fullwidth">
                <span>Amenities (comma or newline separated)</span>
                <textarea v-model="projectDraft.amenities" rows="2" placeholder="Pool, Clubhouse, EV charging, 24x7 security"></textarea>
              </label>
              <label class="re-form-fullwidth">
                <span>Description / pitch</span>
                <textarea v-model="projectDraft.description" rows="4" placeholder="Premium gated community with rooftop pool, 80% open spaces, and IGBC Gold certification. RERA-approved, OC expected Dec 2027."></textarea>
              </label>
              <div class="re-form-row">
                <label>
                  <span>Builder</span>
                  <input v-model="projectDraft.builder_name" type="text" placeholder="Acme Developers" />
                </label>

                <label>
                  <span>Site contact</span>
                  <input v-model="projectDraft.contact_phone" type="tel" placeholder="+91 …" />
                </label>
              </div>
              <div class="mfa-actions">
                <button v-if="projectDraft.id" type="button" class="ghost-button" :disabled="isSavingProject" @click="startNewProject">Cancel edit</button>
                <button type="submit" class="primary-button" :disabled="isSavingProject">
                  {{ isSavingProject ? 'Saving…' : (projectDraft.id ? 'Update project' : 'Add project') }}
                </button>
              </div>
            </form>

            <div class="mfa-actions">
              <button type="button" class="primary-button" :disabled="!projects.length" @click="advanceRealEstateStep">Continue →</button>
            </div>
          </div>

          <!-- STEP 2: Fields -->
          <div v-else-if="realEstateOnboardingStep === 'fields'" class="re-wizard-step">
            <div class="re-fields-block">
              <h4>Site visit fields</h4>
              <p class="login-help compact">
                These show up on the Site Visits tab when the agent or your team creates a record.
              </p>
              <div v-for="(field, index) in realEstateFieldsEditor.tickets" :key="`ticket-${index}`" class="re-field-row">
                <input v-model="field.label" type="text" placeholder="Field label" />
                <input v-model="field.key" type="text" placeholder="snake_case_key" />
                <select v-model="field.type">
                  <option v-for="type in fieldTypes" :key="type" :value="type">{{ type }}</option>
                </select>
                <label class="re-field-required">
                  <input v-model="field.required" type="checkbox" /> required
                </label>
                <button type="button" class="ghost-button compact danger" @click="removeRealEstateField('tickets', index)">×</button>
              </div>
              <button type="button" class="ghost-button" @click="addRealEstateField('tickets')">+ Add site-visit field</button>
            </div>

            <p class="login-help compact">
              Leads are captured automatically as name + phone + call notes — no fields to configure.
            </p>

            <div class="mfa-actions">
              <button type="button" class="ghost-button" :disabled="realEstateFieldsEditor.isSaving" @click="goBackRealEstateStep">← Back</button>
              <button type="button" class="primary-button" :disabled="realEstateFieldsEditor.isSaving" @click="advanceRealEstateStep">
                {{ realEstateFieldsEditor.isSaving ? 'Saving…' : 'Continue →' }}
              </button>
            </div>
          </div>

          <!-- STEP 3: Single prompt -->
          <div v-else-if="realEstateOnboardingStep === 'prompt'" class="re-wizard-step">
            <p class="login-help compact">
              Write the instructions for your inbound agent. It will already know about your projects and field schemas — focus on tone, qualification questions, and follow-up rules.
            </p>
            <textarea
              v-model="realEstateAgentDraft.prompt"
              class="db-input sample-prompt-textarea"
              rows="10"
              placeholder="Example: You are Riya, the property assistant for Skyline Realty. Greet the caller warmly, capture their name and budget, suggest matching projects from the list, and offer to schedule a site visit. Never quote unverified prices."
              :maxlength="8000"
            ></textarea>
            <div class="sample-prompt-meta">
              <small>{{ (realEstateAgentDraft.prompt || '').length }} / 8000 characters</small>
            </div>
            <div class="mfa-actions">
              <button type="button" class="ghost-button" @click="goBackRealEstateStep">← Back</button>
              <button type="button" class="primary-button" :disabled="(realEstateAgentDraft.prompt || '').trim().length < 20" @click="advanceRealEstateStep">Continue →</button>
            </div>
          </div>

          <!-- STEP 4: Tools -->
          <div v-else-if="realEstateOnboardingStep === 'tools'" class="re-wizard-step">
            <p class="login-help compact">
              Pre-selected defaults work for most real-estate teams. Toggle anything you don't need.
            </p>
            <div class="re-tool-groups">
              <article v-for="group in toolCatalogGroups" :key="group.label" class="re-tool-group">
                <h4>{{ group.label }}</h4>
                <label v-for="tool in group.tools" :key="tool.key" class="re-tool-row" :class="{ active: realEstateAgentDraft.toolKeys.includes(tool.key) }">
                  <input
                    type="checkbox"
                    :checked="realEstateAgentDraft.toolKeys.includes(tool.key)"
                    @change="toggleRealEstateTool(tool.key)"
                  />
                  <div>
                    <strong>{{ tool.display_name }}</strong>
                    <small>{{ tool.description }}</small>
                  </div>
                </label>
              </article>
            </div>
            <div class="mfa-actions">
              <button type="button" class="ghost-button" @click="goBackRealEstateStep">← Back</button>
              <button type="button" class="primary-button" @click="advanceRealEstateStep">Continue →</button>
            </div>
          </div>

          <!-- STEP 5: Agent naming -->
          <div v-else class="re-wizard-step">
            <div class="db-form-block outcome-name-row">
              <label class="db-label" for="re-agent-name">Agent name</label>
              <input
                id="re-agent-name"
                v-model="realEstateAgentDraft.agentName"
                class="db-input"
                type="text"
                maxlength="120"
                placeholder="Property Assistant"
              />
            </div>
            <p class="login-help compact">
              Almost done — we'll create your agent with the projects, fields, prompt, and tools you've picked.
            </p>
            <div class="mfa-actions">
              <button type="button" class="ghost-button" :disabled="realEstateAgentDraft.isSaving" @click="goBackRealEstateStep">← Back</button>
              <button type="button" class="primary-button" :disabled="realEstateAgentDraft.isSaving" @click="advanceRealEstateStep">
                {{ realEstateAgentDraft.isSaving ? 'Creating agent…' : 'Create my agent →' }}
              </button>
            </div>
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
      </div><!-- /.auth-v2__card -->

        <p class="auth-v2__foot-note">
          Self-serve with work-email signup. Activations are reviewed by Nokvo before calling features unlock.
        </p>
      </section><!-- /.auth-v2__form-shell -->
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

    <main v-else class="n-shell">
      <div class="global-top-left-logo">
        <img src="../assets/nokvo-logo.png" alt="Nokvo" />
      </div>
      <header class="n-shell-dock" role="banner">
        <div class="n-shell-dock__glow" aria-hidden="true"></div>
        <div class="n-shell-dock__inner">
          <a class="n-shell-brand" href="#" @click.prevent="switchPage('dashboard')" aria-label="Nokvo One home">
            <span class="n-shell-brand__mark">
              <img src="../assets/nokvo-logo.png" alt="Nokvo" class="n-shell-brand__image" />
            </span>
          </a>

          <span class="n-shell-dock__rule" aria-hidden="true"></span>

          <nav class="n-shell-nav" aria-label="Primary">
            <button
              v-if="isMemberOnly"
              type="button"
              class="n-shell-nav__item"
              :class="{ 'is-active': currentPage === 'my_timetable' }"
              @click="switchPage('my_timetable')"
            >
              <CalendarDays :size="14" />
              <span>My timetable</span>
            </button>
            <!-- Ticket Board: members claim AI-captured site-visit requests here. -->
            <button
              v-if="isMemberOnly && isRealEstateTemplate"
              type="button"
              class="n-shell-nav__item"
              :class="{ 'is-active': currentPage === 'ticket_board' }"
              @click="switchPage('ticket_board')"
            >
              <ClipboardList :size="14" />
              <span>Ticket Board</span>
            </button>

            <!-- Operations group -->
            <template v-if="!isMemberOnly">
              <button type="button" class="n-shell-nav__item" :class="{ 'is-active': currentPage === 'dashboard' }" @click="switchPage('dashboard')">
                <Activity :size="14" />
                <span>Dashboard</span>
              </button>
              <button v-if="showTicketsTab" type="button" class="n-shell-nav__item" :class="{ 'is-active': currentPage === 'tickets' }" @click="switchPage('tickets')">
                <FileText :size="14" />
                <span>{{ ticketsTabLabel }}</span>
              </button>
              <button v-if="!isClinicTemplate" type="button" class="n-shell-nav__item" :class="{ 'is-active': currentPage === 'leads' }" @click="switchPage('leads')">
                <UserPlus :size="14" />
                <span>Leads</span>
              </button>
              <button
                v-if="isClinicTemplate"
                type="button"
                class="n-shell-nav__item"
                :class="{ 'is-active': currentPage === 'customers' }"
                @click="switchPage('customers')"
              >
                <Users :size="14" />
                <span>Customer base</span>
              </button>
              <button type="button" class="n-shell-nav__item" :class="{ 'is-active': currentPage === 'followups' }" @click="switchPage('followups')">
                <Repeat :size="14" />
                <span>Follow-ups</span>
              </button>
              <button type="button" class="n-shell-nav__item" :class="{ 'is-active': currentPage === 'transcripts' }" @click="switchPage('transcripts')">
                <ScrollText :size="14" />
                <span>Transcripts</span>
              </button>
              <button
                v-if="isRealEstateTemplate"
                type="button"
                class="n-shell-nav__item"
                :class="{ 'is-active': currentPage === 'projects' }"
                @click="switchPage('projects')"
              >
                <Layers :size="14" />
                <span>Projects</span>
              </button>
              <button
                v-if="isClinicTemplate"
                type="button"
                class="n-shell-nav__item"
                :class="{ 'is-active': currentPage === 'services' }"
                @click="switchPage('services')"
              >
                <Stethoscope :size="14" />
                <span>Services</span>
              </button>
              <button
                v-if="showAppointmentsTab"
                type="button"
                class="n-shell-nav__item"
                :class="{ 'is-active': currentPage === 'appointments' }"
                @click="switchPage('appointments')"
              >
                <CalendarDays :size="14" />
                <span>Appointments</span>
              </button>

              <span class="n-shell-nav__divider" aria-hidden="true"></span>

              <!-- Agents group -->
              <button type="button" class="n-shell-nav__item" :class="{ 'is-active': currentPage === 'agent' }" @click="switchPage('agent')">
                <Bot :size="14" />
                <span>Inbound</span>
              </button>
              <button type="button" class="n-shell-nav__item" :class="{ 'is-active': currentPage === 'outgoing_agent' }" @click="switchPage('outgoing_agent')">
                <PhoneCall :size="14" />
                <span>Outbound</span>
              </button>
            </template>
          </nav>

          <span class="n-shell-dock__rule" aria-hidden="true"></span>

          <div class="n-shell-foot">


            <div v-if="!isMemberOnly" class="n-shell-bell">
              <button
                type="button"
                class="n-shell-icon-btn n-shell-bell__trigger"
                :class="{ 'is-active': isNotificationsOpen, 'has-unread': unreadCount > 0 }"
                @click="openNotifications"
                aria-label="Notifications"
                title="Notifications"
              >
                <Bell :size="15" />
                <span
                  v-if="unreadCount > 0"
                  class="n-shell-bell__badge"
                  :aria-label="`${unreadCount} unread`"
                >{{ unreadCount > 99 ? '99+' : unreadCount }}</span>
              </button>
              <div v-if="isNotificationsOpen" class="n-shell-bell__panel" role="dialog" aria-label="Notifications">
                <header class="n-shell-bell__head">
                  <strong>Notifications</strong>
                  <button
                    v-if="unreadCount > 0"
                    type="button"
                    class="n-shell-bell__mark-all"
                    @click="markAllNotificationsRead"
                  >
                    Mark all read
                  </button>
                </header>
                <p v-if="!notifications.length && !isLoadingNotifications" class="n-shell-bell__empty">
                  Nothing new. Hot leads, integration issues, and outbound batch results show up here.
                </p>
                <p v-else-if="isLoadingNotifications && !notifications.length" class="n-shell-bell__empty">
                  Loading…
                </p>
                <ul v-else class="n-shell-bell__list">
                  <li
                    v-for="notif in notifications"
                    :key="notif.id"
                    class="n-shell-bell__row"
                    :class="[`severity-${notif.severity}`, { 'is-unread': !notif.read_at }]"
                    @click="markNotificationRead(notif)"
                  >
                    <span class="n-shell-bell__dot" :class="`severity-${notif.severity}`"></span>
                    <div class="n-shell-bell__row-body">
                      <strong>{{ notif.title }}</strong>
                      <small v-if="notif.body">{{ notif.body }}</small>
                      <small class="n-shell-bell__row-time">
                        {{ notif.created_at ? new Date(notif.created_at).toLocaleString() : '' }}
                      </small>
                    </div>
                  </li>
                </ul>
              </div>
            </div>


            <button type="button" class="n-shell-user" @click="handleLogout" aria-label="Account">
              <span class="n-shell-user__avatar">
                <span class="n-shell-user__avatar-letter">{{ organizationInitial }}</span>
                <span class="n-shell-user__online" aria-hidden="true"></span>
              </span>
              <span class="n-shell-user__meta">
                <span class="n-shell-user__org n-truncate">{{ currentOrganization?.name || 'Workspace' }}</span>
                <span class="n-shell-user__role">{{ currentUser?.role || 'member' }}</span>
              </span>
            </button>
          </div>
        </div>
      </header>

      <section class="n-shell-banner" v-if="errorMsg || infoMsg">
        <div class="n-shell-banner__inner" :class="errorMsg ? 'is-error' : 'is-info'">
          <span class="n-dot" :class="errorMsg ? 'n-dot--danger' : 'n-dot--success'"></span>
          <span>{{ errorMsg || infoMsg }}</span>
        </div>
      </section>

      <section
        v-if="onboardingV2Enabled && currentUser?.mfa_pending && currentPage === 'dashboard'"
        class="n-shell-callout"
      >
        <div class="n-shell-callout__inner">
          <div class="n-shell-callout__icon">
            <Shield :size="16" />
          </div>
          <div class="n-shell-callout__copy">
            <strong>Secure advanced actions</strong>
            <span>Enable multi-factor auth to unlock calling, campaigns, invites and integrations.</span>
          </div>
          <button type="button" class="n-btn n-btn--brand n-btn--sm" :disabled="isAuthenticating" @click="startSessionTotpSetup">
            Set up MFA
          </button>
        </div>
      </section>

      <router-view v-if="authState === 'ready'" />
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
            <h3>{{ fieldEditor.title }}</h3>
            <p>Tick the details the agent should ask for. It collects ONLY the selected fields, in this order — and stores them on this tab.</p>
          </div>
          <button type="button" class="ghost-button compact" @click="closeFieldEdit">Close</button>
        </div>

        <div v-if="fieldEditor.isLoading" class="field-editor-list">
          <p class="field-editor-empty">Loading fields…</p>
        </div>
        <div v-else class="field-editor-list">
          <div
            v-for="(field, index) in fieldEditor.items"
            :key="`${field.key}:${index}`"
            class="field-editor-row catalog-row"
            :class="{ 'is-selected': field.selected }"
          >
            <label class="field-required-toggle">
              <input v-model="field.selected" type="checkbox" />
              <span>Collect</span>
            </label>
            <label>
              <span>Field</span>
              <input v-if="field.custom" v-model="field.label" type="text" placeholder="Field name" />
              <strong v-else class="catalog-field-name">{{ field.label }}</strong>
            </label>
            <label v-if="field.custom">
              <span>Type</span>
              <select v-model="field.type">
                <option v-for="type in fieldTypes" :key="type" :value="type">{{ type }}</option>
              </select>
            </label>
            <label class="field-required-toggle">
              <input v-model="field.required" type="checkbox" :disabled="!field.selected" />
              <span>Required</span>
            </label>
            <p v-if="field.agent_question" class="catalog-question">Agent asks: “{{ field.agent_question }}”</p>
            <button v-if="field.custom" type="button" class="nav-icon-button" @click="removeField(index)">
              <Trash2 :size="16" />
            </button>
          </div>
        </div>

        <div class="field-modal-actions">
          <button type="button" class="ghost-button compact" @click="addField">
            <Plus :size="15" />
            Add custom field
          </button>
          <button type="button" class="primary-button compact" :disabled="fieldEditor.isSaving || fieldEditor.isLoading" @click="saveFieldEdit">
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

    <!-- Footer removed for brutalist aesthetic -->
  </section>
</template>

<style scoped>
/* ============================================================
   v2 shell styles (premium minimalism). Reads design-system tokens.
   ============================================================ */
.n-shell {
  position: relative;
  z-index: 1;
  min-height: 100vh;
  background: var(--n-bg);
  color: var(--n-text);
  font-family: var(--n-font-body);
  display: flex;
  flex-direction: column;
}

.global-top-left-logo {
  position: absolute;
  top: 16px;
  left: var(--n-page-x);
  width: 44px;
  height: 44px;
  display: grid;
  place-items: center;
  overflow: hidden;
  z-index: 30;
}
.global-top-left-logo img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  transform: scale(4.5);
}

/* ─── Floating bottom dock ─────────────────────────── */
.n-shell { padding-bottom: calc(96px + env(safe-area-inset-bottom)); }

.n-shell-dock {
  position: fixed;
  z-index: 40;
  left: 50%;
  bottom: max(20px, env(safe-area-inset-bottom, 0px));
  transform: translateX(-50%);
  isolation: isolate;
  /* Float-rise entrance */
  animation: nDockRise 480ms var(--n-ease) 100ms both;
}
@keyframes nDockRise {
  from { opacity: 0; transform: translate(-50%, 16px); }
  to { opacity: 1; transform: translate(-50%, 0); }
}

.n-shell-dock__glow {
  display: none;
}

.n-shell-dock__inner {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 10px 7px 8px;
  background: var(--n-bg-elev);
  border: 2px solid var(--n-text);
  border-radius: 0;
  box-shadow: 4px 4px 0 0 var(--n-text);
}
:deep(.org-shell.dark) .n-shell-dock__inner {
  background: rgba(20, 20, 25, 0.45);
  border-color: rgba(255, 255, 255, 0.08);
  box-shadow:
    0 1px 0 rgba(255, 255, 255, 0.05) inset,
    0 0 0 1px rgba(255, 255, 255, 0.04),
    0 14px 36px -10px rgba(0, 0, 0, 0.7),
    0 6px 18px -4px rgba(99, 102, 241, 0.24);
}

.n-shell-dock__rule {
  display: inline-block;
  width: 1px;
  height: 22px;
  background: linear-gradient(180deg, transparent, var(--n-border-strong) 25%, var(--n-border-strong) 75%, transparent);
  margin: 0 4px;
  opacity: 0.65;
}

/* ─── Brand (mark only) ─────────────────────────── */
.n-shell-brand {
  display: inline-flex;
  align-items: center;
  padding: 4px;
  color: var(--n-text);
  text-decoration: none;
  cursor: pointer;
  border-radius: 12px;
  transition: background var(--n-t-fast) var(--n-ease);
}
.n-shell-brand:hover { background: rgba(99, 102, 241, 0.08); }
.n-shell-brand:hover .n-shell-brand__mark {
  box-shadow:
    0 0 0 1px rgba(99, 102, 241, 0.18),
    0 6px 16px -6px rgba(99, 102, 241, 0.45),
    0 1px 0 rgba(255, 255, 255, 0.12) inset;
}
.n-shell-brand__mark {
  position: relative;
  width: 30px;
  height: 30px;
  background: transparent;
  display: grid;
  place-items: center;
  overflow: hidden;
  flex-shrink: 0;
}
.n-shell-brand__image {
  width: 100%;
  height: 100%;
  object-fit: contain;
  transform: scale(4.5);
}
.n-shell-brand__name {
  font-family: var(--n-font-display);
  font-weight: 600;
  font-size: 14px;
  letter-spacing: -0.025em;
  color: var(--n-text);
}
.n-shell-brand__name-soft {
  color: var(--n-text-4);
  font-weight: 400;
  margin-left: 1px;
}

/* ─── Nav ────────────────────────────────────────── */
.n-shell-nav {
  display: flex;
  align-items: center;
  gap: 1px;
}

.n-shell-nav__divider {
  display: inline-block;
  width: 1px;
  height: 18px;
  background: var(--n-border-strong);
  opacity: 0.6;
  margin: 0 6px;
}

.n-shell-nav__item {
  appearance: none;
  background: transparent;
  border: 0;
  color: var(--n-text-3);
  font-family: var(--n-font-body);
  font-size: 12.5px;
  font-weight: 500;
  letter-spacing: -0.005em;
  padding: 8px 10px;
  border-radius: 10px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
  position: relative;
  transition:
    color var(--n-t-fast) var(--n-ease),
    background var(--n-t-fast) var(--n-ease),
    transform var(--n-t-fast) var(--n-ease);
}
.n-shell-nav__item:hover {
  color: var(--n-text);
  background: rgba(15, 15, 20, 0.04);
}
.n-shell-nav__item:active { transform: translateY(0.5px); }
.n-shell-nav__item.is-active {
  color: var(--n-brand-ink);
  background: linear-gradient(180deg, rgba(99, 102, 241, 0.14), rgba(99, 102, 241, 0.08));
  box-shadow:
    0 0 0 1px rgba(99, 102, 241, 0.16) inset,
    0 1px 0 rgba(255, 255, 255, 0.4) inset,
    0 2px 4px -1px rgba(99, 102, 241, 0.18);
}
.n-shell-nav__item.is-active svg {
  color: var(--n-brand);
}
.n-shell-nav__item.is-active::after {
  content: '';
  position: absolute;
  bottom: -8px;
  left: 50%;
  transform: translateX(-50%);
  width: 4px;
  height: 4px;
  background: var(--n-brand);
  border-radius: 50%;
  box-shadow: 0 0 8px rgba(99, 102, 241, 0.5);
}
:deep(.org-shell.dark) .n-shell-nav__item.is-active {
  color: var(--n-brand);
  background: rgba(99, 102, 241, 0.18);
}
:deep(.org-shell.dark) .n-shell-nav__item:hover { background: rgba(255, 255, 255, 0.04); }

/* ─── Right cluster ─────────────────────────────── */
.n-shell-foot {
  display: flex;
  align-items: center;
  gap: 4px;
}

.n-shell-icon-btn {
  appearance: none;
  background: transparent;
  border: 0;
  border-radius: 10px;
  padding: 8px;
  color: var(--n-text-3);
  cursor: pointer;
  display: grid;
  place-items: center;
  transition: color var(--n-t-fast) var(--n-ease),
              background var(--n-t-fast) var(--n-ease);
}
.n-shell-icon-btn:hover {
  color: var(--n-text);
  background: rgba(15, 15, 20, 0.04);
}
:deep(.org-shell.dark) .n-shell-icon-btn:hover { background: rgba(255, 255, 255, 0.04); }
.n-shell-icon-btn.is-active {
  color: var(--n-brand);
  background: linear-gradient(180deg, rgba(99, 102, 241, 0.14), rgba(99, 102, 241, 0.08));
  box-shadow: 0 0 0 1px rgba(99, 102, 241, 0.16) inset;
}

/* ─── Bell + notif panel (opens upward) ─────────── */
.n-shell-bell { position: relative; }
.n-shell-bell__trigger { position: relative; }
.n-shell-bell__trigger.has-unread {
  color: var(--n-text);
}
.n-shell-bell__badge {
  position: absolute;
  top: 3px;
  right: 3px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  background: linear-gradient(180deg, #f43f5e 0%, #e11d48 100%);
  color: #ffffff;
  font-family: var(--n-font-mono);
  font-size: 10px;
  font-weight: 600;
  border-radius: 8px;
  display: grid;
  place-items: center;
  border: 2px solid rgba(255, 255, 255, 0.85);
  box-shadow: 0 1px 4px -1px rgba(244, 63, 94, 0.45);
  letter-spacing: -0.02em;
  line-height: 1;
}
:deep(.org-shell.dark) .n-shell-bell__badge {
  border-color: rgba(20, 20, 25, 0.85);
}

.n-shell-bell__panel {
  position: absolute;
  bottom: calc(100% + 14px);
  right: -8px;
  width: min(360px, calc(100vw - 32px));
  max-height: min(520px, calc(100vh - 140px));
  overflow: hidden;
  background: rgba(255, 255, 255, 0.92);
  backdrop-filter: saturate(200%) blur(28px);
  -webkit-backdrop-filter: saturate(200%) blur(28px);
  border: 1px solid var(--n-border);
  border-radius: 18px;
  box-shadow:
    0 1px 0 rgba(255, 255, 255, 0.6) inset,
    0 24px 48px -16px rgba(15, 15, 20, 0.22),
    0 8px 16px -4px rgba(99, 102, 241, 0.12);
  display: grid;
  grid-template-rows: auto 1fr;
  animation: nBellPop 240ms var(--n-ease) both;
}
:deep(.org-shell.dark) .n-shell-bell__panel {
  background: rgba(18, 18, 24, 0.92);
  border-color: rgba(255, 255, 255, 0.07);
}
@keyframes nBellPop {
  from { opacity: 0; transform: translateY(6px) scale(0.98); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}
/* Tail pointing down toward the bell */
.n-shell-bell__panel::after {
  content: '';
  position: absolute;
  bottom: -7px;
  right: 25px;
  width: 14px;
  height: 14px;
  background: inherit;
  border-right: 1px solid var(--n-border);
  border-bottom: 1px solid var(--n-border);
  transform: rotate(45deg);
}

.n-shell-bell__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px 12px;
  border-bottom: 1px solid var(--n-border-subtle);
}
.n-shell-bell__head strong {
  font-family: var(--n-font-display);
  font-size: 14px;
  font-weight: 600;
  color: var(--n-text);
  letter-spacing: -0.005em;
}
.n-shell-bell__mark-all {
  appearance: none;
  background: transparent;
  border: 0;
  color: var(--n-brand);
  font-family: var(--n-font-body);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  padding: 2px 4px;
  border-radius: 6px;
  transition: background var(--n-t-fast) var(--n-ease);
}
.n-shell-bell__mark-all:hover { background: var(--n-brand-soft); }

.n-shell-bell__empty {
  margin: 0;
  padding: 24px 20px;
  text-align: center;
  font-size: 13px;
  color: var(--n-text-3);
  line-height: 1.5;
}

.n-shell-bell__list {
  list-style: none;
  margin: 0;
  padding: 4px 0;
  overflow-y: auto;
  max-height: 420px;
}
.n-shell-bell__row {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 10px;
  align-items: flex-start;
  padding: 12px 18px;
  cursor: pointer;
  transition: background var(--n-t-fast) var(--n-ease);
  border-left: 2px solid transparent;
}
.n-shell-bell__row:hover { background: var(--n-surface); }
.n-shell-bell__row.is-unread {
  background: var(--n-brand-soft);
  border-left-color: var(--n-brand);
}
.n-shell-bell__row.is-unread:hover { background: rgba(99, 102, 241, 0.16); }

.n-shell-bell__dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--n-text-4);
  margin-top: 5px;
  flex-shrink: 0;
}
.n-shell-bell__dot.severity-info { background: var(--n-info); box-shadow: 0 0 0 3px var(--n-info-soft); }
.n-shell-bell__dot.severity-warning { background: var(--n-warning); box-shadow: 0 0 0 3px var(--n-warning-soft); }
.n-shell-bell__dot.severity-error,
.n-shell-bell__dot.severity-critical { background: var(--n-danger); box-shadow: 0 0 0 3px var(--n-danger-soft); }
.n-shell-bell__dot.severity-success { background: var(--n-success); box-shadow: 0 0 0 3px var(--n-success-soft); }

.n-shell-bell__row-body { display: grid; gap: 2px; min-width: 0; }
.n-shell-bell__row-body strong {
  font-size: 13px;
  font-weight: 600;
  color: var(--n-text);
  letter-spacing: -0.005em;
  line-height: 1.35;
}
.n-shell-bell__row-body small {
  font-size: 12px;
  color: var(--n-text-3);
  line-height: 1.45;
}
.n-shell-bell__row-time {
  font-family: var(--n-font-mono) !important;
  font-size: 10.5px !important;
  color: var(--n-text-4) !important;
  letter-spacing: 0.02em;
  margin-top: 2px;
}

.n-shell-user {
  appearance: none;
  background: transparent;
  border: 0;
  border-radius: 999px;
  padding: 3px 12px 3px 3px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  color: var(--n-text);
  transition: background var(--n-t-fast) var(--n-ease);
  margin-left: 4px;
}
.n-shell-user:hover { background: rgba(15, 15, 20, 0.04); }
:deep(.org-shell.dark) .n-shell-user:hover { background: rgba(255, 255, 255, 0.04); }

.n-shell-user__avatar {
  position: relative;
  width: 28px;
  height: 28px;
  border-radius: 0;
  background: var(--n-text);
  display: grid;
  place-items: center;
  flex-shrink: 0;
  border: 2px solid var(--n-text);
}
.n-shell-user__avatar-letter {
  font-family: var(--n-font-display);
  font-weight: 600;
  font-size: 12.5px;
  color: var(--n-text-inverse);
  letter-spacing: -0.02em;
}
.n-shell-user__online {
  position: absolute;
  bottom: -4px;
  right: -4px;
  width: 9px;
  height: 9px;
  background: var(--n-success);
  border: 2px solid var(--n-text);
  border-radius: 0;
}

.n-shell-user__meta { display: grid; line-height: 1.15; text-align: left; max-width: 140px; }
.n-shell-user__org {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--n-text);
  letter-spacing: -0.01em;
}
.n-shell-user__role {
  font-family: var(--n-font-mono);
  font-size: 9.5px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--n-text-3);
}

@media (max-width: 1100px) {
  .n-shell-cmdk span { display: none; }
  .n-shell-cmdk { padding: 6px 8px; }
}

/* ─── Banner & callout ─────────────────────────────── */
.n-shell-banner {
  max-width: var(--n-content-w);
  margin: 12px auto 0;
  padding: 0 var(--n-page-x);
}
.n-shell-banner__inner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-radius: var(--n-r-lg);
  background: var(--n-surface);
  border: 1px solid var(--n-border);
  color: var(--n-text);
  font-size: 13px;
}
.n-shell-banner__inner.is-error { background: var(--n-danger-soft); border-color: rgba(220, 38, 38, 0.18); color: var(--n-danger-2); }
.n-shell-banner__inner.is-info { background: var(--n-success-soft); border-color: rgba(5, 150, 105, 0.18); color: var(--n-success-2); }

.n-shell-callout {
  max-width: var(--n-content-w);
  margin: 16px auto 0;
  padding: 0 var(--n-page-x);
}
.n-shell-callout__inner {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 16px;
  align-items: center;
  padding: 16px 20px;
  background: var(--n-bg);
  border: 2px solid var(--n-text);
  border-radius: 0;
  box-shadow: 4px 4px 0 0 var(--n-text);
}
.n-shell-callout__icon {
  width: 36px;
  height: 36px;
  border-radius: 0;
  background: var(--n-text);
  color: var(--n-text-inverse);
  display: grid;
  place-items: center;
  border: 2px solid var(--n-text);
}
.n-shell-callout__copy { display: grid; gap: 2px; }
.n-shell-callout__copy strong {
  font-family: var(--n-font-display);
  font-size: 14px;
  color: var(--n-brand-ink);
  font-weight: 600;
  letter-spacing: -0.005em;
}
.n-shell-callout__copy span {
  font-size: 13px;
  color: var(--n-text-2);
}

/* ─── Responsive ─────────────────────────────────── */
@media (max-width: 1100px) {
  .n-shell-nav__item span { display: none; }
  .n-shell-nav__item { padding: 8px; }
  .n-shell-nav__divider { margin: 0 3px; height: 16px; }
  .n-shell-user__meta { display: none; }
  .n-shell-user { padding: 3px; }
}
@media (max-width: 720px) {
  .n-shell-dock { left: 12px; right: 12px; transform: none; bottom: max(12px, env(safe-area-inset-bottom, 0px)); }
  @keyframes nDockRise { from { opacity: 0; transform: translateY(16px); } to { opacity: 1; transform: translateY(0); } }
  .n-shell-dock__inner { width: 100%; justify-content: space-between; }
  .n-shell-dock__rule { display: none; }
}

/* ============================================================
   v2 AUTH (login / signup / MFA / invite / wizards)
   ============================================================ */
.auth-v2 {
  position: relative;
  min-height: 100vh;
  display: grid;
  grid-template-columns: 1fr 1fr;
  font-family: var(--n-font-body);
  color: #000000;
  background: #ffffff;
}

@media (max-width: 960px) {
  .auth-v2 { grid-template-columns: 1fr; }
  .auth-v2__stage { display: none; }
}

/* ─── Left stage ─────────────────────────────────────── */
.auth-v2__stage {
  position: relative;
  overflow: hidden;
  isolation: isolate;
  background: #000000;
  color: #ffffff;
  display: flex;
  align-items: center;
  padding: 4vw;
}

/* Stage content */
.auth-v2__stage-content {
  position: relative;
  z-index: 5;
  width: 100%;
  animation: authFade 640ms var(--n-ease) 80ms both;
}

@keyframes authFade {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}

/* Brand on stage */
.auth-v2__brand-name {
  font-family: var(--n-font-display);
  font-weight: 800;
  font-size: clamp(60px, 8vw, 120px);
  line-height: 0.9;
  letter-spacing: -0.04em;
  text-transform: uppercase;
  margin-bottom: 60px;
}

.auth-v2__brand--mobile { display: none; margin-bottom: 24px; }
.auth-v2__brand--mobile .auth-v2__brand-name { font-size: 24px; }

/* Pitch block (the headline) */
.auth-v2__pitch { display: grid; gap: 18px; width: 100%; }

.auth-v2__pitch-title {
  margin: 0;
  font-family: var(--n-font-display);
  font-weight: 500;
  font-size: clamp(40px, 5vw, 80px);
  line-height: 1.0;
  letter-spacing: -0.03em;
  color: #ffffff;
}

/* ─── Right form shell ────────────────────────────── */
.auth-v2__form-shell {
  position: relative;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  padding: 4vw;
  background: #ffffff;
  color: #000000;
  overflow-y: auto;
}

@media (max-width: 960px) {
  .auth-v2__brand--mobile { display: inline-flex; }
  .auth-v2__brand--mobile .auth-v2__brand-name { color: #000000; font-size: 24px; font-weight: 600; }
  .auth-v2__brand--mobile .auth-v2__brand-name span { color: #666666; font-weight: 400; }
}

.auth-v2__card {
  position: relative;
  width: 100%;
  max-width: 480px;
  padding: 0;
  background: transparent;
  border: none;
  box-shadow: none;
  animation: authFade 600ms var(--n-ease) 140ms both;
}
:deep(.org-shell.dark) .auth-v2__card {
  background: transparent;
  box-shadow: none;
}

.auth-v2__foot-note {
  margin: 24px auto 0;
  max-width: 440px;
  text-align: center;
  font-size: 12px;
  color: #666666;
  line-height: 1.5;
}

/* ─── Override the legacy auth primitives inside .auth-v2 ─── */
.auth-v2 :deep(.message) {
  border-radius: 4px;
  padding: 10px 14px;
  font-size: 13px;
  margin-bottom: 18px;
  border: 1px solid #000000;
}
.auth-v2 :deep(.message.error) {
  background: #fdf2f2;
  border-color: #dc2626;
  color: #dc2626;
}
.auth-v2 :deep(.message.info) {
  background: #f0fdf4;
  border-color: #16a34a;
  color: #16a34a;
}

.auth-v2 :deep(.mfa-panel) {
  display: grid;
  gap: 14px;
  background: transparent;
  border: 0;
  padding: 0;
  box-shadow: none;
}
.auth-v2 :deep(.mfa-head) { display: grid; gap: 6px; margin-bottom: 6px; }
.auth-v2 :deep(.mfa-head strong) {
  font-family: var(--n-font-display);
  font-size: 28px;
  font-weight: 600;
  letter-spacing: -0.025em;
  color: #000000;
  line-height: 1.2;
}
.auth-v2 :deep(.mfa-head span) {
  font-size: 14px;
  color: #666666;
  line-height: 1.5;
}

.auth-v2 :deep(.code-label) {
  font-family: var(--n-font-body);
  font-size: 13px;
  font-weight: 500;
  color: #000000;
  letter-spacing: -0.005em;
  display: block;
  margin: 4px 0 6px;
}

.auth-v2 :deep(.totp-input) {
  appearance: none;
  width: 100%;
  font-family: var(--n-font-body);
  font-size: 16px;
  color: #000000;
  background: transparent;
  border: 2px solid #000000;
  border-radius: 0;
  padding: 14px 16px;
  line-height: 1.4;
  transition: background var(--n-t-fast) var(--n-ease);
}
.auth-v2 :deep(.totp-input::placeholder) { color: #888888; }
.auth-v2 :deep(.totp-input:hover) { background: #f9f9f9; }
.auth-v2 :deep(.totp-input:focus) {
  outline: none;
  background: #ffffff;
  box-shadow: 4px 4px 0 0 #000000;
}
.auth-v2 :deep(.totp-input[inputmode="numeric"]) {
  font-family: var(--n-font-mono);
  letter-spacing: 0.5em;
  text-align: center;
  font-size: 24px;
  padding: 18px 12px;
}

.auth-v2 :deep(.mfa-actions) {
  display: flex;
  gap: 12px;
  justify-content: space-between;
  margin-top: 12px;
}
.auth-v2 :deep(.primary-button) {
  appearance: none;
  border: 2px solid #000000;
  background: #000000;
  color: #ffffff;
  font-family: var(--n-font-body);
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 0;
  padding: 14px 24px;
  border-radius: 0;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  box-shadow: none;
  transition: all 150ms ease;
  transform: translate(0, 0);
}
.auth-v2 :deep(.primary-button:hover:not(:disabled)) {
  background: #ffffff;
  color: #000000;
  box-shadow: 4px 4px 0 0 #000000;
  transform: translate(-2px, -2px);
}
.auth-v2 :deep(.primary-button:disabled) { opacity: 0.55; cursor: not-allowed; transform: none; }

.auth-v2 :deep(.ghost-button) {
  appearance: none;
  background: transparent;
  border: 2px solid #000000;
  color: #000000;
  font-family: var(--n-font-body);
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 0;
  padding: 14px 20px;
  border-radius: 0;
  cursor: pointer;
  transition: all 150ms ease;
  transform: translate(0, 0);
}
.auth-v2 :deep(.ghost-button:hover:not(:disabled)) {
  background: #f5f5f5;
  box-shadow: 4px 4px 0 0 #000000;
  transform: translate(-2px, -2px);
}

.auth-v2 :deep(.login-help) {
  font-size: 12.5px;
  color: #666666;
  line-height: 1.55;
  margin: 4px 0 0;
}
.auth-v2 :deep(.login-help.compact) { margin: 0; }

/* Google button + divider */
.auth-v2 :deep(.google-action) { margin-bottom: 12px; position: relative; }
.auth-v2 :deep(.invisible-iframe) {
  position: absolute;
  inset: 0;
  opacity: 0.01;
  z-index: 10;
  overflow: hidden;
}
.auth-v2 :deep(.invisible-iframe iframe) {
  width: 100% !important;
  height: 100% !important;
}
.auth-v2 :deep(.google-fallback-button) {
  width: 100%;
  appearance: none;
  background: #ffffff;
  border: 2px solid #000000;
  border-radius: 0;
  padding: 14px 16px;
  font-family: var(--n-font-body);
  font-size: 16px;
  font-weight: 600;
  color: #000000;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  cursor: pointer;
  transition: all 150ms ease;
  transform: translate(0, 0);
  position: relative;
  z-index: 1;
}
.auth-v2 :deep(.google-action:hover .google-fallback-button:not(.disabled)) {
  box-shadow: 4px 4px 0 0 #000000;
  transform: translate(-2px, -2px);
  background: #f5f5f5;
}
.auth-v2 :deep(.google-fallback-button.disabled) {
  opacity: 0.55;
  cursor: not-allowed;
}
.auth-v2 :deep(.google-mark) {
  width: 24px;
  height: 24px;
  border-radius: 0;
  background: #000000;
  color: #ffffff;
  font-weight: 800;
  display: grid;
  place-items: center;
  font-size: 14px;
  font-family: var(--n-font-display);
}

.auth-v2 :deep(.auth-divider) {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 4px 0 4px;
  color: #888888;
  font-family: var(--n-font-mono);
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
}
.auth-v2 :deep(.auth-divider::before),
.auth-v2 :deep(.auth-divider::after) {
  content: '';
  flex: 1;
  height: 1px;
  background: #e5e5e5;
}

/* QR + secret + provisioning */
.auth-v2 :deep(.qr-shell) {
  background: var(--n-bg);
  border: 1px solid var(--n-border);
  border-radius: 14px;
  padding: 16px;
  display: grid;
  place-items: center;
  margin: 6px 0;
}
.auth-v2 :deep(.secret-note) {
  font-size: 12px;
  color: var(--n-text-3);
  background: var(--n-surface);
  padding: 10px 12px;
  border-radius: 8px;
  word-break: break-all;
}
.auth-v2 :deep(.secret-note code) {
  font-family: var(--n-font-mono);
  font-size: 11.5px;
  color: var(--n-text);
}

.auth-v2 :deep(.provisioning-block) {
  background: var(--n-surface);
  border: 1px solid var(--n-border-subtle);
  border-radius: 14px;
  padding: 16px;
}
.auth-v2 :deep(.provisioning-head) {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.auth-v2 :deep(.provisioning-head strong) { font-size: 13px; font-weight: 600; color: var(--n-text); }
.auth-v2 :deep(.provisioning-steps) {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 8px;
}
.auth-v2 :deep(.provisioning-steps li) {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 10px;
  align-items: center;
  padding: 10px 12px;
  background: var(--n-bg);
  border: 1px solid var(--n-border-subtle);
  border-radius: 10px;
  font-size: 12.5px;
}
.auth-v2 :deep(.provisioning-steps li strong) { font-size: 12.5px; color: var(--n-text); display: block; }
.auth-v2 :deep(.provisioning-steps li small) { font-size: 11.5px; color: var(--n-text-3); display: block; }
.auth-v2 :deep(.step-marker) {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--n-text-4);
}
.auth-v2 :deep(.step-marker[data-state="running"]) { background: var(--n-brand); box-shadow: 0 0 0 3px var(--n-brand-soft); animation: authPulse 1.6s ease-in-out infinite; }
.auth-v2 :deep(.step-marker[data-state="success"]),
.auth-v2 :deep(.step-marker[data-state="completed"]) { background: var(--n-success); box-shadow: 0 0 0 3px var(--n-success-soft); }
.auth-v2 :deep(.step-marker[data-state="failed"]),
.auth-v2 :deep(.step-marker[data-state="error"]) { background: var(--n-danger); box-shadow: 0 0 0 3px var(--n-danger-soft); }
.auth-v2 :deep(.step-state) {
  font-family: var(--n-font-mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--n-text-4);
}

/* Wizards (business type / outcome / real estate / sample upload) — give
   them breathing room so they don't overflow the card. */
.auth-v2 :deep(.outcome-wizard),
.auth-v2 :deep(.real-estate-wizard) { display: grid; gap: 14px; }

/* Hide the legacy footer-links wrapper if it shows up */
.auth-v2 :deep(.footer-links) { display: none; }

/* ============================================================
   Legacy styles below — kept for non-redesigned views.
   ============================================================ */
.org-shell {
  position: relative;
  min-height: 100vh;
  width: 100%;
  overflow: hidden;
  --cursor-x: 50%;
  --cursor-y: 30%;
  background: var(--paper);
  color: var(--ink);
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
  position: absolute;
  top: 0;
  right: 0;
  z-index: 100;
  width: auto;
  padding: 2rem 2rem 0;
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
  pointer-events: none;
}
.mode-bar > * {
  pointer-events: auto;
}

.mode-link {
  border: 1px solid rgba(27, 28, 21, 0.12);
  background: rgba(255, 255, 255, 0.82);
  color: var(--ink);
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
  color: var(--ink);
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
  color: var(--ink);
  font-family: var(--nokvo-body);
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
  font-family: var(--nokvo-display);
  font-optical-sizing: auto;
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
  color: var(--surface);
  font-family: var(--nokvo-body);
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
  background: var(--ink);
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
  color: var(--ink);
  border-color: rgba(244, 240, 225, 0.3);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
}

.org-shell.dark .connect-continue-button:hover {
  background: var(--surface);
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
  color: var(--ink);
}

.connect-panel-head h2 {
  font-family: var(--nokvo-display);
  font-optical-sizing: auto;
  font-size: 1.6rem;
  margin: 0 0 0.35rem;
}

.connect-panel-head p {
  color: var(--muted);
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
  background: var(--ink);
  color: var(--surface);
  border-radius: 12px;
  padding: 1rem 1.1rem;
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
}

.connect-secret-callout strong {
  font-family: var(--nokvo-body);
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
  color: var(--surface);
  word-break: break-all;
}

.connect-secret-dismiss {
  align-self: flex-end;
  background: rgba(244, 240, 225, 0.12);
  border: 1px solid rgba(244, 240, 225, 0.22);
  color: var(--surface);
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
  color: var(--muted);
}

.connect-create-form input,
.connect-create-form select,
.connect-create-form textarea {
  border: 1px solid rgba(27, 28, 21, 0.18);
  background: #fff;
  border-radius: 8px;
  padding: 0.6rem 0.75rem;
  font-size: 0.9rem;
  color: var(--ink);
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
  font-family: var(--nokvo-body);
  font-size: 0.95rem;
  margin: 0.5rem 0 0;
}

.connect-key-empty {
  color: var(--muted);
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
  font-family: var(--nokvo-body);
  font-size: 0.92rem;
}

.connect-key-card-head code {
  font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.78rem;
  color: var(--muted);
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
  color: var(--muted);
}

.connect-key-card-meta div {
  display: flex;
  justify-content: space-between;
}

.connect-key-card-meta dt {
  color: var(--muted);
}

.connect-key-card-meta dd {
  margin: 0;
  color: var(--ink);
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
  color: var(--surface);
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
  color: var(--surface);
}

.org-shell.dark .connect-key-card {
  background: rgba(28, 29, 24, 0.7);
  border-color: rgba(244, 240, 225, 0.16);
}

.org-shell.dark .connect-key-card-meta dd {
  color: var(--surface);
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
  color: var(--surface);
  background:
    radial-gradient(circle at 12% 18%, rgba(255, 168, 78, 0.4), transparent 55%),
    radial-gradient(circle at 88% 14%, rgba(80, 140, 220, 0.45), transparent 55%),
    radial-gradient(circle at 50% 95%, rgba(150, 90, 220, 0.45), transparent 55%),
    linear-gradient(135deg, #1c1d18 0%, #2a2620 40%, #1d2a3d 75%, #2c1d3d 100%);
}

.org-shell.dark .connect-back-link {
  border-color: rgba(244, 240, 225, 0.28);
  background: rgba(28, 29, 24, 0.7);
  color: var(--surface);
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
  font-family: var(--nokvo-body);
  font-size: clamp(2.25rem, 4vw, 3rem);
  letter-spacing: -0.05em;
  font-weight: 700;
}

.brand-block p {
  margin: 0;
  color: var(--muted);
  font-size: 1rem;
}

.login-card {
  background: rgba(255, 255, 255, 0.82);
  border: 1px solid var(--line-solid);
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
  border: 1px solid var(--line-solid);
  background: var(--surface);
  color: var(--ink);
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
  border: 1px solid var(--line-solid);
  font-family: Arial, sans-serif;
  font-weight: 700;
  color: #4285f4;
}

.auth-divider {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin: 0.4rem 0;
  color: var(--muted);
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
  color: var(--muted);
  line-height: 1.6;
}

.login-help.compact {
  margin-top: 0;
}

.mfa-panel {
  display: grid;
  gap: 1rem;
}

/* ── Payment (Razorpay) onboarding panel ── */
.pay-plans {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.6rem;
}
@media (max-width: 480px) {
  .pay-plans { grid-template-columns: 1fr; }
}
.pay-plan {
  display: grid;
  gap: 0.25rem;
  text-align: left;
  padding: 0.85rem 0.9rem;
  border-radius: 12px;
  border: 1.5px solid var(--n-border, rgba(120,120,140,0.3));
  background: transparent;
  color: inherit;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s, transform 0.15s;
}
.pay-plan:hover { transform: translateY(-1px); }
.pay-plan.is-selected {
  border-color: #4f46e5;
  background: rgba(79, 70, 229, 0.08);
}
.pay-plan__name { font-weight: 600; font-size: 0.92rem; }
.pay-plan__price { font-size: 1.35rem; font-weight: 700; letter-spacing: -0.02em; }
.pay-plan__price small { font-size: 0.7rem; font-weight: 500; opacity: 0.6; }
.pay-plan__desc { font-size: 0.74rem; opacity: 0.7; line-height: 1.3; }
.pay-tiers {
  border: 1px solid var(--n-border, rgba(120,120,140,0.25));
  border-radius: 10px;
  padding: 0.6rem 0.8rem;
}
.pay-tiers__head {
  font-family: var(--n-font-mono, monospace);
  font-size: 0.62rem;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  opacity: 0.55;
}
.pay-tiers ul { list-style: none; margin: 0.4rem 0 0; padding: 0; display: grid; gap: 0.25rem; }
.pay-tiers li { display: flex; justify-content: space-between; font-size: 0.78rem; }
.pay-tiers li span { opacity: 0.7; }
.pay-cta { width: 100%; }

.mfa-head {
  display: flex;
  flex-direction: column;
  gap: 0.18rem;
}

.mfa-head strong {
  font-family: var(--nokvo-body);
  font-size: 1.1rem;
}

.mfa-head span {
  color: var(--muted);
  font-size: 0.92rem;
}

.qr-shell {
  display: flex;
  justify-content: center;
  padding: 1rem;
  border-radius: 1rem;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid var(--line-solid);
}

.secret-note {
  color: var(--muted);
  font-size: 0.92rem;
  text-align: center;
}

.secret-note code {
  color: var(--ink);
  font-weight: 700;
}

.code-label {
  font-size: 0.86rem;
  font-weight: 700;
  color: var(--ink);
}

.totp-input {
  width: 100%;
  border-radius: 0.9rem;
  border: 1px solid var(--line-solid);
  background: rgba(255, 255, 255, 0.8);
  color: var(--ink);
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
  background: var(--surface);
  color: var(--ink);
}

.primary-button {
  border: none;
  background: #1d1c0f;
  color: var(--surface);
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
  border: 1px solid var(--line-solid);
  background: var(--paper);
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
  background: var(--paper-2);
  box-shadow: inset 0 0 0 1px #000000;
}

.provider-name {
  font-weight: 700;
  color: var(--ink);
}

.provider-option small {
  color: var(--muted);
}

.schema-preview {
  border: 1px solid var(--line-solid);
  background: var(--paper);
  border-radius: 0.95rem;
  padding: 1rem;
}

.schema-preview strong,
.schema-field-row strong {
  color: var(--ink);
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
  border: 1px solid var(--line-solid);
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
  color: var(--muted);
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
  border: 1px solid var(--line-solid);
  background: var(--surface);
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
  border: 1px solid var(--line-solid);
  background: var(--paper);
}

/* Catalog-driven picker rows: [Collect] [Field] [Required] + the agent's question. */
.field-editor-row.catalog-row {
  grid-template-columns: 88px minmax(160px, 1fr) 110px;
  align-items: center;
  opacity: 0.72;
}
.field-editor-row.catalog-row.is-selected {
  opacity: 1;
  border-color: var(--accent, #6b8f5e);
}
.field-editor-row.catalog-row .catalog-field-name {
  font-weight: 600;
}
.field-editor-row.catalog-row .catalog-question {
  grid-column: 1 / -1;
  margin: 0;
  font-size: 0.82rem;
  color: var(--muted);
  font-style: italic;
}
.field-editor-empty {
  color: var(--muted);
  padding: 1rem 0;
}

.field-editor-row label {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.field-editor-row label span {
  color: var(--muted);
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

.field-editor-row input[type="text"],
.field-editor-row select {
  width: 100%;
  border-radius: 0.75rem;
  border: 1px solid var(--line-solid);
  background: var(--surface);
  color: var(--ink);
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
  border-top: 1px solid var(--line-solid);
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
  color: var(--muted);
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
  border: 1px solid var(--line-solid);
  background: var(--surface);
  color: var(--ink);
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
  border: 1px solid var(--line-solid);
  border-radius: 0.8rem;
  background: var(--paper);
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
  border-color: var(--line-solid);
  background: #fff9e8;
  color: #7a5b1e;
}

.calendar-timezone {
  border: 1px solid var(--line-solid);
  border-radius: 999px;
  background: var(--surface);
  color: var(--muted);
  font-size: 0.78rem;
  font-weight: 800;
  padding: 0.45rem 0.7rem;
  white-space: nowrap;
}

.schedule-calendar-card {
  overflow: hidden;
  border: 1px solid var(--line-solid);
  border-radius: 0.8rem;
  background: var(--surface);
  box-shadow: 0 14px 28px rgba(27, 28, 21, 0.08);
}

.calendar-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  border-bottom: 1px solid var(--line-solid);
  background: #f5f4e8;
  color: var(--ink);
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
  color: var(--muted);
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
  border-bottom: 1px solid var(--line-solid);
  background: var(--surface);
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
  border-right: 1px solid var(--line-solid);
  background: var(--paper);
  color: var(--ink);
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
  color: var(--ink);
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
  border: 1px solid var(--line-solid);
  border-radius: 0.8rem;
  background: var(--surface);
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
  color: var(--muted);
  font-size: 0.72rem;
  font-weight: 900;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.time-planner-head strong {
  color: var(--ink);
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
  border: 1px solid var(--line-solid);
  border-radius: 0.75rem;
  background: var(--paper);
  color: var(--ink);
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
  color: var(--muted);
  font-size: 0.72rem;
  font-weight: 900;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.time-preset-button strong {
  color: var(--ink);
  font-size: 0.86rem;
}

.time-range-control {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.75rem;
}

.time-input-card {
  border: 1px solid var(--line-solid);
  border-radius: 0.8rem;
  background: var(--paper);
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
  border: 1px solid var(--line-solid);
  border-radius: 0.8rem;
  background: var(--surface);
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
  color: var(--muted);
  font-size: 0.88rem;
  line-height: 1.45;
}

.blocked-slot-form {
  align-items: end;
  border: 1px solid var(--line-solid);
  border-radius: 0.8rem;
  background: var(--surface);
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
  background: var(--surface);
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
  color: var(--ink);
  font-size: 0.82rem;
}

.event-main {
  display: grid;
  min-width: 0;
  gap: 0.2rem;
}

.event-main strong {
  overflow: hidden;
  color: var(--ink);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.event-main span {
  color: var(--muted);
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
  border: 1px solid var(--line-solid);
  background: var(--paper);
  border-radius: 999px;
  padding: 0.55rem 0.75rem;
}

.assignment-chip span {
  color: var(--ink) !important;
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
  border: 1px solid var(--line-solid);
  border-radius: 0.8rem;
  background: var(--paper);
  padding: 0.75rem;
}

.blocked-slot-row span {
  color: var(--muted);
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
  color: var(--ink);
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
  border-bottom: 1px solid var(--line-solid);
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
  background: var(--ink);
  color: var(--surface);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: var(--nokvo-body);
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
  background: var(--ink);
  color: var(--surface);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: var(--nokvo-body);
  font-weight: 800;
}

.brand-copy {
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
  min-width: 0;
}

.brand-copy strong {
  font-family: var(--nokvo-body);
  font-size: 1.05rem;
  line-height: 1;
}

.brand-copy span {
  color: var(--muted);
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
  color: var(--ink);
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
  background: var(--ink);
  border-color: var(--ink);
  color: var(--surface);
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
  font-family: var(--nokvo-body);
  font-weight: 800;
}

.dashboard-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 1.5rem;
}

.dashboard-header h2 {
  font-family: var(--nokvo-body);
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
  border: 1px solid var(--ink);
  background: var(--ink);
  color: var(--surface);
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
  color: var(--muted);
  font-size: 0.72rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.summary-pill strong {
  font-family: var(--nokvo-body);
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
  font-family: var(--nokvo-body);
  font-size: 1.55rem;
  letter-spacing: -0.03em;
}

.dashboard-section-head p {
  max-width: 28rem;
  color: var(--muted);
  font-size: 0.94rem;
  line-height: 1.7;
  text-align: right;
}

.section-kicker {
  display: inline-block;
  margin-bottom: 0.35rem;
  color: var(--muted);
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
  font-family: var(--nokvo-body);
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
  color: var(--nokvo-green);
  box-shadow: inset 0 0 0 1px rgba(23, 63, 29, 0.18);
}

.member-action-icon.unavailable-icon {
  background: linear-gradient(145deg, rgba(193, 80, 56, 0.18), rgba(193, 80, 56, 0.05));
  color: #b1452a;
  box-shadow: inset 0 0 0 1px rgba(193, 80, 56, 0.22);
}

.member-action-title h4 {
  margin: 0 0 0.15rem;
  font-family: var(--nokvo-body);
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
  font-family: var(--nokvo-body);
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
  color: var(--nokvo-ink, var(--ink));
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
  background: var(--surface);
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
  color: var(--nokvo-ink, var(--ink));
  font-family: var(--nokvo-body);
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
  background: linear-gradient(180deg, #1f4a26, var(--nokvo-green));
  border-color: var(--nokvo-green);
  color: var(--surface);
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
  border: 1px solid var(--nokvo-green);
  background: linear-gradient(180deg, #1f4a26, var(--nokvo-green));
  color: var(--surface);
  font-family: var(--nokvo-body);
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
  font-family: var(--nokvo-body);
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
  background: var(--paper-2);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: var(--nokvo-body);
  font-weight: 800;
}

.organization-card h3,
.compact-card-head h3,
.members-card-head h3 {
  font-family: var(--nokvo-body);
  font-size: 1.2rem;
  line-height: 1.15;
}

.organization-card p,
.compact-card-head p,
.members-card-head p {
  margin-top: 0.18rem;
  color: var(--muted);
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
  background: var(--surface);
}

.health-ring strong {
  color: var(--ink);
  font-family: var(--nokvo-body);
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
  color: var(--ink);
  font-family: var(--nokvo-body);
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
  font-family: var(--nokvo-body);
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
  color: var(--muted);
  font-size: 0.78rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.usage-labels strong {
  color: var(--ink);
}

.usage-track {
  width: 100%;
  height: 0.45rem;
  overflow: hidden;
  border-radius: 999px;
  background: var(--line-solid);
}

.usage-fill {
  height: 100%;
  border-radius: 999px;
  background: var(--ink);
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
  color: var(--muted);
  font-size: 0.72rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-bottom: 0.25rem;
}

.organization-metrics strong,
.workspace-profile-grid strong {
  font-family: var(--nokvo-body);
  font-size: 1rem;
}

.dashboard-detail-list {
  margin-top: 1.2rem;
  display: grid;
  gap: 0.8rem;
}

.dashboard-detail-list dt {
  color: var(--muted);
  font-size: 0.76rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.dashboard-detail-list dd {
  margin-top: 0.22rem;
  font-family: var(--nokvo-body);
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

.agent-document-actions {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.ghost-button.compact.danger {
  color: #8a1f1f;
  border-color: rgba(220, 64, 64, 0.45);
}

.ghost-button.compact.danger:hover:not(:disabled) {
  background: rgba(220, 64, 64, 0.08);
}

.ghost-button.compact.danger:disabled {
  color: rgba(138, 31, 31, 0.5);
  border-color: rgba(220, 64, 64, 0.2);
  cursor: not-allowed;
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
  background: var(--paper-2);
  border: 1px solid rgba(196, 199, 199, 0.45);
}

.agent-document-main strong,
.agent-document-main small,
.agent-warning {
  display: block;
}

.agent-document-main strong {
  font-family: var(--nokvo-body);
  line-height: 1.3;
}

.agent-document-main small {
  margin-top: 0.24rem;
  color: var(--muted);
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
  font-family: var(--nokvo-body);
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
  font-family: var(--nokvo-body);
  display: block;
}

.custom-tab-row small {
  display: block;
  color: var(--muted);
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
  color: var(--muted);
}

.ghost-button.danger {
  color: #a13b2c;
  border-color: rgba(161, 59, 44, 0.4);
}

.outcome-wizard .mfa-head {
  margin-bottom: 1rem;
}

/* ── Real-estate onboarding wizard ─────────────────────────────────── */
.real-estate-wizard {
  max-width: 720px;
  width: 100%;
}

.real-estate-stepper {
  list-style: none;
  display: flex;
  gap: 0.55rem;
  padding: 0;
  margin: 0.4rem 0 1rem;
  flex-wrap: wrap;
}

.real-estate-stepper li {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.35rem 0.7rem;
  border: 1px solid rgba(95, 95, 83, 0.2);
  border-radius: 999px;
  font-size: 0.8rem;
  color: #6f6f60;
  background: rgba(255, 255, 255, 0.55);
}

.real-estate-stepper li.active {
  border-color: rgba(47, 122, 74, 0.7);
  background: rgba(47, 122, 74, 0.08);
  color: #2f6d3a;
}

.real-estate-stepper li.done {
  opacity: 0.7;
}

.re-step-number {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: rgba(47, 122, 74, 0.15);
  color: #2f6d3a;
  font-size: 0.7rem;
  font-weight: 700;
}

.re-wizard-step {
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
}

.re-project-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0.55rem;
}

.re-project-card {
  border: 1px solid rgba(95, 95, 83, 0.2);
  border-radius: 10px;
  padding: 0.6rem 0.75rem;
  background: rgba(255, 255, 255, 0.6);
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.re-project-card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
}

.re-project-card-actions {
  display: flex;
  gap: 0.3rem;
}

/* Onboarding wizard extras */
.onb-days { display: flex; flex-wrap: wrap; gap: 6px; }
.onb-day {
  padding: 6px 11px; border: 1px solid rgba(95, 95, 83, 0.3); border-radius: 999px;
  background: transparent; cursor: pointer; font-size: 12.5px;
}
.onb-day.is-on { background: #4f46e5; border-color: #4f46e5; color: #fff; font-weight: 600; }
.onb-check { display: flex; align-items: flex-start; gap: 8px; font-size: 13.5px; }
.onb-check input { margin-top: 3px; }
.onb-brochure { border: 1px dashed rgba(79, 70, 229, 0.4); border-radius: 10px; padding: 0.7rem; background: rgba(79, 70, 229, 0.05); }

.re-project-form {
  border: 1px dashed rgba(95, 95, 83, 0.25);
  border-radius: 10px;
  padding: 0.85rem;
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
  background: rgba(255, 255, 255, 0.4);
}

.re-project-form h4 {
  margin: 0 0 0.25rem;
  font-size: 0.95rem;
}

.re-form-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 0.55rem;
}

.re-form-row label,
.re-form-fullwidth {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-size: 0.8rem;
  color: var(--muted);
}

.re-form-row input,
.re-form-row select,
.re-form-fullwidth textarea,
.re-form-fullwidth input {
  border: 1px solid rgba(95, 95, 83, 0.25);
  border-radius: 8px;
  padding: 0.45rem 0.6rem;
  background: #fff;
  font: inherit;
  color: inherit;
}

.re-fields-block {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  padding: 0.75rem;
  border: 1px solid rgba(95, 95, 83, 0.2);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.45);
}

.re-fields-block h4 {
  margin: 0;
}

.re-field-row {
  display: grid;
  grid-template-columns: 2fr 1fr 1fr auto auto;
  gap: 0.35rem;
  align-items: center;
}

.re-field-row input,
.re-field-row select {
  border: 1px solid rgba(95, 95, 83, 0.25);
  border-radius: 8px;
  padding: 0.4rem 0.55rem;
  background: #fff;
  font: inherit;
}

.re-field-required {
  display: flex;
  align-items: center;
  gap: 0.3rem;
  font-size: 0.8rem;
}

.re-tool-groups {
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
  max-height: 360px;
  overflow-y: auto;
  padding-right: 0.25rem;
}

.re-tool-group h4 {
  margin: 0 0 0.4rem;
  font-size: 0.95rem;
}

.re-tool-row {
  display: flex;
  align-items: flex-start;
  gap: 0.55rem;
  padding: 0.55rem 0.7rem;
  border: 1px solid rgba(95, 95, 83, 0.2);
  border-radius: 8px;
  margin-bottom: 0.35rem;
  background: rgba(255, 255, 255, 0.55);
  cursor: pointer;
}

.re-tool-row.active {
  border-color: rgba(47, 122, 74, 0.6);
  background: rgba(47, 122, 74, 0.08);
}

.re-tool-row input {
  margin-top: 0.3rem;
}

.re-tool-row small {
  display: block;
  color: #6f6f60;
  font-size: 0.78rem;
}

.ghost-button.danger {
  border-color: rgba(161, 59, 44, 0.5);
  color: #a13b2c;
}

/* ── Projects sidebar page ─────────────────────────────────────────── */
.projects-section {
  margin-top: 0.75rem;
}

.projects-layout {
  display: grid;
  grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr);
  gap: 1rem;
}

@media (max-width: 960px) {
  .projects-layout {
    grid-template-columns: 1fr;
  }
}

.projects-form,
.projects-form .re-form-row {
  display: grid;
  gap: 0.55rem;
}

.projects-form {
  display: flex;
  flex-direction: column;
}

.projects-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  max-height: 520px;
  overflow-y: auto;
}

.projects-list-item {
  border: 1px solid rgba(95, 95, 83, 0.2);
  border-radius: 10px;
  padding: 0.6rem 0.75rem;
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 0.6rem;
  background: rgba(255, 255, 255, 0.6);
}

.projects-list-body {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}

.projects-list-actions {
  display: flex;
  gap: 0.3rem;
  flex-shrink: 0;
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
  font-family: var(--nokvo-body);
  font-size: 0.9rem;
  margin-bottom: 0.18rem;
}

.sample-mode-tab small {
  display: block;
  color: var(--muted);
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
  color: var(--muted);
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
  background: var(--surface);
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
  font-family: var(--nokvo-body);
  font-size: 0.88rem;
  margin-bottom: 0.15rem;
}

.nav-settings-item small {
  display: block;
  color: var(--muted);
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
  color: var(--muted);
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

.outgoing-connections-pinned {
  margin: 0.5rem 0 1.25rem;
}

.outbound-tester-form {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.7rem;
  margin-top: 0.6rem;
}

.outbound-tester-form .kb-field-wide {
  grid-column: 1 / -1;
}

.outbound-tester-form textarea {
  border: 1px solid rgba(27, 28, 21, 0.16);
  background: #fff;
  border-radius: 8px;
  padding: 0.55rem 0.7rem;
  font-size: 0.85rem;
  font-family: inherit;
  color: var(--ink);
  resize: vertical;
}

.outbound-tester-call {
  align-self: start;
}

.outbound-tester-campaign-row {
  margin-top: 1rem;
  padding-top: 1rem;
  border-top: 1px solid var(--line-soft);
}

.outbound-tester-campaign-row .kb-field {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.outbound-tester-campaign-row select {
  border: 1px solid var(--line-solid);
  background: var(--surface);
  border-radius: var(--r-sm);
  padding: 0.55rem 0.7rem;
  font-family: var(--nokvo-body);
  font-size: var(--fs-small);
  color: var(--ink);
  cursor: pointer;
}

.outbound-tester-doc {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.outbound-tester-hint {
  margin: 0;
  font-size: 0.78rem;
  color: var(--muted);
}

.outbound-tester-doc-row {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  flex-wrap: wrap;
}

.outbound-tester-doc-pick {
  cursor: pointer;
}

.outbound-tester-doc-pick input[type="file"] {
  display: none;
}

.outbound-tester-doc-name {
  margin: 0.2rem 0 0;
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.82rem;
  color: var(--ink);
}

.outbound-tester-doc-chars {
  color: var(--muted);
  font-size: 0.74rem;
}

.org-shell.dark .outbound-tester-hint,
.org-shell.dark .outbound-tester-doc-chars {
  color: rgba(244, 240, 225, 0.7);
}

.org-shell.dark .outbound-tester-doc-name {
  color: var(--surface);
}

.org-shell.dark .outbound-tester-form textarea {
  background: rgba(28, 29, 24, 0.6);
  border-color: rgba(244, 240, 225, 0.2);
  color: var(--surface);
}

.nokvo-form-builder {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.75rem;
  margin-top: 0.6rem;
}

.nokvo-form-builder .kb-field-wide {
  grid-column: 1 / -1;
}

.nokvo-form-fields {
  grid-column: 1 / -1;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  padding-top: 0.55rem;
  border-top: 1px dashed rgba(27, 28, 21, 0.16);
  margin-top: 0.25rem;
}

.nokvo-form-fields-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-family: var(--nokvo-body);
  font-size: 0.82rem;
  color: var(--muted);
  font-weight: 600;
  letter-spacing: 0.02em;
}

.nokvo-form-empty {
  margin: 0;
  font-size: 0.8rem;
  color: var(--muted);
}

.nokvo-form-field-row {
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) minmax(0, 0.9fr) auto auto;
  gap: 0.5rem;
  align-items: center;
}

.nokvo-form-field-label,
.nokvo-form-field-type {
  border: 1px solid rgba(27, 28, 21, 0.16);
  background: #fff;
  border-radius: 8px;
  padding: 0.5rem 0.65rem;
  font-size: 0.85rem;
  font-family: inherit;
  color: var(--ink);
}

.nokvo-form-field-required {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.8rem;
  color: var(--muted);
  white-space: nowrap;
}

.nokvo-form-field-remove {
  border: 1px solid rgba(220, 64, 64, 0.4);
  background: transparent;
  color: #8a1f1f;
  border-radius: 999px;
  width: 1.95rem;
  height: 1.95rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}

.nokvo-form-field-remove:hover {
  background: rgba(220, 64, 64, 0.08);
}

.nokvo-form-link-callout {
  margin-top: 0.85rem;
  background: rgba(27, 28, 21, 0.04);
  border: 1px solid rgba(27, 28, 21, 0.1);
  border-radius: 10px;
  padding: 0.7rem 0.85rem;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  align-items: flex-start;
}

.nokvo-form-link-callout code {
  font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.78rem;
  word-break: break-all;
  color: var(--ink);
}

.org-shell.dark .nokvo-form-field-label,
.org-shell.dark .nokvo-form-field-type {
  background: rgba(28, 29, 24, 0.6);
  border-color: rgba(244, 240, 225, 0.2);
  color: var(--surface);
}

.org-shell.dark .nokvo-form-fields,
.org-shell.dark .nokvo-form-link-callout {
  border-color: rgba(244, 240, 225, 0.18);
}

.org-shell.dark .nokvo-form-link-callout {
  background: rgba(28, 29, 24, 0.55);
}

.org-shell.dark .nokvo-form-link-callout code {
  color: var(--surface);
}

.org-shell.dark .nokvo-form-empty,
.org-shell.dark .nokvo-form-fields-head,
.org-shell.dark .nokvo-form-field-required {
  color: rgba(244, 240, 225, 0.72);
}

.outgoing-provider-stack {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 0.6rem;
  margin-top: 1rem;
}

@media (min-width: 960px) {
  .outgoing-provider-stack {
    grid-template-columns: repeat(4, 1fr);
  }
}

.provider-icon-tile {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.55rem;
  padding: 1.05rem 0.85rem;
  border-radius: 12px;
  border: 1px solid rgba(27, 28, 21, 0.12);
  background: rgba(255, 255, 255, 0.85);
  color: var(--ink);
  cursor: pointer;
  font-family: var(--nokvo-body);
  transition: background 0.15s ease, transform 0.15s ease, border-color 0.15s ease;
  text-align: center;
}

.provider-icon-tile:hover {
  background: var(--surface);
  border-color: rgba(27, 28, 21, 0.22);
  transform: translateY(-1px);
}

.provider-icon-tile svg {
  flex-shrink: 0;
}

.provider-icon-label {
  font-size: 0.9rem;
  font-weight: 600;
  letter-spacing: 0.01em;
}

.org-shell.dark .provider-icon-tile {
  background: rgba(28, 29, 24, 0.75);
  border-color: rgba(244, 240, 225, 0.18);
  color: var(--surface);
}

.org-shell.dark .provider-icon-tile:hover {
  background: rgba(28, 29, 24, 0.92);
  border-color: rgba(244, 240, 225, 0.28);
}

.outgoing-tabs button {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  border: 1px solid rgba(196, 199, 199, 0.6);
  background: rgba(255, 255, 255, 0.88);
  color: var(--ink);
  border-radius: 999px;
  padding: 0.6rem 0.85rem;
  font-size: 0.8rem;
  font-weight: 800;
  cursor: pointer;
}

.outgoing-tabs button.active {
  background: var(--ink);
  border-color: var(--ink);
  color: var(--surface);
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
  font-family: var(--nokvo-body);
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

/* Nokvo Prime aesthetic — warm editorial / organic, forest-green ink-on-paper */
.org-shell {
  --nokvo-ink: #151710;
  --nokvo-muted: #67685f;
  --nokvo-soft: #f6f4ea;
  --nokvo-panel: rgba(255, 254, 248, 0.88);
  --nokvo-panel-solid: var(--surface);
  --nokvo-line: rgba(26, 43, 23, 0.10);
  --nokvo-green: rgb(23, 63, 29);
  --nokvo-green-2: #23572a;
  --nokvo-green-soft: #e8f1e0;
  --nokvo-warn: #b66514;
  /* Restrained type system. Display = Fraunces (optical serif, used sparingly),
     body = Manrope (humanist sans), data = JetBrains Mono. */
  --nokvo-display: 'Fraunces', 'Playfair Display', Georgia, "Times New Roman", serif;
  --nokvo-body: 'Manrope', ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --nokvo-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;

  /* P0 consolidated design tokens. The component used to hardcode ~155 distinct
     hex colors, 20 radii and 49 box-shadows; these tokens collapse the most
     common ones to a single source of truth so the look is cohesive and a
     theme swap is one edit. New code MUST use these, not raw hex. */
  --ink: #1b1c15;
  --ink-soft: #2a2c25;
  --muted: #5f5f53;
  --muted-soft: #67685f;
  --paper: #fbfaf5;
  --paper-2: #f6f5ef;
  --surface: #fffef8;
  --surface-elevated: #fffef8;
  --line-solid: rgb(228, 227, 215);
  --line-soft: rgba(26, 43, 23, 0.06);
  --danger: #c0392b;
  --success: #2f6d3a;

  /* Canonical radius scale (was 20+ ad-hoc values). */
  --r-xs: 6px;
  --r-sm: 10px;
  --r-md: 14px;
  --r-lg: 20px;
  --r-pill: 999px;

  /* Canonical shadow scale (was 49 ad-hoc values). */
  --shadow-hair: 0 1px 2px rgba(21, 23, 16, 0.04);
  --shadow-card: 0 12px 28px -22px rgba(21, 23, 16, 0.18);
  --shadow-pop: 0 22px 50px -28px rgba(21, 23, 16, 0.28);
  --shadow-focus: 0 0 0 3px rgba(23, 63, 29, 0.18);

  /* Type scale — 6 sizes + 3 line-heights, used in place of one-off rems. */
  --fs-display: clamp(2.75rem, 5.4vw, 4.25rem);
  --fs-h2: 1.85rem;
  --fs-h3: 1.2rem;
  --fs-h4: 1rem;
  --fs-body: 0.95rem;
  --fs-small: 0.85rem;
  --fs-eyebrow: 0.72rem;
  --lh-tight: 1.1;
  --lh-snug: 1.3;
  --lh-body: 1.55;
  --ring: 0 0 0 3px rgba(23, 63, 29, 0.22);

  /* Minimalist surface — a single near-flat warm-paper wash. No radials, no
     grain, no orbs: restraint over atmosphere. Hierarchy is carried by
     whitespace, hairline rules and quiet typography. */
  background: linear-gradient(180deg, var(--paper) 0%, var(--paper-2) 100%);
  color: var(--nokvo-ink);
  font-family: var(--nokvo-body);
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
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
  background: rgba(255, 254, 248, 0.7);
  box-shadow: none;
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
  color: var(--surface);
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
  color: var(--surface);
  box-shadow: 0 14px 26px -20px rgba(23, 63, 29, 0.8);
}

.nav-page-button.active svg {
  color: var(--surface);
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

/* h1 / h2 — the only place the display serif speaks loudly. */
.dashboard-header h2,
.brand-block h1 {
  font-family: var(--nokvo-display);
  font-optical-sizing: auto;
  font-weight: 500;
  letter-spacing: -0.012em;
  line-height: var(--lh-tight);
}

/* h3 / h4 — restrained body voice, larger size. Manrope keeps cards calm. */
.dashboard-section-head h3,
.try-agent-card h3,
.organization-card h3,
.compact-card-head h3,
.members-card-head h3,
.health-score-card h4 {
  font-family: var(--nokvo-body);
  font-weight: 600;
  letter-spacing: -0.008em;
  line-height: var(--lh-snug);
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
  color: var(--muted);
  font-size: var(--fs-eyebrow);
  letter-spacing: 0.18em;
  font-weight: 700;
  text-transform: uppercase;
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
  background: var(--nokvo-green);
  color: var(--surface);
  box-shadow: none;
}

.dashboard-card,
.login-card,
.field-modal {
  border-radius: 1.15rem;
  border-color: var(--nokvo-line);
  background: var(--nokvo-panel);
  box-shadow: 0 1px 2px rgba(21, 23, 16, 0.03);
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
  color: var(--surface);
  font-family: var(--nokvo-display);
  font-optical-sizing: auto;
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
  color: var(--nokvo-green);
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
  color: var(--surface);
  box-shadow: 0 12px 24px -20px rgba(23, 63, 29, 0.78);
}

.provider-option.active .provider-name,
.provider-option.active small {
  color: var(--surface);
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
  font-family: var(--nokvo-body);
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
  color: var(--muted);
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
  font-family: var(--nokvo-body);
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
  font-family: var(--nokvo-body);
  font-size: 0.92rem;
  line-height: 1.2;
}

.provisioning-steps small {
  display: block;
  margin-top: 0.18rem;
  color: var(--muted);
  font-size: 0.78rem;
}

.step-state {
  font-size: 0.7rem;
  font-weight: 800;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
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
  background: var(--paper-2);
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
  color: var(--muted);
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.invite-domain-banner strong {
  display: block;
  font-family: var(--nokvo-body);
  font-size: 1.3rem;
  overflow-wrap: anywhere;
}

.invite-domain-banner small {
  display: block;
  margin-top: 0.45rem;
  color: var(--muted);
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
  border: 1px solid var(--line-solid);
  background: rgba(255, 255, 255, 0.8);
  color: var(--ink);
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
  color: var(--surface);
  padding: 0.95rem 1.2rem;
  font-weight: 800;
}

.invite-form button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.invite-helper {
  margin: 0;
  color: var(--muted);
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
  color: var(--nokvo-green);
  font-family: var(--nokvo-body);
  font-weight: 900;
}

.team-identity {
  min-width: 0;
  display: grid;
  gap: 0.15rem;
}

.team-identity strong {
  color: var(--nokvo-ink);
  font-family: var(--nokvo-body);
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
  color: var(--nokvo-green);
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
  font-family: var(--nokvo-body);
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
  font-family: var(--nokvo-body);
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
  font-family: var(--nokvo-body);
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
  color: var(--surface);
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.16);
}

.timetable-date-cell.active span {
  background: rgba(255, 254, 248, 0.18);
  color: var(--surface);
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
  font-family: var(--nokvo-body);
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
  font-family: var(--nokvo-body);
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
  font-family: var(--nokvo-body);
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
  font-family: var(--nokvo-body);
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
  font-family: var(--nokvo-body);
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
  font-family: var(--nokvo-body);
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
  color: var(--muted);
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
  color: var(--muted);
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
  color: var(--muted);
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
  color: var(--muted);
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
  color: var(--ink);
}

.empty-state {
  padding: 1rem 0;
  color: var(--muted);
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
  border-top: 1px solid var(--line-solid);
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
  /* Dark variants of the P0 tokens. Any token-driven code switches themes
     automatically; the older hand-written ``.org-shell.dark .foo`` overrides
     below remain authoritative for now (separate cleanup pass). */
  --ink: #f2f1e5;
  --ink-soft: #d1cfc1;
  --muted: #97968d;
  --muted-soft: #7d7c72;
  --paper: #131511;
  --paper-2: #1a1c17;
  --surface: #1f211b;
  --surface-elevated: #25271f;
  --line-solid: rgba(242, 241, 229, 0.10);
  --line-soft: rgba(242, 241, 229, 0.05);
  --shadow-hair: 0 1px 2px rgba(0, 0, 0, 0.5);
  --shadow-card: 0 12px 28px -22px rgba(0, 0, 0, 0.6);
  --shadow-pop: 0 22px 50px -28px rgba(0, 0, 0, 0.7);
  --ring: 0 0 0 3px rgba(232, 241, 224, 0.22);

  background: var(--paper);
  color: var(--ink);
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
  background: var(--surface);
  border-color: var(--surface);
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
  font-family: var(--nokvo-body);
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
  color: var(--muted);
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
  color: var(--muted);
}

.pipeline-chip strong {
  font-family: var(--nokvo-body);
  font-size: 0.85rem;
  line-height: 1.2;
  margin-top: 0.1rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pipeline-chip small {
  font-size: 0.7rem;
  color: var(--muted);
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

.campaign-head-actions {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
}

.campaign-create--prompt-first {
  background: rgba(15, 23, 42, 0.04);
  border-radius: 0.9rem;
  padding: 1rem 1rem 0.85rem;
  margin-bottom: 0.35rem;
}

.campaign-objective-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0.5rem;
  margin-top: 0.35rem;
}

.campaign-objective-tile {
  display: flex;
  align-items: flex-start;
  gap: 0.55rem;
  padding: 0.65rem 0.8rem;
  border: 1px solid var(--line-solid);
  border-radius: 0.65rem;
  background: var(--surface);
  cursor: pointer;
  transition: border-color 120ms ease, background 120ms ease;
}

.campaign-objective-tile:hover {
  border-color: var(--ink);
}

.campaign-objective-tile.active {
  border-color: var(--nokvo-green);
  background: rgba(5, 150, 105, 0.05);
}

.campaign-objective-tile input[type="checkbox"] {
  margin-top: 0.2rem;
}

.campaign-objective-tile strong {
  display: block;
  font-size: 0.88rem;
  line-height: 1.2;
}

.campaign-objective-tile small {
  display: block;
  margin-top: 0.2rem;
  color: var(--muted);
  font-size: 0.74rem;
  line-height: 1.35;
}

.campaign-objective-warning {
  display: block;
  margin-top: 0.45rem;
  color: #b88528;
  font-size: 0.78rem;
}

.campaign-card-list {
  display: flex;
  flex-direction: column;
  gap: 0.65rem;
}

.campaign-card-row {
  border: 1px solid var(--line-soft);
  border-radius: 0.9rem;
  background: var(--surface);
  transition: border-color 140ms ease, box-shadow 140ms ease, background 140ms ease;
  overflow: hidden;
}

.campaign-card-row.expanded {
  border-color: var(--ink);
  box-shadow: 0 8px 24px -16px rgba(15, 23, 42, 0.18);
}

.campaign-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 1rem;
  padding: 0.85rem 1rem;
  cursor: pointer;
}

.campaign-card-header:focus-visible {
  outline: 2px solid var(--ink);
  outline-offset: -2px;
}

.campaign-card-title {
  display: inline-flex;
  align-items: center;
  gap: 0.55rem;
  font-family: var(--nokvo-body);
}

.campaign-card-title strong {
  font-size: 0.98rem;
  font-weight: 700;
}

.campaign-card-meta {
  display: inline-flex;
  align-items: center;
  gap: 0.9rem;
  font-size: 0.78rem;
  color: var(--muted);
}

.campaign-card-meta strong {
  color: var(--ink);
  font-variant-numeric: tabular-nums;
}

.campaign-card-body {
  border-top: 1px solid var(--line-soft);
  padding: 0.95rem 1rem 1rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.campaign-card-section {
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
}

.campaign-card-section-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.5rem;
}

.campaign-card-section-head strong {
  font-size: 0.86rem;
  font-weight: 700;
}

.campaign-card-section-head small {
  font-size: 0.74rem;
  color: var(--muted);
}

.campaign-prompt-preview {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  background: rgba(15, 23, 42, 0.04);
  padding: 0.75rem 0.9rem;
  border-radius: 0.7rem;
  font-size: 0.85rem;
  color: var(--ink);
}

.campaign-prompt-block {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.campaign-prompt-block--row {
  flex-direction: row;
  flex-wrap: wrap;
  gap: 0.4rem;
}

.campaign-prompt-kicker {
  text-transform: uppercase;
  font-size: 0.65rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  color: var(--muted);
}

.campaign-prompt-block p {
  margin: 0;
  line-height: 1.5;
}

.campaign-prompt-block ul {
  margin: 0;
  padding-left: 1.1rem;
  line-height: 1.55;
}

.campaign-attached-leads {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.campaign-attached-leads li {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0.55rem 0.75rem;
  border-radius: 0.7rem;
  background: rgba(15, 23, 42, 0.03);
}

.campaign-attached-leads li strong {
  display: block;
  font-size: 0.88rem;
}

.campaign-attached-leads li small {
  font-size: 0.74rem;
  color: var(--muted);
}

.campaign-lead-picker {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 0.45rem;
  max-height: 260px;
  overflow-y: auto;
  padding-right: 0.25rem;
}

.campaign-lead-picker-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
  padding: 0.5rem 0.7rem;
  border: 1px solid var(--line-soft);
  border-radius: 0.65rem;
  cursor: pointer;
  background: var(--surface);
  transition: border-color 120ms ease, background 120ms ease;
}

.campaign-lead-picker-row:hover {
  border-color: var(--ink);
}

.campaign-lead-picker-row.active {
  border-color: var(--nokvo-green);
  background: rgba(5, 150, 105, 0.05);
}

.campaign-lead-picker-row strong {
  display: block;
  font-size: 0.85rem;
}

.campaign-lead-picker-row small {
  display: block;
  font-size: 0.72rem;
  color: var(--muted);
}

.campaign-card-section--actions {
  flex-direction: row;
  flex-wrap: wrap;
  gap: 0.5rem;
  padding-top: 0.4rem;
  border-top: 1px dashed var(--line-soft);
}

/* End-of-call outcome banner inside the Tester card. Colour-coded by the
   classifier's verdict so the operator can scan the result without reading. */
.outbound-tester-outcome {
  border-radius: 0.85rem;
  padding: 0.85rem 1rem;
  margin: 0.6rem 0;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
  border: 1px solid var(--line-solid);
  background: var(--surface);
}

.outbound-tester-outcome-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.8rem;
}

.outbound-tester-outcome-head strong {
  font-size: 0.95rem;
}

.outbound-tester-outcome p {
  margin: 0;
  font-size: 0.85rem;
  color: var(--ink);
  opacity: 0.85;
}

.outbound-tester-outcome--interested {
  background: rgba(5, 150, 105, 0.07);
  border-color: rgba(5, 150, 105, 0.4);
}

.outbound-tester-outcome--partial {
  background: rgba(184, 133, 40, 0.08);
  border-color: rgba(184, 133, 40, 0.35);
}

.outbound-tester-outcome--not_interested {
  background: rgba(15, 23, 42, 0.05);
  border-color: var(--line-solid);
  opacity: 0.9;
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
  color: var(--muted);
}

.voice-status-dot {
  width: 0.5rem;
  height: 0.5rem;
  border-radius: 999px;
  background: currentColor;
  opacity: 0.6;
}

.voice-status-idle { color: var(--muted); }
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
  color: var(--muted);
}

.voice-latency-row strong { color: var(--ink); font-variant-numeric: tabular-nums; }
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
  color: var(--ink);
  font-size: 0.9rem;
  line-height: 1.55;
}

.voice-turn-meta {
  grid-column: 2 / -1;
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  font-size: 0.72rem;
  color: var(--muted);
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
  color: var(--muted);
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
  font-family: var(--nokvo-body);
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
  background: var(--paper-2);
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
  color: var(--ink);
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
  color: var(--muted);
}

.kb-stat-value {
  font-family: var(--nokvo-body);
  font-size: 1.85rem;
  line-height: 1;
  margin-top: 0.4rem;
  color: var(--ink);
  font-variant-numeric: tabular-nums;
}

.kb-stat-meta {
  margin-top: 0.55rem;
  color: var(--muted);
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
  background: var(--paper-2);
  border: 1px solid rgba(196, 199, 199, 0.55);
  color: var(--ink);
}

.kb-card-icon-primary {
  background: #1d1c0f;
  color: var(--surface);
  border-color: #1d1c0f;
}

.kb-card-head h4 {
  font-family: var(--nokvo-body);
  font-size: 1.05rem;
  line-height: 1.2;
  margin: 0;
}

.kb-card-head p {
  margin: 0.18rem 0 0;
  color: var(--muted);
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
  background: var(--surface);
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
  color: var(--ink);
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
  color: var(--ink);
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
  font-family: var(--nokvo-body);
  font-size: 0.98rem;
}

.kb-dropzone-empty span {
  color: var(--muted);
  font-size: 0.8rem;
}

.kb-dropzone-icon {
  width: 2.6rem;
  height: 2.6rem;
  border-radius: 0.85rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--surface);
  border: 1px solid rgba(196, 199, 199, 0.55);
  color: var(--ink);
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
  font-family: var(--nokvo-body);
  font-size: 0.95rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kb-dropzone-file span {
  color: var(--muted);
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
  color: var(--muted);
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
  color: var(--ink);
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

.kb-single-tradeoff {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0.55rem;
  margin: 0.35rem 0 0.8rem;
}

.kb-single-tradeoff-tile {
  border: 1px solid var(--line-soft);
  border-radius: 0.7rem;
  padding: 0.65rem 0.8rem;
  background: rgba(15, 23, 42, 0.025);
}

.kb-single-tradeoff-tile strong {
  display: block;
  font-size: 0.85rem;
  margin-bottom: 0.35rem;
}

.kb-single-tradeoff-tile ul {
  margin: 0;
  padding-left: 1.05rem;
  color: var(--muted);
  font-size: 0.78rem;
  line-height: 1.45;
}

.kb-single-tradeoff-tile li {
  margin-bottom: 0.18rem;
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
  color: var(--ink);
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
  font-family: var(--nokvo-body);
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
  color: var(--muted);
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
  color: var(--muted);
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
  font-family: var(--nokvo-body);
  font-weight: 800;
  font-size: 0.78rem;
  color: var(--muted);
}

.kb-result-head strong {
  flex: 1;
  font-family: var(--nokvo-body);
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
  color: var(--muted);
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
  font-family: var(--nokvo-body);
  font-size: 1.25rem;
  line-height: 1.2;
  margin: 0.3rem 0 0.2rem;
}

.kb-list-head p {
  margin: 0;
  color: var(--muted);
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
  background: var(--paper-2);
  color: var(--ink);
  border: 1px solid rgba(196, 199, 199, 0.55);
  margin-bottom: 0.5rem;
}

.kb-empty strong {
  font-family: var(--nokvo-body);
  font-size: 1rem;
}

.kb-empty span {
  color: var(--muted);
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
  background: var(--paper-2);
  border: 1px solid rgba(196, 199, 199, 0.55);
  color: var(--ink);
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
  font-family: var(--nokvo-body);
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
  color: var(--muted);
  font-size: 0.78rem;
}

.kb-doc-meta strong {
  color: var(--ink);
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
  color: var(--ink);
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

.cost-grid {
  grid-template-columns: 1fr;
}

/* Notifications bell + dropdown panel. The panel anchors below the bell
   inside the topbar; it's a fixed-width column with a scrollable list
   so a busy inbox doesn't push the page layout around. */
.notif-wrap {
  position: relative;
}
.notif-button {
  position: relative;
}
.notif-button.has-unread {
  color: #ef4444;
}
.notif-badge {
  position: absolute;
  top: -4px;
  right: -4px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 999px;
  background: #ef4444;
  color: #fff;
  font-size: 10px;
  line-height: 16px;
  font-weight: 700;
  text-align: center;
  box-shadow: 0 0 0 2px var(--surface-page, #fff);
}
.notif-panel {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  width: 360px;
  max-width: 92vw;
  max-height: 480px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  background: var(--surface-elevated, #fff);
  border: 1px solid var(--surface-border, rgba(15, 23, 42, 0.08));
  border-radius: 14px;
  box-shadow: 0 18px 48px rgba(15, 23, 42, 0.18);
  z-index: 50;
}
.notif-panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 14px;
  border-bottom: 1px solid var(--surface-border, rgba(15, 23, 42, 0.06));
}
.notif-mark-all {
  background: none;
  border: none;
  color: var(--accent, #4f46e5);
  font-size: 12px;
  cursor: pointer;
  padding: 4px 6px;
  border-radius: 6px;
}
.notif-mark-all:hover {
  background: var(--surface-muted, rgba(99, 102, 241, 0.08));
}
.notif-empty {
  padding: 24px 18px;
  margin: 0;
  font-size: 13px;
  color: var(--text-muted, #6b7280);
}
.notif-list {
  list-style: none;
  padding: 0;
  margin: 0;
  overflow-y: auto;
  max-height: 420px;
}
.notif-row {
  display: flex;
  gap: 10px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--surface-border, rgba(15, 23, 42, 0.04));
  cursor: pointer;
  transition: background 0.15s ease;
}
.notif-row:hover {
  background: var(--surface-muted, rgba(99, 102, 241, 0.04));
}
.notif-row.unread {
  background: var(--surface-muted, rgba(99, 102, 241, 0.05));
}
.notif-row:last-child {
  border-bottom: none;
}
.notif-dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  flex-shrink: 0;
  margin-top: 6px;
  background: #9ca3af;
}
.notif-dot.severity-p1 {
  background: #ef4444;
  box-shadow: 0 0 0 3px rgba(239, 68, 68, 0.15);
}
.notif-dot.severity-p2 {
  background: #f59e0b;
}
.notif-dot.severity-p3 {
  background: #9ca3af;
}
.notif-row-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}
.notif-row-body strong {
  font-size: 13px;
  color: var(--text-primary, #111827);
  word-wrap: break-word;
}
.notif-row-body small {
  font-size: 11px;
  color: var(--text-muted, #6b7280);
  word-wrap: break-word;
}
.notif-row-time {
  margin-top: 4px;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

/* Member self-service calendar.
   Mirrors the admin's timetable-calendar layout but anchored on the
   member's own /me/timetable payload — working-day highlight on dates
   that match assignment.working_days, block-count badges for one-off
   buffers/unavailable slots, and a side panel showing the selected
   date's blocks with inline remove. */
.my-calendar-shell {
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr);
  gap: 1.5rem;
  margin-bottom: 1.75rem;
}
.my-calendar-card .timetable-date-cell.working-day:not(.muted) {
  background: rgba(99, 102, 241, 0.06);
  border-color: rgba(99, 102, 241, 0.18);
}
.my-calendar-working-hint {
  color: rgba(79, 70, 229, 0.8);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.my-calendar-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
  padding: 12px 14px 0;
  font-size: 11px;
  color: var(--text-muted, #6b7280);
}
.my-calendar-legend .legend-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 999px;
  margin-right: 6px;
  vertical-align: -1px;
}
.my-calendar-legend .legend-dot.working {
  background: rgba(99, 102, 241, 0.6);
}
.my-calendar-legend .legend-dot.visit {
  background: #f59e0b;
}
.my-calendar-legend .legend-dot.blocked {
  background: #ef4444;
}
.my-calendar-legend .legend-dot.today {
  background: #10b981;
}
/* Date cells with a customer visit get an amber border so they stand
   out at a glance even before the user clicks them. */
.my-calendar-card .timetable-date-cell.has-visit:not(.muted) {
  border-color: rgba(245, 158, 11, 0.55);
  box-shadow: inset 0 0 0 1px rgba(245, 158, 11, 0.18);
}
.my-calendar-queue {
  margin-top: 1.25rem;
}
.my-calendar-queue-list {
  margin-top: 12px;
}

/* Field schema cards (Site Visits / Leads tabs). Quiet beige tone
   matching .provider-option — neutral border, no gradients, no badges.
   A small leading icon hints at the type; required is shown as muted
   italic text after the type label. */
.field-schema-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 0.7rem;
  margin-top: 0.85rem;
}
.field-schema-tile {
  display: flex;
  align-items: center;
  gap: 0.7rem;
  padding: 0.75rem 0.9rem;
  border-radius: 0.8rem;
  background: rgba(255, 255, 255, 0.72);
  border: 1px solid var(--line-solid);
  transition: border-color 0.15s ease;
}
.field-schema-tile:hover {
  border-color: #8a8f86;
}
.field-schema-icon {
  flex-shrink: 0;
  width: 26px;
  height: 26px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 0.55rem;
  background: var(--paper-2);
  color: var(--muted);
}
.field-schema-body {
  display: flex;
  flex-direction: column;
  gap: 0.18rem;
  min-width: 0;
  flex: 1;
}
.field-schema-body strong {
  font-size: 0.92rem;
  font-weight: 600;
  color: var(--ink);
  word-break: break-word;
}
.field-schema-meta {
  font-size: 0.78rem;
  color: var(--muted);
}
.field-schema-meta em {
  font-style: normal;
  color: #8a8f86;
}

/* Dark-mode parity. */
.org-shell.dark .field-schema-tile {
  background: rgba(255, 255, 255, 0.04);
  border-color: rgba(255, 255, 255, 0.1);
}
.org-shell.dark .field-schema-tile:hover {
  border-color: rgba(255, 255, 255, 0.22);
}
.org-shell.dark .field-schema-body strong {
  color: #f3f4f6;
}
.org-shell.dark .field-schema-icon {
  background: rgba(255, 255, 255, 0.08);
  color: #d1d5db;
}
.org-shell.dark .field-schema-meta {
  color: #9ca3af;
}
.org-shell.dark .field-schema-meta em {
  color: #6b7280;
}

/* Live in-call cost meter, displayed next to the voice-status badge
   on both the inbound and outbound testers. Ticks once per second
   while the call is active; the ledger row on the server uses the
   same Decimal math so the dashboard tile reconciles exactly when
   the call ends. */
.voice-cost-meter {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  padding: 0.35rem 0.75rem;
  border-radius: 999px;
  border: 1px solid var(--line-solid);
  background: rgba(255, 255, 255, 0.72);
  color: var(--ink);
  font-size: 0.82rem;
  font-variant-numeric: tabular-nums;
}
.voice-cost-meter strong {
  font-weight: 700;
}
.voice-cost-meter span {
  color: var(--muted);
  font-size: 0.74rem;
}
.org-shell.dark .voice-cost-meter {
  background: rgba(255, 255, 255, 0.05);
  border-color: rgba(255, 255, 255, 0.12);
  color: #f3f4f6;
}
.org-shell.dark .voice-cost-meter span {
  color: #9ca3af;
}

/* ──────────────────────────────────────────────────────────────────
   Comprehensive mobile pass. The earlier breakpoints handle isolated
   pieces; this block makes sure every section that landed after the
   initial responsive work also behaves on a phone.
   ────────────────────────────────────────────────────────────────── */

/* Tablet: one big content column, calendar splits collapse. */
@media (max-width: 920px) {
  .dashboard-layout {
    width: 100%;
    margin-left: 0;
    padding-left: 5.7rem;
  }
  .timetable-calendar-shell,
  .my-calendar-shell {
    grid-template-columns: 1fr;
  }
  .org-health-grid {
    grid-template-columns: 1fr;
  }
  .org-health-grid .organization-card {
    grid-column: 1 / -1;
  }
  .timetable-summary-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

/* Phone breakpoint. Single column everywhere, larger tap targets,
   bottom-sheet feel for modals, and a more compact top bar so the
   notif + settings + logout buttons all fit. */
@media (max-width: 720px) {
  /* Use the full viewport width and trim padding so cards aren't
     letterboxed inside narrow gutters. */
  .login-layout,
  .workspace-layout {
    padding: 1rem 0.85rem 2rem;
  }
  .dashboard-layout {
    padding-left: 5.4rem;
    padding-right: 0.75rem;
    padding-top: 0.85rem;
    gap: 1.25rem;
  }
  .dashboard-section {
    gap: 1rem;
  }
  .dashboard-card {
    padding: 1rem;
    border-radius: 1rem;
  }

  /* Top mode bar — wrap and shrink so notif + settings + logout fit
     side-by-side on a narrow phone without overflowing. */
  .mode-bar {
    flex-wrap: wrap;
    padding: 0.85rem 0.85rem 0;
    gap: 0.4rem;
  }
  .mode-link {
    padding: 0.55rem 0.75rem;
    font-size: 0.78rem;
  }
  .mode-link--icon {
    width: 2.1rem;
    height: 2.1rem;
    padding: 0.4rem;
  }
  .mode-settings-menu,
  .nav-settings-menu {
    width: min(320px, calc(100vw - 1.5rem));
    right: 0;
  }

  /* Notifications dropdown — anchor to the screen edge so the panel
     can't be clipped by the narrow viewport. */
  .notif-panel {
    width: min(360px, calc(100vw - 1.5rem));
    right: -0.5rem;
    max-height: 70vh;
  }
  .notif-list {
    max-height: 60vh;
  }

  /* Section headers stack and de-emphasize on phone. */
  .dashboard-section-head {
    flex-direction: column;
    align-items: flex-start;
    gap: 0.45rem;
  }
  .dashboard-section-head h3 {
    font-size: 1.1rem;
  }

  /* Every grid I added recently → 1 column. */
  .dashboard-grid,
  .overview-grid,
  .control-grid,
  .member-timetable-grid,
  .cost-grid,
  .cost-stats,
  .org-health-grid {
    grid-template-columns: 1fr !important;
    gap: 0.8rem;
  }
  .field-schema-grid {
    grid-template-columns: 1fr;
  }

  /* Stat tiles — comfy height + tabular font even when stacked. */
  .cost-stat-value {
    font-size: 20px;
  }
  .cost-stat {
    padding: 14px 16px;
  }

  /* Member calendar — give the date cells a bigger tap area, and
     stack the legend rows. */
  .timetable-month-grid {
    gap: 2px;
  }
  .timetable-date-cell {
    min-height: 3.2rem;
    padding: 0.4rem 0.35rem;
    font-size: 0.85rem;
  }
  .timetable-date-cell small {
    font-size: 9px;
  }
  .my-calendar-legend {
    flex-direction: column;
    gap: 6px;
  }
  .my-calendar-card .timetable-calendar-head {
    padding: 0.55rem 0.75rem;
  }

  /* Dashboard header strip — title + context pill on their own rows. */
  .dashboard-header {
    flex-direction: column;
    align-items: flex-start;
    gap: 0.6rem;
  }
  .dashboard-header-actions {
    flex-wrap: wrap;
  }

  /* Modals — use the full screen as a bottom sheet so the long forms
     remain reachable. */
  .field-modal-shell {
    padding: 0;
    align-items: flex-end;
  }
  .field-modal,
  .timetable-modal {
    width: 100%;
    max-height: 92vh;
    border-radius: 1.2rem 1.2rem 0 0;
    padding: 1.1rem 1rem 1.4rem;
  }
  .timetable-summary-row {
    grid-template-columns: 1fr;
  }
  .field-modal-actions {
    flex-direction: column;
    gap: 0.5rem;
  }
  .field-modal-actions > * {
    width: 100%;
  }

  /* Record + member tables → card rows. The desktop layouts use a
     grid template column scheme that turns into illegible slivers on
     a phone; switching to a flex column gives each cell breathing
     room and clear hierarchy. */
  .member-table .member-head {
    display: none;
  }
  .member-row {
    grid-template-columns: 1fr !important;
    gap: 0.5rem;
    padding: 0.9rem 0.95rem;
  }
  .tab-record-row {
    flex-direction: column;
    align-items: flex-start;
    gap: 0.6rem;
  }
  .tab-record-meta,
  .blocked-slot-row,
  .assignment-summary-cell {
    grid-template-columns: 1fr !important;
    align-items: flex-start !important;
    gap: 0.5rem;
  }

  /* Form fields — minimum 44px height for comfortable tapping. */
  input[type="text"],
  input[type="email"],
  input[type="password"],
  input[type="tel"],
  input[type="number"],
  input[type="search"],
  input[type="datetime-local"],
  input[type="date"],
  input[type="time"],
  select,
  textarea,
  .member-field-input {
    font-size: 16px; /* prevents iOS zoom-on-focus */
    min-height: 2.75rem;
  }
  textarea {
    min-height: 5rem;
  }

  /* Buttons — large enough to tap, full width on action rows. */
  .ghost-button,
  .primary-button,
  .danger-button,
  .dashboard-inline-button,
  .member-action-submit {
    min-height: 2.6rem;
  }
  .member-action-submit,
  .field-card-actions .ghost-button {
    width: 100%;
  }

  /* Cost-card refresh button moves to its own row when stacked. */
  .cost-refresh-button {
    width: 100%;
    margin-left: 0;
    margin-top: 0.6rem;
    justify-content: center;
  }
  .cost-card .compact-card-head {
    flex-wrap: wrap;
  }

  /* Members card head + similar headers — wrap action chips so the
     count + refresh button don't overflow. */
  .members-card-head,
  .field-card-actions {
    flex-direction: column;
    align-items: flex-start;
    gap: 0.55rem;
  }
  .field-card-actions {
    width: 100%;
  }

  /* Field-schema tiles: tighter padding on phone. */
  .field-schema-tile {
    padding: 0.65rem 0.75rem;
  }
}

/* Extra-narrow phones (≤380px) — drop a few padding rungs further and
   shrink the always-visible sidebar so the page isn't a sliver. */
@media (max-width: 380px) {
  .dashboard-layout {
    padding-left: 4.6rem;
  }
  .floating-top-nav {
    width: 4.4rem;
    padding: 0.5rem;
  }
  .nav-page-button,
  .theme-toggle-button,
  .dashboard-nav-actions .nav-icon-button,
  .dashboard-nav-actions .org-avatar-button {
    width: 2.4rem;
    height: 2.4rem;
  }
  .dashboard-card {
    padding: 0.85rem;
  }
}
.my-calendar-selected {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.my-calendar-hours {
  margin: 0;
}
.my-calendar-empty {
  margin-top: 8px;
}

@media (max-width: 920px) {
  .my-calendar-shell {
    grid-template-columns: 1fr;
  }
}
/* Org Health is now a standalone page; the org card should stretch to
   fill the row rather than be stuck at the 2-of-3 overview-grid span. */
.org-health-grid {
  grid-template-columns: 1fr;
}
.org-health-grid .organization-card {
  grid-column: 1 / -1;
}
/* ── Call cost ledger ─────────────────────────────────────────────────────
   Token-driven Nokvo Prime rebuild. Rupee totals carry editorial weight in
   Fraunces; minutes and counts stay restrained (Manrope tabular). Recent
   calls render as an editorial list, not boxed tiles. Replaces the older
   indigo-fallback styling (--surface-muted / --surface-border / --text-*)
   that clashed against the warm-paper Prime aesthetic. */

.cost-card {
  position: relative;
  padding: 1.5rem 1.65rem;
}

.cost-card .compact-card-head {
  align-items: flex-start;
  padding-bottom: 1.15rem;
  border-bottom: 1px solid var(--line-soft);
  margin-bottom: 1.15rem;
}

.cost-card .compact-card-head h3 {
  font-size: 1.05rem;
  font-weight: 600;
  letter-spacing: -0.005em;
  color: var(--ink);
  margin: 0;
}

.cost-card .compact-card-head > div:nth-child(2) p {
  margin: 0.2rem 0 0;
  color: var(--muted);
  font-size: var(--fs-small);
  font-variant-numeric: tabular-nums;
}

.cost-refresh-button {
  margin-left: auto;
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.42rem 0.85rem;
  border-radius: var(--r-pill);
  border: 1px solid var(--line-solid);
  background: transparent;
  color: var(--muted);
  font-family: var(--nokvo-body);
  font-size: var(--fs-eyebrow);
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  cursor: pointer;
  transition: color 140ms ease, border-color 140ms ease, background 140ms ease;
}
.cost-refresh-button:hover:not(:disabled) {
  color: var(--ink);
  border-color: var(--ink);
  background: var(--paper);
}
.cost-refresh-button:disabled {
  opacity: 0.5;
  cursor: progress;
}

.cost-stats {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  column-gap: 1.6rem;
  row-gap: 1.2rem;
  margin: 0 0 1.25rem;
}

.cost-stat {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  padding: 0;
  background: transparent;
  border: 0;
  border-radius: 0;
  position: relative;
}

/* Vertical hairline between the two columns — disappears when stacked. */
.cost-stat:nth-child(odd)::after {
  content: "";
  position: absolute;
  right: -0.8rem;
  top: 0.25rem;
  bottom: 0.25rem;
  width: 1px;
  background: var(--line-soft);
}

.cost-stat-label {
  font-family: var(--nokvo-body);
  font-size: var(--fs-eyebrow);
  letter-spacing: 0.16em;
  text-transform: uppercase;
  font-weight: 700;
  color: var(--muted);
}

.cost-stat-value {
  font-family: var(--nokvo-display);
  font-optical-sizing: auto;
  font-weight: 500;
  font-size: clamp(1.85rem, 3.6vw, 2.55rem);
  line-height: 1.05;
  color: var(--ink);
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.012em;
}

/* Minutes tiles speak in the body voice — quieter, secondary metric. */
.cost-stat-minutes .cost-stat-value {
  font-family: var(--nokvo-body);
  font-weight: 600;
  font-size: 1.35rem;
  letter-spacing: -0.004em;
}

.cost-stat-meta {
  font-family: var(--nokvo-body);
  font-size: var(--fs-small);
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}

.cost-recent {
  margin-top: 0.4rem;
  padding-top: 1.1rem;
  border-top: 1px solid var(--line-soft);
}

.cost-recent-head {
  margin-bottom: 0.55rem;
}

.cost-recent-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 0;
  max-height: 264px;
  overflow-y: auto;
  scrollbar-width: thin;
}

.cost-recent-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 1rem;
  padding: 0.65rem 0;
  border-bottom: 1px solid var(--line-soft);
  transition: background 140ms ease;
}
.cost-recent-row:last-child {
  border-bottom: 0;
}
.cost-recent-row:hover {
  background: var(--paper);
}

.cost-recent-row strong {
  font-family: var(--nokvo-display);
  font-optical-sizing: auto;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  font-size: 1rem;
  color: var(--ink);
  letter-spacing: -0.005em;
}

.cost-recent-row small {
  display: block;
  margin-top: 0.12rem;
  font-family: var(--nokvo-mono);
  font-size: 0.72rem;
  color: var(--muted);
  letter-spacing: 0.04em;
}

.cost-recent-time {
  font-family: var(--nokvo-mono);
  font-size: 0.72rem;
  color: var(--muted);
  white-space: nowrap;
  letter-spacing: 0.02em;
}

.cost-empty {
  margin-top: 0.4rem;
  padding: 1rem 0 0.25rem;
  border-top: 1px solid var(--line-soft);
  font-family: var(--nokvo-body);
  font-size: var(--fs-small);
  color: var(--muted);
  font-style: italic;
}

@media (max-width: 720px) {
  .cost-card {
    padding: 1.25rem 1.25rem;
  }
  .cost-stats {
    grid-template-columns: 1fr;
    row-gap: 1rem;
  }
  .cost-stat:nth-child(odd)::after {
    display: none;
  }
  .cost-stat-value {
    font-size: 1.85rem;
  }
  .cost-stat-minutes .cost-stat-value {
    font-size: 1.2rem;
  }
}

/* Inbound Agent — inline rename in the hero */
.agent-rename-row {
  display: flex;
  gap: 10px;
  align-items: center;
}
.agent-rename-row .db-input {
  flex: 1 1 auto;
}
.agent-hero-rename {
  margin-top: 14px;
  max-width: 420px;
}
@media (max-width: 540px) {
  .agent-rename-row {
    flex-direction: column;
    align-items: stretch;
  }
}

/* ── Lead campaign tabs ───────────────────────────────────────────────────
   One tab per outbound campaign with leads, plus an "All Leads" default.
   Lives inside the Leads card, above the records list. */
.lead-campaign-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin: 0.85rem 0 0.95rem;
  padding-bottom: 0.85rem;
  border-bottom: 1px solid var(--line-soft);
}

.lead-campaign-tab {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.42rem 0.85rem 0.42rem 0.95rem;
  border: 1px solid var(--line-solid);
  background: transparent;
  border-radius: var(--r-pill);
  color: var(--muted);
  font-family: var(--nokvo-body);
  font-size: var(--fs-small);
  font-weight: 600;
  letter-spacing: 0.005em;
  cursor: pointer;
  transition: color 140ms ease, border-color 140ms ease, background 140ms ease;
}

.lead-campaign-tab:hover {
  color: var(--ink);
  border-color: var(--ink);
}

.lead-campaign-tab.active {
  color: var(--surface);
  background: var(--nokvo-green);
  border-color: var(--nokvo-green);
}

.lead-campaign-tab-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 1.55rem;
  padding: 0 0.35rem;
  height: 1.25rem;
  font-family: var(--nokvo-mono);
  font-size: 0.7rem;
  font-weight: 600;
  border-radius: var(--r-pill);
  background: var(--paper-2);
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}

.lead-campaign-tab.active .lead-campaign-tab-count {
  background: rgba(255, 254, 248, 0.18);
  color: var(--surface);
}

/* Uncategorized tab — distinct from regular campaign tabs because it
   represents records the tester couldn't fit anywhere else. Dashed border
   + amber accent so it reads as "needs triage" without being alarming. */
.lead-campaign-tab--uncategorized {
  border-style: dashed;
  color: var(--ink);
  opacity: 0.75;
}

.lead-campaign-tab--uncategorized:hover {
  opacity: 1;
  border-color: var(--ink);
}

.lead-campaign-tab--uncategorized.active {
  background: #b88528;
  border-color: #b88528;
  border-style: solid;
  color: #fffbf0;
  opacity: 1;
}

/* P5 — beautification primitives.
   Universal keyboard focus ring (token-driven, drops the ad-hoc rings) and a
   reduce-motion guard so the connect-screen gradient drift + char reveal don't
   harass anyone who's asked the OS to slow down. */
.org-shell button:focus-visible,
.org-shell a:focus-visible,
.org-shell [role="button"]:focus-visible,
.org-shell input:focus-visible,
.org-shell select:focus-visible,
.org-shell textarea:focus-visible {
  outline: none;
  box-shadow: var(--ring);
  border-radius: var(--r-sm);
}

@media (prefers-reduced-motion: reduce) {
  .org-shell *,
  .org-shell *::before,
  .org-shell *::after {
    animation-duration: 0.001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.001ms !important;
  }
}
</style>
