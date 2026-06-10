<script setup>
import { computed } from 'vue';
import {
  Activity,
  Bot,
  Brain,
  CheckCircle2,
  Database,
  Edit2,
  Layers,
  Link as LinkIcon,
  MessageSquare,
  Mic,
  PhoneCall,
  Plus,
  Radio,
  Send,
  Square,
  Volume2,
  Wallet,
  XCircle,
} from 'lucide-vue-next';
import { useDashboardState } from '../composables/useDashboardState.js';

const {
  profileAgent,
  renameDraft,
  isRenamingAgent,
  renameAgent,
  runtimeStatus,
  currentOrganization,
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
  agents,
  activeAgent,
  businessTypeLabel,
  onboardingV2Enabled,
  isAdmin,
  phoneLinkInput,
  isSavingPhoneLink,
  savePhoneLink,
  phoneLink,
  emailDrafts,
  loadEmailDrafts,
  discardDraft,
} = useDashboardState();

const pipeline = computed(() => [
  { key: 'stt', label: 'STT', icon: Mic, model: runtimeStatus?.value?.stt?.model || 'Sarvam saaras:v3', meta: runtimeStatus?.value?.stt?.status || 'ready' },
  { key: 'llm', label: 'LLM', icon: Brain, model: runtimeStatus?.value?.llm?.model || 'gpt-4.1-mini', meta: 'South India' },
  { key: 'tts', label: 'TTS', icon: Volume2, model: runtimeStatus?.value?.tts?.model || 'Sarvam bulbul:v3', meta: runtimeStatus?.value?.tts?.status || 'ready' },
  { key: 'emb', label: 'Embeddings', icon: Layers, model: 'text-embedding-3-small', meta: '1536 · cosine' },
  { key: 'vec', label: 'Vector store', icon: Database, model: 'Qdrant', meta: runtimeStatus?.value?.knowledge_scope?.replace?.(/_/g, ' ') || 'tenant scope' },
  { key: 'cache', label: 'Cache', icon: Activity, model: runtimeStatus?.value?.optimization?.semantic_cache_enabled ? 'Redis · on' : 'off', meta: `top-k ${runtimeStatus?.value?.optimization?.qdrant_top_k || 3}` },
]);

const voiceStatus = computed(() => voice?.value?.status || 'idle');
const isCalling = computed(() => !['idle', 'error'].includes(voiceStatus.value));
</script>

