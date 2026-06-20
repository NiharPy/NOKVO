<script setup>
import { computed } from 'vue';
import {
  ChevronDown,
  ChevronUp,
  Copy,
  FileText,
  MessageSquare,
  Mic,
  PhoneCall,
  Plus,
  Radio,
  RefreshCw,
  Search,
  Send,
  Square,
  Trash2,
  UserPlus,
  Users,
  Wallet,
  XCircle,
} from 'lucide-vue-next';
import { useDashboardState } from '../composables/useDashboardState.js';

const {
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
  phoneHref,
  outboundTester,
  OUTBOUND_OBJECTIVE_OPTIONS,
  voice,
  voiceLanguageOptions,
  selectedTesterCampaignId,
  campaigns,
  liveCostRupees,
  liveCostDuration,
  startVoiceCall,
  endVoiceCall,
  sendTextToVoiceAgent,
  chatInput,
  outboundTesterOutcome,
  switchPage,
  activeLeadCampaignTab,
  UNCATEGORIZED_TAB_ID,
  isAdmin,
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
  formatRelativeDate,
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
  isTogglingFollowup,
  setCampaignFollowup,
  removeCampaign,
} = useDashboardState();

const voiceStatus = computed(() => voice?.value?.status || 'idle');
const isCalling = computed(() => !['idle', 'error'].includes(voiceStatus.value));

const callableLeads = computed(() => (outgoingLeads?.value || []).filter((l) => l.callable));

// The lead-form chip currently selected (a real form, not "All"/"Uncategorized")
// — drives the "Create & call this form" action.
const activeFormChip = computed(() =>
  (leadFormFilters?.value || []).find(
    (c) => c.id && c.id !== '__none__' && c.id === activeLeadFormId?.value,
  ),
);

const tabs = [
  { id: 'campaigns', label: 'Campaigns', icon: PhoneCall },
  { id: 'leads', label: 'Consented leads', icon: Users },
  { id: 'tester', label: 'Tester', icon: Mic },
];

function setTab(id) {
  outgoingTab.value = id;
  if (id === 'campaigns' || id === 'tester') loadCampaigns();
}

