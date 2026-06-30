<script setup>
// APEX Didn't Pick Up — no connection (no answer / telephony failure).
import { inject, ref, computed } from 'vue';
import { categorizeContacts } from '../../composables/bulkCalling.js';

const apex = inject('apex');
const filter = ref(null);
const scoped = computed(() => {
  const all = apex.deterministicCampaigns.value;
  return filter.value ? all.filter((c) => String(c.id) === String(filter.value)) : all;
});
const rows = computed(() => categorizeContacts(scoped.value).no_pickup);
</script>

<template>
  <div class="ax-card ax-card-pad ax-anim">
    <div class="ax-card-head">
      <div style="display:flex;align-items:center;gap:13px;">
        <h2 class="ax-h2">Didn't pick up</h2>
        <span class="ax-count ax-count--grey">{{ rows.length }}</span>
      </div>
      <button type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" @click="apex.reload()">↻ Refresh</button>
    </div>
    <p class="ax-muted">Numbers we couldn't reach — no answer or the call didn't connect. Re-run a campaign to try its unreached numbers again.</p>

    <div v-if="apex.deterministicCampaigns.value.length > 1" class="ax-filters" style="margin-bottom:8px;">
      <button type="button" class="ax-chip" :class="{ 'is-active': filter === null }" @click="filter = null">All</button>
      <button v-for="c in apex.deterministicCampaigns.value" :key="c.id" type="button" class="ax-chip" :class="{ 'is-active': filter === c.id }" @click="filter = c.id">{{ c.name }}</button>
    </div>

    <div v-if="!rows.length" class="ax-empty" style="margin-top:16px;">
      <div class="ax-empty-icon">⌀</div>
      <p class="ax-empty-text">Everyone we dialed connected — unreached numbers will appear here.</p>
    </div>
    <template v-else>
      <div class="ax-thead" style="grid-template-columns:1.4fr 1.4fr auto;"><span>Name</span><span>Phone</span><span>Outcome</span></div>
      <div v-for="(row, i) in rows" :key="row.call_link_id || i" class="ax-trow" style="grid-template-columns:1.4fr 1.4fr auto;">
        <span class="ax-cell-name">{{ row.name }}</span>
        <span class="ax-cell-phone">{{ row.phone }}</span>
        <span class="ax-outcome">{{ row.status === 'failed' ? 'Call failed' : 'No answer' }}</span>
      </div>
    </template>
  </div>
</template>
