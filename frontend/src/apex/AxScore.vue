<script setup>
// Lead-score visualization — the number plus a small graded fill bar, so a 7/9
// reads at a glance instead of as text. tone: 'green' (qualified) | 'grey'.
import { computed } from 'vue';

const props = defineProps({
  score: { type: [Number, String], default: 0 },
  max: { type: [Number, String], default: 0 },
  tone: { type: String, default: 'green' },
});

const pct = computed(() => {
  const m = Number(props.max) || 0;
  if (!m) return 0;
  return Math.max(0, Math.min(1, (Number(props.score) || 0) / m));
});
</script>

<template>
  <span class="ax-scoreviz" :class="[`is-${tone}`, { 'is-high': pct >= 0.8 }]">
    <template v-if="Number(max)">
      <span class="ax-scoreviz-num">{{ Number(score) || 0 }}/{{ Number(max) }}</span>
      <span class="ax-scoreviz-track"><span class="ax-scoreviz-fill" :style="{ width: (pct * 100).toFixed(0) + '%' }"></span></span>
    </template>
    <span v-else class="ax-scoreviz-num">—</span>
  </span>
</template>
