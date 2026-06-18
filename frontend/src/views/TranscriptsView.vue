<script setup>
import { onMounted, ref } from 'vue';
import { ScrollText, Download, Clock } from 'lucide-vue-next';
import { useDashboardState } from '../composables/useDashboardState.js';

const {
  transcripts,
  transcriptsLoading,
  loadTranscripts,
  loadTranscript,
  downloadTranscript,
} = useDashboardState();

const selected = ref(null);        // the open transcript { call_id, turns, ... }
const loadingDetail = ref(false);

onMounted(() => {
  if (typeof loadTranscripts === 'function') loadTranscripts();
});

const list = () => transcripts?.value || [];

function fmtDate(iso) {
  if (!iso) return '—';
  try { return new Date(iso).toLocaleString(); } catch (_) { return iso; }
}
function fmtDur(s) {
  const n = Math.round(Number(s) || 0);
  if (!n) return '—';
  const m = Math.floor(n / 60);
  const sec = n % 60;
  return m ? `${m}m ${sec}s` : `${sec}s`;
}
function roleLabel(role) {
  const r = String(role || '').toLowerCase();
  if (r === 'assistant' || r === 'agent') return 'Agent';
  if (r === 'user') return 'Caller';
  return role || 'Turn';
}

async function openCall(row) {
  if (!row.has_transcript) return;
  loadingDetail.value = true;
  selected.value = { call_id: row.call_id, turns: [] };
  try {
    const data = await loadTranscript(row.call_id);
    if (data) selected.value = data;
  } finally {
    loadingDetail.value = false;
  }
}
</script>

<template>
  <div class="n-page">
    <header class="n-page-head n-rise">
      <span class="n-page-head__eyebrow">Calls</span>
      <div class="n-page-head__row">
        <div>
          <h1 class="n-page-head__title">Transcripts</h1>
          <p class="n-page-head__sub">
            Every call and its conversation. Transcripts are kept for one month, then deleted
            automatically — download any as a text file before it expires.
          </p>
        </div>
        <button type="button" class="n-btn n-btn--ghost n-btn--sm" :disabled="transcriptsLoading" @click="loadTranscripts">
          Refresh
        </button>
      </div>
    </header>

    <section class="tx__layout n-rise" data-delay="1">
      <!-- Call list -->
      <article class="n-card tx__list">
        <p v-if="transcriptsLoading" class="tx__state">Loading…</p>
        <div v-else-if="!list().length" class="n-empty tx__empty">
          <div class="n-empty__icon"><ScrollText :size="18" /></div>
          <p class="n-empty__title">No calls yet</p>
          <p class="n-empty__copy">Calls and their transcripts will appear here.</p>
        </div>
        <ul v-else class="tx__items">
          <li
            v-for="c in list()"
            :key="c.call_id"
            class="tx__item"
            :class="{ 'is-active': selected && selected.call_id === c.call_id, 'is-expired': !c.has_transcript }"
            @click="openCall(c)"
          >
            <div class="tx__item-main">
              <span class="n-tag n-tag--mono tx__item-kind">{{ c.kind }}</span>
              <span class="tx__item-date">{{ fmtDate(c.started_at) }}</span>
            </div>
            <div class="tx__item-meta">
              <span><Clock :size="11" /> {{ fmtDur(c.duration_seconds) }}</span>
              <span v-if="!c.has_transcript" class="tx__expired">transcript expired</span>
              <button
                v-else
                type="button"
                class="n-btn n-btn--ghost n-btn--sm tx__dl"
                @click.stop="downloadTranscript(c.call_id)"
              >
                <Download :size="12" /> .txt
              </button>
            </div>
          </li>
        </ul>
      </article>

      <!-- Transcript detail -->
      <article class="n-card tx__detail">
        <div v-if="!selected" class="tx__detail-empty">
          <p>Select a call to read its transcript.</p>
        </div>
        <template v-else>
          <header class="tx__detail-head">
            <div>
              <strong>Transcript</strong>
              <span class="tx__detail-sub">{{ fmtDate(selected.started_at) }} · {{ fmtDur(selected.duration_seconds) }}</span>
            </div>
            <button type="button" class="n-btn n-btn--brand n-btn--sm" @click="downloadTranscript(selected.call_id)">
              <Download :size="13" /> Download .txt
            </button>
          </header>
          <p v-if="loadingDetail" class="tx__state">Loading…</p>
          <div v-else class="tx__turns">
            <p v-if="!(selected.turns || []).length" class="tx__state">No conversation captured.</p>
            <div
              v-for="(t, i) in (selected.turns || [])"
              :key="i"
              class="tx__turn"
              :class="`tx__turn--${String(t.role || '').toLowerCase()}`"
            >
              <span class="tx__turn-role">{{ roleLabel(t.role) }}</span>
              <span class="tx__turn-text">{{ t.content }}</span>
            </div>
          </div>
        </template>
      </article>
    </section>
  </div>
