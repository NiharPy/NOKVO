<script setup>
// Nova — the APEX in-product assistant. A right-side chat panel opened from the
// header. Plain request/response per message (no streaming — gpt-5-mini latency
// is fine with a typing indicator). Side effects (tickets) render as confirm
// cards; campaign drafts render as a preview card whose "Apply to campaign form"
// emits the draft up to ApexApp for a ONE-WAY handoff into the Campaign tab.
import { nextTick, onMounted, ref, watch } from 'vue';
import { novaBriefing, novaChat, novaConfirm, novaSession, novaUpload, extractError } from './apexApi.js';
import AxIcon from './AxIcon.vue';

const props = defineProps({
  role: { type: String, default: 'member' },
});
const emit = defineEmits(['close', 'apply-draft']);

const GREETING = props.role === 'admin'
  ? 'Hi, I\'m Nova. Ask me anything about APEX — or upload a pitch document / describe your offer and I\'ll draft a campaign with you.'
  : 'Hi, I\'m Nova. Ask me anything about APEX — how it works, the terms, your account — or raise a support ticket.';

const sessionId = ref(sessionStorage.getItem('nokvo_apex_nova_session') || null);
const messages = ref([{ role: 'assistant', text: GREETING, cards: [] }]);

// Reopen restore: the Redis session outlives the panel (30 min TTL) — pull the
// history back so closing the panel mid-conversation loses nothing. Cards are
// not replayed (pending actions expire with their session state anyway).
onMounted(async () => {
  if (sessionId.value) {
    try {
      const data = await novaSession(sessionId.value);
      const restored = (data.messages || []).map((m) => ({ role: m.role, text: m.content, cards: [] }));
      if (restored.length) messages.value = [{ role: 'assistant', text: GREETING, cards: [] }, ...restored];
      await scrollDown();
    } catch { sessionId.value = null; }
  }
  // Proactive briefing: deterministic server-composed highlights, shown as one
  // ephemeral bubble (never persisted — the server session is history's truth).
  try {
    const brief = await novaBriefing();
    const lines = brief?.highlights || [];
    if (lines.length) {
      messages.value.push({ role: 'assistant', text: `Right now:\n• ${lines.join('\n• ')}`, cards: [] });
      await scrollDown();
    }
  } catch { /* briefing is best-effort — the panel works fine without it */ }
});
watch(sessionId, (v) => {
  if (v) sessionStorage.setItem('nokvo_apex_nova_session', v);
});
const input = ref('');
const busy = ref(false);
const analyzing = ref(false);
const listEl = ref(null);
const fileInput = ref(null);

async function scrollDown() {
  await nextTick();
  if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight;
}

async function send() {
  const text = input.value.trim();
  if (!text || busy.value) return;
  input.value = '';
  messages.value.push({ role: 'user', text, cards: [] });
  busy.value = true;
  await scrollDown();
  try {
    const data = await novaChat(sessionId.value, text);
    sessionId.value = data.session_id;
    messages.value.push({ role: 'assistant', text: data.reply, cards: data.cards || [] });
  } catch (e) {
    messages.value.push({ role: 'assistant', text: extractError(e, 'Sorry — that didn\'t go through. Try again.'), cards: [] });
  } finally {
    busy.value = false;
    await scrollDown();
  }
}

function pickFile() {
  if (!analyzing.value && fileInput.value) fileInput.value.click();
}
async function onFile(e) {
  const file = e.target.files && e.target.files[0];
  e.target.value = '';
  if (!file) return;
  messages.value.push({ role: 'user', text: `📎 ${file.name}`, cards: [] });
  analyzing.value = true;
  await scrollDown();
  try {
    const data = await novaUpload(sessionId.value, file);
    sessionId.value = data.session_id || sessionId.value;
    messages.value.push({ role: 'assistant', text: data.reply, cards: data.cards || [] });
  } catch (err) {
    messages.value.push({ role: 'assistant', text: extractError(err, 'I couldn\'t read that document. PDF, DOCX, PPTX, or TXT up to 20MB works best.'), cards: [] });
  } finally {
    analyzing.value = false;
    await scrollDown();
  }
}

