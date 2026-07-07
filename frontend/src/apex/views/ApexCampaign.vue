<script setup>
// APEX Campaign tab — DETERMINISTIC bulk create (intro + questionnaire + outro +
// threshold + working window) and the deterministic campaign list with outcome
// counts. Reuses the shared bulkCalling.js logic + the existing endpoints.
// The create form is a 3-step wizard: Details → Script & questions → Review & launch.
import { inject, onMounted, ref, computed, watch } from 'vue';
import {
  newQuestion, maxPointsFor, estCallSeconds, fmtDuration, scheduleEstimate, countCsvRows,
  buildBulkCampaignFormData, categorizeContacts, leadCategory, campaignWindow, cleanQuestions,
} from '../../composables/bulkCalling.js';
import AxIcon from '../AxIcon.vue';

const apex = inject('apex');

const emptyForm = () => ({
  name: '', company_name: '', caller_name: 'Riya', language: 'en',
  content: '', file: null, intro: '', outro: '', questions: [], threshold: 1,
  working_days: 5, hours_per_day: 10, calls_per_day: 200,
  intro_i18n: {}, outro_i18n: {}, // reviewed hi/te lines (translate-preview flow)
});
const form = ref(emptyForm());
const fileName = ref('');
const listSize = ref(null); // CSV row count (data rows), for the estimation widget
const launching = ref(false);
const launchError = ref('');

// ── wizard ──
const step = ref(1);
const STEPS = ['Details', 'Script & questions', 'Review & launch'];
const canNext = computed(() => (step.value === 1 ? !!form.value.name.trim() : true));
const canLaunch = computed(() => !!form.value.name.trim() && !!form.value.file);
function goStep(n) {
  if (n === step.value) return;
  if (n < step.value) { step.value = n; return; }
  if (form.value.name.trim()) step.value = n; // forward needs at least a name
}

// ── Nova draft prefill (ONE-WAY handoff) ──
// Nova's "Apply to campaign form" parks a draft on apex.novaDraft; we copy it
// into the form ONCE and clear it. Edits made here never flow back to the chat.
function applyNovaDraft(draft) {
  if (!draft) return;
  const q = draft.questionnaire || {};
  form.value = {
    ...emptyForm(),
    name: draft.name || '',
    company_name: draft.company_name || '',
    caller_name: draft.caller_name || 'Riya',
    language: draft.language || 'en',
    content: draft.content || '',
    intro: q.intro || '',
    outro: q.outro || '',
    threshold: Number(q.threshold) || 1,
    working_days: Number(draft.working_days) || 5,
    hours_per_day: Number(draft.hours_per_day) || 10,
    calls_per_day: Number(draft.calls_per_day) || 200,
    questions: (q.questions || []).map((item) => ({
      ...newQuestion(),
      type: item.type === 'answer' ? 'answer' : 'intent',
      text: item.text || '',
      desired_answer: item.desired_answer || '',
      points: Number(item.points) || 1,
      graded: Array.isArray(item.tiers) && item.tiers.length > 0,
      tiers: Array.isArray(item.tiers) ? item.tiers : [],
      gate: !!item.gate,
    })),
  };
  fileName.value = '';
  listSize.value = null;
  step.value = 1;
  apex.toast('Draft from Nova applied — add your contacts file and launch when ready.');
  apex.clearNovaDraft && apex.clearNovaDraft();
}
onMounted(() => applyNovaDraft(apex.novaDraft?.value));
watch(() => apex.novaDraft?.value, (d) => applyNovaDraft(d));

const det = computed(() => apex.deterministicCampaigns.value);

async function onFile(ev) {
  form.value.file = ev?.target?.files?.[0] || null;
  fileName.value = form.value.file?.name || '';
  listSize.value = await countCsvRows(form.value.file);
}
function addQuestion() {
  form.value.questions.push(newQuestion());
  if (!form.value.threshold) form.value.threshold = 1;
}
function removeQuestion(i) { form.value.questions.splice(i, 1); }
function addTier(q) { if (!Array.isArray(q.tiers)) q.tiers = []; q.tiers.push({ label: '', points: 10 }); }
function removeTier(q, ti) { q.tiers.splice(ti, 1); }
// Catch-all / else band: exactly one per question (radio semantics). Clicking the
// current default clears it; clicking another moves it. The scorer awards this band
// server-side for any answer that matches no specific band.
function setCatchAll(q, ti) {
  const cur = !!(q.tiers && q.tiers[ti] && q.tiers[ti].default);
  (q.tiers || []).forEach((t, i) => { t.default = (!cur && i === ti); });
}

const maxPoints = computed(() => maxPointsFor(form.value.questions));
const estSeconds = computed(() => estCallSeconds(form.value.questions));
const estLabel = computed(() => fmtDuration(estSeconds.value));
// Estimation uses the wallet's current slab rate (falls back to the ₹5.5 floor).
const slab = computed(() => Number(apex.wallet?.value?.slab_rate) || 5.5);
const estimate = computed(() => scheduleEstimate({
  listSize: listSize.value,
  callsPerDay: form.value.calls_per_day,
  workingDays: form.value.working_days,
  perCallSeconds: estSeconds.value,
  slabRate: slab.value,
}));
const fmtCr = (n) => Math.round(Number(n) || 0).toLocaleString('en-IN');