</template>

<style scoped>
.tx__layout { display: grid; grid-template-columns: 0.9fr 1.1fr; gap: 16px; align-items: start; }
@media (max-width: 1000px) { .tx__layout { grid-template-columns: 1fr; } }

.tx__list, .tx__detail { padding: 0; overflow: hidden; }
.tx__state { padding: 28px; text-align: center; color: var(--n-text-3); font-size: 13.5px; }
.tx__empty { margin: 16px; }

.tx__items { list-style: none; margin: 0; padding: 4px 0; max-height: 72vh; overflow-y: auto; }
.tx__item {
  padding: 12px 18px; border-bottom: 2px solid var(--n-border);
  cursor: pointer; display: grid; gap: 6px; position: relative;
}
.tx__item:last-child { border-bottom: 0; }
.tx__item:hover { background: var(--n-surface); }
.tx__item.is-active { 
  background: var(--n-surface); 
  outline: 2px solid var(--n-text);
  outline-offset: -2px;
  border-right: 6px solid var(--n-text);
  z-index: 1;
}
.tx__item.is-expired { cursor: default; opacity: 0.7; }
.tx__item.is-expired:hover { background: transparent; }
.tx__item-main { display: flex; align-items: center; gap: 10px; }
.tx__item-kind { text-transform: capitalize; }
.tx__item-date { font-size: 13px; color: var(--n-text); font-weight: 500; }
.tx__item-meta {
  display: flex; align-items: center; justify-content: space-between;
  gap: 10px; font-size: 12px; color: var(--n-text-3); font-family: var(--n-font-mono);
}
.tx__item-meta > span { display: inline-flex; align-items: center; gap: 4px; }
.tx__expired { font-style: italic; }
.tx__dl { padding: 2px 8px; }

.tx__detail-empty { padding: 44px 24px; text-align: center; color: var(--n-text-3); font-size: 13.5px; font-style: italic; }
.tx__detail-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 16px 20px; border-bottom: 2px solid var(--n-border);
}
.tx__detail-sub { display: block; font-size: 12px; color: var(--n-text-3); margin-top: 2px; font-family: var(--n-font-mono); }
.tx__turns { padding: 16px 20px; max-height: 72vh; overflow-y: auto; display: grid; gap: 12px; }
.tx__turn { display: grid; gap: 2px; }
.tx__turn-role {
  font-size: 11px; font-weight: 600; color: var(--n-text);
  text-transform: uppercase; letter-spacing: 0.04em; font-family: var(--n-font-mono);
}
.tx__turn--assistant .tx__turn-role, .tx__turn--agent .tx__turn-role { color: var(--n-text); }
.tx__turn-text { font-size: 13.5px; line-height: 1.5; color: var(--n-text); white-space: pre-wrap; font-family: var(--n-font-mono); }
</style>
