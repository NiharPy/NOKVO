<script setup>
// APEX Campaign tab — DETERMINISTIC bulk create (intro + questionnaire + outro +
// threshold + working window) and the deterministic campaign list with outcome
// counts. Reuses the shared bulkCalling.js logic + the existing endpoints.
import { inject, ref, computed } from 'vue';
import {
  newQuestion, maxPointsFor, estCallSeconds, fmtDuration, scheduleEstimate, countCsvRows,
  buildBulkCampaignFormData, categorizeContacts, leadCategory, campaignWindow,
} from '../../composables/bulkCalling.js';

const apex = inject('apex');

const emptyForm = () => ({
  name: '', company_name: '', caller_name: 'Riya', language: 'en',
  content: '', file: null, intro: '', outro: '', questions: [], threshold: 1,
  working_days: 5, hours_per_day: 10, calls_per_day: 200,
});
const form = ref(emptyForm());
const fileName = ref('');
const listSize = ref(null); // CSV row count (data rows), for the estimation widget
const launching = ref(false);
const errorMsg = ref('');
const notice = ref('');

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
function statusTone(s) {
  if (s === 'running') return 'run';
  if (s === 'completed') return 'done';
  if (s === 'ingesting') return 'run';
  return 'off';
}
function redialCount(c) {
  if (c.v2) return counts(c).pending;  // pending rows are what a relaunch would dial
  return (c.contacts || []).filter((ct) => ct.status !== 'answered' && !ct.answered_at).length;
}

async function submit() {
  errorMsg.value = ''; notice.value = '';
  launching.value = true;
  try {
    const fd = buildBulkCampaignFormData(form.value, { deterministic: true });
    const data = await apex.createCampaign(fd);
    const contacts = Array.isArray(data?.contacts) ? data.contacts : [];
    const failed = contacts.filter((c) => c.status === 'failed');
    if (failed.length && failed.length >= contacts.length) {
      errorMsg.value = `Calls couldn't be placed: ${failed[0]?.error || 'check the dedicated number setup'}.`;
    } else {
      const note = failed.length ? ` (${failed.length} couldn't be placed)` : '';
      notice.value = `Calling started — ${data?.total_count || 0} contacts${note}.`;
    }
    form.value = emptyForm();
    fileName.value = '';
    await apex.reload();
  } catch (e) {
    errorMsg.value = e?.message || apex.extractError(e, 'Could not start the campaign.');
  } finally {
    launching.value = false;
  }
}

const rerunning = ref(null);
async function rerun(id) {
  if (!id || rerunning.value) return;
  rerunning.value = id;
  errorMsg.value = ''; notice.value = '';
  try {
    await apex.rerunCampaign(id);
    await apex.reload();
  } catch (e) {
    errorMsg.value = apex.extractError(e, 'Re-run failed.');
  } finally {
    rerunning.value = null;
  }
}

const deleting = ref(null);
async function remove(id, name) {
  if (!id || deleting.value) return;
  if (!window.confirm(`Delete campaign "${name || 'this campaign'}"? This can't be undone. Leads already captured are kept.`)) return;
  deleting.value = id;
  errorMsg.value = ''; notice.value = '';
  try {
    await apex.deleteCampaign(id);
    await apex.reload();
  } catch (e) {
    // Running campaigns are protected server-side (409).
    errorMsg.value = e?.response?.status === 409
      ? 'Cancel the campaign before deleting it — running campaigns are protected.'
      : apex.extractError(e, 'Could not delete the campaign.');
  } finally {
    deleting.value = null;
  }
}
</script>

