<script setup>
// Bulk Lead Capturing — a dedicated page for the bulk-CSV outbound add-on.
// Three sub-tabs: Campaign (upload + bulk campaign list), Qualified Leads
// (lead records the bulk calls produced), Call Logs (each dialed contact +
// its transcript). Reuses existing dashboard state/endpoints via
// useDashboardState — no new backend for v1.
import { computed, ref, onMounted, watch } from 'vue';
import { useRouter } from 'vue-router';
import {
  PhoneCall,
  Users,
  ScrollText,
  Download,
  Clock,
  RefreshCw,
  ChevronDown,
  ChevronUp,
  ArrowLeft,
} from 'lucide-vue-next';
import { useDashboardState } from '../composables/useDashboardState.js';

const router = useRouter();

const {
  isAdmin,
  // Bulk CSV calling
  bulkCalling,
  bulkRequestForm,
  bulkCampaignForm,
  isSubmittingBulkRequest,
  submitBulkCallingRequest,
  isLaunchingBulkCampaign,
  onBulkCampaignFilePick,
  startBulkCallingCampaign,
  isRerunningBulkCampaign,
  rerunBulkCampaign,
  bulkCallingError,
  bulkCallingNotice,
  bulkDialFailures,
  bulkDndDropped,
  voiceLanguageOptions,
  // Campaigns
  campaigns,
  loadCampaigns,
  expandedCampaignId,
  toggleCampaignExpansion,
  // Formatters
  phoneHref,
  formatRelativeDate,
  // Transcripts
  loadTranscript,
  downloadTranscript,
} = useDashboardState();

const TABS = [
  { id: 'campaign', label: 'Campaign', icon: PhoneCall },
  { id: 'leads', label: 'Qualified Leads', icon: Users },
  { id: 'logs', label: 'Call Logs', icon: ScrollText },
];
const bulkTab = ref('campaign');

onMounted(() => {
  if (typeof loadCampaigns === 'function') loadCampaigns();
});

// Only the bulk-CSV campaigns (agent_config.bulk_csv === true).
const bulkCampaigns = computed(() =>
  (campaigns?.value || []).filter((c) => c?.agent_config?.bulk_csv),
);

// Local copies of helpers that live (scoped) inside OutgoingAgentView.
function campaignStatusTone(s) {
  if (s === 'running') return 'n-tag--success';
  if (s === 'draft') return 'n-tag--brand';
  return 'n-tag--danger';
}
function bulkRedialCount(c) {
  return (c.contacts || []).filter((ct) => ct.status !== 'answered' && !ct.answered_at).length;
}

function goBack() {
  router.push({ name: 'dash-campaigns' });
}

// ── Qualified Leads — contacts the agent judged 'interested' on the call ──
// Sourced from each bulk campaign's contacts[].interest_outcome, which the
// post-call classifier stamps at WS teardown (surfaced via GET /campaigns).
// ── Questionnaire builder (campaign create form) ──────────────────────────
function makeId(prefix) {
  return (
    (typeof crypto !== 'undefined' && crypto.randomUUID && crypto.randomUUID()) ||
    (prefix + Date.now().toString(36) + Math.random().toString(16).slice(2, 6))
  );
}
function addQuestion() {
  bulkCampaignForm.value.questions.push({
    id: makeId('q'), type: 'intent', text: '', desired_answer: '',
    required: 'yes', gate: false, points: 1, graded: false, tiers: [],
  });
  if (!bulkCampaignForm.value.threshold) bulkCampaignForm.value.threshold = 1;
}
function removeQuestion(i) {
  bulkCampaignForm.value.questions.splice(i, 1);
}
// Graded scoring bands (answer questions): the post-call scorer awards the
// best-matching band's points; the question's flat `points` weight is ignored.
function addTier(q) {
  if (!Array.isArray(q.tiers)) q.tiers = [];
  q.tiers.push({ id: makeId('t'), label: '', points: 10 });
}
function removeTier(q, ti) {
  q.tiers.splice(ti, 1);
}
function onGradedToggle(q) {
  if (q.graded && !(q.tiers && q.tiers.length)) addTier(q);
}
// Best achievable lead score — mirrors backend questionnaire_max_points so the
// threshold cap + hint match what the scorer will use.
const maxPoints = computed(() =>
  (bulkCampaignForm.value.questions || []).reduce((sum, q) => {
    if (q.type === 'answer' && q.graded && (q.tiers || []).length) {
      const pts = q.tiers.map((t) => Math.max(1, Number(t.points) || 1));
      return sum + (pts.length ? Math.max(...pts) : 0);
    }
    return sum + Math.max(1, Number(q.points) || 1);
  }, 0),
);
// Keep the threshold within 1..maxPoints as the rubric changes.
watch(maxPoints, (mp) => {
  const t = Number(bulkCampaignForm.value.threshold) || 0;
  if (mp >= 1 && t > mp) bulkCampaignForm.value.threshold = mp;
  if (mp >= 1 && t < 1) bulkCampaignForm.value.threshold = 1;
});

const leadCampaignFilter = ref(null); // null = all bulk campaigns
const expandedLead = ref(null); // call_link_id whose score breakdown is open
function toggleLead(key) {
  expandedLead.value = expandedLead.value === key ? null : key;
}
const qualifiedLeads = computed(() => {
  const rows = [];
  for (const c of bulkCampaigns.value) {
    if (leadCampaignFilter.value && String(c.id) !== String(leadCampaignFilter.value)) continue;
    // Questionnaire campaigns qualify by LEAD SCORE (score ≥ threshold);
    // campaigns without one keep the legacy "interested" verdict. For a
    // questionnaire campaign a contact with no score yet is simply not (yet)
    // qualified — we never fall back to the interest verdict for it.
    const hasQ = !!(c.questionnaire && (c.questionnaire.questions || []).length);
    const threshold = hasQ ? Number(c.questionnaire.threshold) || 0 : 0;
    const maxScore = hasQ ? (c.max_score || (c.questionnaire.questions || []).length) : null;
    for (const ct of c.contacts || []) {
      const isQualified = hasQ
        ? (ct.qualified === true ||
           (typeof ct.lead_score === 'number' && ct.lead_score >= threshold))
        : ct.interest_outcome === 'interested';
      if (!isQualified) continue;
      rows.push({
        name: ct.name || ct.phone,
        phone: ct.phone,
        campaign_id: c.id,
        answered_at: ct.answered_at,
        call_link_id: ct.call_link_id,
        lead_score: hasQ ? (typeof ct.lead_score === 'number' ? ct.lead_score : null) : null,
        max_score: maxScore,
        score_breakdown: hasQ ? (ct.score_breakdown || []) : null,
        call_note: ct.call_note || null,
      });
    }
  }
  return rows.sort((a, b) =>
    String(b.answered_at || '').localeCompare(String(a.answered_at || '')),
  );
});

