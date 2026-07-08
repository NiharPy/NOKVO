<script setup>
// "LINE LIVE" — the campaign-launch sequence. The product's emotional peak
// ("my campaign is now dialing thousands of people") gets ~1.8s of ceremony:
// veil → mark + radar rings → CAMPAIGN LIVE stamps in → the contact count
// rolls up → veil lifts. Pure CSS delays orchestrate it; one timer ends it.
// Click anywhere skips. Under reduced motion every layer lands instantly
// (the theme kill-switch) and the timer shortens to a beat.
import { onMounted, onBeforeUnmount } from 'vue';
import AxCount from './AxCount.vue';
import nokvoMark from '../assets/nokvo-logo.png';

const props = defineProps({
  contacts: { type: Number, default: 0 },
});
const emit = defineEmits(['done']);

const reduced = typeof window !== 'undefined'
  && window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

let timer = null;
function finish() {
  if (timer) { clearTimeout(timer); timer = null; }
  emit('done');
}
onMounted(() => { timer = setTimeout(finish, reduced ? 800 : 2050); });
onBeforeUnmount(() => { if (timer) clearTimeout(timer); });
</script>

<template>
  <div class="ax-launchfx" role="status" @click="finish">
    <div class="ax-lfx-stage">
      <div class="ax-lfx-emitter">
        <span class="ax-lfx-ring"></span>
        <span class="ax-lfx-ring ax-lfx-ring--2"></span>
        <img :src="nokvoMark" class="ax-lfx-mark" alt="" />
      </div>
      <div class="ax-lfx-stamp">CAMPAIGN LIVE</div>
      <div class="ax-lfx-sub">
        <AxCount :value="contacts" :duration="1400" /> CONTACTS QUEUED · DIALING 09:00–19:00 IST
      </div>
      <div class="ax-lfx-skip">click to continue</div>
    </div>
  </div>
</template>

<style scoped>
.ax-launchfx {
  position: fixed; inset: 0; z-index: 1400; cursor: pointer;
  display: grid; place-items: center; overflow: hidden;
  background: rgba(6, 6, 7, 0.92);
  backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
  animation: lfxVeil 0.2s ease both;
}
@keyframes lfxVeil { from { opacity: 0; } to { opacity: 1; } }

.ax-lfx-stage { display: flex; flex-direction: column; align-items: center; text-align: center; padding: 24px; }

/* the mark arrives, broadcasting two radar rings */
.ax-lfx-emitter { position: relative; display: grid; place-items: center; width: 120px; height: 120px; }
.ax-lfx-mark {
  height: 52px; width: auto; position: relative; z-index: 1;
  animation: lfxMark 0.75s cubic-bezier(0.22, 1, 0.36, 1) 0.15s both;
}
@keyframes lfxMark { from { opacity: 0; transform: scale(0.6); } to { opacity: 1; transform: scale(1); } }
.ax-lfx-ring {
  position: absolute; inset: 0; border-radius: 50%;
  border: 1.5px solid rgba(230, 38, 48, 0.55);
  animation: lfxRing 1s cubic-bezier(0.22, 1, 0.36, 1) 0.3s both;
}
.ax-lfx-ring--2 { animation-delay: 0.6s; border-color: rgba(230, 38, 48, 0.35); }
@keyframes lfxRing { from { opacity: 0.9; transform: scale(0.4); } to { opacity: 0; transform: scale(2.6); } }

/* the stamp: tracking collapses while a scanline sweeps it once */
.ax-lfx-stamp {
  position: relative; overflow: hidden; margin-top: 26px; padding: 2px 8px;
  font-family: 'JetBrains Mono', monospace; font-size: 21px; font-weight: 700;
  color: #F3F2F0; white-space: nowrap;
  animation: lfxStamp 0.5s cubic-bezier(0.22, 1, 0.36, 1) 0.7s both;
}
@keyframes lfxStamp {
  from { opacity: 0; letter-spacing: 0.9em; padding-left: 0.9em; }
  to { opacity: 1; letter-spacing: 0.3em; padding-left: 0.3em; }
}
.ax-lfx-stamp::after {
  content: ''; position: absolute; top: 0; bottom: 0; left: 0; width: 34%;
  background: linear-gradient(100deg, transparent, rgba(230, 38, 48, 0.45), transparent);
  transform: translateX(-120%) skewX(-18deg);
  animation: lfxScan 0.6s ease-in-out 0.95s both;
}
@keyframes lfxScan { from { transform: translateX(-120%) skewX(-18deg); } to { transform: translateX(360%) skewX(-18deg); } }

.ax-lfx-sub {
  margin-top: 16px; font-family: 'JetBrains Mono', monospace; font-size: 12px;
  letter-spacing: 0.14em; color: rgba(255, 255, 255, 0.55);
  animation: lfxRise 0.5s cubic-bezier(0.22, 1, 0.36, 1) 1.1s both;
}
.ax-lfx-skip {
  margin-top: 34px; font-size: 10.5px; letter-spacing: 0.22em; text-transform: uppercase;
  color: rgba(255, 255, 255, 0.22);
  animation: lfxRise 0.5s ease 1.5s both;
}
@keyframes lfxRise { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }

@media (prefers-reduced-motion: reduce) {
  .ax-launchfx, .ax-launchfx *, .ax-launchfx *::after {
    animation-duration: 0.01ms !important; animation-delay: 0ms !important;
  }
}
</style>