<template>
  <div class="n-page agent">
    <!-- Hero -->
    <header class="agent__hero n-rise">
      <div class="agent__hero-top">
        <div>
          <span class="n-page-head__eyebrow">Inbound agent</span>
          <h1 class="n-page-head__title agent__hero-title">{{ profileAgent?.name || 'Your inbound agent' }}</h1>
          <p class="n-page-head__sub">
            {{ profileAgent?.description || 'Build, test, and run a live voice agent: STT → retrieval → LLM → TTS on tenant-isolated infrastructure.' }}
          </p>
          <div v-if="profileAgent" class="agent__rename">
            <span class="agent__rename-icon">
              <Edit2 :size="12" />
            </span>
            <input
              v-model="renameDraft"
              type="text"
              class="agent__rename-input"
              placeholder="Agent name"
              @keyup.enter="renameAgent"
            />
            <button
              type="button"
              class="n-btn n-btn--primary n-btn--sm"
              :disabled="isRenamingAgent || !renameDraft.trim() || renameDraft.trim() === profileAgent.name"
              @click="renameAgent"
            >
              {{ isRenamingAgent ? 'Saving…' : 'Save' }}
            </button>
          </div>
        </div>
      </div>

      <div class="agent__pipeline">
        <div
          v-for="(c, i) in pipeline"
          :key="c.key"
          class="agent__chip"
          :style="{ animationDelay: `${80 + i * 40}ms` }"
        >
          <span class="agent__chip-icon"><component :is="c.icon" :size="14" /></span>
          <div class="agent__chip-body">
            <span class="agent__chip-label">{{ c.label }}</span>
            <strong class="agent__chip-model n-truncate">{{ c.model }}</strong>
            <span class="agent__chip-meta n-truncate">{{ c.meta }}</span>
          </div>
        </div>
      </div>

      <div v-if="currentOrganization?.status === 'pending_approval'" class="agent__pending">
        <span class="n-dot n-dot--warning n-dot--pulse"></span>
        <span>Your organization is awaiting Nokvo activation. Voice testing and CRUD work today; outbound calling unlocks after approval.</span>
      </div>
    </header>

    <!-- Voice tester -->
    <section class="n-section n-rise" data-delay="1">
      <header class="n-section__head">
        <div>
          <h2 class="n-section__title">Voice tester</h2>
          <p class="n-section__sub">Talk to your agent live in the browser. Full pipeline: mic → Sarvam STT → RAG → LLM → Sarvam TTS.</p>
        </div>
        <div class="agent__status">
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

      <article class="n-card agent__tester">
        <div class="agent__controls">
          <select v-model="voice.language" class="n-select agent__lang" :disabled="isCalling">
            <option v-for="opt in voiceLanguageOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
          </select>
          <button
            v-if="!isCalling"
            type="button"
            class="n-btn n-btn--brand"
            @click="startVoiceCall"
          >
            <PhoneCall :size="14" />
            Start call
          </button>
          <button
            v-else
            type="button"
            class="n-btn n-btn--danger"
            @click="endVoiceCall"
          >
            <Square :size="14" />
            End call
          </button>
          <button
            type="button"
            class="n-btn n-btn--ghost"
            :disabled="!voice?.ws"
            @click="sendTextToVoiceAgent"
            title="Send a text-only query through the same pipeline"
          >
            <MessageSquare :size="14" />
            Text turn
          </button>
          <input
            v-if="voice?.ws"
            v-model="chatInput"
            type="text"
            class="n-input agent__text-input"
            placeholder="Type a query to send through the pipeline"
            @keyup.enter="sendTextToVoiceAgent"
          />
        </div>

        <div v-if="voice?.errorMsg" class="agent__error">
          <XCircle :size="14" />
          {{ voice.errorMsg }}
        </div>

        <div class="agent__telemetry" v-if="voice?.liveTranscript || voice?.firstSentenceMs || voice?.ttsFirstAudioMs || voice?.callId">
          <div v-if="voice?.liveTranscript" class="agent__live">
            <Radio :size="13" />
            <em>{{ voice.liveTranscript }}</em>
            <span v-if="voice.transcriptLang" class="n-tag n-tag--mono">{{ voice.transcriptLang }}</span>
          </div>
          <div class="agent__metrics">
            <span v-if="voice?.firstSentenceMs" class="n-tag n-tag--brand n-tag--mono">LLM first sentence · {{ voice.firstSentenceMs }}ms</span>
            <span v-if="voice?.ttsFirstAudioMs" class="n-tag n-tag--brand n-tag--mono">TTS first audio · {{ voice.ttsFirstAudioMs }}ms</span>
            <span v-if="voice?.callId" class="n-tag n-tag--mono">call · {{ voice.callId.slice(0, 8) }}</span>
          </div>
        </div>

        <div v-if="!(voice?.turns?.length) && voiceStatus === 'idle'" class="n-empty agent__empty">
          <div class="n-empty__icon"><Mic :size="18" /></div>
          <p class="n-empty__title">Start a call to test the pipeline</p>
          <p class="n-empty__copy">Pick a language, hit Start call, grant mic access, and speak. The agent talks back in real time.</p>
        </div>

        <div v-if="voice?.turns?.length" class="agent__transcript">
          <div v-for="turn in voice.turns" :key="turn.id" class="agent__turn">
            <div class="agent__turn-side">
              <span class="n-tag">caller</span>
              <p>{{ turn.query }}</p>
            </div>
            <div class="agent__turn-side agent__turn-side--agent">
              <span class="n-tag n-tag--brand">agent</span>
              <p>{{ turn.sentences.join(' ') || turn.answer || '…' }}</p>
              <div class="agent__turn-meta">
                <span v-if="turn.latencyMs" class="n-mono">{{ turn.latencyMs }}ms</span>
                <span v-if="turn.cacheHit" class="n-tag n-tag--success">cache</span>
                <span v-if="turn.citations?.length" class="n-tag">{{ turn.citations.length }} citation{{ turn.citations.length === 1 ? '' : 's' }}</span>
              </div>
            </div>
          </div>
        </div>
      </article>
    </section>

    <!-- Phone link -->
    <section class="n-section n-rise" data-delay="2">
      <header class="n-section__head">
        <div>
          <h2 class="n-section__title">Phone number (call forwarding)</h2>
          <p class="n-section__sub">Forward your business number to your assigned Plivo number — incoming calls then reach your agent automatically.</p>
        </div>
        <span v-if="phoneLink?.number_status === 'active'" class="n-tag n-tag--success">
          <span class="n-dot n-dot--success"></span> active
        </span>
        <span v-else class="n-tag">provisioning</span>
      </header>

      <article class="n-card agent__phone">
        <div class="agent__phone-urls">
          <div class="agent__phone-url-row">
            <div>
              <span class="agent__phone-cap">Forward your calls to this number</span>
              <strong class="n-mono">{{ phoneLink?.plivo_number || 'Provisioning — awaiting carrier verification' }}</strong>
            </div>
            <span class="n-tag n-tag--mono">PLIVO</span>
          </div>
        </div>
        <p v-if="phoneLink?.forwarding_instructions" class="agent__phone-hint">{{ phoneLink.forwarding_instructions }}</p>

        <label class="n-field agent__phone-field">
          <span class="n-field__label">Your number <span class="n-field__sub">the line you'll forward from</span></span>
          <input v-model="phoneLinkInput" type="text" class="n-input" placeholder="+91 98765 43210" :disabled="!isAdmin" />
        </label>
        <div v-if="isAdmin" class="agent__phone-actions">
          <button type="button" class="n-btn n-btn--primary" :disabled="isSavingPhoneLink" @click="savePhoneLink">
            <CheckCircle2 :size="14" />
            {{ isSavingPhoneLink ? 'Saving…' : 'Save number' }}
          </button>
        </div>
      </article>
    </section>

    <!-- Agent CRUD (only when v1 onboarding) -->
    <section v-if="!onboardingV2Enabled" class="n-section n-rise" data-delay="3">
      <header class="n-section__head">
        <div>
          <h2 class="n-section__title">Agents</h2>
          <p class="n-section__sub">Create from the safe predefined tool catalog. Tools are sandboxed and audited.</p>
        </div>
        <span class="n-tag n-tag--mono">{{ agents.length }} agent{{ agents.length === 1 ? '' : 's' }}</span>
      </header>

      <div class="agent__crud-grid">
        <article class="n-card agent__create">
          <div class="agent__create-head">
            <div class="agent__create-icon">
              <Bot :size="16" />
            </div>
            <div>
              <h3 class="agent__create-title">Create agent</h3>
              <p class="agent__create-sub">Tools generated from your {{ businessTypeLabel }} tab schema.</p>
            </div>
          </div>

          <label class="n-field">
            <span class="n-field__label">Agent name</span>
            <input v-model="newAgent.name" class="n-input" type="text" placeholder="Sales sidekick" />
          </label>
          <label class="n-field">
            <span class="n-field__label">Description</span>
            <input v-model="newAgent.description" class="n-input" type="text" placeholder="One-line purpose" />
          </label>
          <label class="n-field">
            <span class="n-field__label">System prompt</span>
            <textarea v-model="newAgent.system_prompt" class="n-textarea" rows="5" :placeholder="businessPromptPlaceholder"></textarea>
            <span class="n-field__sub">Global Nokvo rules and {{ businessTypeLabel }} template rules are injected at runtime.</span>
          </label>

          <div class="agent__tools">
            <header class="agent__tools-head">
              <strong>Enabled tools</strong>
              <button
                type="button"
                class="n-btn n-btn--quiet n-btn--sm"
                :disabled="!toolCatalogDefaults.length"
                @click="selectDefaultAgentTools"
              >
                Reset to recommended
              </button>
            </header>

            <div v-if="!toolCatalogGroups.length" class="agent__tools-empty">
              No tools available yet. Pick a business type to populate the catalog.
            </div>

            <div v-for="group in toolCatalogGroups" :key="group.label" class="agent__tool-group">
              <div class="agent__tool-group-head">
                <strong>{{ group.label }}</strong>
                <span class="n-tag n-tag--mono">{{ group.tools.length }}</span>
                <button
                  type="button"
                  class="n-btn n-btn--quiet n-btn--sm"
                  @click="toggleAgentToolGroup(group)"
                >
                  {{ isAgentToolGroupAllOn(group) ? 'Deselect all' : 'Select all' }}
                </button>
              </div>
              <div class="agent__tool-grid">
                <label
                  v-for="t in group.tools"
                  :key="t.key"
                  class="agent__tool"
                  :class="{ 'is-on': newAgent.tool_keys.includes(t.key) }"
                >
                  <input
                    type="checkbox"
                    class="agent__tool-input"
                    :checked="newAgent.tool_keys.includes(t.key)"
                    @change="toggleAgentTool(t.key)"
                  />
                  <strong>{{ t.display_name }}</strong>
                  <span>{{ t.description }}</span>
                  <span v-if="t.requires_confirmation" class="n-tag n-tag--warning agent__tool-warn">
                    needs human confirmation
                  </span>
                </label>
              </div>
            </div>
          </div>

          <div class="agent__create-foot">
            <button type="button" class="n-btn n-btn--primary" @click="createAgent">
              <CheckCircle2 :size="14" />
              Create agent
            </button>
          </div>
        </article>

        <article class="n-card agent__list">
          <header class="agent__list-head">
            <strong>Roster</strong>
            <span class="n-tag n-tag--mono">{{ agents.length }}</span>
          </header>
          <div v-if="!agents.length" class="agent__list-empty">No agents yet. Create one to start testing.</div>
          <ul v-else class="agent__list-items">
            <li
              v-for="a in agents"
              :key="a.id"
              class="agent__list-row"
              :class="{ 'is-active': activeAgent?.id === a.id }"
              @click="activeAgent = a; chatLog = []"
            >
              <span class="agent__list-icon"><Bot :size="14" /></span>
              <div>
                <strong>{{ a.name }}</strong>
                <span class="n-mono">{{ (a.tool_keys || []).length }} tool{{ (a.tool_keys || []).length === 1 ? '' : 's' }} · {{ a.description || 'No description' }}</span>
              </div>
              <span class="n-tag" :class="{ 'n-tag--brand': activeAgent?.id === a.id }">
                {{ activeAgent?.id === a.id ? 'Active' : 'Idle' }}
              </span>
            </li>
          </ul>
        </article>
      </div>
    </section>

    <!-- Chat tester + Email drafts -->
    <section class="n-section n-rise" data-delay="4">
      <header class="n-section__head">
        <div>
          <h2 class="n-section__title">Chat tester</h2>
          <p class="n-section__sub">Talk to {{ activeAgent?.name || 'an agent' }}. Tool calls are sandboxed and audited.</p>
        </div>
        <span class="n-tag" :class="{ 'n-tag--brand': activeAgent }">
          {{ activeAgent ? activeAgent.name : 'No agent selected' }}
        </span>
      </header>

      <div class="agent__chat-grid">
        <article class="n-card agent__chat">
          <label class="n-field">
            <span class="n-field__label">Message</span>
            <textarea v-model="chatInput" class="n-textarea" rows="3" placeholder="Ask the agent…"></textarea>
          </label>
          <div class="agent__chat-actions">
            <button type="button" class="n-btn n-btn--ghost n-btn--sm" :disabled="!activeAgent" @click="chatLog = []">Clear</button>
            <button type="button" class="n-btn n-btn--primary" :disabled="!activeAgent || !chatInput.trim()" @click="sendChat">
              <Send :size="14" />
              Send
            </button>
          </div>

          <div class="agent__chat-thread">
            <p v-if="!chatLog.length" class="agent__chat-empty">Pick an agent and send a message to start.</p>
            <div v-else>
              <div
                v-for="(turn, i) in chatLog"
                :key="i"
                class="agent__chat-turn"
                :class="`agent__chat-turn--${turn.role}`"
              >
                <span class="n-tag" :class="{
                  'n-tag--brand': turn.role === 'agent',
                  'n-tag--danger': turn.role === 'system',
                }">{{ turn.role }}</span>
                <p>{{ turn.text }}</p>
                <p v-if="turn.tool_calls?.length" class="agent__chat-tools">
                  <span class="agent__chat-tools-label">Tool calls</span>
                  <span v-for="(tc, idx) in turn.tool_calls" :key="idx" class="n-tag n-tag--mono">
                    {{ tc.tool }}{{ tc.ok === false ? ' · error' : '' }}
                  </span>
                </p>
              </div>
            </div>
          </div>
        </article>

        <article class="n-card agent__drafts">
          <header class="agent__drafts-head">
            <div>
              <h3 class="agent__create-title">Email drafts</h3>
              <p class="agent__create-sub">Queued until a human confirms. Nokvo One never sends email automatically.</p>
            </div>
            <button type="button" class="n-btn n-btn--ghost n-btn--sm" @click="loadEmailDrafts">Refresh</button>
          </header>

          <div v-if="!emailDrafts.length" class="agent__drafts-empty">
            No drafts yet. They appear here when the agent calls <span class="n-kbd">send_email_draft</span>.
          </div>

          <ul v-else class="agent__draft-list">
            <li v-for="d in emailDrafts" :key="d.id" class="agent__draft">
              <div class="agent__draft-icon"><LinkIcon :size="14" /></div>
              <div class="agent__draft-body">
                <strong>{{ d.data?.subject }}</strong>
                <span class="n-mono">to {{ d.data?.to_email }}</span>
                <p>{{ d.data?.body }}</p>
              </div>
              <div class="agent__draft-actions">
                <span class="n-tag" :class="d.status === 'pending_confirmation' ? 'n-tag--warning' : 'n-tag--brand'">{{ d.status }}</span>
                <button
                  v-if="d.status === 'pending_confirmation'"
                  type="button"
                  class="n-btn n-btn--danger n-btn--sm"
                  @click="discardDraft(d.id)"
                >
                  <XCircle :size="13" />
                  Discard
                </button>
              </div>
            </li>
          </ul>
        </article>
      </div>
    </section>
  </div>
