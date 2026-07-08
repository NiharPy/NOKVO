<script setup>
// APEX Call Logs — each dialed contact + its transcript.
// V2 campaigns keep contacts in per-row storage (the inline blob is empty), so
// rows come from the paginated /contacts endpoint's "dialed" bucket; legacy
// blob campaigns keep reading inline. Transcripts are keyed by call_link_id
// (the internal call id) — NOT Plivo's call_id.
import { inject, ref, computed, onMounted, watch } from 'vue';
import { useNewRows } from '../../composables/axMotion.js';
import AxIcon from '../AxIcon.vue';

const apex = inject('apex');
// Flash calls that arrived on a refresh — the dialer just finished someone.
const newRows = useNewRows((r) => r.call_link_id || r.call_id);
const filter = ref(null);
const selected = ref(null); // { call_link_id, call_id, name, phone, status, time }
const transcript = ref(null);
const loadingT = ref(false);
const v2Rows = ref([]);       // dialed rows fetched for V2 campaigns
const loadingRows = ref(false);

const scoped = computed(() => {
  const all = apex.deterministicCampaigns.value;
  return filter.value ? all.filter((c) => String(c.id) === String(filter.value)) : all;
});

async function loadV2Rows() {
  const v2Camps = scoped.value.filter((c) => c.v2);
  if (!v2Camps.length) { v2Rows.value = []; return; }
  loadingRows.value = true;
  try {
    const batches = await Promise.all(v2Camps.map(async (c) => {
      try {
        const data = await apex.fetchCampaignContacts(c.id, 'dialed', null, 200);
        return (data?.rows || []).map((r) => ({ ...r, _campaign: c.name }));
      } catch { return []; }
    }));
    v2Rows.value = batches.flat();
  } finally {
    loadingRows.value = false;
  }
}
onMounted(loadV2Rows);
watch([filter, () => apex.deterministicCampaigns.value], loadV2Rows);

const calls = computed(() => {
  const rows = [];
  for (const r of v2Rows.value) {
    rows.push({
      call_link_id: r.call_link_id,
      call_id: r.call_id,
      name: r.name || r.phone,
      phone: r.phone,
      status: r.status,
      answered_at: r.answered_at,
      time: r.answered_at ? new Date(r.answered_at).toLocaleString('en-IN') : '—',
    });
  }
  for (const c of scoped.value) {
    if (c.v2) continue; // covered above
    for (const ct of c.contacts || []) {
      if (!ct.call_id) continue;
      rows.push({
        call_link_id: ct.call_link_id,
        call_id: ct.call_id,
        name: ct.name || ct.phone,
        phone: ct.phone,
        status: ct.status,
        answered_at: ct.answered_at,
        time: ct.answered_at ? new Date(ct.answered_at).toLocaleString('en-IN') : '—',
      });
    }
  }
  return rows.sort((a, b) => String(b.answered_at || '').localeCompare(String(a.answered_at || '')));
});
watch(calls, (rows) => newRows.track(rows || []), { immediate: true });

function statusType(s) {
  if (s === 'answered' || s === 'completed') return 'pos';
  if (s === 'calling' || s === 'ringing' || s === 'dialing') return 'live';
  return 'neg';
}

async function open(row) {
  selected.value = row;
  transcript.value = null;
  loadingT.value = true;
  try {
    // Transcripts are stored under the INTERNAL call id (= call_link_id for
    // outbound). Plivo's call_id is only a fallback for ancient rows.
    const data = await apex.fetchTranscript(row.call_link_id || row.call_id);
    // Normalize: API returns { turns:[{role,content}] } or { transcript:[...] }.
    const turns = data?.turns || data?.transcript || data?.lines || [];
    transcript.value = turns.map((t) => ({
      who: (t.role === 'user' || t.role === 'lead' || t.mine) ? (row.name) : (t.speaker || 'Agent'),
      text: t.content || t.text || '',
      mine: t.role === 'user' || t.role === 'lead' || !!t.mine,
    })).filter((l) => l.text);
  } catch {
    transcript.value = [];
  } finally {
    loadingT.value = false;
  }
}
</script>