// Header CTA: switch to campaigns tab and open the create form in one click.
function startCreateCampaign() {
  outgoingTab.value = 'campaigns';
  showCampaignCreateForm.value = true;
  loadCampaigns();
  // Smooth-scroll to the campaigns card after the tab swap renders.
  setTimeout(() => {
    const el = document.querySelector('.outbound__campaigns');
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, 60);
}

function campaignStatusTone(s) {
  if (s === 'running') return 'n-tag--success';
  if (s === 'draft') return 'n-tag--brand';
  return 'n-tag--danger';
}
</script>

<template>
  <div class="n-page outbound">
    <!-- Page head -->
    <header class="n-page-head n-rise">
      <span class="n-page-head__eyebrow">Outbound agent</span>
      <div>
        <h1 class="n-page-head__title">Campaign workspace</h1>
        <p class="n-page-head__sub">
          Source consented leads, mint pitch prompts, and run a live tester through the same pipeline your campaigns use.
        </p>
      </div>
      <div class="outbound__head-toolbar">
        <!-- Tabs -->
        <nav class="outbound__tabnav" aria-label="Outbound workspace">
          <button
            v-for="t in tabs"
            :key="t.id"
            type="button"
            class="outbound__tab-btn"
            :class="{ 'is-active': outgoingTab === t.id }"
            @click="setTab(t.id)"
          >
            <component :is="t.icon" :size="14" />
            {{ t.label }}
          </button>
        </nav>

        <div class="outbound__head-actions">
          <button
            type="button"
            class="outbound__icon-btn"
            :aria-label="isLoadingLeadSources ? 'Refreshing...' : 'Refresh workspace'"
            :disabled="isLoadingLeadSources"
            @click="loadOutgoingAgentWorkspace"
            :title="isLoadingLeadSources ? 'Refreshing...' : 'Refresh workspace'"
          >
            <RefreshCw :size="16" :class="{ 'n-spin': isLoadingLeadSources }" />
          </button>
          <button
            v-if="isAdmin"
            type="button"
            class="outbound__cta outbound__cta--ghost"
            aria-label="Connect Meta Ads"
            @click="requestLeadOAuth('meta_ads')"
          >
            <span class="outbound__cta-icon">
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                <path d="M8 8c-2.2 0-4 1.8-4 4s1.8 4 4 4c3 0 4-4 6-4s3 4 6 4 4-1.8 4-4-1.8-4-4-4c-3 0-4 4-6 4s-3-4-6-4Z"/>
              </svg>
            </span>
            <span class="outbound__cta-label">
              <strong>Connect Meta Ads</strong>
              <small>Facebook & Instagram</small>
            </span>
          </button>
          <button
            v-if="isAdmin"
            type="button"
            class="outbound__cta"
            @click="startCreateCampaign"
          >
            <span class="outbound__cta-icon"><Plus :size="14" /></span>
            <span class="outbound__cta-label">
              <strong>Create campaign</strong>
              <small>Mint a pitch + map consented leads</small>
            </span>
            <span class="outbound__cta-glow" aria-hidden="true"></span>
          </button>
        </div>
      </div>
    </header>

    <section class="n-section n-rise" data-delay="2">
      <!-- Leads tab -->
      <article v-if="outgoingTab === 'leads'" class="n-card outbound__forms">
        <header class="outbound__list-head">
          <div>
            <strong>Consented leads</strong>
            <p>Sorted by lead form. Pick a form (= campaign/project), then select its callable leads. Unknown-consent rows are visible but blocked.</p>
          </div>
          <span class="n-tag n-tag--brand n-tag--mono">{{ selectedCallableLeads.length }} selected</span>
        </header>

        <!-- Form filter: sort leads by the form/post they came in on -->
        <div v-if="leadFormFilters.length > 1" class="outbound__form-filter">
          <button
            v-for="chip in leadFormFilters"
            :key="chip.id === null ? '__all__' : chip.id"
            type="button"
            class="outbound__form-chip"
            :class="{ 'is-active': activeLeadFormId === chip.id }"
            @click="setLeadFormFilter(chip.id)"
          >
            {{ chip.name }}
            <span class="outbound__form-chip-count">
              {{ chip.callable != null ? `${chip.callable}/${chip.total}` : chip.total }}
            </span>
          </button>
        </div>

        <div v-if="!outgoingLeads.length" class="outbound__empty">
          No leads imported yet. Connect a source or publish a Nokvo form above.
        </div>
        <template v-else>
          <div class="outbound__lead-actions">
            <button type="button" class="n-btn n-btn--ghost n-btn--sm" @click="selectAllCallableInForm">
              Select all callable{{ activeLeadFormId !== null ? ' in this form' : '' }}
            </button>
            <button
              v-if="selectedCallableLeads.length"
              type="button"
              class="n-btn n-btn--ghost n-btn--sm"
              @click="clearLeadSelection"
            >
              Clear selection
            </button>
            <button
              v-if="isAdmin && activeFormChip && activeFormChip.callable > 0 && outboundNumber.calling_enabled"
              type="button"
              class="n-btn n-btn--primary n-btn--sm outbound__callbtn"
              @click="createAndCallFromForm(activeFormChip)"
            >
              <PhoneCall :size="13" /> Create &amp; call this form ({{ activeFormChip.callable }})
            </button>
            <span
              v-else-if="isAdmin && activeFormChip && activeFormChip.callable > 0 && !outboundNumber.calling_enabled"
              class="outbound__upgrade-hint"
            >
              Upgrade to Inbound + Outbound to call these leads.
            </span>
          </div>
          <div v-if="!visibleLeads.length" class="outbound__empty">
            No leads for this form yet.
          </div>
          <ul v-else class="outbound__lead-list">
            <li
              v-for="lead in visibleLeads"
              :key="lead.id"
              class="outbound__lead-row"
              :class="{ 'is-on': selectedLeadIds.includes(lead.id), 'is-off': !lead.callable }"
              role="button"
              tabindex="0"
              @click="toggleLeadSelection(lead)"
              @keydown.enter.prevent="toggleLeadSelection(lead)"
              @keydown.space.prevent="toggleLeadSelection(lead)"
            >
              <span class="outbound__lead-icon"><PhoneCall :size="14" /></span>
              <div>
                <strong>{{ lead.name || lead.phone_e164 || 'Unnamed lead' }}</strong>
                <span class="n-mono">{{ leadFormName(lead) }} · {{ lead.phone_e164 || 'no phone' }} · {{ lead.consent_status }}</span>
              </div>
            <div class="outbound__lead-foot">
              <a
                v-if="phoneHref(lead.phone_e164 || lead.phone_raw)"
                class="outbound__lead-call"
                :href="phoneHref(lead.phone_e164 || lead.phone_raw)"
                @click.stop
              >
                <PhoneCall :size="11" /> Call
              </a>
              <span class="n-tag" :class="lead.callable ? 'n-tag--success' : 'n-tag--warning'">{{ lead.callable ? 'Callable' : 'Blocked' }}</span>
            </div>
          </li>
          </ul>
        </template>
      </article>

      <!-- Tester -->
      <div v-else-if="outgoingTab === 'tester'" class="outbound__tester-grid">
        <article class="n-card outbound__tester-form">
          <header class="outbound__card-head">
            <div>
              <h2 class="outbound__card-title">Outbound tester</h2>
              <p class="outbound__card-sub">Rehearse the agent live. Edit goal, objectives, tone — call into the same pipeline campaigns use.</p>
            </div>
          </header>

          <div class="outbound__tester-body">
            <div class="outbound__tester-grid-row">
              <label class="n-field">
                <span class="n-field__label">Caller name</span>
                <input v-model="outboundTester.caller_name" type="text" class="n-input" placeholder="Riya" />
              </label>
              <label class="n-field">
                <span class="n-field__label">Company</span>
                <input v-model="outboundTester.company_name" type="text" class="n-input" placeholder="Raghava Constructions" />
              </label>
            </div>
            <label class="n-field">
              <span class="n-field__label">Pitch summary <span class="n-field__sub">one-liner the agent says in the opener</span></span>
              <input v-model="outboundTester.pitch_summary" type="text" class="n-input" placeholder="2BHK apartments in Kompally starting at 78 lakhs" />
            </label>
            <div class="outbound__tester-grid-row">
              <label class="n-field">
                <span class="n-field__label">Objective</span>
                <select v-model="outboundTester.objective" class="n-select" :disabled="isCalling">
                  <option v-for="opt in OUTBOUND_OBJECTIVE_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                </select>
              </label>
              <label class="n-field">
                <span class="n-field__label">Language</span>
                <select v-model="outboundTester.language" class="n-select" :disabled="isCalling">
                  <option v-for="opt in voiceLanguageOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                </select>
              </label>
            </div>
            <label class="n-field">
              <span class="n-field__label">Goal <span class="n-field__sub">plain-language, e.g. "book demo for Saturday"</span></span>
              <input v-model="outboundTester.goal" type="text" class="n-input" placeholder="Confirm the prospect can take a 15-minute site visit this week." />
            </label>
            <label class="n-field">
              <span class="n-field__label">Session name</span>
              <input v-model="outboundTester.name" type="text" class="n-input" placeholder="Outbound test call" />
            </label>
            <label class="n-field">
              <span class="n-field__label">System prompt <span class="n-field__sub">ignored when a campaign is selected</span></span>
              <textarea v-model="outboundTester.agent_prompt" class="n-textarea" rows="5"></textarea>
            </label>
            <div class="outbound__tester-grid-row">
              <label class="n-field">
                <span class="n-field__label">Objectives <span class="n-field__sub">one per line</span></span>
                <textarea v-model="outboundTester.objectives" class="n-textarea" rows="3"></textarea>
              </label>
              <label class="n-field">
                <span class="n-field__label">Exit conditions <span class="n-field__sub">one per line</span></span>
                <textarea v-model="outboundTester.exit_conditions" class="n-textarea" rows="3"></textarea>
              </label>
            </div>
            <label class="n-field">
              <span class="n-field__label">Tone</span>
              <input v-model="outboundTester.tone" type="text" class="n-input" placeholder="warm, direct, and respectful" />
            </label>

            <div class="outbound__campaign-picker">
              <label class="n-field">
                <span class="n-field__label">Test with campaign config <span class="n-field__sub">overrides the form above</span></span>
                <select v-model="selectedTesterCampaignId" class="n-select">
                  <option :value="null">— Use the inline form above —</option>
                  <option v-for="c in campaigns" :key="c.id" :value="c.id">{{ c.name }}</option>
                </select>
              </label>
            </div>
          </div>
        </article>

        <article class="n-card outbound__tester-call">
          <header class="outbound__card-head">
            <div>
              <h2 class="outbound__card-title">Live test call</h2>
              <p class="outbound__card-sub">Start the call and watch the same STT → LLM → TTS pipeline campaigns use.</p>
            </div>
            <div class="outbound__status">
              <span class="n-dot" :class="{
                'n-dot--success': voiceStatus === 'connected' || voiceStatus === 'listening',
                'n-dot--warning': voiceStatus === 'connecting',
                'n-dot--danger': voiceStatus === 'error',
                'n-dot--muted': voiceStatus === 'idle',
              }"></span>
              <span class="n-mono">{{ voiceStatus }}</span>
              <span v-if="voice?.callStartedAt" class="agent__cost n-mono">
                <Wallet :size="11" /> ₹{{ liveCostRupees }} · {{ liveCostDuration }}
              </span>
            </div>
          </header>

          <div class="outbound__controls">
            <button
              v-if="!isCalling"
              type="button"
              class="n-btn n-btn--brand"
              @click="startVoiceCall('outbound')"
            >
              <PhoneCall :size="14" />
              Start test call
            </button>
            <button v-else type="button" class="n-btn n-btn--danger" @click="endVoiceCall">
              <Square :size="14" />
              End call
            </button>
            <button
              type="button"
              class="n-btn n-btn--ghost"
              :disabled="!voice?.ws"
              @click="sendTextToVoiceAgent"
            >
              <MessageSquare :size="14" />
              Text turn
            </button>
            <input
              v-if="voice?.ws"
              v-model="chatInput"
              type="text"
              class="n-input outbound__chat-input"
              placeholder="Type a reply…"
              @keyup.enter="sendTextToVoiceAgent"
            />
          </div>

          <div v-if="voice?.errorMsg" class="outbound__error">
            <XCircle :size="14" />
            {{ voice.errorMsg }}
          </div>

          <div
            v-if="outboundTesterOutcome"
            class="outbound__outcome"
            :class="`is-${outboundTesterOutcome.outcome}`"
          >
            <div class="outbound__outcome-head">
              <strong>
                <template v-if="outboundTesterOutcome.outcome === 'interested'">
                  Saved as lead<template v-if="outboundTesterOutcome.campaignName"> under "{{ outboundTesterOutcome.campaignName }}"</template>
                </template>
                <template v-else-if="outboundTesterOutcome.outcome === 'partial'">Saved to Uncategorized</template>
                <template v-else>Discarded — not interested</template>
              </strong>
              <button type="button" class="n-btn n-btn--quiet n-btn--sm" @click="outboundTesterOutcome = null">Dismiss</button>
            </div>
            <p v-if="outboundTesterOutcome.reason">{{ outboundTesterOutcome.reason }}</p>
            <button
              v-if="outboundTesterOutcome.leadId"
              type="button"
              class="n-btn n-btn--ghost n-btn--sm"
              @click="
                switchPage('leads');
                activeLeadCampaignTab = outboundTesterOutcome.uncategorized
                  ? UNCATEGORIZED_TAB_ID
                  : (outboundTesterOutcome.campaignId || null);
              "
            >
              View in leads
            </button>
          </div>

          <div v-if="voice?.liveTranscript" class="outbound__live">
            <Radio :size="13" />
            <em>{{ voice.liveTranscript }}</em>
            <span v-if="voice.transcriptLang" class="n-tag n-tag--mono">{{ voice.transcriptLang }}</span>
          </div>

          <div v-if="voice?.firstSentenceMs || voice?.ttsFirstAudioMs || voice?.callId" class="outbound__metrics">
            <span v-if="voice.firstSentenceMs" class="n-tag n-tag--brand n-tag--mono">LLM first sentence · {{ voice.firstSentenceMs }}ms</span>
            <span v-if="voice.ttsFirstAudioMs" class="n-tag n-tag--brand n-tag--mono">TTS first audio · {{ voice.ttsFirstAudioMs }}ms</span>
            <span v-if="voice.callId" class="n-tag n-tag--mono">call · {{ voice.callId.slice(0, 8) }}</span>
          </div>

          <div v-if="!(voice?.turns?.length) && voiceStatus === 'idle'" class="n-empty outbound__call-empty">
            <div class="n-empty__icon"><Mic :size="18" /></div>
            <p class="n-empty__title">Ready when you are</p>
            <p class="n-empty__copy">Set the goal on the left, hit Start, and the agent will open the conversation.</p>
          </div>

          <div v-if="voice?.turns?.length" class="outbound__turns">
            <div v-for="turn in voice.turns" :key="turn.id" class="outbound__turn">
              <div class="outbound__turn-side">
                <span class="n-tag">caller</span>
                <p>{{ turn.query }}</p>
              </div>
              <div class="outbound__turn-side">
                <span class="n-tag n-tag--brand">agent</span>
                <p>{{ turn.answer || turn.sentences.join(' ') }}</p>
                <div class="outbound__turn-meta">
                  <span v-if="turn.latencyMs" class="n-mono">{{ turn.latencyMs }}ms</span>
                  <span v-if="turn.cacheHit" class="n-tag n-tag--success">cache</span>
                </div>
              </div>
            </div>
          </div>
        </article>
      </div>

      <!-- Campaigns -->
      <article v-else class="n-card outbound__campaigns">
        <header class="outbound__list-head">
          <div>
            <strong>Outbound campaigns</strong>
            <p>Each campaign carries a prompt (the agent's knowledge) and the consented leads it can call.</p>
          </div>
          <div class="outbound__list-actions">
            <button
              v-if="isAdmin"
              type="button"
              class="n-btn n-btn--primary n-btn--sm"
              @click="showCampaignCreateForm = !showCampaignCreateForm"
            >
              <Plus :size="13" />
              {{ showCampaignCreateForm ? 'Close' : 'New campaign' }}
            </button>
            <button type="button" class="n-btn n-btn--ghost n-btn--sm" @click="loadCampaigns">
              <RefreshCw :size="13" />
              Refresh
            </button>
          </div>
        </header>

        <div v-if="isAdmin && showCampaignCreateForm" class="outbound__create">
          <div v-if="campaignFormPreset" class="outbound__preset-banner">
            <span>
              <strong>Create &amp; call:</strong>
              {{ campaignFormPreset.callableCount }} callable lead(s) from
              <strong>{{ campaignFormPreset.formName }}</strong> — launches the moment you create.
            </span>
            <button type="button" class="n-btn n-btn--ghost n-btn--sm" @click="cancelCampaignPreset">Cancel</button>
          </div>
          <div class="outbound__create-grid">
            <label class="n-field">
              <span class="n-field__label">Campaign name</span>
              <input v-model="campaignForm.name" type="text" class="n-input" placeholder="Diwali outreach" />
            </label>
            <div class="n-field">
              <span class="n-field__label">Caller ID <span class="n-field__sub">your allotted number — used for all campaigns</span></span>
              <div class="outbound__callerid">
                <span v-if="outboundNumber.number" class="n-mono">{{ outboundNumber.number }}</span>
                <span v-else class="outbound__callerid-none">No outbound number provisioned yet</span>
              </div>
            </div>
            <label class="n-field outbound__col-2">
              <span class="n-field__label">Agent prompt <span class="n-field__sub">the agent's full knowledge: persona, what to pitch, what to know</span></span>
              <textarea v-model="campaignForm.agent_prompt" class="n-textarea" rows="8"></textarea>
            </label>

            <div class="n-field outbound__col-2">
              <span class="n-field__label">Objectives <span class="n-field__sub">pick which side-flows the agent is allowed to drive</span></span>
              <div class="outbound__objectives">
                <label
                  v-for="opt in CAMPAIGN_OBJECTIVE_OPTIONS"
                  :key="opt.value"
                  class="outbound__objective"
                  :class="{ 'is-on': (campaignForm.objectives || []).includes(opt.value) }"
                >
                  <input
                    type="checkbox"
                    :checked="(campaignForm.objectives || []).includes(opt.value)"
                    @change="toggleCampaignObjective(opt.value)"
                  />
                  <div>
                    <strong>{{ opt.label }}</strong>
                    <span>{{ opt.description }}</span>
                  </div>
                </label>
              </div>
              <p v-if="!campaignForm.objectives?.length" class="outbound__warn">
                Pick at least one objective — without one the agent will only chat, never close.
              </p>
            </div>

            <label class="n-field outbound__col-2">
              <span class="n-field__label">Exit conditions</span>
              <textarea v-model="campaignForm.exit_conditions" class="n-textarea" rows="3" placeholder="One condition per line"></textarea>
            </label>
            <label class="n-field">
              <span class="n-field__label">Tone</span>
              <input v-model="campaignForm.tone" type="text" class="n-input" placeholder="warm, direct, respectful" />
            </label>
            <label class="n-field">
              <span class="n-field__label">Silence nudge <span class="n-field__sub">seconds</span></span>
              <input v-model.number="campaignForm.silence_timeout_seconds" type="number" class="n-input" min="2" max="20" step="1" />
            </label>

            <!-- ── Follow-up rules ───────────────────────────────────────
                 Admin sets the fallback retry policy here. Promise-extracted
                 callbacks from memory always take priority over these rules;
                 these only fire when no promise was extracted. -->
            <div v-if="campaignForm.followup_rules" class="n-field outbound__col-2 outbound__followup-rules">
              <div class="outbound__followup-head">
                <span class="n-field__label">
                  Follow-up rules
                  <span class="n-field__sub">when caller doesn't promise a callback time</span>
                </span>
                <label class="outbound__followup-toggle">
                  <input type="checkbox" v-model="campaignForm.followup_rules.enabled" />
                  <span>Enabled</span>
                </label>
              </div>
              <p class="outbound__followup-note">
                Memory-extracted promises ("call me Saturday at 4") always win.
                These rules are the safety net for no-answer / voicemail / failed dispositions.
                Every calculated time is clamped to the call window.
              </p>
              <div class="outbound__followup-grid" :class="{ 'is-off': !campaignForm.followup_rules.enabled }">
                <div class="outbound__followup-row">
                  <span class="outbound__followup-cap">No-answer</span>
                  <label>
                    <input
                      v-model.number="campaignForm.followup_rules.rules.no_answer.delay_minutes"
                      type="number" min="1" max="10080" step="5"
                      class="n-input"
                    />
                    <small>delay (min)</small>
                  </label>
                  <label>
                    <input
                      v-model.number="campaignForm.followup_rules.rules.no_answer.max_attempts"
                      type="number" min="1" max="10"
                      class="n-input"
                    />
                    <small>max attempts</small>
                  </label>
                </div>
                <div class="outbound__followup-row">
                  <span class="outbound__followup-cap">Voicemail</span>
                  <label>
                    <input
                      v-model.number="campaignForm.followup_rules.rules.voicemail.delay_minutes"
                      type="number" min="1" max="10080" step="5"
                      class="n-input"
                    />
                    <small>delay (min)</small>
                  </label>
                  <label>
                    <input
                      v-model.number="campaignForm.followup_rules.rules.voicemail.max_attempts"
                      type="number" min="1" max="10"
                      class="n-input"
                    />
                    <small>max attempts</small>
                  </label>
                </div>
                <div class="outbound__followup-row">
                  <span class="outbound__followup-cap">Failed</span>
                  <label>
                    <input
                      v-model.number="campaignForm.followup_rules.rules.failed.delay_minutes"
                      type="number" min="1" max="10080" step="5"
                      class="n-input"
                    />
                    <small>delay (min)</small>
                  </label>
                  <label>
                    <input
                      v-model.number="campaignForm.followup_rules.rules.failed.max_attempts"
                      type="number" min="1" max="10"
                      class="n-input"
                    />
                    <small>max attempts</small>
                  </label>
                </div>
                <div class="outbound__followup-row">
                  <span class="outbound__followup-cap">Partial</span>
                  <label>
                    <input
                      v-model.number="campaignForm.followup_rules.rules.partial.delay_minutes"
                      type="number" min="1" max="10080" step="5"
                      class="n-input"
                    />
                    <small>delay (min)</small>
                  </label>
                  <label>
                    <input
                      v-model.number="campaignForm.followup_rules.rules.partial.max_attempts"
                      type="number" min="1" max="10"
                      class="n-input"
                    />
                    <small>max attempts</small>
                  </label>
                </div>
              </div>

              <div class="outbound__followup-window">
                <span class="outbound__followup-cap">Call window</span>
                <label>
                  <input
                    v-model="campaignForm.followup_rules.call_window.start_local"
                    type="time"
                    class="n-input"
                  />
                  <small>start</small>
                </label>
                <label>
                  <input
                    v-model="campaignForm.followup_rules.call_window.end_local"
                    type="time"
                    class="n-input"
                  />
                  <small>end</small>
                </label>
                <label>
                  <input
                    v-model.number="campaignForm.followup_rules.max_attempts_per_lead"
                    type="number" min="1" max="10"
                    class="n-input"
                  />
                  <small>max per lead</small>
                </label>
              </div>
              <p class="outbound__followup-foot">
                Default IST: 09:00–19:00, 3 max attempts. Calls outside the window are auto-pushed to the next valid slot.
              </p>
            </div>
          </div>
          <div class="outbound__create-foot">
            <span class="outbound__hint">The prompt IS the agent's source of truth. Write it like you'd brief a new hire.</span>
            <button
              type="button"
              class="n-btn n-btn--primary"
              :disabled="isCreatingCampaign || !campaignForm.name.trim() || !campaignForm.agent_prompt.trim()"
              @click="createCampaign"
            >
              <Plus :size="13" />
              {{ isCreatingCampaign ? (campaignFormPreset ? 'Creating & calling…' : 'Creating…') : (campaignFormPreset ? 'Create & call' : 'Create campaign') }}
            </button>
          </div>
        </div>

        <div v-if="!campaigns.length" class="n-empty outbound__campaigns-empty">
          <div class="n-empty__icon"><PhoneCall :size="18" /></div>
          <p class="n-empty__title">No campaigns yet</p>
          <p class="n-empty__copy">Mint a pitch prompt, then map consented leads to it. Each campaign carries its own agent persona, objectives, and exit conditions.</p>
          <button
            v-if="isAdmin && !showCampaignCreateForm"
            type="button"
            class="n-btn n-btn--brand"
            @click="startCreateCampaign"
          >
            <Plus :size="14" />
            Create your first campaign
          </button>
        </div>

        <ul v-else class="outbound__campaign-list">
          <li v-for="c in campaigns" :key="c.id" class="outbound__campaign" :class="{ 'is-open': expandedCampaignId === c.id }">
            <header
              class="outbound__campaign-head"
              role="button"
              tabindex="0"
              @click="toggleCampaignExpansion(c.id)"
              @keydown.enter.prevent="toggleCampaignExpansion(c.id)"
              @keydown.space.prevent="toggleCampaignExpansion(c.id)"
            >
              <div class="outbound__campaign-title">
                <span class="outbound__campaign-icon"><PhoneCall :size="14" /></span>
                <strong>{{ c.name }}</strong>
                <span class="n-tag" :class="campaignStatusTone(c.status)">{{ c.status }}</span>
              </div>
              <div class="outbound__campaign-meta">
                <span><strong>{{ c.total_count || 0 }}</strong> leads</span>
                <span v-if="c.answered_count"><strong>{{ c.answered_count }}</strong> answered</span>
                <span v-if="c.failed_count"><strong>{{ c.failed_count }}</strong> failed</span>
                <span v-if="c.created_at" class="n-mono">{{ formatRelativeDate(c.created_at) }}</span>
                <component :is="expandedCampaignId === c.id ? ChevronUp : ChevronDown" :size="14" />
              </div>
            </header>

            <div v-if="expandedCampaignId === c.id" class="outbound__campaign-body">
              <section class="outbound__campaign-section">
                <header>
                  <strong>Prompt &amp; knowledge</strong>
                  <small v-if="c.from_number">Calls go out from {{ c.from_number }}</small>
                </header>
                <div v-if="c.agent_config?.agent_prompt" class="outbound__prompt-block">
                  <span class="outbound__prompt-cap">Agent prompt</span>
                  <p>{{ c.agent_config.agent_prompt }}</p>
                </div>
                <div v-if="c.agent_config?.objectives?.length" class="outbound__prompt-block">
                  <span class="outbound__prompt-cap">Objectives</span>
                  <ul>
                    <li v-for="(obj, idx) in c.agent_config.objectives" :key="idx">{{ campaignObjectiveLabel(obj) }}</li>
                  </ul>
                </div>
                <div class="outbound__prompt-tags">
                  <span v-if="c.doc_blob_path" class="n-tag n-tag--brand">
                    <FileText :size="11" /> Knowledge doc indexed
                  </span>
                  <span v-if="c.agent_config?.exit_conditions?.length" class="n-tag">
                    {{ c.agent_config.exit_conditions.length }} exit conditions
                  </span>
                  <span v-if="c.agent_config?.tone" class="n-tag">tone · {{ c.agent_config.tone }}</span>
                </div>
              </section>

              <section class="outbound__campaign-section">
                <header>
                  <strong>Attached leads ({{ (c.contacts || []).length }})</strong>
                  <small>Only consented, callable leads can be attached.</small>
                </header>
                <div v-if="!(c.contacts || []).length" class="outbound__inline-empty">
                  No leads attached yet. Use the picker below to map consented leads.
                </div>
                <ul v-else class="outbound__attached">
                  <li v-for="contact in c.contacts" :key="contact.lead_id || contact.call_link_id">
                    <div>
                      <strong>{{ contact.name || contact.phone }}</strong>
                      <span class="n-mono">{{ contact.phone }}<template v-if="contact.source_provider"> · {{ contact.source_provider }}</template></span>
                    </div>
                    <button
                      v-if="isAdmin && c.status === 'draft' && contact.lead_id"
                      type="button"
                      class="n-btn n-btn--danger n-btn--sm"
                      @click="detachLeadFromCampaign(c.id, contact.lead_id)"
                    >
                      <Trash2 :size="13" /> Remove
                    </button>
                  </li>
                </ul>
              </section>

              <section v-if="isAdmin && c.status !== 'cancelled'" class="outbound__campaign-section">
                <header>
                  <strong>Attach more leads</strong>
                  <small>{{ callableLeads.length }} callable in inventory{{ c.status !== 'draft' ? ' · Relaunch to call new ones' : '' }}</small>
                </header>
                <div v-if="!callableLeads.length" class="outbound__inline-empty">
                  No consented leads imported yet.
                </div>
                <div v-else class="outbound__picker">
                  <article
                    v-for="lead in callableLeads.filter((l) => !(c.contacts || []).some((ct) => ct.lead_id === l.id))"
                    :key="lead.id"
                    class="outbound__picker-row"
                    :class="{ 'is-on': campaignAttachLeadIds.includes(lead.id) }"
                    role="button"
                    tabindex="0"
                    @click="toggleCampaignAttachLead(lead.id)"
                    @keydown.enter.prevent="toggleCampaignAttachLead(lead.id)"
                    @keydown.space.prevent="toggleCampaignAttachLead(lead.id)"
                  >
                    <div>
                      <strong>{{ lead.name || lead.phone_e164 || 'Unnamed lead' }}</strong>
                      <span class="n-mono">{{ lead.source_provider }} · {{ lead.phone_e164 || 'no phone' }}</span>
                    </div>
                    <span class="n-tag" :class="{ 'n-tag--brand': campaignAttachLeadIds.includes(lead.id) }">
                      {{ campaignAttachLeadIds.includes(lead.id) ? 'Selected' : 'Pick' }}
                    </span>
                  </article>
                </div>
                <div class="outbound__picker-foot">
                  <button
                    type="button"
                    class="n-btn n-btn--primary"
                    :disabled="!campaignAttachLeadIds.length || campaignAttachInFlight === c.id"
                    @click="submitCampaignAttachLeads(c.id)"
                  >
                    <UserPlus :size="13" />
                    {{ campaignAttachInFlight === c.id ? 'Attaching…' : `Attach ${campaignAttachLeadIds.length} lead${campaignAttachLeadIds.length === 1 ? '' : 's'}` }}
                  </button>
                </div>
              </section>

              <section v-if="isAdmin" class="outbound__campaign-actions">
                <button type="button" class="n-btn n-btn--ghost n-btn--sm" @click="testCampaign(c.id)">
                  <Mic :size="13" /> Test
                </button>
                <button
                  v-if="c.status === 'draft'"
                  type="button"
                  class="n-btn n-btn--brand n-btn--sm"
                  :disabled="isLaunchingCampaign === c.id || !(c.contacts || []).length || !outboundNumber.calling_enabled || !outboundNumber.number"
                  :title="!outboundNumber.calling_enabled ? 'Upgrade to Inbound + Outbound to launch calls' : (!outboundNumber.number ? 'No outbound number provisioned yet' : '')"
                  @click="launchCampaign(c.id)"
                >
                  <Send :size="13" />
                  {{ isLaunchingCampaign === c.id ? 'Launching…' : 'Launch' }}
                </button>
                <button
                  v-if="(c.status === 'running' || c.status === 'completed') && (c.pending_to_dial || 0) > 0"
                  type="button"
                  class="n-btn n-btn--brand n-btn--sm"
                  :disabled="isLaunchingCampaign === c.id || !outboundNumber.calling_enabled || !outboundNumber.number"
                  :title="!outboundNumber.calling_enabled ? 'Upgrade to Inbound + Outbound to call' : (!outboundNumber.number ? 'No outbound number provisioned yet' : `Call ${c.pending_to_dial} newly-added lead(s)`)"
                  @click="launchCampaign(c.id)"
                >
                  <Send :size="13" />
                  {{ isLaunchingCampaign === c.id ? 'Relaunching…' : `Relaunch (${c.pending_to_dial})` }}
                </button>
                <button
                  type="button"
                  class="n-btn n-btn--ghost n-btn--sm outbound__followup-switch"
                  :class="{ 'is-off': !c.followup_enabled }"
                  :disabled="isTogglingFollowup === c.id"
                  :title="c.followup_enabled ? 'Follow-up agent is ON — click to turn off' : 'Follow-up agent is OFF — click to turn on'"
                  @click="setCampaignFollowup(c.id, !c.followup_enabled)"
                >
                  {{ isTogglingFollowup === c.id ? 'Saving…' : (c.followup_enabled ? '● Follow-up: On' : '○ Follow-up: Off') }}
                </button>
                <button
                  v-if="c.status === 'draft' || c.status === 'running'"
                  type="button"
                  class="n-btn n-btn--ghost n-btn--sm"
                  @click="cancelCampaign(c.id)"
                >
                  <XCircle :size="13" />
                  Cancel
                </button>
                <button
                  v-if="c.status !== 'running'"
                  type="button"
                  class="n-btn n-btn--danger n-btn--sm outbound__campaign-remove"
                  @click="removeCampaign(c.id, c.name)"
                  :title="c.status === 'draft' ? 'Delete this draft' : 'Delete this campaign permanently'"
                >
                  <Trash2 :size="13" />
                  Remove
                </button>
              </section>
            </div>
          </li>
        </ul>
      </article>
    </section>
  </div>
</template>

<style scoped>
/* ─── Head actions + Create campaign CTA ──────── */
.outbound__head-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 16px;
  margin-top: 24px;
}