const confirmBusy = ref('');
async function decide(card, decision) {
  if (confirmBusy.value) return;
  confirmBusy.value = card.action_id;
  try {
    const data = await novaConfirm(sessionId.value, card.action_id, decision);
    card.resolved = decision;
    messages.value.push({ role: 'assistant', text: data.reply, cards: [] });
  } catch (e) {
    messages.value.push({ role: 'assistant', text: extractError(e, 'That action failed — nothing was done.'), cards: [] });
  } finally {
    confirmBusy.value = '';
    await scrollDown();
  }
}

function applyDraft(card) {
  card.resolved = 'applied';
  emit('apply-draft', card.draft || {});
}

function fmtCredits(n) {
  return Number(n || 0).toLocaleString('en-IN', { maximumFractionDigits: 0 });
}
</script>

<template>
  <div class="nv-overlay" @click.self="emit('close')">
    <div class="nv-panel">
      <div class="nv-head">
        <div class="nv-title-wrap">
          <span class="nv-star">✦</span>
          <span class="nv-title">NOVA</span>
          <span class="nv-sub">APEX assistant</span>
        </div>
        <button type="button" class="nv-x" aria-label="Close" @click="emit('close')">✕</button>
      </div>

      <div ref="listEl" class="nv-list">
        <div v-for="(m, i) in messages" :key="i" class="nv-row" :class="m.role === 'user' ? 'is-user' : 'is-bot'">
          <!-- NOVA speaks from the void: no bubble — a star-marked transmission.
               The user is the one in a (glass) bubble. -->
          <template v-if="m.role !== 'user'">
            <div class="nv-trans">
              <div class="nv-trans-tag"><span class="nv-trans-star">✦</span><span class="nv-trans-label">NOVA</span></div>
              <div class="nv-trans-body">{{ m.text }}</div>
            </div>
          </template>
          <div v-else class="nv-bubble">{{ m.text }}</div>

          <!-- confirm / preview cards -->
          <div v-for="card in m.cards" :key="card.action_id || card.type" class="nv-card">
            <template v-if="card.type === 'ticket_preview'">
              <div class="nv-card-title">Support ticket</div>
              <div class="nv-card-line"><strong>{{ card.subject }}</strong></div>
              <div class="nv-card-line nv-muted">{{ card.description }}</div>
              <div v-if="card.diagnosis_summary" class="nv-card-line nv-muted">Attached diagnosis: {{ card.diagnosis_summary }}</div>
              <div v-if="!card.resolved" class="nv-card-actions">
                <button type="button" class="nv-btn nv-btn--ghost" :disabled="confirmBusy === card.action_id" @click="decide(card, 'cancel')">Cancel</button>
                <button type="button" class="nv-btn nv-btn--accent" :disabled="confirmBusy === card.action_id" @click="decide(card, 'confirm')">
                  {{ confirmBusy === card.action_id ? 'Sending…' : 'Raise ticket' }}
                </button>
              </div>
              <div v-else class="nv-card-done">{{ card.resolved === 'confirm' ? '✓ Ticket raised' : 'Cancelled' }}</div>
            </template>

            <template v-else-if="card.type === 'rerun_preview'">
              <div class="nv-card-title">Re-dial campaign</div>
              <div class="nv-card-line"><strong>{{ card.campaign_name }}</strong></div>
              <div class="nv-card-grid">
                <span v-for="b in card.buckets" :key="b">
                  {{ b === 'busy' ? 'Asked to call back' : "Didn't pick up" }}<template v-if="card.counts && card.counts[b] != null"> · {{ card.counts[b] }}</template>
                </span>
              </div>
              <div v-if="(card.buckets || []).includes('busy')" class="nv-card-line nv-muted">Busy contacts answered before — each connected retry consumes Call Credits.</div>
              <div v-if="!card.resolved" class="nv-card-actions">
                <button type="button" class="nv-btn nv-btn--ghost" :disabled="confirmBusy === card.action_id" @click="decide(card, 'cancel')">Cancel</button>
                <button type="button" class="nv-btn nv-btn--accent" :disabled="confirmBusy === card.action_id" @click="decide(card, 'confirm')">
                  {{ confirmBusy === card.action_id ? 'Starting…' : 'Start re-dial' }}
                </button>
              </div>
              <div v-else class="nv-card-done">{{ card.resolved === 'confirm' ? '✓ Re-dial started' : 'Cancelled' }}</div>
            </template>

            <template v-else-if="card.type === 'draft_flagged'">
              <div class="nv-card-title">Content review</div>
              <div class="nv-card-line"><strong>Draft blocked{{ card.category ? ` — ${String(card.category).replaceAll('_', ' ')}` : '' }}</strong></div>
              <div v-if="card.reason" class="nv-card-line nv-muted">{{ card.reason }}</div>
              <div class="nv-card-line nv-muted">Campaigns can only promote your own company — tell Nova what to reword.</div>
            </template>

            <template v-else-if="card.type === 'campaign_draft'">
              <div class="nv-card-title">Campaign draft</div>
              <div class="nv-card-line"><strong>{{ card.draft?.name || 'Untitled campaign' }}</strong></div>
              <div class="nv-card-grid">
                <span v-if="card.draft?.company_name">{{ card.draft.company_name }}</span>
                <span v-if="card.draft?.caller_name">Caller: {{ card.draft.caller_name }}</span>
                <span v-if="card.draft?.language">Lang: {{ (card.draft.language || '').toUpperCase() }}</span>
                <span v-if="card.draft?.working_days">{{ card.draft.working_days }}d/wk</span>
                <span v-if="card.draft?.calls_per_day">{{ card.draft.calls_per_day }}/day</span>
                <span v-if="card.draft?.questionnaire?.questions?.length">{{ card.draft.questionnaire.questions.length }} questions · threshold {{ card.draft.questionnaire.threshold }}</span>
              </div>
              <div v-if="card.estimate" class="nv-card-stats">
                <span class="nv-stat"><em>{{ fmtCredits(card.estimate.per_call_credits) }}</em> cr / call</span>
                <span class="nv-stat"><em>{{ fmtCredits(card.estimate.daily_spend) }}</em> cr / day max</span>
                <span v-if="card.estimate.balance_warning" class="nv-warn">exceeds balance</span>
              </div>
              <div v-if="!card.resolved" class="nv-card-actions">
                <button type="button" class="nv-btn nv-btn--accent" @click="applyDraft(card)">Apply to campaign form</button>
              </div>
              <div v-else class="nv-card-done">✓ Applied — upload your contacts file on the Campaign tab and launch from there.</div>
            </template>
          </div>
        </div>

        <!-- Thinking: a star gathering light inside an expanding halo -->
        <div v-if="busy || analyzing" class="nv-row is-bot">
          <div class="nv-think">
            <span class="nv-think-halo"><span class="nv-think-star">✦</span></span>
            <span class="nv-think-label">{{ analyzing ? 'Reading your document — up to a minute for big files' : 'Thinking' }}</span>
          </div>
        </div>
      </div>

      <div class="nv-inputbar">
        <button v-if="role === 'admin'" type="button" class="nv-attach" :disabled="analyzing" title="Attach a pitch document (PDF, DOCX, PPTX)" @click="pickFile"><AxIcon name="plus" :size="15" /></button>
        <input ref="fileInput" type="file" accept=".pdf,.docx,.pptx,.txt" style="display:none" @change="onFile" />
        <textarea
          v-model="input" class="nv-input" rows="1" placeholder="Ask NOVA anything…"
          @keydown.enter.exact.prevent="send"
        ></textarea>
        <button type="button" class="nv-send" :disabled="busy || !input.trim()" @click="send">↑</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ── NOVA panel — design language ─────────────────────────────────────────────
   Signature: the user sits in a glass bubble; NOVA has none — it speaks from
   the void itself (star marker + mono microlabel + text on the atmosphere).
   The panel carries a slow-drifting ember nebula so the space feels deep, and
   "thinking" is a star gathering light inside an expanding halo. Sora for
   speech; JetBrains Mono for the instrument layer (labels, numbers). */