// ── outcome counts per campaign (Successful / Not Interested / Didn't pick up) ──
// V2 campaigns get counts from the server GROUP BY summary (scales to 1M); legacy
// blob campaigns are categorized client-side.
function counts(c) {
  if (c.v2) {
    const s = apex.summaryFor && apex.summaryFor(c.id);
    if (s) return { successful: s.qualified || 0, not_interested: s.not_interested || 0, no_pickup: s.no_pickup || 0, pending: s.pending || 0 };
    return { successful: 0, not_interested: 0, no_pickup: 0, pending: 0 };
  }
  const g = categorizeContacts([c]);
  return { successful: g.successful.length, not_interested: g.not_interested.length, no_pickup: g.no_pickup.length, pending: g.pending.length };
}
// Slim dialed-progress per row — done/total from the same counts the badges use.
function prog(c) {
  const k = counts(c);
  const done = k.successful + k.not_interested + k.no_pickup;
  const total = (done + k.pending) || Number(c.total_count) || 0;
  return { done, total, pct: total ? Math.round((done / total) * 100) : 0 };
}
function statusTone(s) {
  if (s === 'running') return 'run';
  if (s === 'completed') return 'done';
  if (s === 'ingesting') return 'run';
  return 'off';
}
function redialCount(c) {
  if (c.v2) {
    // What a re-run would dial: rows still pending (e.g. a just-added CSV) plus
    // unreached misses it re-arms (no_answer/failed). dnd_dropped never redials.
    const s = apex.summaryFor && apex.summaryFor(c.id);
    if (!s) return 0;
    const by = s.by_status || {};
    return (s.pending || 0) + (by.no_answer || 0) + (by.failed || 0);
  }
  return (c.contacts || []).filter((ct) => ct.status !== 'answered' && !ct.answered_at).length;
}

// ── hi/te translation review ──
// "Translate & review" fills the exact Hindi/Telugu lines the agent will speak
// (same server translation that runs at creation) so the admin can hand-edit
// them before launch. Edited lines are submitted with the campaign; the server
// keeps every non-empty language and only fills blanks. Each translation
// remembers its English source (_i18nSrc) — if the admin rewrites the English
// text afterwards, the stale translation is dropped so the server re-translates.
const translating = ref(false);
const translated = ref(false);
const translateError = ref('');

async function translatePreview() {
  translateError.value = '';
  const cleaned = cleanQuestions(form.value.questions);
  if (!cleaned.length) { translateError.value = 'Add at least one question first.'; return; }
  translating.value = true;
  try {
    const q = await apex.translateQuestionnaire({
      intro: (form.value.intro || '').trim(),
      outro: (form.value.outro || '').trim(),
      questions: cleaned,
      threshold: Number(form.value.threshold) || 1,
    });
    // Map returned i18n back by position: cleanQuestions and the server both
    // keep non-blank questions in order.
    const nonBlank = form.value.questions.filter((x) => (x.text || '').trim());
    (q?.questions || []).forEach((rq, i) => {
      if (!nonBlank[i]) return;
      nonBlank[i].text_i18n = { ...(rq.text_i18n || {}) };
      nonBlank[i]._i18nSrc = (nonBlank[i].text || '').trim();
    });
    form.value.intro_i18n = { ...(q?.intro_i18n || {}) };
    form.value.outro_i18n = { ...(q?.outro_i18n || {}) };
    form.value._introSrc = (form.value.intro || '').trim();
    form.value._outroSrc = (form.value.outro || '').trim();
    translated.value = true;
  } catch (e) {
    translateError.value = apex.extractError(e, 'Could not translate the script.');
  } finally {
    translating.value = false;
  }
}

// Drop translations whose English source changed since "Translate & review" —
// submitting them would make the agent speak lines that no longer match.
function pruneStaleI18n() {
  for (const q of form.value.questions) {
    if (q.text_i18n && q._i18nSrc !== (q.text || '').trim()) { q.text_i18n = null; q._i18nSrc = undefined; }
  }
  if (form.value._introSrc !== undefined && form.value._introSrc !== (form.value.intro || '').trim()) form.value.intro_i18n = {};
  if (form.value._outroSrc !== undefined && form.value._outroSrc !== (form.value.outro || '').trim()) form.value.outro_i18n = {};
}

async function submit() {
  launchError.value = '';
  launching.value = true;
  try {
    pruneStaleI18n();
    const fd = buildBulkCampaignFormData(form.value, { deterministic: true });
    const data = await apex.createCampaign(fd);
    const contacts = Array.isArray(data?.contacts) ? data.contacts : [];
    const failed = contacts.filter((c) => c.status === 'failed');
    if (failed.length && failed.length >= contacts.length) {
      launchError.value = `Calls couldn't be placed: ${failed[0]?.error || 'check the dedicated number setup'}.`;
    } else {
      const note = failed.length ? ` (${failed.length} couldn't be placed)` : '';
      apex.toast(`Calling started — ${data?.total_count || 0} contacts${note}.`);
      form.value = emptyForm();
      fileName.value = '';
      listSize.value = null;
      step.value = 1;
      translated.value = false;
      translateError.value = '';
    }
    await apex.reload();
  } catch (e) {
    launchError.value = e?.message || apex.extractError(e, 'Could not start the campaign.');
  } finally {
    launching.value = false;
  }
}

const rerunning = ref(null);
async function rerun(id) {
  if (!id || rerunning.value) return;
  rerunning.value = id;
  try {
    await apex.rerunCampaign(id);
    apex.toast('Re-run started — unreached numbers will be dialed again.');
    await apex.reload();
  } catch (e) {
    apex.toast(apex.extractError(e, 'Re-run failed.'), 'err');
  } finally {
    rerunning.value = null;
  }
}