.outbound__head-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.outbound__cta {
  appearance: none;
  position: relative;
  background: var(--n-text);
  color: var(--n-bg);
  border: 2px solid var(--n-text);
  border-radius: 0;
  padding: 9px 16px 9px 9px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 10px;
  text-align: left;
  font-family: var(--n-font-body);
  overflow: hidden;
  box-shadow: 4px 4px 0 var(--n-brand);
  transition:
    transform var(--n-t-fast) var(--n-ease),
    box-shadow var(--n-t-fast) var(--n-ease);
}
.outbound__cta:hover {
  transform: translate(-2px, -2px);
  box-shadow: 6px 6px 0 var(--n-brand);
}
.outbound__cta:active { 
  transform: translate(0, 0); 
  box-shadow: 0 0 0 var(--n-brand); 
}

.outbound__cta-icon {
  width: 30px;
  height: 30px;
  border-radius: 0;
  background: var(--n-bg);
  color: var(--n-text);
  display: grid;
  place-items: center;
  flex-shrink: 0;
  border: 2px solid var(--n-text);
}
.outbound__cta-label { display: grid; gap: 0; line-height: 1.15; }
.outbound__cta-label strong {
  font-family: var(--n-font-display);
  font-size: 13.5px;
  font-weight: 600;
  letter-spacing: -0.005em;
  color: var(--n-bg);
}
.outbound__cta-label small {
  font-size: 10.5px;
  color: var(--n-bg);
  opacity: 0.8;
  font-weight: 500;
  letter-spacing: -0.005em;
}
.outbound__cta-glow {
  display: none;
}