</template>

<style scoped>
/* ─── Hero ─────────────────────────────────────────── */
.agent__hero {
  display: grid;
  gap: 24px;
  padding: 32px 0 24px;
  border-bottom: 1px solid var(--n-border);
  position: relative;
}
.agent__hero::before {
  content: '';
  position: absolute;
  top: 0;
  right: 0;
  width: 480px;
  height: 280px;
  background: radial-gradient(closest-side, var(--n-brand-soft), transparent 80%);
  filter: blur(20px);
  opacity: 0.6;
  pointer-events: none;
  z-index: -1;
}
.agent__hero-title { margin-top: 6px; }
.agent__rename {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-top: 14px;
  padding: 4px 4px 4px 12px;
  background: var(--n-surface);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-md);
  max-width: 360px;
}
.agent__rename-icon { color: var(--n-text-3); display: grid; place-items: center; }
.agent__rename-input {
  appearance: none;
  background: transparent;
  border: 0;
  outline: none;
  font-family: var(--n-font-body);
  font-size: 13.5px;
  color: var(--n-text);
  flex: 1;
  padding: 6px 0;
  min-width: 0;
}
.agent__rename-input::placeholder { color: var(--n-text-4); }

.agent__pipeline {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 8px;
}
.agent__chip {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 14px;
  background: var(--n-bg-elev);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-lg);
  animation: n-rise 420ms var(--n-ease) both;
  transition: border-color var(--n-t-fast) var(--n-ease), box-shadow var(--n-t-fast) var(--n-ease);
}
.agent__chip:hover { border-color: var(--n-border-strong); box-shadow: var(--n-shadow-sm); }
.agent__chip-icon {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: var(--n-brand-soft);
  color: var(--n-brand);
  display: grid;
  place-items: center;
  flex-shrink: 0;
}
.agent__chip-body { display: grid; gap: 1px; min-width: 0; }
.agent__chip-label {
  font-family: var(--n-font-mono);
  font-size: 9.5px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--n-text-4);
}
.agent__chip-model {
  font-size: 13px;
  font-weight: 600;
  color: var(--n-text);
  letter-spacing: -0.01em;
}
.agent__chip-meta {
  font-family: var(--n-font-mono);
  font-size: 10.5px;
  color: var(--n-text-3);
  letter-spacing: 0.02em;
}