// Stop a running/ingesting campaign: dialing halts (in-flight calls finish),
// the one-campaign slot frees, results are kept. Cancelled ≠ deleted — the
// campaign stays in the list read-only; it can't be resumed or re-run.
const stopping = ref(null);
async function stop(id, name) {
  if (!id || stopping.value) return;
  if (!window.confirm(`Stop campaign "${name || 'this campaign'}"? Remaining contacts won't be dialed and it can't be resumed. Calls in progress finish, and everything captured so far is kept.`)) return;
  stopping.value = id;
  try {
    await apex.cancelCampaign(id);
    apex.toast('Campaign stopped — your campaign slot is free.');
    await apex.reload();
  } catch (e) {
    apex.toast(apex.extractError(e, 'Could not stop the campaign.'), 'err');
  } finally {
    stopping.value = null;
  }
}

const deleting = ref(null);
async function remove(id, name) {
  if (!id || deleting.value) return;
  if (!window.confirm(`Delete campaign "${name || 'this campaign'}"? This can't be undone. Leads already captured are kept.`)) return;
  deleting.value = id;
  try {
    await apex.deleteCampaign(id);
    apex.toast('Campaign deleted.');
    await apex.reload();
  } catch (e) {
    // Show the SERVER's reason — a 409 can be "stop it first" (running) OR
    // "linked records block it"; hardcoding one masked the other.
    apex.toast(apex.extractError(e, 'Could not delete the campaign.'), 'err');
  } finally {
    deleting.value = null;
  }
}

// Add a new CSV to an existing campaign, then resume dialing the new numbers.
const addFileInput = ref(null);
const addTarget = ref(null);   // campaign id the picked file will be added to
const addingTo = ref(null);
function pickAddCsv(id) {
  if (addingTo.value) return;
  addTarget.value = id;
  addFileInput.value && addFileInput.value.click();
}
async function onAddFile(e) {
  const file = e.target.files && e.target.files[0];
  e.target.value = ''; // allow re-picking the same file
  const id = addTarget.value;
  if (!file || !id) return;
  addingTo.value = id;
  try {
    await apex.addCampaignContacts(id, file);
    apex.toast('Adding contacts… new numbers will start dialing shortly. Use Re-run if the campaign does not resume on its own.', 'ok', 6500);
    // The append ingests in the background (no 'ingesting' status flip), so poll
    // a few times until the new pending rows land and the Re-run option appears.
    [2500, 7000, 15000].forEach((ms) => setTimeout(() => apex.reload(), ms));
  } catch (err) {
    apex.toast(apex.extractError(err, 'Could not add contacts.'), 'err');
  } finally {
    addingTo.value = null;
    addTarget.value = null;
  }
}
</script>