.outbound__cta--ghost {
  background: var(--n-bg);
  color: var(--n-text);
  box-shadow: 4px 4px 0 var(--n-text);
}
.outbound__cta--ghost:hover {
  box-shadow: 6px 6px 0 var(--n-text);
}
.outbound__cta--ghost .outbound__cta-icon {
  background: var(--n-text);
  color: var(--n-bg);
}
.outbound__cta--ghost .outbound__cta-label strong {
  color: var(--n-text);
}
.outbound__cta--ghost .outbound__cta-label small {
  color: var(--n-text);
}

.outbound__icon-btn {
  appearance: none;
  background: var(--n-bg);
  border: 2px solid var(--n-text);
  border-radius: 0;
  width: 36px;
  height: 36px;
  display: grid;
  place-items: center;
  color: var(--n-text);
  cursor: pointer;
  box-shadow: 2px 2px 0 var(--n-text);
  transition: transform var(--n-t-fast) var(--n-ease), box-shadow var(--n-t-fast) var(--n-ease);
}
.outbound__icon-btn:hover:not(:disabled) {
  transform: translate(-2px, -2px);
  box-shadow: 4px 4px 0 var(--n-text);
}
.outbound__icon-btn:active:not(:disabled) {
  transform: translate(0, 0);
  box-shadow: 0 0 0 var(--n-text);
}
.outbound__icon-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