.agent__pending {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  background: var(--n-warning-soft);
  border: 1px solid rgba(217, 119, 6, 0.18);
  border-radius: var(--n-r-lg);
  color: var(--n-text-2);
  font-size: 13px;
}

/* ─── Voice tester ─────────────────────────────── */
.agent__status {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  font-size: 12.5px;
  color: var(--n-text-3);
}
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

.agent__tester { padding: 0; overflow: hidden; }
.agent__controls {
  display: flex;
  gap: 8px;
  padding: 16px 20px;
  border-bottom: 1px solid var(--n-border-subtle);
  flex-wrap: wrap;
  align-items: center;
}
.agent__lang { width: auto; min-width: 160px; }
.agent__text-input { flex: 1 1 240px; min-width: 200px; }

.agent__error {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 12px 20px 0;
  padding: 10px 14px;
  background: var(--n-danger-soft);
  border: 1px solid rgba(220, 38, 38, 0.18);
  border-radius: var(--n-r-md);
  color: var(--n-danger-2);
  font-size: 13px;
}

.agent__telemetry { padding: 14px 20px; display: grid; gap: 10px; }
.agent__live {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  background: var(--n-surface);
  border: 1px solid var(--n-border-subtle);
  border-radius: var(--n-r-md);
  color: var(--n-text-2);
}
.agent__live em {
  font-style: italic;
  flex: 1;
  font-size: 13.5px;
}
.agent__metrics { display: flex; gap: 6px; flex-wrap: wrap; }

