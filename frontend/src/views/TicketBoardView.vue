<script setup>
import { computed, onMounted, ref } from 'vue';
import { CalendarDays, Clock, MapPin, Phone, RefreshCw, Hand } from 'lucide-vue-next';
import { useDashboardState } from '../composables/useDashboardState.js';

const {
  isRealEstateTemplate,
  claimableSiteVisits,
  myAssignedTickets,
  isLoadingClaimable,
  claimingVisitId,
  refreshSiteVisitPool,
  claimSiteVisit,
  formatRelativeDate,
  siteVisitActionId,
  setSiteVisitStatus,
  transferOpenId,
  transferTargetId,
  transferMembers,
  openTransfer,
  cancelTransfer,
  submitTransfer,
} = useDashboardState();

// Board has two lanes: the open pool anyone can claim, and the visits this
// member has already claimed. Claiming a card moves it from Available → Mine.
const boardTab = ref('available');

const available = computed(() => claimableSiteVisits?.value || []);
const mine = computed(() => myAssignedTickets?.value || []);
const rows = computed(() => (boardTab.value === 'mine' ? mine.value : available.value));
const isLoading = computed(() => !!isLoadingClaimable?.value);

onMounted(() => {
  if (isRealEstateTemplate?.value) refreshSiteVisitPool?.();
});

// The RE scheduler writes canonical keys (visit_date / visit_time / phone /
// project_name); structured bookings may use the admin's field keys. Read with
// fallbacks so a card is never blank when the data is actually present.
function field(r, keys) {
  const data = r?.data || {};
  for (const k of keys) {
    const v = data[k];
    if (v !== undefined && v !== null && String(v).trim() !== '') return v;
  }
  return null;
}
const project = (r) => field(r, ['project_name', 'project', 'property_id']) || 'Unspecified project';
const visitDate = (r) => field(r, ['visit_date', 'date']);
const visitTime = (r) => field(r, ['visit_time', 'time']);
const phone = (r) => field(r, ['phone', 'contact_phone']) || r?.contact_phone || null;
const customer = (r) => field(r, ['name', 'customer', 'customer_name']);
const requirements = (r) => field(r, ['requirements', 'handoff_note']);
const needsTime = (r) => !visitTime(r) || (r?.data?.assignment_status === 'needs_manual_scheduling');
const isTerminal = (r) => ['done', 'no_show', 'resolved', 'closed'].includes(String(r?.status || '').toLowerCase());

function prettyDate(value) {
  if (!value) return null;
  // ISO YYYY-MM-DD → "Sat, 20 Jun". Anything else: show as-is.
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value));
  if (!m) return String(value);
  const d = new Date(`${value}T00:00:00`);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleDateString('en-IN', { weekday: 'short', day: '2-digit', month: 'short' });
}
function prettyTime(value) {
  if (!value) return null;
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(value));
  if (!m) return String(value);
  let h = parseInt(m[1], 10);
  const min = m[2];
  const ampm = h >= 12 ? 'PM' : 'AM';
  h = h % 12 || 12;
  return `${h}:${min} ${ampm}`;
}
</script>

