<script setup>
// APEX Qualified Leads — contacts that crossed the lead score. V2 campaigns are
// read PAGINATED from the server (scales to 1M); legacy blob campaigns stay
// client-side. Row click opens the slide-over drawer (score breakdown + note).
import { inject, ref, computed, watch, onMounted } from 'vue';
import { categorizeContacts, exportContactsCsv } from '../../composables/bulkCalling.js';
import AxIcon from '../AxIcon.vue';
import AxScore from '../AxScore.vue';
import AxPhone from '../AxPhone.vue';
import AxLeadDrawer from '../AxLeadDrawer.vue';

const apex = inject('apex');
const filter = ref(null); // null = all
const detail = ref(null); // row shown in the drawer
const rows = ref([]);
const loading = ref(false);
const cursors = ref({}); // v2 campaign_id -> next cursor (null = exhausted)
const hasMore = computed(() => Object.values(cursors.value).some((c) => !!c));

const scoped = computed(() => {
  const all = apex.deterministicCampaigns.value;
  return filter.value ? all.filter((c) => String(c.id) === String(filter.value)) : all;
});

function _parseResult(r) {
  const res = typeof r.result === 'string' ? (JSON.parse(r.result || '{}')) : (r.result || {});
  return res;
}
function _mapV2(r, c) {
  const res = _parseResult(r);
  return {
    call_link_id: r.id, // unique row key for the drawer
    name: r.name || r.phone,
    raw_name: r.name || '',
    phone: r.phone,
    lead_score: r.lead_score,
    max_score: c.max_score,
    score_breakdown: res.score_breakdown || [],
    lead_score_reason: res.lead_score_reason || res.interest_reason || null,
    call_note: res.call_note || null,
  };
}

async function loadRows(reset = true) {
  loading.value = true;
  try {
    const legacy = scoped.value.filter((c) => !c.v2);
    const v2 = scoped.value.filter((c) => c.v2);
    const legacyRows = categorizeContacts(legacy).successful;
    const out = reset ? [] : [...rows.value];
    if (reset) cursors.value = {};
    for (const c of v2) {
      const cur = reset ? null : cursors.value[c.id];
      if (!reset && cur === null) continue; // exhausted
      const { rows: page, next_cursor } = await apex.fetchCampaignContacts(c.id, 'qualified', cur, 100);
      out.push(...(page || []).map((r) => _mapV2(r, c)));
      cursors.value[c.id] = next_cursor || null;
    }
    rows.value = reset ? [...out, ...legacyRows] : out;
  } catch (e) {
    console.error('[APEX] load qualified failed', e);
  } finally {
    loading.value = false;
  }
}
watch(filter, () => loadRows(true));
watch(() => apex.deterministicCampaigns.value, () => loadRows(true));
onMounted(() => loadRows(true));

async function downloadCsv() {
  const name = `apex-qualified-leads-${new Date().toISOString().slice(0, 10)}.csv`;
  // A single V2 campaign selected → stream the full bucket server-side.
  const one = scoped.value.length === 1 && scoped.value[0].v2 ? scoped.value[0] : null;
  if (one) {
    await apex.downloadCampaignContactsCsv(one.id, 'qualified', name);
  } else {
    exportContactsCsv(rows.value, name); // legacy / mixed: the loaded rows
  }
  apex.toast('CSV downloaded.');
}
</script>

<template>
  <div class="ax-card ax-card-pad ax-anim">
    <div class="ax-card-head">
      <div style="display:flex;align-items:center;gap:13px;">
        <h2 class="ax-h2">Qualified leads</h2>
        <span class="ax-count ax-count--green">{{ rows.length }}{{ hasMore ? '+' : '' }}</span>
      </div>
      <div style="display:flex;align-items:center;gap:8px;">
        <button type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" :disabled="!rows.length" @click="downloadCsv"><AxIcon name="download" :size="13" /> Generate CSV</button>
        <button type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" @click="apex.reload().then(() => loadRows(true))"><AxIcon name="refresh" :size="13" /> Refresh</button>
      </div>
    </div>
    <p class="ax-muted">Contacts that qualified — by lead score where a questionnaire is set. Click a scored row to see the per-question breakdown.</p>

    <div v-if="apex.deterministicCampaigns.value.length > 1" class="ax-filters" style="margin-bottom:8px;">
      <button type="button" class="ax-chip" :class="{ 'is-active': filter === null }" @click="filter = null">All</button>
      <button v-for="c in apex.deterministicCampaigns.value" :key="c.id" type="button" class="ax-chip" :class="{ 'is-active': filter === c.id }" @click="filter = c.id">{{ c.name }}</button>
    </div>

    <div v-if="loading && !rows.length" style="margin-top:16px;">
      <div v-for="i in 6" :key="i" class="ax-skel-row">
        <span class="ax-skel-bar" style="width:58%"></span>
        <span class="ax-skel-bar" style="width:44%"></span>
        <span class="ax-skel-bar ax-skel-bar--pill"></span>
      </div>
    </div>
    <div v-else-if="!rows.length" class="ax-empty" style="margin-top:16px;">
      <div class="ax-empty-icon"><AxIcon name="star" :size="20" /></div>
      <p class="ax-empty-text">No qualified leads yet — qualified contacts from your campaigns will appear here.</p>
    </div>
    <template v-else>
      <div class="ax-thead" style="grid-template-columns:1.4fr 1.4fr auto;"><span>Name</span><span>Phone</span><span>Score</span></div>
      <div
        v-for="(row, i) in rows" :key="row.call_link_id || i"
        class="ax-trow ax-trow--click ax-row-in" :style="{ '--i': Math.min(i, 14) }"
        style="grid-template-columns:1.4fr 1.4fr auto;" @click="detail = row"
      >
        <span class="ax-cell-name">{{ row.name }}</span>
        <AxPhone :phone="row.phone" />
        <AxScore v-if="row.max_score" :score="row.lead_score" :max="row.max_score" tone="green" />
        <span v-else class="ax-score ax-score--grey">—</span>
      </div>
      <button v-if="hasMore" type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" style="margin-top:12px;" :disabled="loading" @click="loadRows(false)">
        {{ loading ? 'Loading…' : 'Load more' }}
      </button>
    </template>

    <Transition name="axdrawer">
      <AxLeadDrawer v-if="detail" :row="detail" tone="green" @close="detail = null" />
    </Transition>
  </div>
</template>