.agent__empty { margin: 16px 20px 20px; }

.agent__transcript {
  padding: 12px 20px 24px;
  display: grid;
  gap: 16px;
}
.agent__turn {
  display: grid;
  gap: 8px;
  padding: 14px;
  background: var(--n-surface);
  border: 1px solid var(--n-border-subtle);
  border-radius: var(--n-r-lg);
}
.agent__turn-side { display: grid; gap: 6px; }
.agent__turn-side p { margin: 0; font-size: 13.5px; color: var(--n-text); line-height: 1.5; }
.agent__turn-side--agent p { color: var(--n-text); font-weight: 500; }
.agent__turn-meta { display: flex; gap: 6px; flex-wrap: wrap; color: var(--n-text-3); font-size: 11.5px; }

/* ─── Phone link ───────────────────────────────── */
.agent__phone { display: grid; gap: 16px; }
.agent__phone-field { max-width: 420px; }
.agent__phone-actions {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
}
.agent__phone-hint { color: var(--n-text-3); font-size: 12.5px; }
.agent__phone-hint code {
  font-family: var(--n-font-mono);
  background: var(--n-surface);
  padding: 2px 7px;
  border-radius: 5px;
  border: 1px solid var(--n-border);
  font-size: 11.5px;
}

.agent__phone-urls { display: grid; gap: 8px; }
.agent__phone-url-row {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 12px 14px;
  background: var(--n-surface);
  border: 1px solid var(--n-border-subtle);
  border-radius: var(--n-r-md);
  min-width: 0;
}
.agent__phone-url-row > div { display: grid; gap: 2px; min-width: 0; flex: 1; }
.agent__phone-cap {
  font-family: var(--n-font-mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--n-text-4);
}
.agent__phone-url-row strong {
  font-size: 12.5px;
  color: var(--n-text);
  display: block;
}