<template>
  <div class="ax-anim">
    <div v-if="apex.deterministicCampaigns.value.length > 1" class="ax-filters" style="margin-bottom:24px;">
      <button type="button" class="ax-chip" :class="{ 'is-active': filter === null }" @click="filter = null">All</button>
      <button v-for="c in apex.deterministicCampaigns.value" :key="c.id" type="button" class="ax-chip" :class="{ 'is-active': filter === c.id }" @click="filter = c.id">{{ c.name }}</button>
    </div>

    <div class="ax-logs">
      <div class="ax-logs-list">
        <template v-if="loadingRows && !calls.length">
          <div v-for="i in 4" :key="i" class="ax-skel-card">
            <span class="ax-skel-bar" style="width:55%"></span>
            <span class="ax-skel-bar" style="width:70%"></span>
            <span class="ax-skel-bar" style="width:40%"></span>
          </div>
        </template>
        <div v-else-if="!calls.length" class="ax-empty"><div class="ax-empty-icon"><AxIcon name="phone" :size="20" /></div><p class="ax-empty-text">No calls yet for these campaigns.</p></div>
        <button v-for="(row, i) in calls" :key="row.call_link_id || row.call_id" type="button" class="ax-call ax-row-in" :style="{ '--i': Math.min(i, 14) }" :class="{ 'is-active': selected && (selected.call_link_id || selected.call_id) === (row.call_link_id || row.call_id), 'is-new': newRows.isNew(row) }" @click="open(row)">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <span style="font-size:15px;font-weight:600;">{{ row.name }}</span>
            <span class="ax-spill" :class="`is-${statusType(row.status)}`">{{ row.status }}</span>
          </div>
          <div class="ax-call-phone">{{ row.phone }}</div>
          <div class="ax-call-time">{{ row.time }}</div>
        </button>
      </div>

      <div class="ax-logs-detail">
        <div v-if="!selected" class="ax-logs-empty">Select a call to read its transcript.</div>
        <template v-else>
          <div class="ax-logs-head">
            <div style="font-size:17px;font-weight:600;">{{ selected.name }}</div>
            <div class="ax-call-phone" style="margin-top:6px;">{{ selected.phone }} · {{ selected.status }} · {{ selected.time }}</div>
          </div>
          <div v-if="loadingT" style="display:grid;gap:14px;">
            <span class="ax-skel-bar" style="width:60%;height:38px;border-radius:13px;"></span>
            <span class="ax-skel-bar" style="width:45%;height:38px;border-radius:13px;justify-self:end;"></span>
            <span class="ax-skel-bar" style="width:70%;height:38px;border-radius:13px;"></span>
          </div>
          <div v-else-if="!transcript || !transcript.length" class="ax-logs-empty">
            {{ selected.status === 'failed' ? "This call didn't connect — no transcript available." : 'No transcript available — it may have expired (kept 30 days) or the call was too short.' }}
          </div>
          <div v-else>
            <div v-for="(l, li) in transcript" :key="li" class="ax-tline" :class="{ 'is-mine': l.mine }">
              <div class="ax-tline-who">{{ l.mine ? selected.name : l.who }}</div>
              <div class="ax-bubble" :class="l.mine ? 'ax-bubble--mine' : 'ax-bubble--agent'">{{ l.text }}</div>
            </div>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ax-logs { display: grid; grid-template-columns: 360px 1fr; gap: 22px; align-items: start; }