// ── Call Logs ───────────────────────────────────────────────────────────
const logCampaignFilter = ref(null); // null = all bulk campaigns
const selectedCall = ref(null);
const loadingDetail = ref(false);

// Each dialed contact (call_id present) across the in-scope bulk campaigns.
const callRows = computed(() => {
  const rows = [];
  for (const c of bulkCampaigns.value) {
    if (logCampaignFilter.value && String(c.id) !== String(logCampaignFilter.value)) continue;
    const hasQ = !!(c.questionnaire && (c.questionnaire.questions || []).length);
    const maxScore = hasQ ? (c.max_score || (c.questionnaire.questions || []).length) : null;
    for (const ct of c.contacts || []) {
      if (!ct.call_id) continue;
      rows.push({
        call_id: ct.call_id,
        name: ct.name || ct.phone,
        phone: ct.phone,
        status: ct.status,
        duration_s: ct.duration_s,
        answered_at: ct.answered_at,
        campaign_name: c.name,
        lead_score: hasQ && typeof ct.lead_score === 'number' ? ct.lead_score : null,
        max_score: maxScore,
        qualified: hasQ ? !!ct.qualified : null,
        score_breakdown: hasQ ? (ct.score_breakdown || []) : null,
        call_note: ct.call_note || null,
      });
    }
  }
  return rows.sort((a, b) =>
    String(b.answered_at || '').localeCompare(String(a.answered_at || '')),
  );
});

function fmtDate(iso) {
  if (!iso) return '—';
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}
function fmtDur(s) {
  const n = Math.round(Number(s) || 0);
  if (!n) return '—';
  const m = Math.floor(n / 60);
  const sec = n % 60;
  return m ? `${m}m ${sec}s` : `${sec}s`;
}
function roleLabel(role) {
  const r = String(role || '').toLowerCase();
  if (r === 'assistant' || r === 'agent') return 'Agent';
  if (r === 'user') return 'Caller';
  return role || 'Turn';
}
const detailHasTranscript = computed(
  () => !!(selectedCall.value && !selectedCall.value._none && (selectedCall.value.turns || []).length),
);

async function openCall(row) {
  loadingDetail.value = true;
  selectedCall.value = { call_id: row.call_id, turns: [], _meta: row };
  try {
    const data = await loadTranscript(row.call_id);
    if (data && (data.turns || []).length) {
      selectedCall.value = { ...data, _meta: row };
    } else {
      selectedCall.value = { call_id: row.call_id, turns: [], _meta: row, _none: true };
    }
  } catch {
    selectedCall.value = { call_id: row.call_id, turns: [], _meta: row, _none: true };
  } finally {
    loadingDetail.value = false;
  }
}
</script>