.nv-overlay { position: fixed; inset: 0; background: rgba(4,4,5,0.5); backdrop-filter: blur(5px); -webkit-backdrop-filter: blur(5px); z-index: 1200; display: flex; justify-content: flex-end; animation: nvFade .3s ease; }
@keyframes nvFade { from { opacity: 0; } to { opacity: 1; } }

/* Entrance: a decelerating spring + the button's red light bleeding off the
   panel's left edge — opening reads as the button expanding into the panel. */
.nv-panel {
  position: relative; width: min(440px, 100vw); height: 100%;
  background: #0B0B0D;
  border-left: 1px solid rgba(230,38,48,0.22);
  display: flex; flex-direction: column; overflow: hidden;
  box-shadow: -30px 0 80px -30px rgba(0,0,0,0.9), -2px 0 28px -8px rgba(230,38,48,0.3);
  animation: nvSlide .45s cubic-bezier(0.22, 1, 0.36, 1);
}
@keyframes nvSlide { from { transform: translateX(64px); opacity: 0; } to { transform: translateX(0); opacity: 1; } }

/* The nebula: two ember fields drifting on a slow loop. Content sits above. */
.nv-panel::before {
  content: ''; position: absolute; inset: -20%; pointer-events: none; z-index: 0;
  background:
    radial-gradient(ellipse 480px 380px at 82% 8%, rgba(230,38,48,0.10), transparent 65%),
    radial-gradient(ellipse 520px 460px at 12% 92%, rgba(230,38,48,0.06), transparent 70%),
    radial-gradient(ellipse 300px 240px at 55% 45%, rgba(255,255,255,0.015), transparent 70%);
  animation: nvNebula 46s ease-in-out infinite alternate;
}
@keyframes nvNebula {
  from { transform: translate3d(0, 0, 0) scale(1); }
  to { transform: translate3d(-3%, 4%, 0) scale(1.08); }
}
.nv-head, .nv-list, .nv-inputbar { position: relative; z-index: 1; }

