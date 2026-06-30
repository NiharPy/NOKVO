<script setup>
// APEX My Leads — the leads this member has claimed. Expand a row for the score
// reason / call note; move each through a working status (claimed → contacted →
// won / lost).
import { inject, ref } from 'vue';

const apex = inject('apex');
const expanded = ref(null);
const STATUSES = [
  { id: 'claimed', label: 'Claimed' },
  { id: 'contacted', label: 'Contacted' },
  { id: 'won', label: 'Won' },
  { id: 'lost', label: 'Lost' },
];
function toggle(key) { expanded.value = expanded.value === key ? null : key; }
</script>

<template>
  <div class="ax-card ax-card-pad ax-anim">
    <div class="ax-card-head">
      <div style="display:flex;align-items:center;gap:13px;">
        <h2 class="ax-h2">My leads</h2>
        <span class="ax-count">{{ apex.myLeads.value.length }}</span>
      </div>
      <button type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" :disabled="apex.loadingLeads.value" @click="apex.reloadLeads()">↻ Refresh</button>
    </div>
    <p class="ax-muted">Leads you've claimed. Click a name for its score &amp; call note; update the status as you work it.</p>

    <div v-if="!apex.myLeads.value.length" class="ax-empty" style="margin-top:16px;">
      <div class="ax-empty-icon">☆</div>
      <p class="ax-empty-text">You haven't claimed any leads yet — head to Available Leads to claim some.</p>
    </div>
    <template v-else>
      <div class="ax-thead" style="grid-template-columns:1.3fr 1.2fr auto;"><span>Name</span><span>Phone</span><span>Status</span></div>
      <template v-for="(row, i) in apex.myLeads.value" :key="row.call_link_id || i">
        <div class="ax-trow" style="grid-template-columns:1.3fr 1.2fr auto;align-items:center;">
          <span class="ax-cell-name ax-trow--click" @click="toggle(row.call_link_id)">{{ row.name }}</span>
          <span class="ax-cell-phone">{{ row.phone }}</span>
          <select
            class="ax-status-select" :value="row.claim_status || 'claimed'"
            :disabled="apex.leadBusy.value === row.call_link_id"
            @change="apex.setStatus(row, $event.target.value)"
          >
            <option v-for="s in STATUSES" :key="s.id" :value="s.id">{{ s.label }}</option>
          </select>
        </div>
        <div v-if="expanded === row.call_link_id" class="ax-break">
          <p v-if="row.lead_score_reason" class="ax-break-reason">{{ row.lead_score_reason }}</p>
          <div v-if="row.call_note" class="ax-callnote">
            <span class="ax-callnote-label">Call note&nbsp;&nbsp;</span>
            <span class="ax-callnote-text">{{ row.call_note }}</span>
          </div>
          <p v-if="!row.lead_score_reason && !row.call_note" class="ax-muted" style="margin:0;">No notes captured for this lead.</p>
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
.ax-status-select {
  background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.16); color: #F3F2F0;
  border-radius: 7px; padding: 6px 10px; font-family: 'Sora', sans-serif; font-size: 12.5px; cursor: pointer; outline: none;
}
.ax-status-select:focus { border-color: #E62630; }
</style>