<template>
  <div class="n-page">
    <header class="n-page-head n-rise">
      <span class="n-page-head__eyebrow">Outbound</span>
      <div class="n-page-head__row">
        <div>
          <h1 class="n-page-head__title">Bulk Lead Capturing</h1>
          <p class="n-page-head__sub">
            Upload a contact list, let the agent dial it, and track the leads and calls it produces.
          </p>
        </div>
        <button type="button" class="n-btn n-btn--ghost n-btn--sm" @click="goBack">
          <ArrowLeft :size="13" /> Back to Outbound
        </button>
      </div>
    </header>

    <nav class="blc__tabnav" aria-label="Bulk lead capturing">
      <button
        v-for="t in TABS"
        :key="t.id"
        type="button"
        class="blc__tab-btn"
        :class="{ 'is-active': bulkTab === t.id }"
        @click="bulkTab = t.id"
      >
        <component :is="t.icon" :size="14" />
        {{ t.label }}
      </button>
    </nav>

    <section class="n-section n-rise" data-delay="1">
      <!-- ════════════ CAMPAIGN TAB ════════════ -->
      <div v-if="bulkTab === 'campaign'" class="blc__panel">
        <!-- Upload / request-access card -->
        <article class="n-card">
          <div v-if="bulkCalling && bulkCalling.plan_eligible" class="outbound__bulk">
            <div class="outbound__bulk-head">
              <div>
                <strong>Bulk CSV calling</strong>
                <p>Upload a CSV of phone numbers and call the whole list automatically.</p>
              </div>
              <span
                class="n-tag"
                :class="bulkCalling.enabled ? 'n-tag--success' : 'n-tag--muted'"
              >{{ bulkCalling.enabled ? 'Enabled' : 'Add-on' }}</span>
            </div>

            <p v-if="bulkCallingNotice" class="outbound__bulk-notice">{{ bulkCallingNotice }}</p>
            <p v-if="bulkCallingError" class="outbound__bulk-error">{{ bulkCallingError }}</p>
            <div v-if="bulkDialFailures && bulkDialFailures.length" class="outbound__bulk-failures">
              <p class="outbound__bulk-failures-title">These numbers couldn't be dialed (carrier rejected the call):</p>
              <ul class="outbound__bulk-failures-list">
                <li v-for="f in bulkDialFailures" :key="f.phone">
                  <span class="n-mono">{{ f.phone }}</span> — {{ f.error }}
                </li>
              </ul>
            </div>
            <div v-if="bulkDndDropped && bulkDndDropped.length" class="outbound__bulk-dnd">
              <p class="outbound__bulk-dnd-title">
                {{ bulkDndDropped.length }} number(s) skipped — registered on the DND (Do Not Disturb) list:
              </p>
              <ul class="outbound__bulk-dnd-list">
                <li v-for="p in bulkDndDropped" :key="p"><span class="n-mono">{{ p }}</span></li>
              </ul>
            </div>

            <!-- Not yet granted → request access -->
            <template v-if="!bulkCalling.enabled">
              <div v-if="bulkCalling.request_status === 'pending'" class="outbound__bulk-pending">
                Request received — our team will reach you on
                <strong>{{ bulkCalling.contact_number }}</strong> to set up your dedicated calling number.
              </div>
              <div v-else class="outbound__bulk-request">
                <p class="outbound__bulk-pitch">
                  Bulk calling runs on a dedicated number our team provisions for you.
                  Leave a number we can reach you on to request access.
                </p>
                <div class="outbound__bulk-row">
                  <label class="n-field">
                    <span class="n-field__label">Contact number</span>
                    <input
                      v-model="bulkRequestForm.contact_number"
                      type="text"
                      class="n-input"
                      placeholder="+91 98765 43210"
                      :disabled="!isAdmin"
                    />
                  </label>
                  <label class="n-field">
                    <span class="n-field__label">Note <span class="n-field__sub">optional</span></span>
                    <input
                      v-model="bulkRequestForm.note"
                      type="text"
                      class="n-input"
                      placeholder="Best time to call, expected volume…"
                      :disabled="!isAdmin"
                    />
                  </label>
                </div>
                <button
                  type="button"
                  class="n-btn n-btn--primary n-btn--sm"
                  :disabled="!isAdmin || isSubmittingBulkRequest"
                  @click="submitBulkCallingRequest"
                >
                  {{ isSubmittingBulkRequest ? 'Sending…' : 'Request access' }}
                </button>
                <p v-if="bulkCalling.request_status === 'denied'" class="outbound__bulk-denied">
                  A previous request was declined — submit again or contact support.
                </p>
              </div>
            </template>

            <!-- Granted → upload CSV + start calling -->
            <div v-else class="outbound__bulk-launch">
              <!-- ── Campaign type ── -->
              <div class="blc__type">
                <button
                  type="button"
                  class="blc__type-card"
                  :class="{ 'is-active': bulkCampaignForm.campaign_type === 'deterministic' }"
                  :disabled="!isAdmin"
                  @click="bulkCampaignForm.campaign_type = 'deterministic'"
                >
                  <strong>Deterministic</strong>
                  <span>Structured questionnaire — intro + scored questions, auto-qualify by lead score.</span>
                </button>
                <button
                  type="button"
                  class="blc__type-card"
                  :class="{ 'is-active': bulkCampaignForm.campaign_type === 'non_deterministic' }"
                  :disabled="!isAdmin"
                  @click="bulkCampaignForm.campaign_type = 'non_deterministic'"
                >
                  <strong>Non-deterministic</strong>
                  <span>Free-form pitch — give the offer details and the agent improvises the call.</span>
                </button>
              </div>

              <div class="outbound__bulk-row">
                <label class="n-field">
                  <span class="n-field__label">Campaign name</span>
                  <input v-model="bulkCampaignForm.name" type="text" class="n-input" placeholder="December outreach" :disabled="!isAdmin" />
                </label>
                <label class="n-field">
                  <span class="n-field__label">Contacts file <span class="n-field__sub">CSV or XLSX — phone in column A, name in B</span></span>
                  <input type="file" accept=".csv,.xlsx" class="n-input" :disabled="!isAdmin" @change="onBulkCampaignFilePick" />
                </label>
              </div>
              <div class="outbound__bulk-row">
                <label class="n-field">
                  <span class="n-field__label">Business name</span>
                  <input v-model="bulkCampaignForm.company_name" type="text" class="n-input" placeholder="Raghava Estates" :disabled="!isAdmin" />
                </label>
                <label class="n-field">
                  <span class="n-field__label">Agent's name</span>
                  <input v-model="bulkCampaignForm.caller_name" type="text" class="n-input" placeholder="Riya" :disabled="!isAdmin" />
                </label>
                <label class="n-field">
                  <span class="n-field__label">Language</span>
                  <select v-model="bulkCampaignForm.language" class="n-input" :disabled="!isAdmin">
                    <option v-for="opt in voiceLanguageOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                  </select>
                </label>
              </div>
              <label class="n-field">
                <span class="n-field__label">
                  {{ bulkCampaignForm.campaign_type === 'deterministic' ? 'Background / context' : 'What to say / offer details' }}
                  <span class="n-field__sub">{{ bulkCampaignForm.campaign_type === 'deterministic' ? 'optional — extra info the agent can lean on if the prospect goes off-script' : "the offer + key facts — we turn this into the agent's script" }}</span>
                </span>
                <textarea
                  v-model="bulkCampaignForm.content"
                  class="n-input"
                  rows="3"
                  :placeholder="bulkCampaignForm.campaign_type === 'deterministic' ? 'Optional: e.g. Raghava Estates — gated community in Kollur, ready December, ₹85L onward. The agent can reference this if asked.' : 'New 3BHK gated community in Kollur, ₹85L onward, ready by December. Offer: free registration this month.'"
                  :disabled="!isAdmin"
                ></textarea>
              </label>

              <!-- ── Deterministic: intro + lead-capture questionnaire ── -->
              <div v-if="bulkCampaignForm.campaign_type === 'deterministic'" class="blc__qbuild">
                <div class="blc__qbuild-head">
                  <span class="n-field__label">
                    Lead qualification questionnaire
                    <span class="n-field__sub">the agent asks these, scores each, and auto-qualifies by lead score</span>
                  </span>
                  <button type="button" class="n-btn n-btn--ghost n-btn--sm" :disabled="!isAdmin" @click="addQuestion">
                    + Add question
                  </button>
                </div>
                <label class="n-field blc__q-intro">
                  <span class="n-field__label">
                    Intro line
                    <span class="n-field__sub">how the agent opens the call before the questions — said first, in the campaign language</span>
                  </span>
                  <textarea
                    v-model="bulkCampaignForm.intro"
                    class="n-input"
                    rows="2"
                    placeholder="Hi, this is Riya from Raghava Estates — I'm calling about our new gated community in Kollur. Do you have a quick minute?"
                    :disabled="!isAdmin"
                  ></textarea>
                </label>
                <label class="n-field blc__q-intro">
                  <span class="n-field__label">
                    Outro line
                    <span class="n-field__sub">how the agent closes — spoken to end every call, and immediately when a dealbreaker question fails</span>
                  </span>
                  <textarea
                    v-model="bulkCampaignForm.outro"
                    class="n-input"
                    rows="2"
                    placeholder="Thanks so much for your time — have a great day!"
                    :disabled="!isAdmin"
                  ></textarea>
                </label>
                <div v-if="bulkCampaignForm.questions.length" class="blc__qlist">
                  <div v-for="(q, i) in bulkCampaignForm.questions" :key="q.id" class="blc__q">
                    <div class="blc__q-row">
                      <span class="blc__q-num">{{ i + 1 }}</span>
                      <select v-model="q.type" class="n-input blc__q-type" :disabled="!isAdmin">
                        <option value="intent">Intent detection (yes/no)</option>
                        <option value="answer">Desired answer (match)</option>
                      </select>
                      <input
                        v-model="q.text"
                        type="text"
                        class="n-input blc__q-text"
                        placeholder="e.g. Are you looking to buy in the next 3 months?"
                        :disabled="!isAdmin"
                      />
                      <input
                        v-if="q.type === 'answer' && !q.graded"
                        v-model="q.desired_answer"
                        type="text"
                        class="n-input blc__q-ans"
                        placeholder="expected answer e.g. Kollur"
                        :disabled="!isAdmin"
                      />
                      <select
                        v-else-if="q.type === 'intent'"
                        v-model="q.required"
                        class="n-input blc__q-ans"
                        title="The Yes/No answer needed to qualify"
                        :disabled="!isAdmin"
                      >
                        <option value="yes">Required: Yes</option>
                        <option value="no">Required: No</option>
                      </select>
                      <label
                        v-if="!(q.type === 'answer' && q.graded)"
                        class="blc__q-pts"
                        title="Points earned when this question is answered correctly"
                      >
                        <input v-model.number="q.points" type="number" min="1" max="100" class="n-input" :disabled="!isAdmin" />
                        <span>pts</span>
                      </label>
                      <button type="button" class="blc__q-del" :disabled="!isAdmin" title="Remove question" @click="removeQuestion(i)">×</button>
                    </div>
                    <label v-if="q.type === 'answer'" class="blc__q-graded">
                      <input type="checkbox" v-model="q.graded" :disabled="!isAdmin" @change="onGradedToggle(q)" />
                      <span>Graded scoring — different answers earn different points (e.g. budget bands)</span>
                    </label>
                    <div v-if="q.type === 'answer' && q.graded" class="blc__tiers">
                      <div v-for="(t, ti) in q.tiers" :key="t.id" class="blc__tier">
                        <input
                          v-model="t.label"
                          type="text"
                          class="n-input blc__tier-label"
                          placeholder="band, e.g. above 1 crore"
                          :disabled="!isAdmin"
                        />
                        <label class="blc__q-pts" title="Points for this band">
                          <input v-model.number="t.points" type="number" min="1" max="100" class="n-input" :disabled="!isAdmin" />
                          <span>pts</span>
                        </label>
                        <button type="button" class="blc__q-del" :disabled="!isAdmin" title="Remove band" @click="removeTier(q, ti)">×</button>
                      </div>
                      <button type="button" class="n-btn n-btn--ghost n-btn--sm blc__tier-add" :disabled="!isAdmin" @click="addTier(q)">+ Add band</button>
                    </div>
                    <label v-if="!(q.type === 'answer' && q.graded)" class="blc__q-gate">
                      <input type="checkbox" v-model="q.gate" :disabled="!isAdmin" />
                      <span>Dealbreaker — if this isn't answered correctly, go to the outro and cut the call</span>
                    </label>
                  </div>
                  <div class="blc__q-thresh">
                    <span class="blc__q-thresh-label">Qualify when score ≥</span>
                    <input
                      v-model.number="bulkCampaignForm.threshold"
                      type="number"
                      class="n-input blc__q-thresh-input"
                      :min="1"
                      :max="maxPoints"
                      :disabled="!isAdmin"
                    />
                    <span class="blc__q-thresh-hint">of {{ maxPoints }} point(s) available</span>
                  </div>
                </div>
                <p v-else class="blc__q-empty">
                  Add at least one question — set each question's points (or graded bands) to weight the lead score.
                </p>
              </div>

              <button
                type="button"
                class="n-btn n-btn--primary n-btn--sm"
                :disabled="!isAdmin || isLaunchingBulkCampaign"
                @click="startBulkCallingCampaign"
              >
                {{ isLaunchingBulkCampaign ? 'Starting…' : 'Upload & start calling' }}
              </button>
            </div>
          </div>
          <div v-else class="n-empty blc__empty">
            <p class="n-empty__title">Bulk calling isn't on your plan</p>
            <p class="n-empty__copy">Contact us to enable Bulk Lead Capturing for your account.</p>
          </div>
        </article>

        <!-- Bulk campaign list -->
        <article class="n-card blc__camp-card">
          <header class="blc__section-head">
            <strong>Bulk campaigns</strong>
            <button type="button" class="n-btn n-btn--ghost n-btn--sm" @click="loadCampaigns">
              <RefreshCw :size="13" /> Refresh
            </button>
          </header>
          <div v-if="!bulkCampaigns.length" class="n-empty blc__empty">
            <p class="n-empty__copy">No bulk campaigns yet — upload a list above to start one.</p>
          </div>
          <ul v-else class="blc__camp-list">
            <li v-for="c in bulkCampaigns" :key="c.id" class="blc__camp">
              <header class="blc__camp-head" role="button" tabindex="0" @click="toggleCampaignExpansion(c.id)">
                <div class="blc__camp-title">
                  <PhoneCall :size="14" />
                  <strong class="n-truncate">{{ c.name }}</strong>
                  <span class="n-tag" :class="campaignStatusTone(c.status)">{{ c.status }}</span>
                  <span class="n-tag n-tag--mono">{{ c.deterministic ? 'Deterministic' : 'Pitch' }}</span>
                </div>
                <div class="blc__camp-meta">
                  <span><strong>{{ c.total_count || 0 }}</strong> dialed</span>
                  <span v-if="c.answered_count"><strong>{{ c.answered_count }}</strong> answered</span>
                  <span v-if="c.failed_count"><strong>{{ c.failed_count }}</strong> failed</span>
                  <span v-if="c.created_at" class="n-mono">{{ formatRelativeDate(c.created_at) }}</span>
                  <button
                    v-if="isAdmin && bulkRedialCount(c) > 0"
                    type="button"
                    class="n-btn n-btn--brand n-btn--sm"
                    :disabled="isRerunningBulkCampaign === c.id || !bulkCalling?.enabled"
                    :title="!bulkCalling?.enabled ? 'Bulk calling isn\'t enabled' : `Call the ${bulkRedialCount(c)} contact(s) you haven't reached yet`"
                    @click.stop="rerunBulkCampaign(c.id)"
                  >
                    <RefreshCw :size="13" />
                    {{ isRerunningBulkCampaign === c.id ? 'Re-running…' : `Re-run (${bulkRedialCount(c)})` }}
                  </button>
                  <component :is="expandedCampaignId === c.id ? ChevronUp : ChevronDown" :size="14" />
                </div>
              </header>
              <div v-if="expandedCampaignId === c.id" class="blc__camp-contacts">
                <div v-if="!(c.contacts || []).length" class="blc__muted">No contacts attached.</div>
                <div v-for="ct in (c.contacts || [])" :key="ct.call_link_id || ct.phone" class="blc__contact">
                  <strong class="n-truncate">{{ ct.name || ct.phone }}</strong>
                  <span class="n-mono">{{ ct.phone }}</span>
                  <span class="n-tag n-tag--mono">{{ ct.status }}</span>
                </div>
              </div>
            </li>
          </ul>
        </article>
      </div>

      <!-- ════════════ QUALIFIED LEADS TAB ════════════ -->
      <div v-else-if="bulkTab === 'leads'" class="blc__panel">
        <article class="n-card">
          <header class="blc__section-head">
            <div class="blc__section-title">
              <strong>Qualified leads</strong>
              <span class="n-tag n-tag--mono">{{ qualifiedLeads.length }}</span>
            </div>
            <button type="button" class="n-btn n-btn--ghost n-btn--sm" @click="loadCampaigns">
              <RefreshCw :size="13" /> Refresh
            </button>
          </header>
          <p class="blc__hint">Contacts that qualified — by lead score where a questionnaire is set, otherwise judged interested. Click a scored row to see the per-question breakdown.</p>

          <nav v-if="bulkCampaigns.length > 1" class="n-pillnav blc__pillnav" aria-label="Filter by campaign">
            <button
              type="button"
              class="n-pillnav__btn"
              :class="{ 'n-pillnav__btn--active': leadCampaignFilter === null }"
              @click="leadCampaignFilter = null"
            ><span>All</span></button>
            <button
              v-for="c in bulkCampaigns"
              :key="c.id"
              type="button"
              class="n-pillnav__btn"
              :class="{ 'n-pillnav__btn--active': leadCampaignFilter === c.id }"
              @click="leadCampaignFilter = c.id"
            ><span>{{ c.name }}</span></button>
          </nav>

          <div v-if="!qualifiedLeads.length" class="n-empty blc__empty">
            <div class="n-empty__icon"><Users :size="18" /></div>
            <p class="n-empty__copy">No qualified leads yet — interested contacts from bulk calls will appear here.</p>
          </div>
          <table v-else class="blc__table">
            <thead>
              <tr><th>Name</th><th>Phone</th><th>Score</th></tr>
            </thead>
            <tbody>
              <template v-for="(row, i) in qualifiedLeads" :key="row.call_link_id || i">
                <tr
                  :class="{ 'blc__row-clickable': row.call_note || (row.score_breakdown && row.score_breakdown.length) }"
                  @click="(row.call_note || (row.score_breakdown && row.score_breakdown.length)) ? toggleLead(row.call_link_id) : null"
                >
                  <td class="blc__td-name">
                    <strong class="n-truncate">{{ row.name }}</strong>
                    <span v-if="row.call_note" class="blc__td-note n-truncate">{{ row.call_note }}</span>
                  </td>
                  <td class="blc__td-phone">
                    <a v-if="phoneHref(row.phone)" :href="phoneHref(row.phone)" @click.stop><PhoneCall :size="11" /> {{ row.phone }}</a>
                    <span v-else class="n-mono">{{ row.phone }}</span>
                  </td>
                  <td class="blc__td-score">
                    <span v-if="row.max_score" class="n-tag n-tag--success">{{ row.lead_score }}/{{ row.max_score }}</span>
                    <span v-else class="n-mono">—</span>
                  </td>
                </tr>
                <tr
                  v-if="expandedLead === row.call_link_id && (row.call_note || (row.score_breakdown && row.score_breakdown.length))"
                  class="blc__breakdown-row"
                >
                  <td colspan="3">
                    <p v-if="row.call_note" class="blc__callnote"><span class="blc__callnote-label">Call note</span> {{ row.call_note }}</p>
                    <ul v-if="row.score_breakdown && row.score_breakdown.length" class="blc__breakdown">
                      <li v-for="(b, bi) in row.score_breakdown" :key="bi" :class="b.awarded ? 'is-awarded' : 'is-missed'">
                        <span class="blc__bk-mark">{{ b.awarded ? '✓' : '✗' }}</span>
                        <span class="blc__bk-text">{{ b.text }}</span>
                        <span v-if="b.awarded_points" class="blc__bk-pts">+{{ b.awarded_points }}</span>
                        <span v-if="b.evidence" class="blc__bk-ev n-mono">“{{ b.evidence }}”</span>
                      </li>
                    </ul>
                  </td>
                </tr>
              </template>
            </tbody>
          </table>
        </article>
      </div>

      <!-- ════════════ CALL LOGS TAB ════════════ -->
      <div v-else class="blc__panel">
        <nav v-if="bulkCampaigns.length > 1" class="n-pillnav blc__pillnav" aria-label="Filter calls by campaign">
          <button
            type="button"
            class="n-pillnav__btn"
            :class="{ 'n-pillnav__btn--active': logCampaignFilter === null }"
            @click="logCampaignFilter = null"
          ><span>All</span></button>
          <button
            v-for="c in bulkCampaigns"
            :key="c.id"
            type="button"
            class="n-pillnav__btn"
            :class="{ 'n-pillnav__btn--active': logCampaignFilter === c.id }"
            @click="logCampaignFilter = c.id"
          ><span>{{ c.name }}</span></button>
        </nav>

        <div class="blc__logs">
          <article class="n-card blc__logs-list">
            <div v-if="!callRows.length" class="n-empty blc__empty">
              <div class="n-empty__icon"><ScrollText :size="18" /></div>
              <p class="n-empty__copy">No calls yet for these campaigns.</p>
            </div>
            <ul v-else class="blc__call-items">
              <li
                v-for="row in callRows"
                :key="row.call_id"
                class="blc__call-item"
                :class="{ 'is-active': selectedCall && selectedCall.call_id === row.call_id }"
                @click="openCall(row)"
              >
                <div class="blc__call-main">
                  <strong class="n-truncate">{{ row.name }}</strong>
                  <span class="n-tag n-tag--mono">{{ row.status }}</span>
                </div>
                <div class="blc__call-meta">
                  <span class="n-mono">{{ row.phone }}</span>
                  <span><Clock :size="11" /> {{ fmtDur(row.duration_s) }}</span>
                  <span class="n-mono">{{ fmtDate(row.answered_at) }}</span>
                </div>
              </li>
            </ul>
          </article>

          <article class="n-card blc__logs-detail">
            <div v-if="!selectedCall" class="blc__detail-empty">
              <p>Select a call to read its transcript.</p>
            </div>
            <template v-else>
              <header class="blc__detail-head">
                <div>
                  <strong class="n-truncate">{{ selectedCall._meta?.name }}</strong>
                  <span class="blc__detail-sub">
                    {{ fmtDate(selectedCall._meta?.answered_at) }} · {{ fmtDur(selectedCall._meta?.duration_s) }}
                  </span>
                </div>
                <button
                  v-if="detailHasTranscript"
                  type="button"
                  class="n-btn n-btn--brand n-btn--sm"
                  @click="downloadTranscript(selectedCall.call_id)"
                >
                  <Download :size="13" /> .txt
                </button>
              </header>
              <!-- Lead score badge + per-question breakdown (questionnaire campaigns) -->
              <div v-if="selectedCall._meta?.max_score" class="blc__detail-score">
                <span class="n-tag" :class="selectedCall._meta?.qualified ? 'n-tag--success' : 'n-tag--danger'">
                  Score {{ selectedCall._meta?.lead_score ?? 0 }}/{{ selectedCall._meta?.max_score }}
                  · {{ selectedCall._meta?.qualified ? 'Qualified' : 'Not qualified' }}
                </span>
                <ul v-if="(selectedCall._meta?.score_breakdown || []).length" class="blc__breakdown">
                  <li v-for="(b, bi) in selectedCall._meta.score_breakdown" :key="bi" :class="b.awarded ? 'is-awarded' : 'is-missed'">
                    <span class="blc__bk-mark">{{ b.awarded ? '✓' : '✗' }}</span>
                    <span class="blc__bk-text">{{ b.text }}</span>
                    <span v-if="b.awarded_points" class="blc__bk-pts">+{{ b.awarded_points }}</span>
                    <span v-if="b.evidence" class="blc__bk-ev n-mono">“{{ b.evidence }}”</span>
                  </li>
                </ul>
              </div>
              <!-- Call Note: the condenser's summary for a qualified/interested contact -->
              <p v-if="selectedCall._meta?.call_note" class="blc__callnote blc__callnote--detail">
                <span class="blc__callnote-label">Call note</span> {{ selectedCall._meta.call_note }}
              </p>
              <p v-if="loadingDetail" class="blc__state">Loading…</p>
              <div v-else-if="!detailHasTranscript" class="blc__state">
                No transcript available — it may have expired (kept 30 days) or the call was too short.
              </div>
              <div v-else class="blc__turns">
                <div
                  v-for="(t, i) in (selectedCall.turns || [])"
                  :key="i"
                  class="blc__turn"
                  :class="`blc__turn--${String(t.role || '').toLowerCase()}`"
                >
                  <span class="blc__turn-role">{{ roleLabel(t.role) }}</span>
                  <span class="blc__turn-text">{{ t.content }}</span>
                </div>
              </div>
            </template>
          </article>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