/* Reused mini-helper */
.agent__cost {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 9px;
  background: var(--n-surface);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-pill);
  color: var(--n-text);
  font-size: 11.5px;
}



.outbound__card-head {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 14px;
}
.outbound__card-title {
  font-family: var(--n-font-display);
  font-size: 16px;
  font-weight: 600;
  letter-spacing: -0.01em;
  margin: 0;
  color: var(--n-text);
}
.outbound__card-sub {
  margin: 4px 0 0;
  font-size: 12.5px;
  color: var(--n-text-3);
  line-height: 1.45;
}


.outbound__nokvo-form { display: grid; gap: 16px; }
.outbound__form-body { display: grid; gap: 12px; }
.outbound__custom-fields { display: grid; gap: 8px; }
.outbound__custom-fields-head {
  display: flex; align-items: center; justify-content: space-between;
}
.outbound__custom-fields-head strong { font-size: 12.5px; font-weight: 600; color: var(--n-text); }
.outbound__inline-empty {
  font-size: 12.5px; color: var(--n-text-3);
  padding: 10px 12px;
  border: 1px dashed var(--n-border);
  border-radius: var(--n-r-md);
  background: var(--n-surface);
}
.outbound__custom-field {
  display: grid;
  grid-template-columns: 2fr 1fr auto auto;
  gap: 6px;
  align-items: center;
}
.outbound__custom-required {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11.5px;
  color: var(--n-text-3);
  font-family: var(--n-font-mono);
  white-space: nowrap;
}

