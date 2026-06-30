<script setup>
// APEX Call Logs — each dialed contact (call_id present) + its transcript.
import { inject, ref, computed } from 'vue';

const apex = inject('apex');
const filter = ref(null);
const selected = ref(null); // { call_id, name, phone, status, time }
const transcript = ref(null);
const loadingT = ref(false);

const scoped = computed(() => {
  const all = apex.deterministicCampaigns.value;
  return filter.value ? all.filter((c) => String(c.id) === String(filter.value)) : all;
});
const calls = computed(() => {
  const rows = [];
  for (const c of scoped.value) {
    for (const ct of c.contacts || []) {
      if (!ct.call_id) continue;
      rows.push({
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

function statusType(s) {
  if (s === 'answered') return 'pos';
  if (s === 'calling') return 'live';
  return 'neg';
}

async function open(row) {
  selected.value = row;
  transcript.value = null;
  loadingT.value = true;
  try {
    const data = await apex.fetchTranscript(row.call_id);
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
        <div v-if="!calls.length" class="ax-empty"><div class="ax-empty-icon">☎</div><p class="ax-empty-text">No calls yet for these campaigns.</p></div>
        <button v-for="row in calls" :key="row.call_id" type="button" class="ax-call" :class="{ 'is-active': selected && selected.call_id === row.call_id }" @click="open(row)">
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
          <div v-if="loadingT" class="ax-logs-empty">Loading…</div>
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
.ax-call { width: 100%; text-align: left; cursor: pointer; border-radius: 9px; padding: 18px 20px; background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.08); transition: all .15s; font-family: 'Sora', sans-serif; display: block; color: #F3F2F0; }
.ax-call.is-active { border-color: rgba(230,38,48,0.5); background: rgba(230,38,48,0.06); }
.ax-call-phone { font-family: 'JetBrains Mono', monospace; font-size: 12.5px; color: rgba(255,255,255,0.45); margin-top: 11px; }
.ax-call-time { font-family: 'JetBrains Mono', monospace; font-size: 11.5px; color: rgba(255,255,255,0.3); margin-top: 6px; }
.ax-spill { font-family: 'JetBrains Mono', monospace; font-size: 11px; letter-spacing: 0.04em; padding: 4px 10px; border-radius: 5px; }
.ax-spill.is-pos { color: #7FD9A8; border: 1px solid rgba(127,217,168,0.3); }
.ax-spill.is-neg { color: rgba(255,255,255,0.55); border: 1px solid rgba(255,255,255,0.16); }
.ax-spill.is-live { color: #E62630; border: 1px solid rgba(230,38,48,0.4); background: rgba(230,38,48,0.07); }
.ax-logs-detail { background: rgba(255,255,255,0.025); border: 1px solid rgba(255,255,255,0.09); border-radius: 8px; min-height: 420px; padding: 28px 30px; }
.ax-logs-empty { height: 360px; display: flex; align-items: center; justify-content: center; text-align: center; color: rgba(255,255,255,0.4); font-size: 14px; padding: 0 30px; line-height: 1.5; }
.ax-logs-head { border-bottom: 1px solid rgba(255,255,255,0.08); padding-bottom: 18px; margin-bottom: 24px; }
.ax-tline { display: flex; flex-direction: column; align-items: flex-start; margin-bottom: 16px; }
.ax-tline.is-mine { align-items: flex-end; }
.ax-tline-who { font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; color: rgba(255,255,255,0.34); margin-bottom: 7px; font-weight: 600; }
.ax-bubble { padding: 11px 15px; font-size: 14px; line-height: 1.5; max-width: 80%; }
.ax-bubble--agent { background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.07); color: rgba(255,255,255,0.85); border-radius: 11px 11px 11px 3px; }
.ax-bubble--mine { background: rgba(230,38,48,0.08); border: 1px solid rgba(230,38,48,0.2); color: #F3F2F0; border-radius: 11px 11px 3px 11px; }
@media (max-width: 760px) { .ax-logs { grid-template-columns: 1fr; } }
</style>
