// Motion helpers shared by the APEX views.
import { ref } from 'vue';

// Track which rows ARRIVED on the latest reload so the view can flash them
// (.is-new). The first load is never "new" — only rows that appear on a
// subsequent refresh count as arrivals.
export function useNewRows(keyFn) {
  const seen = ref(null);          // null until the first track()
  const fresh = ref(new Set());

  function track(rows) {
    const keys = new Set((rows || []).map(keyFn));
    if (seen.value === null) {
      seen.value = keys;
      fresh.value = new Set();
      return;
    }
    const arrived = new Set();
    for (const k of keys) if (!seen.value.has(k)) arrived.add(k);
    seen.value = keys;
    fresh.value = arrived;
  }

  const isNew = (row) => fresh.value.has(keyFn(row));
  return { track, isNew };
}
