<script setup>
// APEX Not Interested — reached but below the lead score. Same table; the
// drawer shows what they missed (score tag stays neutral).
import { inject, ref, computed, watch, onMounted } from 'vue';
import { categorizeContacts } from '../../composables/bulkCalling.js';
import { useNewRows } from '../../composables/axMotion.js';
import AxIcon from '../AxIcon.vue';
import AxCount from '../AxCount.vue';
import AxScore from '../AxScore.vue';
import AxPhone from '../AxPhone.vue';
import AxLeadDrawer from '../AxLeadDrawer.vue';

const apex = inject('apex');
const newRows = useNewRows((r) => r.call_link_id || r.phone);
const filter = ref(null);
const detail = ref(null);
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
    newRows.track(rows.value);
  } finally {
    loading.value = false;
  }
}
watch(filter, () => loadRows(true));
watch(() => apex.deterministicCampaigns.value, () => loadRows(true));
onMounted(() => loadRows(true));
</script>

<template>
  <div class="ax-card ax-card-pad ax-anim">
    <div style="display:flex;align-items:center;gap:13px;">
      <h2 class="ax-h2">Not interested</h2>
      <span class="ax-count ax-count--grey"><AxCount :value="rows.length" />{{ hasMore ? '+' : '' }}</span>
    </div>
    <p class="ax-muted">Numbers we reached but that didn't cross the lead score. The breakdown shows what they missed.</p>

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
      <div class="ax-empty-icon"><AxIcon name="ban" :size="20" /></div>
      <p class="ax-empty-text">No "not interested" numbers yet — reached contacts that don't qualify will appear here.</p>
    </div>
    <template v-else>
      <div class="ax-thead" style="grid-template-columns:1.4fr 1.4fr auto;"><span>Name</span><span>Phone</span><span>Score</span></div>
      <TransitionGroup name="axlist">
        <div
          v-for="(row, i) in rows" :key="row.call_link_id || row.phone"
          class="ax-trow ax-trow--click ax-row-in" :class="{ 'is-new': newRows.isNew(row) }" :style="{ '--i': Math.min(i, 14) }"
          style="grid-template-columns:1.4fr 1.4fr auto;" @click="detail = row"
        >
          <span class="ax-cell-name">{{ row.name }}</span>
          <AxPhone :phone="row.phone" />
          <AxScore v-if="row.max_score" :score="row.lead_score ?? 0" :max="row.max_score" tone="grey" />
          <span v-else class="ax-score ax-score--grey">—</span>
        </div>
      </TransitionGroup>
      <button v-if="hasMore" type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" style="margin-top:12px;" :disabled="loading" @click="loadRows(false)">
        {{ loading ? 'Loading…' : 'Load more' }}
      </button>
    </template>

    <Transition name="axdrawer">
      <AxLeadDrawer v-if="detail" :row="detail" tone="grey" @close="detail = null" />
    </Transition>
  </div>
</template>