/* ── Sub-tab nav (mirrors OutgoingAgentView's tab nav) ── */
.blc__tabnav {
  display: flex;
  align-items: stretch;
  background: var(--n-bg);
  border: 2px solid var(--n-text);
  border-radius: 0;
  box-shadow: 4px 4px 0 var(--n-brand);
  width: fit-content;
  max-width: 100%;
  overflow-x: auto;
}
.blc__tab-btn {
  appearance: none;
  background: transparent;
  border: none;
  border-right: 2px solid var(--n-text);
  padding: 12px 18px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-family: var(--n-font-display);
  font-weight: 600;
  font-size: 13px;
  color: var(--n-text);
  cursor: pointer;
  white-space: nowrap;
}
.blc__tab-btn:last-child { border-right: none; }
.blc__tab-btn:hover { background: var(--n-surface); }
.blc__tab-btn.is-active { background: var(--n-text); color: var(--n-bg); }

.blc__panel { display: grid; gap: 16px; }
.blc__section-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  margin-bottom: 12px;
}
.blc__section-title { display: inline-flex; align-items: center; gap: 8px; }
.blc__pillnav { margin-bottom: 12px; }
.blc__state { padding: 24px; text-align: center; color: var(--n-text-3); font-size: 13.5px; }
.blc__empty { margin: 8px 0; }
.blc__muted { font-size: 13px; color: var(--n-text-3); font-style: italic; }
.blc__cap {
  display: block; font-size: 10.5px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--n-text-3); font-family: var(--n-font-mono); margin-bottom: 3px;
}

