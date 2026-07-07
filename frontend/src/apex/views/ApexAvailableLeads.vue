<script setup>
// APEX Available Leads — the claim pool: qualified, UNCLAIMED leads from the team's
// campaigns that any member can take. Claiming is exclusive (first-come).
import { inject } from 'vue';
import AxIcon from '../AxIcon.vue';
import AxScore from '../AxScore.vue';
import AxPhone from '../AxPhone.vue';

const apex = inject('apex');
</script>

<template>
  <div class="ax-card ax-card-pad ax-anim">
    <div class="ax-card-head">
      <div style="display:flex;align-items:center;gap:13px;">
        <h2 class="ax-h2">Available leads</h2>
        <span class="ax-count ax-count--green">{{ apex.qualifiedLeads.value.length }}</span>
      </div>
      <button type="button" class="ax-btn2 ax-btn2--ghost ax-btn2--sm" :disabled="apex.loadingLeads.value" @click="apex.reloadLeads()"><AxIcon name="refresh" :size="13" /> Refresh</button>
    </div>
    <p class="ax-muted">Qualified leads from your team's campaigns. Claim one to add it to your list — once claimed, it's yours alone.</p>

    <div v-if="apex.loadingLeads.value && !apex.qualifiedLeads.value.length" style="margin-top:16px;">
      <div v-for="i in 4" :key="i" class="ax-skel-row">
        <span class="ax-skel-bar" style="width:58%"></span>
        <span class="ax-skel-bar" style="width:44%"></span>
        <span class="ax-skel-bar ax-skel-bar--pill"></span>
      </div>
    </div>
    <div v-else-if="!apex.qualifiedLeads.value.length" class="ax-empty" style="margin-top:16px;">
      <div class="ax-empty-icon"><AxIcon name="star" :size="20" /></div>
      <p class="ax-empty-text">No leads to claim right now — qualified leads appear here as campaigns run.</p>
    </div>
    <template v-else>
      <div class="ax-thead" style="grid-template-columns:1.4fr 1.3fr auto auto;"><span>Name</span><span>Phone</span><span>Score</span><span></span></div>
      <div
        v-for="(row, i) in apex.qualifiedLeads.value" :key="row.call_link_id || i"
        class="ax-trow ax-row-in" :style="{ '--i': Math.min(i, 14) }"
        style="grid-template-columns:1.4fr 1.3fr auto auto;align-items:center;"
      >
        <span class="ax-cell-name">{{ row.name }}</span>
        <AxPhone :phone="row.phone" />
        <AxScore v-if="row.max_score" :score="row.lead_score" :max="row.max_score" tone="green" />
        <span v-else class="ax-score ax-score--grey">—</span>
        <button
          type="button" class="ax-btn2 ax-btn2--accent ax-btn2--sm"
          :disabled="apex.leadBusy.value === row.call_link_id"
          @click="apex.claim(row)"
        >{{ apex.leadBusy.value === row.call_link_id ? '…' : 'Claim' }}</button>
      </div>
    </template>
  </div>
</template>