/* ── header ── */
.nv-head { display: flex; align-items: center; justify-content: space-between; padding: 17px 20px; border-bottom: 1px solid rgba(255,255,255,0.07); }
.nv-head::after { content: ''; position: absolute; left: 0; right: 0; bottom: -1px; height: 1px; background: linear-gradient(90deg, rgba(230,38,48,0.55), rgba(230,38,48,0.12) 45%, transparent 80%); }
.nv-title-wrap { display: flex; align-items: baseline; gap: 10px; }
.nv-star { font-size: 14px; line-height: 1; color: #E62630; text-shadow: 0 0 10px rgba(230,38,48,0.8); animation: nvTwinkle 1.8s ease-in-out infinite; align-self: center; }
@keyframes nvTwinkle { 0%, 100% { opacity: 0.75; transform: scale(1); } 50% { opacity: 1; transform: scale(1.25) rotate(18deg); } }
.nv-title {
  font-weight: 700; font-size: 15px; letter-spacing: 0.26em;
  background: linear-gradient(100deg, #FFFFFF 20%, #FFB9BE 50%, #FFFFFF 80%);
  background-size: 200% 100%; -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent; color: transparent;
  animation: nvWordmark 5s ease-in-out infinite;
}
@keyframes nvWordmark { 0%, 100% { background-position: 0% 0; } 50% { background-position: 100% 0; } }
.nv-sub { font-family: 'JetBrains Mono', monospace; font-size: 9.5px; color: rgba(255,255,255,0.34); letter-spacing: 0.22em; text-transform: uppercase; }
.nv-x { background: none; border: none; color: rgba(255,255,255,0.45); font-size: 14px; cursor: pointer; padding: 6px 8px; border-radius: 8px; transition: color .15s ease, background .15s ease; }
.nv-x:hover { color: #fff; background: rgba(255,255,255,0.06); }

/* ── conversation ── */
.nv-list { flex: 1; overflow-y: auto; padding: 22px 20px; display: flex; flex-direction: column; gap: 18px; scrollbar-width: thin; scrollbar-color: rgba(230,38,48,0.35) transparent; }
.nv-list::-webkit-scrollbar { width: 4px; }
.nv-list::-webkit-scrollbar-thumb { background: rgba(230,38,48,0.35); border-radius: 4px; }
.nv-list::-webkit-scrollbar-track { background: transparent; }

.nv-row { display: flex; flex-direction: column; gap: 10px; animation: nvMsgIn .32s cubic-bezier(0.22, 1, 0.36, 1) both; }
@keyframes nvMsgIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
.nv-row.is-user { align-items: flex-end; }
.nv-row.is-bot { align-items: flex-start; }

/* NOVA's transmission — unbubbled, star-marked, a hairline rule that fades as
   the thought trails off. The asymmetry IS the hierarchy: you're a guest in
   its space, not the other way around. */
.nv-trans { max-width: 94%; padding-left: 2px; }
.nv-trans-tag { display: flex; align-items: center; gap: 7px; margin-bottom: 6px; }
.nv-trans-star { font-size: 10px; line-height: 1; color: #E62630; text-shadow: 0 0 8px rgba(230,38,48,0.7); }
.nv-trans-label { font-family: 'JetBrains Mono', monospace; font-size: 9px; font-weight: 700; letter-spacing: 0.3em; color: rgba(255,255,255,0.38); }
.nv-trans-body {
  font-size: 13.5px; line-height: 1.62; color: #EDEBE8;
  white-space: pre-wrap; word-break: break-word;
  padding-left: 17px; position: relative;
}
.nv-trans-body::before {
  content: ''; position: absolute; left: 4px; top: 3px; bottom: 3px; width: 1px;
  background: linear-gradient(180deg, rgba(230,38,48,0.55), rgba(230,38,48,0.1) 70%, transparent);
}

/* The user's glass bubble — contained, catching the light along its top edge. */
.nv-bubble {
  max-width: 86%; padding: 11px 15px; border-radius: 16px 16px 5px 16px;
  font-size: 13.5px; line-height: 1.55; white-space: pre-wrap; word-break: break-word;
  color: #FBF3F2;
  background: linear-gradient(180deg, rgba(230,38,48,0.2), rgba(230,38,48,0.1));
  border: 1px solid rgba(230,38,48,0.32);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.1), 0 6px 18px -10px rgba(230,38,48,0.4);
}

/* Thinking — a star gathering light inside a breathing halo. */
.nv-think { display: flex; align-items: center; gap: 12px; padding-left: 2px; }
.nv-think-halo {
  position: relative; width: 26px; height: 26px; border-radius: 50%;
  display: grid; place-items: center; flex-shrink: 0;
}
.nv-think-halo::before {
  content: ''; position: absolute; inset: 0; border-radius: 50%;
  border: 1px solid rgba(230,38,48,0.55);
  animation: nvHalo 1.6s ease-out infinite;
}
@keyframes nvHalo { from { transform: scale(0.4); opacity: 1; } to { transform: scale(1.5); opacity: 0; } }
.nv-think-star { font-size: 12px; line-height: 1; color: #E62630; text-shadow: 0 0 10px rgba(230,38,48,0.9); animation: nvGather 1.6s ease-in-out infinite; }
@keyframes nvGather { 0%, 100% { transform: scale(0.85); opacity: 0.7; } 50% { transform: scale(1.15); opacity: 1; } }
.nv-think-label { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; letter-spacing: 0.12em; color: rgba(255,255,255,0.42); text-transform: uppercase; animation: nvLabelPulse 1.6s ease-in-out infinite; }
@keyframes nvLabelPulse { 0%, 100% { opacity: 0.55; } 50% { opacity: 1; } }

/* ── cards — glass with a lit gradient edge ── */
.nv-card {
  max-width: 94%; border-radius: 15px; padding: 15px 17px;
  display: flex; flex-direction: column; gap: 8px;
  border: 1px solid transparent;
  background:
    linear-gradient(180deg, #141417, #0F0F12) padding-box,
    linear-gradient(155deg, rgba(230,38,48,0.55), rgba(255,255,255,0.09) 38%, rgba(230,38,48,0.14)) border-box;
  box-shadow: 0 14px 34px -18px rgba(0,0,0,0.9), inset 0 1px 0 rgba(255,255,255,0.05);
}
.nv-card-title { font-family: 'JetBrains Mono', monospace; font-size: 9.5px; letter-spacing: 0.3em; text-transform: uppercase; color: #E62630; font-weight: 700; }
.nv-card-line { font-size: 13px; line-height: 1.5; }
.nv-card-grid { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 12px; color: rgba(255,255,255,0.6); }
.nv-muted { color: rgba(255,255,255,0.5); font-size: 12.5px; }
.nv-card-done { font-size: 12.5px; color: #7FD9A8; }
/* The estimate row — numbers in mono, set like an instrument readout. */
.nv-card-stats { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 18px; padding-top: 4px; border-top: 1px solid rgba(255,255,255,0.06); }
.nv-stat { font-size: 11px; color: rgba(255,255,255,0.45); letter-spacing: 0.04em; }
.nv-stat em { font-family: 'JetBrains Mono', monospace; font-style: normal; font-size: 14px; font-weight: 600; color: #F3F2F0; margin-right: 3px; }
.nv-warn { font-family: 'JetBrains Mono', monospace; font-size: 10.5px; letter-spacing: 0.08em; color: #FF5A63; text-transform: uppercase; }
.nv-card-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 5px; }
.nv-btn { border-radius: 10px; padding: 9px 18px; font-size: 12.5px; font-weight: 600; cursor: pointer; border: 1px solid transparent; font-family: inherit; transition: transform .12s ease, box-shadow .15s ease; }
.nv-btn--ghost { background: none; border-color: rgba(255,255,255,0.16); color: rgba(255,255,255,0.75); }
.nv-btn--ghost:hover:not(:disabled) { border-color: rgba(255,255,255,0.35); color: #fff; }
.nv-btn--accent { background: #E62630; color: #fff; box-shadow: 0 0 14px rgba(230,38,48,0.3); }
.nv-btn--accent:hover:not(:disabled) { box-shadow: 0 0 20px rgba(230,38,48,0.55); }
.nv-btn:active:not(:disabled) { transform: scale(0.97); }
.nv-btn:disabled { opacity: 0.55; cursor: default; }

/* ── composer — a glass capsule that wakes when focused ── */
.nv-inputbar { display: flex; align-items: flex-end; gap: 9px; padding: 14px 16px 16px; border-top: 1px solid rgba(255,255,255,0.07); background: linear-gradient(180deg, rgba(255,255,255,0.014), transparent); }
.nv-attach { width: 36px; height: 36px; border-radius: 11px; border: 1px solid rgba(255,255,255,0.14); background: rgba(255,255,255,0.03); color: rgba(255,255,255,0.65); font-size: 16px; cursor: pointer; flex-shrink: 0; transition: color .15s ease, border-color .15s ease, box-shadow .15s ease; }
.nv-attach:hover:not(:disabled) { color: #fff; border-color: rgba(230,38,48,0.5); box-shadow: 0 0 12px rgba(230,38,48,0.2); }
.nv-input {
  flex: 1; resize: none; background: rgba(255,255,255,0.045);
  border: 1px solid rgba(255,255,255,0.11); border-radius: 12px;
  color: #F3F2F0; font-family: inherit; font-size: 13.5px;
  padding: 9px 13px; line-height: 1.45; max-height: 110px; outline: none;
  transition: border-color .2s ease, box-shadow .25s ease, background .2s ease;
}
.nv-input::placeholder { color: rgba(255,255,255,0.28); }
.nv-input:focus {
  border-color: rgba(230,38,48,0.55); background: rgba(230,38,48,0.04);
  box-shadow: 0 0 0 1px rgba(230,38,48,0.15), 0 0 22px -6px rgba(230,38,48,0.45);
}
.nv-send { width: 36px; height: 36px; border-radius: 11px; border: none; background: #E62630; color: #fff; font-size: 15px; cursor: pointer; flex-shrink: 0; box-shadow: 0 0 12px rgba(230,38,48,0.35); transition: transform .12s ease, box-shadow .15s ease; }
.nv-send:hover:not(:disabled) { box-shadow: 0 0 20px rgba(230,38,48,0.65); }
.nv-send:active:not(:disabled) { transform: scale(0.88); }
.nv-send:disabled { opacity: 0.35; cursor: default; box-shadow: none; }

.nv-btn:focus-visible, .nv-send:focus-visible, .nv-attach:focus-visible, .nv-x:focus-visible { outline: 2px solid rgba(230,38,48,0.7); outline-offset: 2px; }

@media (prefers-reduced-motion: reduce) {
  .nv-overlay, .nv-panel, .nv-panel::before, .nv-row, .nv-star, .nv-title,
  .nv-think-halo::before, .nv-think-star, .nv-think-label { animation: none; }
  .nv-send, .nv-btn, .nv-send:active:not(:disabled), .nv-btn:active:not(:disabled) { transition: none; transform: none; }
}
</style>