<template>
  <div class="ax-anim">
    <!-- create -->
    <div class="ax-card ax-card-pad">
      <div class="ax-card-head" style="align-items:flex-start;">
        <div>
          <h2 class="ax-h2">Bulk CSV calling</h2>
          <p class="ax-muted" style="margin:7px 0 0;">Upload a CSV of phone numbers and run a scored questionnaire on the whole list automatically.</p>
        </div>
        <span v-if="apex.bulkStatus.value.enabled" class="ax-status-on"><span class="ax-status-dot"></span>Enabled</span>
        <span v-else class="ax-status-off">Not enabled</span>
      </div>

      <div class="ax-grid2" style="margin-top:28px;">
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

      <!-- questionnaire builder -->
      <div class="ax-qbuild">
        <div class="ax-qbuild-head">
          <div>
            <div class="ax-field-label" style="margin:0;">Lead qualification questionnaire</div>
            <div class="ax-field-hint" style="font-size:12px;">the agent asks these, scores each, and auto-qualifies by lead score</div>
          </div>
          <button type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" @click="addQuestion">+ Add question</button>
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
              <button type="button" class="ax-q-del" title="Remove" @click="removeQuestion(i)">×</button>
            </div>
            <label v-if="q.type === 'answer'" class="ax-q-check"><input type="checkbox" v-model="q.graded" /> <span>Graded scoring — different answers earn different points</span></label>
            <div v-if="q.type === 'answer' && q.graded" class="ax-tiers">
              <div v-for="(t, ti) in q.tiers" :key="ti" class="ax-tier-wrap">
                <div class="ax-tier" :class="{ 'ax-tier--default': t.default }">
                  <input v-model="t.label" type="text" class="ax-text ax-tier-label" :placeholder="t.default ? 'Any other answer (label ignored by scoring)' : 'band, e.g. above 1 crore'" :disabled="t.default" />
                  <label class="ax-q-pts"><input v-model.number="t.points" type="number" min="1" class="ax-text" /><span>pts</span></label>
                  <label class="ax-tier-catchall" title="Earns these points for any answer that matches no other band"><input type="checkbox" :checked="!!t.default" @change="setCatchAll(q, ti)" /> <span>Catch-all</span></label>
                  <button type="button" class="ax-q-del" @click="removeTier(q, ti)">×</button>
                </div>
                <div v-if="t.default" class="ax-field-hint ax-tier-hint">Earns these points for any answer that doesn't match the other bands — the label above is for your reference only, scoring ignores it.</div>
              </div>
              <button type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" @click="addTier(q)">+ Add band</button>
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

      <p v-if="errorMsg" class="ax-notice ax-notice--err">{{ errorMsg }}</p>
      <p v-if="notice" class="ax-notice ax-notice--ok">{{ notice }}</p>
      <button type="button" class="ax-btn2 ax-btn2--accent" style="margin-top:24px;" :disabled="launching" @click="submit">
        {{ launching ? 'Starting…' : 'Upload & start calling' }}
      </button>
    </div>

    <!-- list -->
    <div class="ax-card" style="margin-top:28px;overflow:hidden;">
      <div class="ax-card-head" style="padding:26px 36px 22px;">
        <h2 class="ax-h2">Campaigns</h2>
        <button type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" @click="apex.reload()">↻ Refresh</button>
      </div>
      <div v-if="!det.length" style="padding:0 36px 30px;color:rgba(255,255,255,0.4);font-size:14px;">No deterministic campaigns yet — create one above.</div>
      <div v-for="c in det" :key="c.id" class="ax-camp-row">
        <div class="ax-camp-main">
          <span class="ax-camp-dot" :class="`is-${statusTone(c.status)}`"></span>
          <span class="ax-camp-name">{{ c.name }}</span>
          <span class="ax-camp-status" :class="`is-${statusTone(c.status)}`">{{ c.status }}</span>
          <span class="ax-camp-mode">Deterministic</span>
          <span v-if="campaignWindow(c)" class="ax-camp-mode">{{ campaignWindow(c) }}</span>
        </div>
        <div class="ax-camp-meta">
          <span style="color:rgba(255,255,255,0.55);">{{ c.total_count || 0 }} dialed</span>
          <span v-if="counts(c).successful" style="color:#7FD9A8;">{{ counts(c).successful }} qualified</span>
          <span v-if="counts(c).no_pickup" style="color:#E62630;">{{ counts(c).no_pickup }} missed</span>
          <button v-if="redialCount(c) > 0" type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" :disabled="rerunning === c.id" @click="rerun(c.id)">
            ↻ {{ rerunning === c.id ? 'Re-running…' : `Re-run (${redialCount(c)})` }}
          </button>
          <button type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm ax-camp-del" :disabled="deleting === c.id" @click="remove(c.id, c.name)" title="Delete campaign">
            {{ deleting === c.id ? 'Deleting…' : '🗑 Delete' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ax-status-on { display: inline-flex; align-items: center; gap: 7px; font-size: 11.5px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: #7FD9A8; background: rgba(74,200,140,0.1); padding: 5px 12px; border-radius: 5px; }
.ax-status-dot { width: 6px; height: 6px; border-radius: 50%; background: #7FD9A8; }
.ax-status-off { font-size: 11.5px; font-weight: 600; text-transform: uppercase; color: rgba(255,255,255,0.34); background: rgba(255,255,255,0.04); padding: 5px 12px; border-radius: 5px; }

.ax-qbuild { border: 1px solid rgba(255,255,255,0.09); border-radius: 7px; padding: 18px 20px; margin-top: 22px; }
.ax-qbuild-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.ax-qlist { margin-top: 16px; display: grid; gap: 14px; }
.ax-q { border: 1px solid rgba(255,255,255,0.07); border-radius: 6px; padding: 14px; display: grid; gap: 10px; }
.ax-q-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ax-q-num { width: 22px; height: 22px; border-radius: 50%; background: rgba(255,255,255,0.06); display: flex; align-items: center; justify-content: center; font-size: 12px; color: rgba(255,255,255,0.6); flex: none; }
.ax-q-type { flex: 0 0 auto; max-width: 200px; }
.ax-q-text { flex: 1 1 240px; }
.ax-q-ans { flex: 0 0 auto; max-width: 200px; }
.ax-q-pts { display: inline-flex; align-items: center; gap: 5px; color: rgba(255,255,255,0.5); font-size: 12px; }
.ax-q-pts .ax-text { width: 60px; padding: 8px; }
.ax-q-del { background: transparent; border: 1px solid rgba(255,255,255,0.14); color: rgba(255,255,255,0.55); border-radius: 4px; width: 30px; height: 30px; cursor: pointer; font-size: 16px; }
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
.ax-cap-stat { flex: 1 1 120px; display: grid; gap: 2px; padding: 8px 12px; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; }
.ax-cap-val { font-size: 17px; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
.ax-cap-lbl { font-size: 11px; color: rgba(255,255,255,0.4); }

.ax-camp-row { display: grid; grid-template-columns: 1fr auto; gap: 18px; align-items: center; padding: 20px 36px; border-top: 1px solid rgba(255,255,255,0.06); }
.ax-camp-main { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.ax-camp-dot { width: 8px; height: 8px; border-radius: 50%; }
.ax-camp-dot.is-run { background: #7FD9A8; } .ax-camp-dot.is-done { background: rgba(255,255,255,0.3); } .ax-camp-dot.is-off { background: rgba(255,255,255,0.2); }
.ax-camp-name { font-size: 16px; font-weight: 600; }
.ax-camp-status { display: inline-flex; align-items: center; padding: 4px 11px; border-radius: 5px; font-size: 11.5px; font-weight: 600; text-transform: uppercase; }
.ax-camp-status.is-run { background: rgba(74,200,140,0.1); color: #7FD9A8; } .ax-camp-status.is-done { background: rgba(255,255,255,0.05); color: rgba(255,255,255,0.5); } .ax-camp-status.is-off { background: rgba(255,255,255,0.04); color: rgba(255,255,255,0.34); }
.ax-camp-mode { font-family: 'JetBrains Mono', monospace; font-size: 11px; padding: 4px 9px; border: 1px solid rgba(255,255,255,0.14); border-radius: 4px; color: rgba(255,255,255,0.55); }
.ax-camp-meta { display: flex; align-items: center; gap: 18px; font-family: 'JetBrains Mono', monospace; font-size: 12.5px; flex-wrap: wrap; }
.ax-camp-del:hover:not(:disabled) { color: #E62630; border-color: rgba(230,38,48,0.4); background: rgba(230,38,48,0.08); }
</style>
