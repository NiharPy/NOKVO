<script setup>
// Animated number — tweens between values so wallet figures and counters
// count up instead of snapping. Respects prefers-reduced-motion.
import { ref, watch, onMounted, onBeforeUnmount } from 'vue';

const props = defineProps({
  value: { type: [Number, String], default: 0 },
  format: { type: Function, default: (n) => Math.round(n).toLocaleString('en-IN') },
  duration: { type: Number, default: 700 },
});

const shown = ref(0);
let raf = null;
const reduced = typeof window !== 'undefined'
  && window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function tween(from, to) {
  if (raf) cancelAnimationFrame(raf);
  if (reduced || from === to) { shown.value = to; return; }
  const t0 = performance.now();
  const step = (t) => {
    const p = Math.min(1, (t - t0) / props.duration);
    const e = 1 - Math.pow(1 - p, 3); // ease-out cubic
    shown.value = from + (to - from) * e;
    if (p < 1) raf = requestAnimationFrame(step);
  };
  raf = requestAnimationFrame(step);
}

onMounted(() => tween(0, Number(props.value) || 0));
watch(() => props.value, (to, from) => tween(Number(from) || 0, Number(to) || 0));
onBeforeUnmount(() => { if (raf) cancelAnimationFrame(raf); });
</script>

<template><span>{{ format(shown) }}</span></template>