.outbound__form-foot { display: flex; justify-content: flex-end; }
.outbound__form-link {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  background: var(--n-brand-soft);
  border: 1px solid rgba(99, 102, 241, 0.16);
  border-radius: var(--n-r-md);
  font-size: 12.5px;
}
.outbound__form-link-cap {
  font-family: var(--n-font-mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--n-brand-ink);
}
.outbound__form-link code {
  flex: 1;
  font-family: var(--n-font-mono);
  font-size: 12px;
  color: var(--n-brand-ink);
  background: transparent;
  padding: 0;
  min-width: 0;
}

/* ─── Tabs / table ─────────────────────────────── */
.outbound__tabnav {
  display: flex;
  align-items: stretch;
  background: var(--n-bg);
  border: 2px solid var(--n-text);
  border-radius: 0;
  box-shadow: 4px 4px 0 var(--n-brand);
}
.outbound__tab-btn {
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
  transition: background var(--n-t-fast) var(--n-ease), color var(--n-t-fast) var(--n-ease);
}
.outbound__tab-btn:last-child {
  border-right: none;
}
.outbound__tab-btn:hover {
  background: var(--n-surface);
}
.outbound__tab-btn.is-active {
  background: var(--n-text);
  color: var(--n-bg);
}
.outbound__list-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 14px;
  padding: 16px 20px;
  border-bottom: 2px solid var(--n-text);
}
.outbound__list-head strong {
  font-family: var(--n-font-display);
  font-size: 15px;
  font-weight: 600;
  color: var(--n-text);
  letter-spacing: -0.005em;
  display: block;
}
.outbound__list-head p {
  margin: 2px 0 0;
  font-size: 12.5px;
  color: var(--n-text-3);
}
.outbound__list-actions { display: flex; gap: 8px; }

.outbound__forms { padding: 0; }
.outbound__empty {
  padding: 36px 20px;
  text-align: center;
  font-size: 13.5px;
  color: var(--n-text-3);
}

/* Leads-by-form filter (Option A: sort leads by originating form/project) */
.outbound__form-filter {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 12px 20px;
  border-bottom: 1px solid var(--n-border-subtle);
}
.outbound__form-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border: 2px solid var(--n-text);
  border-radius: 0;
  background: transparent;
  font-size: 12.5px;
  color: var(--n-text);
  cursor: pointer;
  transition: transform 0.1s, box-shadow 0.1s;
}
.outbound__form-chip:hover { 
  transform: translate(-1px, -1px);
  box-shadow: 2px 2px 0 var(--n-brand);
}
.outbound__form-chip.is-active {
  background: var(--n-text);
  color: var(--n-bg);
  font-weight: 600;
  box-shadow: 2px 2px 0 var(--n-brand);
  transform: translate(-1px, -1px);
}
.outbound__form-chip-count {
  font-family: var(--n-font-mono, monospace);
  font-size: 11px;
  opacity: 0.75;
}
.outbound__lead-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 10px 20px 4px;
}
.outbound__callbtn { margin-left: auto; display: inline-flex; align-items: center; gap: 6px; }
.outbound__upgrade-hint {
  margin-left: auto;
  font-size: 12px;
  color: var(--n-text-3);
}