/* ── Bulk upload card (neutralize OutgoingAgentView's standalone divider look) ── */
.outbound__bulk {
  display: flex; flex-direction: column; gap: 12px;
  padding: 0; border-bottom: none; background: transparent;
}
.outbound__bulk-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.outbound__bulk-head p { margin: 2px 0 0; color: var(--n-text-3); font-size: 13px; }
.outbound__bulk-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.outbound__bulk-pitch,
.outbound__bulk-pending,
.outbound__bulk-denied { font-size: 13px; color: var(--n-text-3); margin: 0; }
.outbound__bulk-pending { color: var(--n-text); }
.outbound__bulk-launch,
.outbound__bulk-request { display: flex; flex-direction: column; gap: 12px; align-items: flex-start; }
.outbound__bulk-launch .n-field,
.outbound__bulk-request .n-field { width: 100%; }
.outbound__bulk-notice { color: var(--n-success, #1a7f37); font-size: 13px; margin: 0; }
.outbound__bulk-error { color: var(--n-danger, #c0362c); font-size: 13px; margin: 0; }
.outbound__bulk-failures { border: 1px solid var(--n-danger, #c0362c); border-radius: 8px; padding: 8px 12px; }
.outbound__bulk-failures-title { color: var(--n-danger, #c0362c); font-size: 13px; font-weight: 600; margin: 0 0 4px; }
.outbound__bulk-failures-list { margin: 0; padding-left: 18px; font-size: 12px; color: var(--n-text-3); }
.outbound__bulk-failures-list li { margin: 2px 0; }
.outbound__bulk-dnd { border: 1px solid var(--n-border); border-radius: 8px; padding: 8px 12px; }
.outbound__bulk-dnd-title { font-size: 13px; font-weight: 600; margin: 0 0 4px; color: var(--n-text); }
.outbound__bulk-dnd-list { margin: 0; padding-left: 18px; font-size: 12px; color: var(--n-text-3); columns: 2; }
.outbound__bulk-dnd-list li { margin: 2px 0; }

/* ── Bulk campaign list ── */
.blc__camp-card { padding: 16px 18px; }
.blc__camp-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
.blc__camp { border: 2px solid var(--n-border); border-radius: var(--n-r-md, 8px); overflow: hidden; }
.blc__camp-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 12px 14px; cursor: pointer; flex-wrap: wrap;
}
.blc__camp-head:hover { background: var(--n-surface); }
.blc__camp-title { display: inline-flex; align-items: center; gap: 8px; min-width: 0; }
.blc__camp-meta {
  display: inline-flex; align-items: center; gap: 14px; flex-wrap: wrap;
  font-size: 12.5px; color: var(--n-text-3);
}
.blc__camp-meta strong { color: var(--n-text); }
.blc__camp-contacts {
  border-top: 2px solid var(--n-border); padding: 10px 14px;
  display: grid; gap: 6px; background: var(--n-surface);
}
.blc__contact {
  display: grid; grid-template-columns: 1fr auto auto; align-items: center; gap: 10px;
  font-size: 13px;
}

/* ── Qualified leads table ── */
.blc__hint { margin: 0 0 12px; font-size: 13px; color: var(--n-text-3); }
.blc__table { width: 100%; border-collapse: collapse; }
.blc__table thead th {
  text-align: left; font-size: 10.5px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--n-text-3); font-family: var(--n-font-mono);
  padding: 8px 12px; border-bottom: 2px solid var(--n-border);
}
.blc__table tbody td { padding: 12px; border-bottom: 2px solid var(--n-border); font-size: 13.5px; vertical-align: middle; }
.blc__table tbody tr:last-child td { border-bottom: 0; }
.blc__table tbody tr:hover { background: var(--n-surface); }
.blc__td-name { max-width: 280px; }
.blc__td-note { display: block; margin-top: 2px; font-size: 11.5px; color: var(--n-text-3); max-width: 280px; }
.blc__td-phone a {
  display: inline-flex; align-items: center; gap: 5px;
  color: var(--n-brand, #6366f1); text-decoration: none; font-family: var(--n-font-mono); font-size: 13px;
}
@media (max-width: 760px) { .outbound__bulk-row { grid-template-columns: 1fr; } }

/* ── Call logs (list + detail) ── */
.blc__logs { display: grid; grid-template-columns: 0.9fr 1.1fr; gap: 16px; align-items: start; }
@media (max-width: 1000px) { .blc__logs { grid-template-columns: 1fr; } }
.blc__logs-list, .blc__logs-detail { padding: 0; overflow: hidden; }
.blc__call-items { list-style: none; margin: 0; padding: 4px 0; max-height: 72vh; overflow-y: auto; }
.blc__call-item {
  padding: 12px 16px; border-bottom: 2px solid var(--n-border);
  cursor: pointer; display: grid; gap: 6px;
}
.blc__call-item:last-child { border-bottom: 0; }
.blc__call-item:hover { background: var(--n-surface); }
.blc__call-item.is-active {
  background: var(--n-surface); outline: 2px solid var(--n-text); outline-offset: -2px;
  border-right: 6px solid var(--n-text);
}
.blc__call-main { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.blc__call-meta {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  font-size: 12px; color: var(--n-text-3); font-family: var(--n-font-mono);
}
.blc__call-meta > span { display: inline-flex; align-items: center; gap: 4px; }
.blc__detail-empty { padding: 44px 24px; text-align: center; color: var(--n-text-3); font-size: 13.5px; font-style: italic; }
.blc__detail-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 16px 18px; border-bottom: 2px solid var(--n-border);
}
.blc__detail-sub { display: block; font-size: 12px; color: var(--n-text-3); margin-top: 2px; font-family: var(--n-font-mono); }
.blc__turns { padding: 16px 18px; max-height: 72vh; overflow-y: auto; display: grid; gap: 12px; }
.blc__turn { display: grid; gap: 2px; }
.blc__turn-role {
  font-size: 11px; font-weight: 600; color: var(--n-text); text-transform: uppercase;
  letter-spacing: 0.04em; font-family: var(--n-font-mono);
}
.blc__turn-text { font-size: 13.5px; line-height: 1.5; color: var(--n-text); white-space: pre-wrap; font-family: var(--n-font-mono); }

/* ── Campaign type selector ── */
.blc__type { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.blc__type-card {
  appearance: none; text-align: left; cursor: pointer;
  border: 2px solid var(--n-border); border-radius: var(--n-r-md, 8px);
  background: var(--n-bg); padding: 12px 14px; display: grid; gap: 4px;
}
.blc__type-card:hover { background: var(--n-surface); }
.blc__type-card.is-active { border-color: var(--n-text); box-shadow: 3px 3px 0 var(--n-brand); }
.blc__type-card strong { font-size: 14px; color: var(--n-text); }
.blc__type-card span { font-size: 12px; color: var(--n-text-3); line-height: 1.4; }
.blc__type-card:disabled { opacity: 0.6; cursor: not-allowed; }
@media (max-width: 760px) { .blc__type { grid-template-columns: 1fr; } }

/* ── Questionnaire builder ── */
.blc__qbuild { border: 2px solid var(--n-border); border-radius: var(--n-r-md, 8px); padding: 12px; display: grid; gap: 10px; }
.blc__qbuild-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.blc__qlist { display: grid; gap: 10px; }
.blc__q { display: grid; gap: 6px; border: 1px solid var(--n-border); border-radius: 6px; padding: 8px; }
.blc__q-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.blc__q-text { flex: 1 1 180px; min-width: 140px; }
.blc__q-ans { flex: 0 0 160px; }
.blc__q-gate, .blc__q-graded { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--n-text-3); padding-left: 24px; cursor: pointer; }
.blc__q-gate input, .blc__q-graded input { cursor: pointer; }
.blc__q-graded { color: var(--n-text); font-weight: 600; }
.blc__q-num { font-family: var(--n-font-mono); font-size: 12px; color: var(--n-text-3); width: 16px; text-align: right; flex: 0 0 auto; }
.blc__q-type { flex: 0 0 170px; max-width: 170px; }
/* points weight + graded scoring bands */
.blc__q-pts { display: inline-flex; align-items: center; gap: 4px; font-size: 11.5px; color: var(--n-text-3); flex: 0 0 auto; }
.blc__q-pts input { width: 56px; }
.blc__tiers { display: grid; gap: 6px; padding-left: 24px; }
.blc__tier { display: flex; gap: 8px; align-items: center; }
.blc__tier-label { flex: 1 1 auto; min-width: 120px; }
.blc__tier-add { justify-self: start; }
.blc__q-del {
  appearance: none; border: 2px solid var(--n-border); background: var(--n-bg); border-radius: 6px;
  width: 30px; height: 30px; cursor: pointer; font-size: 16px; line-height: 1; color: var(--n-danger, #c0362c);
}
.blc__q-del:hover { background: var(--n-surface); }
.blc__q-thresh { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding-top: 4px; }
.blc__q-thresh-label { font-size: 12.5px; font-weight: 600; color: var(--n-text); }
.blc__q-thresh-input { width: 72px; }
.blc__q-thresh-hint { font-size: 12px; color: var(--n-text-3); }
.blc__q-empty { font-size: 12.5px; color: var(--n-text-3); margin: 0; font-style: italic; }
@media (max-width: 760px) {
  .blc__q-row { flex-direction: column; align-items: stretch; }
  .blc__q-type, .blc__q-ans { flex: 1 1 auto; max-width: none; }
}

/* ── Score column + per-question breakdown ── */
.blc__td-score { white-space: nowrap; }
.blc__row-clickable { cursor: pointer; }
.blc__breakdown-row > td { padding: 0 12px 12px; }
.blc__callnote { margin: 0 0 8px; padding: 8px 12px; font-size: 13px; line-height: 1.5; color: var(--n-text-2, var(--n-text-3)); background: var(--n-surface); border-radius: 6px; }
.blc__callnote-label { display: inline-block; margin-right: 6px; font-size: 10.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; color: var(--n-text-3); font-family: var(--n-font-mono); }
.blc__callnote--detail { margin: 12px 18px; }
.blc__breakdown { list-style: none; margin: 0; padding: 8px 12px; display: grid; gap: 6px; background: var(--n-surface); border-radius: 6px; }
.blc__breakdown li { display: grid; grid-template-columns: auto 1fr auto; gap: 8px; align-items: baseline; font-size: 13px; }
.blc__breakdown li.is-awarded .blc__bk-mark { color: var(--n-success, #1a7f37); }
.blc__breakdown li.is-missed .blc__bk-mark { color: var(--n-danger, #c0362c); }
.blc__bk-mark { font-weight: 700; }
.blc__bk-pts { font-family: var(--n-font-mono); font-size: 11.5px; font-weight: 700; color: var(--n-success, #1a7f37); white-space: nowrap; }
.blc__bk-ev { grid-column: 2 / -1; font-size: 11.5px; color: var(--n-text-3); }
.blc__detail-score { padding: 12px 18px; border-bottom: 2px solid var(--n-border); display: grid; gap: 8px; }
</style>