<template>
  <div class="ax-anim">
    <!-- create — 3-step wizard -->
    <div class="ax-card ax-card-pad">
      <div class="ax-card-head" style="align-items:flex-start;">
        <div>
          <h2 class="ax-h2">Bulk CSV calling</h2>
          <p class="ax-muted" style="margin:7px 0 0;">Upload a CSV of phone numbers and run a scored questionnaire on the whole list automatically.</p>
        </div>
        <span v-if="apex.bulkStatus.value.enabled" class="ax-status-on"><span class="ax-status-dot"></span>Enabled</span>
        <span v-else class="ax-status-off">Not enabled</span>
      </div>

      <!-- stepper -->
      <div class="ax-steps">
        <template v-for="(s, si) in STEPS" :key="s">
          <button
            type="button" class="ax-step"
            :class="{ 'is-on': step === si + 1, 'is-done': step > si + 1 }"
            @click="goStep(si + 1)"
          >
            <span class="ax-step-num"><AxIcon v-if="step > si + 1" name="check" :size="11" /><template v-else>{{ si + 1 }}</template></span>
            <span class="ax-step-lbl">{{ s }}</span>
          </button>
          <span v-if="si < STEPS.length - 1" class="ax-step-line" :class="{ 'is-done': step > si + 1 }"></span>
        </template>
      </div>

      <Transition name="axstep" mode="out-in">
        <!-- STEP 1 · Details -->
        <div v-if="step === 1" key="s1">
          <div class="ax-grid2" style="margin-top:8px;">
            <div>
              <label class="ax-field-label">Campaign name</label>
              <input v-model="form.name" type="text" class="ax-text" placeholder="December outreach" />
            </div>
            <div>
              <label class="ax-field-label">Contacts file <span class="ax-field-hint">CSV or XLSX — phone in column A, name in B</span></label>
              <div class="ax-file">
                <label class="ax-file-btn">Choose file<input type="file" accept=".csv,.xlsx" style="display:none;" @change="onFile" /></label>
                <span class="ax-file-name">{{ fileName || 'No file chosen' }}</span>
              </div>
            </div>
            <div>
              <label class="ax-field-label">Business name</label>
              <input v-model="form.company_name" type="text" class="ax-text" placeholder="Raghava Estates" />
            </div>
            <div>
              <label class="ax-field-label">Agent's name</label>
              <input v-model="form.caller_name" type="text" class="ax-text" />
            </div>
          </div>
          <div style="margin-top:22px;max-width:50%;padding-right:11px;">
            <label class="ax-field-label">Language</label>
            <input v-model="form.language" type="text" class="ax-text" />
          </div>
          <div style="margin-top:22px;">
            <label class="ax-field-label">Background / context <span class="ax-field-hint">optional — extra info the agent can lean on if the prospect goes off-script</span></label>
            <textarea v-model="form.content" rows="2" class="ax-text" placeholder="Optional: e.g. Raghava Estates — gated community in Kollur, ready December, ₹85L onward."></textarea>
          </div>
        </div>

        <!-- STEP 2 · Script & questions -->
        <div v-else-if="step === 2" key="s2">
          <div class="ax-qbuild" style="margin-top:8px;">
            <div class="ax-qbuild-head">
              <div>
                <div class="ax-field-label" style="margin:0;">Lead qualification questionnaire</div>
                <div class="ax-field-hint" style="font-size:12px;">the agent asks these, scores each, and auto-qualifies by lead score</div>
              </div>
              <button type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" @click="addQuestion"><AxIcon name="plus" :size="13" /> Add question</button>
            </div>
            <div style="margin-top:14px;">
              <label class="ax-field-label">Intro line <span class="ax-field-hint">how the agent opens the call</span></label>
              <textarea v-model="form.intro" rows="2" class="ax-text" placeholder="Hi, this is Riya from Raghava Estates — I'm calling about our new gated community in Kollur. Do you have a quick minute?"></textarea>
            </div>
            <div style="margin-top:14px;">
              <label class="ax-field-label">Outro line <span class="ax-field-hint">how the agent closes — and immediately when a dealbreaker fails</span></label>
              <textarea v-model="form.outro" rows="2" class="ax-text" placeholder="Thanks so much for your time — have a great day!"></textarea>
            </div>

            <div v-if="form.questions.length" class="ax-qlist">
              <div v-for="(q, i) in form.questions" :key="i" class="ax-q">
                <div class="ax-q-row">
                  <span class="ax-q-num">{{ i + 1 }}</span>
                  <select v-model="q.type" class="ax-text ax-q-type">
                    <option value="intent">Intent detection (yes/no)</option>
                    <option value="answer">Desired answer (match)</option>
                  </select>
                  <input v-model="q.text" type="text" class="ax-text ax-q-text" placeholder="Question the agent asks" />
                  <input v-if="q.type === 'answer' && !q.graded" v-model="q.desired_answer" type="text" class="ax-text ax-q-ans" placeholder="expected answer e.g. Kollur" />
                  <select v-else-if="q.type === 'intent'" v-model="q.required" class="ax-text ax-q-ans">
                    <option value="yes">Required: Yes</option>
                    <option value="no">Required: No</option>
                  </select>
                  <label v-if="!(q.type === 'answer' && q.graded)" class="ax-q-pts"><input v-model.number="q.points" type="number" min="1" class="ax-text" /><span>pts</span></label>
                  <button type="button" class="ax-q-del" title="Remove" @click="removeQuestion(i)"><AxIcon name="x" :size="13" /></button>
                </div>
                <label v-if="q.type === 'answer'" class="ax-q-check"><input type="checkbox" v-model="q.graded" /> <span>Graded scoring — different answers earn different points</span></label>
                <div v-if="q.type === 'answer' && q.graded" class="ax-tiers">
                  <div v-for="(t, ti) in q.tiers" :key="ti" class="ax-tier-wrap">
                    <div class="ax-tier" :class="{ 'ax-tier--default': t.default }">
                      <input v-model="t.label" type="text" class="ax-text ax-tier-label" :placeholder="t.default ? 'Any other answer (label ignored by scoring)' : 'band, e.g. above 1 crore'" :disabled="t.default" />
                      <label class="ax-q-pts"><input v-model.number="t.points" type="number" min="1" class="ax-text" /><span>pts</span></label>
                      <label class="ax-tier-catchall" title="Earns these points for any answer that matches no other band"><input type="checkbox" :checked="!!t.default" @change="setCatchAll(q, ti)" /> <span>Catch-all</span></label>
                      <button type="button" class="ax-q-del" @click="removeTier(q, ti)"><AxIcon name="x" :size="13" /></button>
                    </div>
                    <div v-if="t.default" class="ax-field-hint ax-tier-hint">Earns these points for any answer that doesn't match the other bands — the label above is for your reference only, scoring ignores it.</div>
                  </div>
                  <button type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" @click="addTier(q)"><AxIcon name="plus" :size="13" /> Add band</button>
                </div>
                <label v-if="!(q.type === 'answer' && q.graded)" class="ax-q-check"><input type="checkbox" v-model="q.gate" /> <span>Dealbreaker — if this isn't answered correctly, go to the outro and cut the call</span></label>
              </div>
              <div class="ax-thresh">
                <span class="ax-thresh-label">Qualify when score ≥</span>
                <input v-model.number="form.threshold" type="number" class="ax-text ax-thresh-input" :min="1" :max="maxPoints" />
                <span class="ax-field-hint">of {{ maxPoints }} point(s) available</span>
              </div>

              <!-- schedule + estimation -->
              <div class="ax-cap">
                <div class="ax-cap-inputs">
                  <label class="ax-field-label" style="margin:0;">Days / week <span class="ax-field-hint">Mon-first</span><input v-model.number="form.working_days" type="number" min="1" max="7" class="ax-text" style="width:120px;margin-top:6px;" /></label>
                  <label class="ax-field-label" style="margin:0;">Calls / day <span class="ax-field-hint">9 AM–7 PM IST</span><input v-model.number="form.calls_per_day" type="number" min="1" step="10" class="ax-text" style="width:120px;margin-top:6px;" /></label>
                </div>
                <div v-if="estSeconds" class="ax-cap-stats">
                  <div class="ax-cap-stat"><span class="ax-cap-val">{{ estLabel }}</span><span class="ax-cap-lbl">est. per call</span></div>
                  <div v-if="estimate.callDays != null" class="ax-cap-stat"><span class="ax-cap-val">{{ estimate.callDays }} days</span><span class="ax-cap-lbl">~{{ estimate.weeks }} week(s) to finish</span></div>
                  <div v-if="estimate.maxSpend != null" class="ax-cap-stat"><span class="ax-cap-val">{{ fmtCr(estimate.maxSpend) }}</span><span class="ax-cap-lbl">max spend · credits</span></div>
                  <div v-if="estimate.dailySpend != null" class="ax-cap-stat"><span class="ax-cap-val">{{ fmtCr(estimate.dailySpend) }}/day</span><span class="ax-cap-lbl">{{ form.calls_per_day }} × {{ fmtCr(estimate.perCallCredits) }} cr</span></div>
                </div>
                <p class="ax-field-hint" style="margin-top:8px;font-size:12px;">{{ listSize != null ? listSize.toLocaleString('en-IN') + ' contacts in the list · ' : '' }}Dials only 09:00–19:00 IST, {{ form.working_days }} day(s)/week, up to {{ form.calls_per_day }}/day.</p>
              </div>
            </div>
            <p v-else class="ax-field-hint" style="margin-top:12px;font-size:12.5px;">Add at least one question — set each question's points (or graded bands) to weight the lead score.</p>
          </div>
        </div>

        <!-- STEP 3 · Review & launch -->
        <div v-else key="s3">
          <div class="ax-review-grid">
            <div class="ax-cap-stat"><span class="ax-cap-val">{{ listSize != null ? listSize.toLocaleString('en-IN') : '—' }}</span><span class="ax-cap-lbl">contacts</span></div>
            <div class="ax-cap-stat"><span class="ax-cap-val">{{ form.questions.length }}</span><span class="ax-cap-lbl">questions</span></div>
            <div class="ax-cap-stat"><span class="ax-cap-val">{{ form.threshold }}/{{ maxPoints }}</span><span class="ax-cap-lbl">qualify threshold</span></div>
            <div class="ax-cap-stat"><span class="ax-cap-val">{{ estLabel || '—' }}</span><span class="ax-cap-lbl">est. per call</span></div>
            <div v-if="estimate.callDays != null" class="ax-cap-stat"><span class="ax-cap-val">{{ estimate.callDays }} days</span><span class="ax-cap-lbl">to finish</span></div>
            <div v-if="estimate.maxSpend != null" class="ax-cap-stat"><span class="ax-cap-val">{{ fmtCr(estimate.maxSpend) }}</span><span class="ax-cap-lbl">max spend · credits</span></div>
          </div>

          <div class="ax-review-script">
            <div class="ax-break-label" style="margin-bottom:16px;">Call script preview</div>
            <div class="ax-rv-bubble">{{ form.intro || `Hi, this is ${form.caller_name || 'the agent'}${form.company_name ? ' from ' + form.company_name : ''}…` }}</div>
            <ol v-if="form.questions.length" class="ax-rv-qs">
              <li v-for="(q, i) in form.questions" :key="i">
                <span>{{ q.text || '(empty question)' }}</span>
                <span v-if="q.gate" class="ax-rv-gate">dealbreaker</span>
              </li>
            </ol>
            <div class="ax-rv-bubble">{{ form.outro || 'Thanks so much for your time — have a great day!' }}</div>
            <p class="ax-field-hint" style="margin-top:14px;font-size:12px;">
              {{ form.name || 'Untitled campaign' }} · {{ form.language || 'en' }} · dials 09:00–19:00 IST,
              {{ form.working_days }} day(s)/week, up to {{ form.calls_per_day }}/day.
            </p>
          </div>

          <!-- hi/te script review: the exact lines the agent speaks when the
               caller is on Hindi/Telugu. Edits are used verbatim on calls. -->
          <div class="ax-review-script" style="margin-top:18px;">
            <div class="ax-break-label" style="margin-bottom:6px;display:flex;align-items:center;gap:12px;">
              <span>Hindi &amp; Telugu script</span>
              <button type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" :disabled="translating" @click="translatePreview">
                <AxIcon name="refresh" :size="12" /> {{ translating ? 'Translating…' : (translated ? 'Re-translate' : 'Translate & review') }}
              </button>
            </div>
            <p class="ax-field-hint" style="font-size:12px;">
              Callers who speak Hindi or Telugu hear these exact lines. Review the translations and edit anything that reads wrong — your edits are spoken verbatim.
            </p>
            <div v-if="translated" class="ax-i18n-grid">
              <template v-for="lang in [{ code: 'hi', label: 'Hindi' }, { code: 'te', label: 'Telugu' }]" :key="lang.code">
                <div class="ax-i18n-col">
                  <div class="ax-field-label" style="margin:10px 0 0;">{{ lang.label }}</div>
                  <label class="ax-field-hint" style="font-size:11.5px;">Intro
                    <textarea v-model="form.intro_i18n[lang.code]" rows="2" class="ax-text" style="margin-top:4px;"></textarea>
                  </label>
                  <template v-for="(q, i) in form.questions" :key="i">
                    <label v-if="q.text_i18n" class="ax-field-hint" style="font-size:11.5px;">Q{{ i + 1 }} · {{ (q.text || '').slice(0, 60) }}
                      <textarea v-model="q.text_i18n[lang.code]" rows="2" class="ax-text" style="margin-top:4px;"></textarea>
                    </label>
                  </template>
                  <label class="ax-field-hint" style="font-size:11.5px;">Outro
                    <textarea v-model="form.outro_i18n[lang.code]" rows="2" class="ax-text" style="margin-top:4px;"></textarea>
                  </label>
                </div>
              </template>
            </div>
            <p v-if="translateError" class="ax-notice ax-notice--err" style="margin-top:10px;">{{ translateError }}</p>
          </div>

          <p v-if="!form.file" class="ax-rv-warn"><AxIcon name="alert" :size="14" /> No contacts file yet — add one in step 1 to launch.</p>
          <p v-if="!form.questions.length" class="ax-rv-warn"><AxIcon name="alert" :size="14" /> No questions added — the agent will only deliver the intro and outro.</p>
          <p v-if="launchError" class="ax-notice ax-notice--err">{{ launchError }}</p>
        </div>
      </Transition>

      <!-- wizard nav -->
      <div class="ax-step-nav">
        <button v-if="step > 1" type="button" class="ax-btn2 ax-btn2--ghost" style="padding:13px 22px;" @click="step--"><AxIcon name="arrow-left" :size="14" /> Back</button>
        <span style="flex:1;"></span>
        <button v-if="step < 3" type="button" class="ax-btn2 ax-btn2--accent" :disabled="!canNext" @click="step++">Continue <AxIcon name="arrow-right" :size="14" /></button>
        <button v-else type="button" class="ax-btn2 ax-btn2--accent" :disabled="launching || !canLaunch" @click="submit">
          {{ launching ? 'Starting…' : 'Upload & start calling' }}
        </button>
      </div>
    </div>

    <!-- list -->
    <div class="ax-card" style="margin-top:28px;overflow:hidden;">
      <div class="ax-card-head" style="padding:26px 36px 22px;">
        <h2 class="ax-h2">Campaigns</h2>
        <button type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" @click="apex.reload()"><AxIcon name="refresh" :size="13" /> Refresh</button>
      </div>
      <div v-if="!det.length" style="padding:0 36px 30px;color:rgba(255,255,255,0.4);font-size:14px;">No deterministic campaigns yet — create one above.</div>
      <div v-for="(c, ci) in det" :key="c.id" class="ax-camp-row ax-row-in" :style="{ '--i': Math.min(ci, 14) }">
        <div class="ax-camp-main">
          <span class="ax-camp-dot" :class="`is-${statusTone(c.status)}`"></span>
          <span class="ax-camp-name">{{ c.name }}</span>
          <span class="ax-camp-status" :class="`is-${statusTone(c.status)}`">{{ c.status }}</span>
          <span class="ax-camp-mode">Deterministic</span>
          <span v-if="campaignWindow(c)" class="ax-camp-mode">{{ campaignWindow(c) }}</span>
        </div>
        <div class="ax-camp-meta">
          <span v-if="counts(c).successful" style="color:#7FD9A8;">{{ counts(c).successful }} qualified</span>
          <span v-if="counts(c).no_pickup" style="color:#E62630;">{{ counts(c).no_pickup }} missed</span>
          <button v-if="redialCount(c) > 0 && c.status !== 'cancelled'" type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" :disabled="rerunning === c.id" @click="rerun(c.id)">
            <AxIcon name="refresh" :size="12" /> {{ rerunning === c.id ? 'Re-running…' : `Re-run (${redialCount(c)})` }}
          </button>
          <button v-if="c.status !== 'cancelled'" type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" :disabled="addingTo === c.id || c.status === 'ingesting'" @click="pickAddCsv(c.id)" title="Add a CSV of new numbers and resume dialing">
            <AxIcon name="plus" :size="12" /> {{ addingTo === c.id ? 'Adding…' : 'Add CSV' }}
          </button>
          <button v-if="c.status === 'running' || c.status === 'ingesting'" type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm ax-camp-del" :disabled="stopping === c.id" @click="stop(c.id, c.name)" title="Stop dialing — in-flight calls finish, results are kept">
            <AxIcon name="stop" :size="12" /> {{ stopping === c.id ? 'Stopping…' : 'Stop' }}
          </button>
          <button type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm ax-camp-del" :disabled="deleting === c.id" @click="remove(c.id, c.name)" title="Delete campaign">
            <AxIcon name="trash" :size="12" /> {{ deleting === c.id ? 'Deleting…' : 'Delete' }}
          </button>
        </div>
        <div v-if="prog(c).total" class="ax-camp-prog">
          <div class="ax-progress" :class="{ 'ax-progress--live': c.status === 'running' || c.status === 'ingesting' }">
            <span class="ax-progress-fill" :style="{ width: prog(c).pct + '%' }"></span>
          </div>
          <span class="ax-camp-prog-lbl">{{ prog(c).done.toLocaleString('en-IN') }} of {{ prog(c).total.toLocaleString('en-IN') }} dialed</span>
        </div>
        <div v-else class="ax-camp-prog"><span class="ax-camp-prog-lbl">{{ c.total_count || 0 }} dialed</span></div>
      </div>
    </div>
    <!-- shared hidden picker for "Add CSV" (one per list; target set on click) -->
    <input ref="addFileInput" type="file" accept=".csv,.xlsx" style="display:none" @change="onAddFile" />
  </div>
