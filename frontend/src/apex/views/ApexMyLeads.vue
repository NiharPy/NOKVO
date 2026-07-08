<script setup>
// APEX My Leads — the leads this member has claimed. Click a name for the
// drawer (score reason / call note); move each through a working status
// (claimed → contacted → won / lost).
import { inject, ref, watch } from 'vue';
import { useNewRows } from '../../composables/axMotion.js';
import AxIcon from '../AxIcon.vue';
import AxCount from '../AxCount.vue';
import AxPhone from '../AxPhone.vue';
import AxLeadDrawer from '../AxLeadDrawer.vue';

const apex = inject('apex');
const detail = ref(null);
// Flash rows that arrived on a refresh (a claim just landed in your list).
const newRows = useNewRows((r) => r.call_link_id || r.phone);
watch(() => apex.myLeads.value, (rows) => newRows.track(rows || []), { immediate: true });
const STATUSES = [
  { id: 'claimed', label: 'Claimed' },
  { id: 'contacted', label: 'Contacted' },
  { id: 'won', label: 'Won' },
  { id: 'lost', label: 'Lost' },
];
</script>

<template>
  <div class="ax-card ax-card-pad ax-anim">
    <div class="ax-card-head">
      <div style="display:flex;align-items:center;gap:13px;">
        <h2 class="ax-h2">My leads</h2>
        <span class="ax-count ax-count--grey"><AxCount :value="apex.myLeads.value.length" /></span>
      </div>
      <button type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" :disabled="apex.loadingLeads.value" @click="apex.reloadLeads()"><AxIcon name="refresh" :size="13" /> Refresh</button>
    </div>
    <p class="ax-muted">Leads you've claimed. Click a name for its score &amp; call note; update the status as you work it.</p>

    <div v-if="apex.loadingLeads.value && !apex.myLeads.value.length" style="margin-top:16px;">
      <div v-for="i in 4" :key="i" class="ax-skel-row">
        <span class="ax-skel-bar" style="width:58%"></span>
        <span class="ax-skel-bar" style="width:44%"></span>
        <span class="ax-skel-bar ax-skel-bar--pill"></span>
      </div>
    </div>
    <div v-else-if="!apex.myLeads.value.length" class="ax-empty" style="margin-top:16px;">
      <div class="ax-empty-icon"><AxIcon name="star" :size="20" /></div>
      <p class="ax-empty-text">You haven't claimed any leads yet — head to Available Leads to claim some.</p>
    </div>
    <template v-else>
      <div class="ax-thead" style="grid-template-columns:1.3fr 1.2fr auto;"><span>Name</span><span>Phone</span><span>Status</span></div>
      <TransitionGroup name="axlist">
        <div
          v-for="(row, i) in apex.myLeads.value" :key="row.call_link_id || row.phone"
          class="ax-trow ax-row-in" :class="{ 'is-new': newRows.isNew(row) }" :style="{ '--i': Math.min(i, 14) }"
          style="grid-template-columns:1.3fr 1.2fr auto;align-items:center;"
        >
          <span class="ax-cell-name ax-trow--click" @click="detail = row">{{ row.name }}</span>
          <AxPhone :phone="row.phone" />
          <select
            class="ax-status-select" :value="row.claim_status || 'claimed'"
            :disabled="apex.leadBusy.value === row.call_link_id"
            @change="apex.setStatus(row, $event.target.value)"
          >
            <option v-for="s in STATUSES" :key="s.id" :value="s.id">{{ s.label }}</option>
          </select>
        </div>
      </TransitionGroup>
    </template>

    <Transition name="axdrawer">
      <AxLeadDrawer v-if="detail" :row="detail" @close="detail = null" />
    </Transition>
  </div>
</template>

<style scoped>
.ax-status-select {
  background: rgba(0,0,0,0.24); border: 1px solid rgba(255,255,255,0.14); color: #F3F2F0;
  border-radius: 9px; padding: 6px 10px; font-family: 'Sora', sans-serif; font-size: 12.5px; cursor: pointer; outline: none;
  box-shadow: inset 0 1px 3px rgba(0,0,0,0.2); transition: border-color .18s, box-shadow .18s;
}
.ax-status-select:hover { border-color: rgba(255,255,255,0.26); }
.ax-status-select:focus { border-color: rgba(230,38,48,0.65); box-shadow: inset 0 1px 3px rgba(0,0,0,0.2), 0 0 0 3px rgba(230,38,48,0.13); }
.ax-status-select option { background: #141416; color: #F3F2F0; }
</style>