/* ─── Agent CRUD ───────────────────────────────── */
.agent__crud-grid {
  display: grid;
  grid-template-columns: 1.5fr 1fr;
  gap: 14px;
  align-items: start;
}
@media (max-width: 1100px) { .agent__crud-grid { grid-template-columns: 1fr; } }

.agent__create { display: grid; gap: 16px; }
.agent__create-head { display: flex; align-items: center; gap: 12px; }
.agent__create-icon {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  background: var(--n-brand-soft);
  color: var(--n-brand);
  display: grid;
  place-items: center;
}
.agent__create-title {
  font-family: var(--n-font-display);
  font-size: 16px;
  font-weight: 600;
  color: var(--n-text);
  letter-spacing: -0.01em;
  margin: 0;
}
.agent__create-sub { font-size: 12.5px; color: var(--n-text-3); margin: 2px 0 0; }

.agent__tools { display: grid; gap: 12px; }
.agent__tools-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
}
.agent__tools-head strong { font-size: 13px; color: var(--n-text); font-weight: 600; }
.agent__tools-empty {
  padding: 16px;
  background: var(--n-surface);
  border: 1px dashed var(--n-border);
  border-radius: var(--n-r-md);
  color: var(--n-text-3);
  font-size: 13px;
  text-align: center;
}