<template>
  <div class="n-page">
    <header class="n-page-head n-rise">
      <span class="n-page-head__eyebrow">Real estate</span>
      <div class="n-page-head__row">
        <div>
          <h1 class="n-page-head__title">Ticket Board</h1>
          <p class="n-page-head__sub">
            Site-visit requests captured by the AI agent. Claim one to make it yours — the visit is then assigned to you.
          </p>
        </div>
        <button
          type="button"
          class="n-btn n-btn--ghost n-btn--sm"
          :disabled="isLoading"
          @click="refreshSiteVisitPool"
        >
          <RefreshCw :size="13" :class="{ 'n-spin': isLoading }" />
          {{ isLoading ? 'Refreshing' : 'Refresh' }}
        </button>
      </div>
    </header>

    <div v-if="!isRealEstateTemplate" class="n-card board__state">
      The Ticket Board is only available for real-estate workspaces.
    </div>

    <template v-else>
      <div class="board__tabs n-rise">
        <button
          type="button"
          class="board__tab"
          :class="{ 'is-on': boardTab === 'available' }"
          @click="boardTab = 'available'"
        >
          Available
          <span class="board__count">{{ available.length }}</span>
        </button>
        <button
          type="button"
          class="board__tab"
          :class="{ 'is-on': boardTab === 'mine' }"
          @click="boardTab = 'mine'"
        >
          Mine
          <span class="board__count">{{ mine.length }}</span>
        </button>
      </div>

      <div v-if="isLoading" class="n-card board__state">Loading site-visit requests…</div>
      <div v-else-if="!rows.length" class="n-card board__state board__state--empty">
        <strong>{{ boardTab === 'mine' ? 'You haven’t claimed any visits yet' : 'No open requests' }}</strong>
        <span>
          {{ boardTab === 'mine'
            ? 'Claim a request from the Available tab and it will show up here.'
            : 'New site-visit requests appear here as soon as the agent captures one.' }}
        </span>
      </div>

      <section v-else class="board__grid n-rise" data-delay="1">
        <article v-for="r in rows" :key="r.id" class="board__card">
          <header class="board__card-head">
            <span class="board__project"><MapPin :size="13" /> {{ project(r) }}</span>
            <span v-if="needsTime(r)" class="n-tag n-tag--warning">Needs time</span>
            <span v-else class="n-tag n-tag--brand">Requested</span>
          </header>

          <div class="board__meta">
            <span class="board__meta-row">
              <CalendarDays :size="13" />
              <strong>{{ prettyDate(visitDate(r)) || 'Date to confirm' }}</strong>
            </span>
            <span class="board__meta-row">
              <Clock :size="13" />
              <strong>{{ prettyTime(visitTime(r)) || 'Time to confirm' }}</strong>
            </span>
            <a
              v-if="phone(r)"
              class="board__meta-row board__phone"
              :href="`tel:${phone(r)}`"
              @click.stop
            >
              <Phone :size="13" />
              <strong>{{ phone(r) }}</strong>
            </a>
          </div>

          <p v-if="customer(r)" class="board__customer">{{ customer(r) }}</p>
          <p v-if="requirements(r)" class="board__req">{{ requirements(r) }}</p>

          <footer class="board__card-foot">
            <span class="board__age">{{ formatRelativeDate?.(r.created_at) || '' }}</span>
            <button
              v-if="boardTab === 'available'"
              type="button"
              class="n-btn n-btn--brand n-btn--sm"
              :disabled="claimingVisitId === r.id"
              @click="claimSiteVisit(r)"
            >
              <Hand :size="13" />
              {{ claimingVisitId === r.id ? 'Claiming…' : 'Claim' }}
            </button>
            <span v-else-if="isTerminal(r)" class="n-tag" :class="r.status === 'done' ? 'n-tag--success' : 'n-tag--muted'">
              {{ r.status === 'done' ? 'Done' : 'No show' }}
            </span>
            <span v-else class="n-tag n-tag--success">Yours</span>
          </footer>

          <!-- Mine: outcome + transfer controls (only on open claimed tickets) -->
          <div v-if="boardTab === 'mine' && !isTerminal(r)" class="board__actions" @click.stop>
            <div class="board__actions-row">
              <button
                type="button"
                class="n-btn n-btn--sm n-btn--brand"
                :disabled="siteVisitActionId === r.id"
                @click="setSiteVisitStatus(r, 'done')"
              >Done</button>
              <button
                type="button"
                class="n-btn n-btn--sm n-btn--ghost"
                :disabled="siteVisitActionId === r.id"
                @click="setSiteVisitStatus(r, 'no_show')"
              >Didn’t show up</button>
              <button
                type="button"
                class="n-btn n-btn--sm n-btn--ghost"
                :disabled="siteVisitActionId === r.id"
                @click="transferOpenId === r.id ? cancelTransfer() : openTransfer(r)"
              >Transfer</button>
            </div>
            <div v-if="transferOpenId === r.id" class="board__transfer">
              <select v-model="transferTargetId" class="n-input n-input--sm">
                <option value="">Transfer to…</option>
                <option v-for="m in transferMembers" :key="m.id" :value="m.id">
                  {{ m.full_name || m.email }}
                </option>
              </select>
              <button
                type="button"
                class="n-btn n-btn--sm n-btn--brand"
                :disabled="siteVisitActionId === r.id || !transferTargetId"
                @click="submitTransfer(r)"
              >Send</button>
              <button type="button" class="n-btn n-btn--sm n-btn--ghost" @click="cancelTransfer">Cancel</button>
            </div>
          </div>
        </article>
      </section>
    </template>
  </div>
</template>

<style scoped>
.board__actions {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--n-border);
  display: grid;
  gap: 8px;
}
.board__actions-row { display: flex; flex-wrap: wrap; gap: 8px; }
.board__transfer { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }

.board__state {
  padding: 40px 24px;
  text-align: center;
  color: var(--n-text-3);
  font-size: 13.5px;
}
.board__state--empty { display: grid; gap: 6px; padding: 56px 24px; }
.board__state--empty strong {
  font-family: var(--n-font-display);
  font-size: 17px;
  color: var(--n-text);
  font-weight: 600;
}

.board__tabs { display: flex; gap: 8px; margin-bottom: 16px; }
.board__tab {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  border-radius: 999px;
  border: 1px solid var(--n-border);
  background: transparent;
  color: var(--n-text-2);
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
  transition: background var(--n-t-fast) var(--n-ease), color var(--n-t-fast) var(--n-ease), border-color var(--n-t-fast) var(--n-ease);
}
.board__tab.is-on { background: var(--n-brand); border-color: var(--n-brand); color: #fff; }
.board__count {
  font-family: var(--n-font-mono);
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 999px;
  background: var(--n-surface-2);
  color: var(--n-text-3);
}
.board__tab.is-on .board__count { background: rgba(255, 255, 255, 0.22); color: #fff; }

.board__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 14px;
}
.board__card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  background: var(--n-bg-elev);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-lg);
  transition: border-color var(--n-t-fast) var(--n-ease), transform var(--n-t-fast) var(--n-ease);
}
.board__card:hover { border-color: var(--n-border-strong); transform: translateY(-1px); }
.board__card-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.board__project {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: var(--n-font-display);
  font-size: 14.5px;
  font-weight: 600;
  color: var(--n-text);
  letter-spacing: -0.01em;
  min-width: 0;
}
.board__project :deep(svg) { color: var(--n-brand); flex-shrink: 0; }

.board__meta { display: grid; gap: 7px; }
.board__meta-row {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--n-text-2);
  text-decoration: none;
}
.board__meta-row :deep(svg) { color: var(--n-text-4); }
.board__meta-row strong { font-weight: 500; color: var(--n-text); }
.board__phone { width: max-content; }
.board__phone strong { color: var(--n-brand); font-family: var(--n-font-mono); font-size: 12.5px; }
.board__phone:hover strong { text-decoration: underline; }

.board__customer { margin: 0; font-size: 12.5px; color: var(--n-text-3); }
.board__req {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.5;
  color: var(--n-text-3);
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.board__card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: auto;
  padding-top: 6px;
}
.board__age { font-family: var(--n-font-mono); font-size: 10.5px; color: var(--n-text-4); }
</style>
