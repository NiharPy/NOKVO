<script setup>
// APEX Didn't Pick Up — no connection. V2 campaigns read paginated from the
// server; legacy blob campaigns stay client-side.
import { inject, ref, computed, watch, onMounted } from 'vue';
import { categorizeContacts, exportContactsCsv } from '../../composables/bulkCalling.js';
import AxIcon from '../AxIcon.vue';
import AxPhone from '../AxPhone.vue';

const apex = inject('apex');
const filter = ref(null);
const rows = ref([]);
const loading = ref(false);
const cursors = ref({});
const hasMore = computed(() => Object.values(cursors.value).some((c) => !!c));

const scoped = computed(() => {
  const all = apex.deterministicCampaigns.value;
  return filter.value ? all.filter((c) => String(c.id) === String(filter.value)) : all;
});

async function loadRows(reset = true) {
  loading.value = true;
  try {
    const legacy = scoped.value.filter((c) => !c.v2);
    const v2 = scoped.value.filter((c) => c.v2);
    const legacyRows = categorizeContacts(legacy).no_pickup;
    const out = reset ? [] : [...rows.value];
    if (reset) cursors.value = {};
    for (const c of v2) {
      const cur = reset ? null : cursors.value[c.id];
      if (!reset && cur === null) continue;
      const { rows: page, next_cursor } = await apex.fetchCampaignContacts(c.id, 'no_pickup', cur, 100);
      out.push(...(page || []).map((r) => ({ name: r.name || r.phone, raw_name: r.name || '', phone: r.phone, status: r.status, call_link_id: r.id })));
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

async function downloadCsv() {
  const name = `apex-didnt-pick-up-${new Date().toISOString().slice(0, 10)}.csv`;
  const one = scoped.value.length === 1 && scoped.value[0].v2 ? scoped.value[0] : null;
  if (one) await apex.downloadCampaignContactsCsv(one.id, 'no_pickup', name);
  else exportContactsCsv(rows.value, name);
  apex.toast('CSV downloaded.');
}
</script>

<template>
  <div class="ax-card ax-card-pad ax-anim">
    <div class="ax-card-head">
      <div style="display:flex;align-items:center;gap:13px;">
        <h2 class="ax-h2">Didn't pick up</h2>
        <span class="ax-count ax-count--grey">{{ rows.length }}{{ hasMore ? '+' : '' }}</span>
      </div>
      <div style="display:flex;align-items:center;gap:8px;">
        <button type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" :disabled="!rows.length" @click="downloadCsv"><AxIcon name="download" :size="13" /> Generate CSV</button>
        <button type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" @click="apex.reload().then(() => loadRows(true))"><AxIcon name="refresh" :size="13" /> Refresh</button>
      </div>
    </div>
    <p class="ax-muted">Numbers we couldn't reach — no answer or the call didn't connect. Re-run a campaign to try its unreached numbers again.</p>

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
      <div class="ax-empty-icon"><AxIcon name="phone-off" :size="20" /></div>
      <p class="ax-empty-text">Everyone we dialed connected — unreached numbers will appear here.</p>
    </div>
    <template v-else>
      <div class="ax-thead" style="grid-template-columns:1.4fr 1.4fr auto;"><span>Name</span><span>Phone</span><span>Outcome</span></div>
      <div
        v-for="(row, i) in rows" :key="row.call_link_id || i"
        class="ax-trow ax-row-in" :style="{ '--i': Math.min(i, 14) }"
        style="grid-template-columns:1.4fr 1.4fr auto;"
      >
        <span class="ax-cell-name">{{ row.name }}</span>
        <AxPhone :phone="row.phone" />
        <span class="ax-outcome">{{ row.status === 'failed' ? 'Call failed' : 'No answer' }}</span>
      </div>
      <button v-if="hasMore" type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" style="margin-top:12px;" :disabled="loading" @click="loadRows(false)">
        {{ loading ? 'Loading…' : 'Load more' }}
      </button>
    </template>
  </div>
</template>