.agent__tool-group { display: grid; gap: 8px; }
.agent__tool-group-head {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 0;
  border-top: 1px solid var(--n-border-subtle);
}
.agent__tool-group-head strong {
  font-family: var(--n-font-display);
  font-size: 13px;
  font-weight: 600;
  color: var(--n-text);
  flex: 1;
}

.agent__tool-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.agent__tool {
  display: grid;
  gap: 4px;
  padding: 10px 12px;
  background: var(--n-surface);
  border: 1px solid var(--n-border-subtle);
  border-radius: var(--n-r-md);
  cursor: pointer;
  transition: border-color var(--n-t-fast) var(--n-ease), background var(--n-t-fast) var(--n-ease);
  position: relative;
}
.agent__tool:hover { border-color: var(--n-border-strong); background: var(--n-bg-elev); }
.agent__tool.is-on {
  background: var(--n-brand-soft);
  border-color: rgba(99, 102, 241, 0.22);
}
.agent__tool-input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
  pointer-events: none;
}
.agent__tool strong {
  font-size: 12.5px;
  color: var(--n-text);
  font-weight: 600;
}
.agent__tool.is-on strong { color: var(--n-brand-ink); }
.agent__tool span {
  font-size: 11.5px;
  color: var(--n-text-3);
  line-height: 1.4;
}
.agent__tool-warn { width: max-content; margin-top: 4px; }

.agent__create-foot { display: flex; justify-content: flex-end; }