.ax-logs-list { display: flex; flex-direction: column; gap: 12px; }
.ax-call { position: relative; width: 100%; text-align: left; cursor: pointer; border-radius: 13px; padding: 18px 20px; background: linear-gradient(180deg, rgba(255,255,255,0.03), rgba(255,255,255,0.012)); border: 1px solid rgba(255,255,255,0.08); transition: all .18s var(--ax-out); font-family: 'Sora', sans-serif; display: block; color: #F3F2F0; box-shadow: inset 0 1px 0 rgba(255,255,255,0.04); overflow: hidden; }
.ax-call:hover:not(.is-active) { border-color: rgba(255,255,255,0.18); transform: translateY(-1px); box-shadow: inset 0 1px 0 rgba(255,255,255,0.05), 0 10px 24px -14px rgba(0,0,0,0.55); }
.ax-call.is-active { border-color: rgba(230,38,48,0.5); background: linear-gradient(180deg, rgba(230,38,48,0.08), rgba(230,38,48,0.04)); box-shadow: inset 0 1px 0 rgba(255,255,255,0.05), 0 12px 28px -14px rgba(230,38,48,0.25); }
.ax-call-phone { font-family: 'JetBrains Mono', monospace; font-size: 12.5px; color: rgba(255,255,255,0.45); margin-top: 11px; }
.ax-call-time { font-family: 'JetBrains Mono', monospace; font-size: 11.5px; color: rgba(255,255,255,0.3); margin-top: 6px; }
.ax-spill { font-family: 'JetBrains Mono', monospace; font-size: 11px; letter-spacing: 0.04em; padding: 4px 11px; border-radius: 999px; }
.ax-spill.is-pos { color: #7FD9A8; border: 1px solid rgba(127,217,168,0.3); background: rgba(74,200,140,0.06); }
.ax-spill.is-neg { color: rgba(255,255,255,0.55); border: 1px solid rgba(255,255,255,0.16); }
.ax-spill.is-live { color: #F0666E; border: 1px solid rgba(230,38,48,0.4); background: rgba(230,38,48,0.08); box-shadow: 0 0 12px -4px rgba(230,38,48,0.4); }
.ax-logs-detail { background: linear-gradient(180deg, rgba(255,255,255,0.035), rgba(255,255,255,0.012)); border: 1px solid rgba(255,255,255,0.085); border-radius: 16px; min-height: 420px; padding: 28px 30px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.05), 0 24px 48px -24px rgba(0,0,0,0.55); }
.ax-logs-empty { height: 360px; display: flex; align-items: center; justify-content: center; text-align: center; color: rgba(255,255,255,0.4); font-size: 14px; padding: 0 30px; line-height: 1.5; }
.ax-logs-head { border-bottom: 1px solid rgba(255,255,255,0.08); padding-bottom: 18px; margin-bottom: 24px; }
/* transcript lines land like a conversation replaying — quick, capped stagger */
.ax-tline { display: flex; flex-direction: column; align-items: flex-start; margin-bottom: 16px; animation: axCasIn var(--ax-t) var(--ax-out) both; }
.ax-tline:nth-child(1) { animation-delay: 0ms; }
.ax-tline:nth-child(2) { animation-delay: 40ms; }
.ax-tline:nth-child(3) { animation-delay: 80ms; }
.ax-tline:nth-child(4) { animation-delay: 120ms; }
.ax-tline:nth-child(5) { animation-delay: 160ms; }
.ax-tline:nth-child(6) { animation-delay: 200ms; }
.ax-tline:nth-child(n+7) { animation-delay: 240ms; }
.ax-tline.is-mine { align-items: flex-end; }
.ax-tline-who { font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; color: rgba(255,255,255,0.34); margin-bottom: 7px; font-weight: 600; }
.ax-bubble { padding: 11px 15px; font-size: 14px; line-height: 1.55; max-width: 80%; box-shadow: inset 0 1px 0 rgba(255,255,255,0.04), 0 4px 12px -8px rgba(0,0,0,0.4); }
.ax-bubble--agent { background: linear-gradient(180deg, rgba(255,255,255,0.05), rgba(255,255,255,0.028)); border: 1px solid rgba(255,255,255,0.07); color: rgba(255,255,255,0.87); border-radius: 13px 13px 13px 4px; }
.ax-bubble--mine { background: linear-gradient(180deg, rgba(230,38,48,0.11), rgba(230,38,48,0.06)); border: 1px solid rgba(230,38,48,0.22); color: #F3F2F0; border-radius: 13px 13px 4px 13px; }
@media (max-width: 760px) { .ax-logs { grid-template-columns: 1fr; } }
</style>
