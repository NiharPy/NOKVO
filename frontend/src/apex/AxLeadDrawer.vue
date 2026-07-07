<script setup>
// Slide-over lead detail — replaces the inline row expansion so the table stays
// intact behind a scrim. Shows whatever the row carries: score, per-question
// breakdown, reason, call note. Esc / overlay click / × close it.
import { onMounted, onBeforeUnmount } from 'vue';
import AxIcon from './AxIcon.vue';
import AxScore from './AxScore.vue';
import AxPhone from './AxPhone.vue';

const props = defineProps({
  row: { type: Object, required: true },
  tone: { type: String, default: 'green' },
});
const emit = defineEmits(['close']);

function onKey(e) { if (e.key === 'Escape') emit('close'); }
onMounted(() => window.addEventListener('keydown', onKey));
onBeforeUnmount(() => window.removeEventListener('keydown', onKey));
</script>

<template>
  <div class="ax-drawer-overlay" @click.self="emit('close')">
    <aside class="ax-drawer" role="dialog" aria-modal="true">
      <button type="button" class="ax-drawer-close" aria-label="Close" @click="emit('close')"><AxIcon name="x" :size="16" /></button>

      <div class="ax-drawer-head">
        <div class="ax-drawer-name">{{ row.name }}</div>
        <AxPhone :phone="row.phone" />
      </div>

      <div v-if="row.max_score" class="ax-drawer-score">
        <AxScore :score="row.lead_score" :max="row.max_score" :tone="tone" />
        <span class="ax-drawer-score-lbl">Lead score</span>
      </div>

      <p v-if="row.lead_score_reason" class="ax-break-reason" style="margin-top:18px;">{{ row.lead_score_reason }}</p>

      <template v-if="row.score_breakdown && row.score_breakdown.length">
        <div class="ax-break-label" style="margin-top:22px;">How it scored</div>
        <div v-for="(b, bi) in row.score_breakdown" :key="bi" class="ax-break-row">
          <span class="ax-break-mark" :class="b.awarded ? 'ax-break-mark--ok' : 'ax-break-mark--miss'">
            <AxIcon :name="b.awarded ? 'check' : 'x'" :size="11" />
          </span>
          <div style="flex:1;">
            <div class="ax-break-q">{{ b.text }}</div>
            <div v-if="b.evidence" class="ax-break-a">"{{ b.evidence }}"</div>
          </div>
          <span v-if="b.awarded_points" class="ax-break-pts">+{{ b.awarded_points }}</span>
        </div>
      </template>

      <div v-if="row.call_note" class="ax-callnote" style="margin-top:24px;">
        <div class="ax-callnote-label" style="display:flex;align-items:center;gap:7px;"><AxIcon name="message" :size="12" />Call note</div>
        <p class="ax-callnote-text" style="margin:8px 0 0;">{{ row.call_note }}</p>
      </div>

      <p
        v-if="!row.lead_score_reason && !(row.score_breakdown && row.score_breakdown.length) && !row.call_note"
        class="ax-empty-text" style="margin-top:26px;"
      >No notes or score details were captured for this lead.</p>
    </aside>
  </div>
</template>