/* Agents list */
.agent__list { padding: 0; overflow: hidden; }
.agent__list-head {
  display: flex; justify-content: space-between; align-items: center;
  padding: 16px 20px;
  border-bottom: 1px solid var(--n-border-subtle);
  font-size: 13px;
  color: var(--n-text);
}
.agent__list-empty {
  padding: 36px 20px;
  color: var(--n-text-3);
  font-size: 13px;
  text-align: center;
}
.agent__list-items { list-style: none; margin: 0; padding: 4px 0; }
.agent__list-row {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 12px;
  align-items: center;
  padding: 12px 20px;
  border-bottom: 1px solid var(--n-border-subtle);
  cursor: pointer;
  transition: background var(--n-t-fast) var(--n-ease);
}
.agent__list-row:last-child { border-bottom: 0; }
.agent__list-row:hover { background: var(--n-surface); }
.agent__list-row.is-active { background: var(--n-brand-soft); }
.agent__list-icon {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: var(--n-surface-2);
  color: var(--n-text-2);
  display: grid;
  place-items: center;
}
.agent__list-row.is-active .agent__list-icon { background: var(--n-bg); color: var(--n-brand); }
.agent__list-row div strong {
  display: block;
  font-size: 13.5px;
  font-weight: 600;
  color: var(--n-text);
}
.agent__list-row div span {
  display: block;
  font-size: 11.5px;
  color: var(--n-text-3);
  margin-top: 1px;
}

/* ─── Chat grid ─────────────────────────────────── */
.agent__chat-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  align-items: start;
}
@media (max-width: 1100px) { .agent__chat-grid { grid-template-columns: 1fr; } }

.agent__chat { display: grid; gap: 14px; }
.agent__chat-actions { display: flex; gap: 8px; justify-content: flex-end; }
.agent__chat-thread { display: grid; gap: 10px; max-height: 360px; overflow-y: auto; }
.agent__chat-empty {
  text-align: center;
  color: var(--n-text-3);
  font-size: 13px;
  padding: 24px 0;
  margin: 0;
}
.agent__chat-turn {
  display: grid;
  gap: 6px;
  padding: 12px;
  background: var(--n-surface);
  border: 1px solid var(--n-border-subtle);
  border-radius: var(--n-r-md);
}
.agent__chat-turn p { margin: 0; font-size: 13px; color: var(--n-text); line-height: 1.5; }
.agent__chat-turn--system { border-color: rgba(220, 38, 38, 0.2); background: var(--n-danger-soft); }
.agent__chat-tools { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.agent__chat-tools-label {
  font-family: var(--n-font-mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--n-text-4);
}

/* Drafts */
.agent__drafts { display: grid; gap: 14px; }
.agent__drafts-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
.agent__drafts-empty {
  padding: 28px 0;
  text-align: center;
  font-size: 13px;
  color: var(--n-text-3);
}
.agent__draft-list { list-style: none; padding: 0; margin: 0; display: grid; gap: 10px; }
.agent__draft {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 12px;
  align-items: flex-start;
  padding: 14px;
  background: var(--n-surface);
  border: 1px solid var(--n-border-subtle);
  border-radius: var(--n-r-md);
}
.agent__draft-icon {
  width: 32px;
  height: 32px;
  border-radius: 9px;
  background: var(--n-bg);
  color: var(--n-text-2);
  display: grid;
  place-items: center;
  border: 1px solid var(--n-border-subtle);
  flex-shrink: 0;
}
.agent__draft-body { display: grid; gap: 4px; min-width: 0; }
.agent__draft-body strong {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--n-text);
}
.agent__draft-body span {
  font-family: var(--n-font-mono);
  font-size: 11px;
  color: var(--n-text-3);
}
.agent__draft-body p {
  font-size: 12.5px;
  color: var(--n-text-2);
  margin: 4px 0 0;
  line-height: 1.45;
  white-space: pre-wrap;
}
.agent__draft-actions {
  display: grid;
  gap: 6px;
  justify-items: end;
}
</style>