/* Read-only allotted caller ID in the create form */
.outbound__callerid {
  display: flex;
  align-items: center;
  min-height: 38px;
  padding: 8px 12px;
  border: 1px dashed var(--n-border-subtle);
  border-radius: 8px;
  background: var(--n-surface-2, rgba(127, 127, 127, 0.04));
}
.outbound__callerid-none { font-size: 12.5px; color: var(--n-text-3); }

/* "Create & call this form" preset banner */
.outbound__preset-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  padding: 12px 16px;
  border: 2px solid var(--n-text);
  border-radius: 0;
  background: var(--n-surface);
  box-shadow: 4px 4px 0 var(--n-brand);
  font-size: 13px;
  color: var(--n-text);
}

.outbound__form-list { list-style: none; margin: 0; padding: 4px 0; }
.outbound__form-row {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 12px;
  align-items: flex-start;
  padding: 14px 20px;
  border-bottom: 1px solid var(--n-border-subtle);
}
.outbound__form-row:last-child { border-bottom: 0; }
.outbound__form-icon {
  width: 32px;
  height: 32px;
  border-radius: 9px;
  background: var(--n-surface-2);
  color: var(--n-text-2);
  display: grid;
  place-items: center;
}
.outbound__form-body-row { display: grid; gap: 3px; min-width: 0; }
.outbound__form-body-row strong { font-size: 13.5px; font-weight: 600; color: var(--n-text); }
.outbound__form-body-row span {
  font-family: var(--n-font-mono);
  font-size: 11.5px;
  color: var(--n-text-3);
}
.outbound__form-url {
  font-family: var(--n-font-mono);
  font-size: 11px;
  color: var(--n-text-3);
  margin: 4px 0 0;
  word-break: break-all;
}

/* Leads list */
.outbound__lead-list { list-style: none; margin: 0; padding: 4px 0; }
.outbound__lead-row {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 12px;
  align-items: center;
  padding: 12px 20px;
  border-bottom: 2px solid var(--n-text);
  cursor: pointer;
  transition: transform var(--n-t-fast) var(--n-ease);
}
.outbound__lead-row:last-child { border-bottom: 0; }
.outbound__lead-row:hover { background: var(--n-surface); transform: translateX(2px); }
.outbound__lead-row.is-on { background: var(--n-surface); box-shadow: inset 4px 0 0 var(--n-brand); }
.outbound__lead-row.is-off { opacity: 0.6; cursor: not-allowed; }
.outbound__lead-icon {
  width: 30px;
  height: 30px;
  border-radius: 0;
  background: var(--n-surface-2);
  color: var(--n-text);
  display: grid;
  place-items: center;
  border: 2px solid var(--n-text);
}
.outbound__lead-row strong { font-size: 13.5px; font-weight: 600; color: var(--n-text); display: block; }
.outbound__lead-row span {
  display: block;
  font-family: var(--n-font-mono);
  font-size: 11px;
  color: var(--n-text-3);
}
.outbound__lead-foot { display: flex; align-items: center; gap: 8px; }
.outbound__lead-call {
  display: inline-flex; align-items: center; gap: 5px;
  font-family: var(--n-font-mono);
  font-size: 11.5px;
  color: var(--n-brand);
  text-decoration: none;
}
.outbound__lead-call:hover { text-decoration: underline; }

/* ─── Tester ──────────────────────────────────── */
.outbound__tester-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  align-items: start;
}
@media (max-width: 1100px) { .outbound__tester-grid { grid-template-columns: 1fr; } }

.outbound__tester-form, .outbound__tester-call { display: grid; gap: 16px; }
.outbound__tester-body { display: grid; gap: 12px; }
.outbound__tester-grid-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
@media (max-width: 700px) { .outbound__tester-grid-row { grid-template-columns: 1fr; } }
.outbound__campaign-picker {
  padding: 12px;
  background: var(--n-surface);
  border: 1px solid var(--n-border-subtle);
  border-radius: var(--n-r-md);
}

.outbound__status {
  display: inline-flex; align-items: center; gap: 10px;
  font-size: 12.5px;
  color: var(--n-text-3);
}
.outbound__controls {
  display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
}
.outbound__chat-input { flex: 1 1 200px; min-width: 200px; }

.outbound__error {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 14px;
  background: var(--n-danger-soft);
  border: 1px solid rgba(220, 38, 38, 0.18);
  border-radius: var(--n-r-md);
  color: var(--n-danger-2);
  font-size: 13px;
}

.outbound__outcome {
  padding: 14px;
  border-radius: 0;
  border: 2px solid var(--n-text);
  background: var(--n-surface);
  box-shadow: 4px 4px 0 var(--n-brand);
  display: grid;
  gap: 8px;
}
.outbound__outcome.is-interested { background: var(--n-success-soft); border-color: rgba(5, 150, 105, 0.2); }
.outbound__outcome.is-partial { background: var(--n-warning-soft); border-color: rgba(217, 119, 6, 0.2); }
.outbound__outcome.is-not_interested { background: var(--n-danger-soft); border-color: rgba(220, 38, 38, 0.2); }
.outbound__outcome-head {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
}
.outbound__outcome-head strong {
  font-family: var(--n-font-display);
  font-size: 14px;
  font-weight: 600;
  color: var(--n-text);
}
.outbound__outcome p {
  margin: 0;
  font-size: 12.5px;
  color: var(--n-text-2);
}

.outbound__live {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 12px;
  background: var(--n-surface);
  border: 2px solid var(--n-text);
  border-radius: 0;
  box-shadow: 2px 2px 0 var(--n-text);
  font-size: 13px;
  color: var(--n-text);
}
.outbound__live em { font-style: italic; flex: 1; }
.outbound__metrics { display: flex; gap: 6px; flex-wrap: wrap; }
.outbound__call-empty { margin: 0; }

.outbound__turns { display: grid; gap: 12px; }
.outbound__turn {
  display: grid;
  gap: 8px;
  padding: 12px;
  background: var(--n-surface);
  border: 2px solid var(--n-text);
  border-radius: 0;
  box-shadow: 2px 2px 0 var(--n-text);
}
.outbound__turn-side { display: grid; gap: 4px; }
.outbound__turn-side p { margin: 0; font-size: 13px; color: var(--n-text); line-height: 1.5; }
.outbound__turn-meta { display: flex; gap: 6px; flex-wrap: wrap; color: var(--n-text-3); font-size: 11.5px; }

/* ─── Campaigns ───────────────────────────────── */
.outbound__campaigns { padding: 0; }
.outbound__campaigns-empty { margin: 24px 20px; }

