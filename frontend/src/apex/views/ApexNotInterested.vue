<script setup>
// APEX Not Interested — reached but below the lead score. Same table + breakdown
// (shows what they missed); score tag neutral.
import { inject, ref, computed, watch, onMounted } from 'vue';
import { categorizeContacts } from '../../composables/bulkCalling.js';

const apex = inject('apex');
const filter = ref(null);
const expanded = ref(null);
const rows = ref([]);
const loading = ref(false);
const cursors = ref({});
const hasMore = computed(() => Object.values(cursors.value).some((c) => !!c));
const scoped = computed(() => {
  const all = apex.deterministicCampaigns.value;
  return filter.value ? all.filter((c) => String(c.id) === String(filter.value)) : all;
});

function _mapV2(r, c) {
  const res = typeof r.result === 'string' ? JSON.parse(r.result || '{}') : (r.result || {});
  return {
    call_link_id: r.id, name: r.name || r.phone, phone: r.phone, lead_score: r.lead_score,
    max_score: c.max_score, score_breakdown: res.score_breakdown || [],
    lead_score_reason: res.lead_score_reason || res.interest_reason || null, call_note: res.call_note || null,
  };
}
async function loadRows(reset = true) {
  loading.value = true;
  try {
    const legacy = scoped.value.filter((c) => !c.v2);
    const v2 = scoped.value.filter((c) => c.v2);
    const legacyRows = categorizeContacts(legacy).not_interested;
    const out = reset ? [] : [...rows.value];
    if (reset) cursors.value = {};
    for (const c of v2) {
      const cur = reset ? null : cursors.value[c.id];
      if (!reset && cur === null) continue;
      const { rows: page, next_cursor } = await apex.fetchCampaignContacts(c.id, 'not_interested', cur, 100);
      out.push(...(page || []).map((r) => _mapV2(r, c)));
      cursors.value[c.id] = next_cursor || null;
    }
    rows.value = reset ? [...out, ...legacyRows] : out;
  } finally {
    loading.value = false;
  }
}
watch(filter, () => loadRows(true));
watch(() => apex.deterministicCampaigns.value, () => loadRows(true));
onMounted(() => loadRows(true));
function toggle(key) { expanded.value = expanded.value === key ? null : key; }
</script>

<template>
  <div class="ax-card ax-card-pad ax-anim">
    <div style="display:flex;align-items:center;gap:13px;">
      <h2 class="ax-h2">Not interested</h2>
      <span class="ax-count ax-count--grey">{{ rows.length }}{{ hasMore ? '+' : '' }}</span>
    </div>
    <p class="ax-muted">Numbers we reached but that didn't cross the lead score. The breakdown shows what they missed.</p>

    <div v-if="apex.deterministicCampaigns.value.length > 1" class="ax-filters" style="margin-bottom:8px;">
      <button type="button" class="ax-chip" :class="{ 'is-active': filter === null }" @click="filter = null">All</button>
      <button v-for="c in apex.deterministicCampaigns.value" :key="c.id" type="button" class="ax-chip" :class="{ 'is-active': filter === c.id }" @click="filter = c.id">{{ c.name }}</button>
    </div>

    <div v-if="!rows.length && !loading" class="ax-empty" style="margin-top:16px;">
      <div class="ax-empty-icon">⃠</div>
      <p class="ax-empty-text">No "not interested" numbers yet — reached contacts that don't qualify will appear here.</p>
    </div>
    <template v-else>
      <div class="ax-thead" style="grid-template-columns:1.4fr 1.4fr auto;"><span>Name</span><span>Phone</span><span>Score</span></div>
      <template v-for="(row, i) in rows" :key="row.call_link_id || i">
        <div class="ax-trow ax-trow--click" style="grid-template-columns:1.4fr 1.4fr auto;" @click="toggle(row.call_link_id)">
          <span class="ax-cell-name">{{ row.name }}</span>
          <span class="ax-cell-phone">{{ row.phone }}</span>
          <span v-if="row.max_score" class="ax-score ax-score--grey">{{ row.lead_score ?? 0 }}/{{ row.max_score }}</span>
          <span v-else class="ax-score ax-score--grey">—</span>
        </div>
        <div v-if="expanded === row.call_link_id && ((row.score_breakdown && row.score_breakdown.length) || row.call_note)" class="ax-break">
          <div class="ax-break-label">How it scored</div>
          <p v-if="row.lead_score_reason" class="ax-break-reason">{{ row.lead_score_reason }}</p>
          <div v-for="(b, bi) in (row.score_breakdown || [])" :key="bi" class="ax-break-row">
            <span class="ax-break-mark" :class="b.awarded ? 'ax-break-mark--ok' : 'ax-break-mark--miss'">{{ b.awarded ? '✓' : '✗' }}</span>
            <div style="flex:1;">
              <div class="ax-break-q">{{ b.text }}</div>
              <div v-if="b.evidence" class="ax-break-a">"{{ b.evidence }}"</div>
            </div>
            <span v-if="b.awarded_points" class="ax-break-pts">+{{ b.awarded_points }}</span>
          </div>
          <div v-if="row.call_note" class="ax-callnote">
            <span class="ax-callnote-label">Call note&nbsp;&nbsp;</span>
            <span class="ax-callnote-text">{{ row.call_note }}</span>
          </div>
        </div>
      </template>
      <button v-if="hasMore" type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" style="margin-top:12px;" :disabled="loading" @click="loadRows(false)">
        {{ loading ? 'Loading…' : 'Load more' }}
      </button>
    </template>
  </div>
</template>
