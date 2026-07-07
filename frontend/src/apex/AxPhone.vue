<script setup>
// Phone cell with hover-reveal copy — click copies the number, the icon morphs
// to a check and a toast confirms. Always visible on touch (CSS).
import { ref, inject } from 'vue';
import AxIcon from './AxIcon.vue';

const props = defineProps({ phone: { type: String, default: '' } });
const apex = inject('apex', null);
const copied = ref(false);
let timer = null;

async function copy() {
  try {
    await navigator.clipboard.writeText(props.phone || '');
    copied.value = true;
    apex?.toast?.('Phone number copied');
    clearTimeout(timer);
    timer = setTimeout(() => { copied.value = false; }, 1400);
  } catch {
    apex?.toast?.('Could not copy the number', 'err');
  }
}
</script>

<template>
  <span class="ax-cell-phone ax-phone">
    <span class="ax-phone-num">{{ phone }}</span>
    <button
      type="button" class="ax-copy" :class="{ 'is-done': copied }"
      :title="copied ? 'Copied' : 'Copy number'" :aria-label="copied ? 'Copied' : 'Copy number'"
      @click.stop="copy"
    ><AxIcon :name="copied ? 'check' : 'copy'" :size="12" /></button>
  </span>
</template>