.outbound__create {
  padding: 20px;
  border-bottom: 1px solid var(--n-border-subtle);
  background: var(--n-surface);
}
.outbound__create-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.outbound__col-2 { grid-column: 1 / -1; }
.outbound__objectives {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 8px;
}
.outbound__objective {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 10px;
  align-items: flex-start;
  padding: 12px;
  background: var(--n-bg);
  border: 2px solid var(--n-text);
  border-radius: 0;
  cursor: pointer;
  transition: transform var(--n-t-fast) var(--n-ease), box-shadow var(--n-t-fast) var(--n-ease);
}
.outbound__objective:hover { transform: translate(-2px, -2px); box-shadow: 4px 4px 0 var(--n-text); }
.outbound__objective.is-on { background: var(--n-surface); box-shadow: inset 4px 0 0 var(--n-brand); transform: translate(0, 0); }
.outbound__objective strong { font-size: 13px; color: var(--n-text); display: block; font-weight: 600; }
.outbound__objective.is-on strong { color: var(--n-brand-ink); }
.outbound__objective span { font-size: 11.5px; color: var(--n-text-3); display: block; margin-top: 2px; line-height: 1.4; }
.outbound__warn {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--n-danger);
  font-style: italic;
}

.outbound__create-foot {
  display: flex; align-items: center; justify-content: space-between;
  gap: 14px;
  margin-top: 14px;
}
.outbound__hint { font-size: 12.5px; color: var(--n-text-3); font-style: italic; }

.outbound__campaign-list { list-style: none; margin: 0; padding: 0; }
.outbound__campaign {
  border-bottom: 1px solid var(--n-border-subtle);
}
.outbound__campaign:last-child { border-bottom: 0; }
.outbound__campaign-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 20px;
  cursor: pointer;
  transition: background var(--n-t-fast) var(--n-ease);
  gap: 12px;
  border-bottom: 2px solid var(--n-text);
}
.outbound__campaign-head:hover { background: var(--n-surface); }
.outbound__campaign.is-open .outbound__campaign-head { background: var(--n-surface); }
.outbound__campaign-title { display: flex; align-items: center; gap: 10px; }
.outbound__campaign-icon {
  width: 28px;
  height: 28px;
  border-radius: 0;
  background: var(--n-surface);
  color: var(--n-text);
  border: 2px solid var(--n-text);
  display: grid;
  place-items: center;
}
.outbound__campaign-title strong {
  font-family: var(--n-font-display);
  font-size: 14.5px;
  font-weight: 600;
  color: var(--n-text);
  letter-spacing: -0.01em;
}
.outbound__campaign-meta {
  display: flex; align-items: center; gap: 14px;
  font-size: 12px;
  color: var(--n-text-3);
}
.outbound__campaign-meta strong { font-weight: 600; color: var(--n-text); font-feature-settings: 'tnum' 1; }

.outbound__campaign-body {
  padding: 8px 20px 20px;
  display: grid;
  gap: 16px;
  background: var(--n-surface);
}
.outbound__campaign-section { display: grid; gap: 8px; }
.outbound__campaign-section header { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.outbound__campaign-section header strong {
  font-family: var(--n-font-display);
  font-size: 13px;
  font-weight: 600;
  color: var(--n-text);
}
.outbound__campaign-section header small { font-size: 11.5px; color: var(--n-text-3); }

.outbound__prompt-block {
  padding: 10px 12px;
  background: var(--n-bg);
  border: 2px solid var(--n-text);
  border-radius: 0;
  box-shadow: 2px 2px 0 var(--n-text);
}
.outbound__prompt-cap {
  font-family: var(--n-font-mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--n-text-4);
  display: block;
  margin-bottom: 4px;
}
.outbound__prompt-block p {
  margin: 0; font-size: 13px;
  color: var(--n-text-2);
  line-height: 1.5; white-space: pre-wrap;
}
.outbound__prompt-block ul { margin: 0; padding-left: 18px; font-size: 13px; color: var(--n-text-2); }
.outbound__prompt-tags { display: flex; gap: 6px; flex-wrap: wrap; }

.outbound__followup-switch { font-weight: 600; }
.outbound__followup-switch.is-off { opacity: 0.6; }
.outbound__attached { list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }
.outbound__attached li {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  padding: 10px 12px;
  background: var(--n-bg);
  border: 2px solid var(--n-text);
  border-radius: 0;
  box-shadow: 2px 2px 0 var(--n-text);
}
.outbound__attached strong { font-size: 13px; color: var(--n-text); display: block; font-weight: 600; }
.outbound__attached span { font-family: var(--n-font-mono); font-size: 11px; color: var(--n-text-3); }

.outbound__picker {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 6px;
  max-height: 240px;
  overflow-y: auto;
  padding: 4px;
  background: var(--n-bg);
  border: 1px solid var(--n-border-subtle);
  border-radius: var(--n-r-md);
}
.outbound__picker-row {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 8px 10px;
  border-radius: var(--n-r-md);
  cursor: pointer;
  transition: background var(--n-t-fast) var(--n-ease);
}
.outbound__picker-row:hover { background: var(--n-surface); }
.outbound__picker-row.is-on { background: var(--n-brand-soft); }
.outbound__picker-row strong { font-size: 12.5px; color: var(--n-text); display: block; font-weight: 600; }
.outbound__picker-row span { font-family: var(--n-font-mono); font-size: 11px; color: var(--n-text-3); display: block; }
.outbound__picker-foot { display: flex; justify-content: flex-end; }

.outbound__campaign-actions {
  display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end;
  padding-top: 8px;
  border-top: 1px solid var(--n-border-subtle);
}

/* ─── Follow-up rules editor (campaign create form) ─────────────── */
.outbound__followup-rules {
  padding: 16px;
  background: var(--n-surface);
  border: 1px solid var(--n-border-subtle);
  border-radius: var(--n-r-lg);
  margin-top: 4px;
  display: grid;
  gap: 12px;
}
.outbound__followup-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
}
.outbound__followup-toggle {
  display: inline-flex; align-items: center; gap: 6px;
  font-family: var(--n-font-mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--n-text-2);
  cursor: pointer;
}
.outbound__followup-note {
  margin: 0;
  font-size: 12px;
  color: var(--n-text-3);
  line-height: 1.5;
}
.outbound__followup-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
  transition: opacity var(--n-t-base) var(--n-ease);
}
.outbound__followup-grid.is-off { opacity: 0.5; pointer-events: none; }
.outbound__followup-row {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 6px;
  align-items: center;
  padding: 8px 10px;
  background: var(--n-bg);
  border: 1px solid var(--n-border-subtle);
  border-radius: var(--n-r-md);
}
.outbound__followup-cap {
  font-family: var(--n-font-mono);
  font-size: 10.5px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--n-text-3);
  font-weight: 600;
}
.outbound__followup-row label,
.outbound__followup-window label {
  display: grid;
  gap: 2px;
}
.outbound__followup-row label small,
.outbound__followup-window label small {
  font-size: 10px;
  color: var(--n-text-4);
  font-family: var(--n-font-mono);
  letter-spacing: 0.06em;
}
.outbound__followup-row input,
.outbound__followup-window input {
  padding: 6px 8px;
  font-size: 12.5px;
}
.outbound__followup-window {
  display: grid;
  grid-template-columns: auto 1fr 1fr 1fr;
  gap: 10px;
  align-items: center;
  padding: 10px 12px;
  background: var(--n-bg);
  border: 1px solid var(--n-border-subtle);
  border-radius: var(--n-r-md);
}
.outbound__followup-foot {
  margin: 0;
  font-size: 11.5px;
  color: var(--n-text-4);
  font-style: italic;
}
@media (max-width: 720px) {
  .outbound__followup-grid { grid-template-columns: 1fr; }
  .outbound__followup-window { grid-template-columns: 1fr 1fr; }
}
</style>
