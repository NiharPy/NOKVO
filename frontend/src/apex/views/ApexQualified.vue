<script setup>
// APEX Qualified Leads — deterministic campaigns' contacts that crossed the lead
// score. Dark table + per-question score breakdown + call note (design 215-261).
import { inject, ref, computed } from 'vue';
import { categorizeContacts } from '../../composables/bulkCalling.js';

const apex = inject('apex');
const filter = ref(null); // null = all
const expanded = ref(null); // call_link_id

const scoped = computed(() => {
  const all = apex.deterministicCampaigns.value;
  return filter.value ? all.filter((c) => String(c.id) === String(filter.value)) : all;
});
const rows = computed(() => categorizeContacts(scoped.value).successful);
function toggle(key) { expanded.value = expanded.value === key ? null : key; }
</script>

<template>
  <div class="ax-card ax-card-pad ax-anim">
    <div class="ax-card-head">
      <div style="display:flex;align-items:center;gap:13px;">
        <h2 class="ax-h2">Qualified leads</h2>
        <span class="ax-count ax-count--green">{{ rows.length }}</span>
      </div>
      <button type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" @click="apex.reload()">↻ Refresh</button>
    </div>
    <p class="ax-muted">Contacts that qualified — by lead score where a questionnaire is set. Click a scored row to see the per-question breakdown.</p>

    <div v-if="apex.deterministicCampaigns.value.length > 1" class="ax-filters" style="margin-bottom:8px;">
      <button type="button" class="ax-chip" :class="{ 'is-active': filter === null }" @click="filter = null">All</button>
      <button v-for="c in apex.deterministicCampaigns.value" :key="c.id" type="button" class="ax-chip" :class="{ 'is-active': filter === c.id }" @click="filter = c.id">{{ c.name }}</button>
    </div>

    <div v-if="!rows.length" class="ax-empty" style="margin-top:16px;">
      <div class="ax-empty-icon">★</div>
      <p class="ax-empty-text">No qualified leads yet — qualified contacts from your campaigns will appear here.</p>
    </div>
    <template v-else>
      <div class="ax-thead" style="grid-template-columns:1.4fr 1.4fr auto;"><span>Name</span><span>Phone</span><span>Score</span></div>
      <template v-for="(row, i) in rows" :key="row.call_link_id || i">
        <div class="ax-trow ax-trow--click" style="grid-template-columns:1.4fr 1.4fr auto;" @click="toggle(row.call_link_id)">
          <span class="ax-cell-name">{{ row.name }}</span>
          <span class="ax-cell-phone">{{ row.phone }}</span>
          <span v-if="row.max_score" class="ax-score">{{ row.lead_score }}/{{ row.max_score }}</span>
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
    </template>
  </div>
</template>