</template>

<style scoped>
.ax-status-on { display: inline-flex; align-items: center; gap: 7px; font-size: 11.5px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: #7FD9A8; background: rgba(74,200,140,0.09); border: 1px solid rgba(74,200,140,0.24); padding: 5px 13px; border-radius: 999px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.04); }
@keyframes axPulse { 0% { box-shadow: 0 0 0 0 rgba(127,217,168,0.4); } 70% { box-shadow: 0 0 0 5px rgba(127,217,168,0); } 100% { box-shadow: 0 0 0 0 rgba(127,217,168,0); } }
.ax-status-dot { width: 6px; height: 6px; border-radius: 50%; background: #7FD9A8; animation: axPulse 2.2s ease-out infinite; }
.ax-status-off { font-size: 11.5px; font-weight: 600; text-transform: uppercase; color: rgba(255,255,255,0.34); background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.09); padding: 5px 13px; border-radius: 999px; }

/* ── stepper ── */
.ax-steps { display: flex; align-items: center; gap: 12px; margin: 26px 0 28px; flex-wrap: wrap; }
.ax-step { display: inline-flex; align-items: center; gap: 10px; background: transparent; border: none; padding: 4px; cursor: pointer; font-family: 'Sora', sans-serif; color: rgba(255,255,255,0.42); transition: color .18s; }
.ax-step:hover { color: rgba(255,255,255,0.75); }
.ax-step-num {
  width: 26px; height: 26px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center;
  font-family: 'JetBrains Mono', monospace; font-size: 12px; flex: none;
  border: 1px solid rgba(255,255,255,0.16); background: rgba(255,255,255,0.03);
  transition: all .22s cubic-bezier(0.22, 1, 0.36, 1);
}
.ax-step-lbl { font-size: 13px; font-weight: 500; white-space: nowrap; }
.ax-step.is-on { color: #F3F2F0; }
.ax-step.is-on .ax-step-num { border-color: rgba(230,38,48,0.75); background: rgba(230,38,48,0.14); color: #fff; box-shadow: 0 0 14px -4px rgba(230,38,48,0.6); }
.ax-step.is-on .ax-step-lbl { font-weight: 600; }
.ax-step.is-done { color: rgba(255,255,255,0.62); }
.ax-step.is-done .ax-step-num { border-color: rgba(74,200,140,0.4); background: rgba(74,200,140,0.1); color: #7FD9A8; }
.ax-step-line { flex: 1 1 24px; min-width: 24px; height: 1px; background: rgba(255,255,255,0.09); }
.ax-step-line.is-done { background: rgba(74,200,140,0.35); }
.ax-step-nav { display: flex; align-items: center; gap: 10px; margin-top: 30px; }
.ax-step-nav .ax-btn2--accent { padding: 14px 26px; font-size: 14px; }
/* step swap */
.axstep-enter-active { transition: opacity .18s ease, transform .18s cubic-bezier(0.22, 1, 0.36, 1); }
.axstep-leave-active { transition: opacity .12s ease; }
.axstep-enter-from { opacity: 0; transform: translateY(8px); }
.axstep-leave-to { opacity: 0; }

/* ── review ── */
.ax-review-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; margin-top: 8px; }
.ax-review-script { border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; padding: 22px 24px; margin-top: 18px; background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.005)); box-shadow: inset 0 1px 0 rgba(255,255,255,0.035); }
.ax-rv-bubble {
  padding: 12px 16px; font-size: 13.5px; line-height: 1.55; max-width: 85%; color: rgba(255,255,255,0.87);
  background: linear-gradient(180deg, rgba(255,255,255,0.05), rgba(255,255,255,0.028));
  border: 1px solid rgba(255,255,255,0.07); border-radius: 13px 13px 13px 4px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.04), 0 4px 12px -8px rgba(0,0,0,0.4);
}
.ax-rv-qs { margin: 16px 0; padding-left: 24px; display: grid; gap: 9px; }
.ax-rv-qs li { font-size: 13.5px; color: rgba(255,255,255,0.72); line-height: 1.5; }
.ax-rv-gate { font-family: 'JetBrains Mono', monospace; font-size: 10px; letter-spacing: 0.06em; text-transform: uppercase; color: #F0666E; border: 1px solid rgba(230,38,48,0.35); background: rgba(230,38,48,0.07); border-radius: 999px; padding: 2px 9px; margin-left: 9px; }
.ax-rv-warn { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #D6A15C; margin: 14px 0 0; }

/* ── hi/te translation review ── */
.ax-i18n-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 8px; }
@media (max-width: 900px) { .ax-i18n-grid { grid-template-columns: 1fr; } }
.ax-i18n-col { display: grid; gap: 10px; align-content: start; }
.ax-i18n-col textarea { width: 100%; }

/* ── questionnaire builder ── */
.ax-qbuild { border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; padding: 18px 20px; margin-top: 22px; background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0.005)); box-shadow: inset 0 1px 0 rgba(255,255,255,0.035); }
.ax-qbuild-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.ax-qlist { margin-top: 16px; display: grid; gap: 14px; }
.ax-q { border: 1px solid rgba(255,255,255,0.07); border-radius: 11px; padding: 14px; display: grid; gap: 10px; background: rgba(0,0,0,0.14); box-shadow: inset 0 1px 3px rgba(0,0,0,0.15); transition: border-color .18s; }
.ax-q:hover { border-color: rgba(255,255,255,0.12); }
.ax-q-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ax-q-num { width: 22px; height: 22px; border-radius: 50%; background: rgba(255,255,255,0.06); display: flex; align-items: center; justify-content: center; font-size: 12px; color: rgba(255,255,255,0.6); flex: none; }
.ax-q-type { flex: 0 0 auto; max-width: 200px; }
.ax-q-text { flex: 1 1 240px; }
.ax-q-ans { flex: 0 0 auto; max-width: 200px; }
.ax-q-pts { display: inline-flex; align-items: center; gap: 5px; color: rgba(255,255,255,0.5); font-size: 12px; }
.ax-q-pts .ax-text { width: 60px; padding: 8px; }
.ax-q-del { background: transparent; border: 1px solid rgba(255,255,255,0.14); color: rgba(255,255,255,0.55); border-radius: 4px; width: 30px; height: 30px; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; transition: all .16s; }
.ax-q-del:hover { color: #F0666E; border-color: rgba(230,38,48,0.4); }
.ax-q-check { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: rgba(255,255,255,0.55); }
.ax-tiers { display: grid; gap: 8px; padding-left: 8px; }
.ax-tier-wrap { display: grid; gap: 4px; }
.ax-tier { display: flex; align-items: center; gap: 8px; }
.ax-tier--default .ax-tier-label { opacity: 0.5; font-style: italic; }
.ax-tier-label { flex: 1 1 auto; }
.ax-tier-catchall { display: inline-flex; align-items: center; gap: 5px; color: rgba(255,255,255,0.6); font-size: 12px; white-space: nowrap; cursor: pointer; }
.ax-tier-hint { padding-left: 2px; }
.ax-thresh { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 14px; }
.ax-thresh-label { font-size: 12.5px; font-weight: 600; }
.ax-thresh-input { width: 72px; }
.ax-cap { border-top: 1px solid rgba(255,255,255,0.08); margin-top: 14px; padding-top: 14px; display: grid; gap: 12px; }
.ax-cap-inputs { display: flex; gap: 16px; flex-wrap: wrap; }
.ax-cap-stats { display: flex; gap: 10px; flex-wrap: wrap; }
.ax-cap-stat { flex: 1 1 120px; display: grid; gap: 2px; padding: 10px 13px; background: linear-gradient(180deg, rgba(255,255,255,0.045), rgba(255,255,255,0.015)); border: 1px solid rgba(255,255,255,0.08); border-radius: 11px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.05), 0 8px 18px -12px rgba(0,0,0,0.5); }
.ax-cap-val { font-size: 17px; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
.ax-cap-lbl { font-size: 11px; color: rgba(255,255,255,0.4); }

/* ── campaign list rows ── */
.ax-camp-row { display: grid; grid-template-columns: 1fr auto; gap: 12px 18px; align-items: center; padding: 20px 36px; border-top: 1px solid rgba(255,255,255,0.055); transition: background .16s; }
.ax-camp-row:hover { background: rgba(255,255,255,0.022); }
.ax-camp-main { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.ax-camp-dot { width: 8px; height: 8px; border-radius: 50%; }
.ax-camp-dot.is-run { background: #7FD9A8; box-shadow: 0 0 8px rgba(127,217,168,0.5); animation: axPulse 2.2s ease-out infinite; }
.ax-camp-dot.is-done { background: rgba(255,255,255,0.3); } .ax-camp-dot.is-off { background: rgba(255,255,255,0.2); }
.ax-camp-name { font-size: 16px; font-weight: 600; letter-spacing: -0.005em; }
.ax-camp-status { display: inline-flex; align-items: center; padding: 4px 12px; border-radius: 999px; font-size: 11px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; border: 1px solid transparent; }
.ax-camp-status.is-run { background: rgba(74,200,140,0.09); color: #7FD9A8; border-color: rgba(74,200,140,0.24); } .ax-camp-status.is-done { background: rgba(255,255,255,0.045); color: rgba(255,255,255,0.5); border-color: rgba(255,255,255,0.1); } .ax-camp-status.is-off { background: rgba(255,255,255,0.035); color: rgba(255,255,255,0.34); border-color: rgba(255,255,255,0.08); }
.ax-camp-mode { font-family: 'JetBrains Mono', monospace; font-size: 11px; padding: 4px 10px; border: 1px solid rgba(255,255,255,0.13); border-radius: 999px; color: rgba(255,255,255,0.55); background: rgba(255,255,255,0.02); }
.ax-camp-meta { display: flex; align-items: center; gap: 18px; font-family: 'JetBrains Mono', monospace; font-size: 12.5px; flex-wrap: wrap; }
.ax-camp-del:hover:not(:disabled) { color: #E62630; border-color: rgba(230,38,48,0.4); background: rgba(230,38,48,0.08); }
.ax-camp-prog { grid-column: 1 / -1; display: flex; align-items: center; gap: 14px; }
.ax-camp-prog .ax-progress { flex: 1 1 auto; }
.ax-camp-prog-lbl { font-family: 'JetBrains Mono', monospace; font-size: 11.5px; color: rgba(255,255,255,0.45); white-space: nowrap; }

@media (max-width: 720px) {
  .ax-camp-row { padding: 18px; grid-template-columns: 1fr; }
  .ax-steps { gap: 8px; }
  .ax-step-line { display: none; }
}
</style>
