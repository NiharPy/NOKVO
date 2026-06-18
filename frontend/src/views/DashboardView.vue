<script setup>
import { computed } from 'vue';
import {
  ArrowUpRight,
  CalendarClock,
  CalendarDays,
  PhoneIncoming,
  PhoneOutgoing,
  RefreshCw,
  Repeat,
  Sparkles,
  Trash2,
  UserPlus,
} from 'lucide-vue-next';
import { useDashboardState } from '../composables/useDashboardState.js';

const {
  costSummary,
  isLoadingCostSummary,
  loadCostSummary,
  formatMinutes,
  memberPageLabel,
  currentOrganization,
  inviteForm,
  isInviteDomainValid,
  inviteValidationMessage,
  isSavingMember,
  inviteCanSubmit,
  inviteMember,
  filteredMembers,
  members,
  isLoadingMembers,
  assignmentForMember,
  openMemberTimetable,
  startAssignmentEdit,
  isAdmin,
  currentUser,
  removingMemberId,
  removeMember,
  followupSummary,
  loadFollowupSummary,
  isRealEstateTemplate,
  orgWorkingHours,
  isSavingOrgHours,
  saveOrgWorkingHours,
  toggleOrgHoursDay,
  ORG_HOURS_DAYS,
} = useDashboardState();

const ORG_HOURS_DAY_LABELS = {
  mon: 'Mon', tue: 'Tue', wed: 'Wed', thu: 'Thu', fri: 'Fri', sat: 'Sat', sun: 'Sun',
};

const followupCounts = computed(() => ({
  scheduled: followupSummary?.value?.scheduled || 0,
  today: followupSummary?.value?.today || 0,
  in_flight: followupSummary?.value?.in_flight || 0,
  exhausted: followupSummary?.value?.exhausted || 0,
  paused: followupSummary?.value?.paused || 0,
}));

const periodLabel = computed(() =>
  new Date().toLocaleString('en-IN', { month: 'long', year: 'numeric' }),
);

const monthTotal = computed(() => costSummary?.value?.this_month?.rupees_formatted || '₹0.00');
const monthCalls = computed(() => costSummary?.value?.this_month?.call_count ?? 0);
const todayTotal = computed(() => costSummary?.value?.today?.rupees_formatted || '₹0.00');
const todayCalls = computed(() => costSummary?.value?.today?.call_count ?? 0);
const minutesToday = computed(() => formatMinutes?.(costSummary?.value?.today?.seconds) || '0 min');
const minutesMonth = computed(() => formatMinutes?.(costSummary?.value?.this_month?.seconds) || '0 min');
const recent = computed(() => (costSummary?.value?.recent_calls || []).slice(0, 8));

const rate = computed(() => {
  const rpm = costSummary?.value?.rate?.rupees_per_minute ?? 8;
  const rps = costSummary?.value?.rate?.rupees_per_second_display ?? '0.13';
  return `₹${rpm} / min · ₹${rps} / sec`;
});

function relTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleString('en-IN', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

function initial(m) {
  return (m?.full_name || m?.email || 'N').trim().charAt(0).toUpperCase();
}
</script>

<template>
  <div class="n-page dash">
    <!-- Page head -->
    <header class="n-page-head n-rise">
      <span class="n-page-head__eyebrow">{{ periodLabel }} · workspace</span>
      <div class="n-page-head__row">
        <div>
          <h1 class="n-page-head__title">{{ currentOrganization?.name || 'Dashboard' }}</h1>
          <p class="n-page-head__sub">Conversation cost, team access and admissions, in one calm view.</p>
        </div>
        <div class="dash__head-actions">
          <span class="n-tag n-tag--mono">{{ rate }}</span>
          <button
            type="button"
            class="n-btn n-btn--ghost n-btn--sm"
            @click="loadCostSummary"
            :disabled="isLoadingCostSummary"
          >
            <RefreshCw :size="13" :class="{ 'n-spin': isLoadingCostSummary }" />
            {{ isLoadingCostSummary ? 'Refreshing' : 'Refresh' }}
          </button>
        </div>
      </div>
    </header>

    <!-- Stats hero -->
    <section class="n-section n-rise" data-delay="1">
      <div class="dash__stats">
        <article class="n-stat dash__stat--hero">
          <span class="n-stat__label">
            <span class="n-dot n-dot--success n-dot--pulse"></span>
            This month
          </span>
          <strong class="n-stat__value">{{ monthTotal }}</strong>
          <span class="n-stat__sub">{{ monthCalls }} call{{ monthCalls === 1 ? '' : 's' }} · {{ minutesMonth }} of conversation</span>
        </article>
        <article class="n-stat">
          <span class="n-stat__label">Today</span>
          <strong class="n-stat__value">{{ todayTotal }}</strong>
          <span class="n-stat__sub">{{ todayCalls }} call{{ todayCalls === 1 ? '' : 's' }}</span>
        </article>
        <article class="n-stat">
          <span class="n-stat__label">Minutes today</span>
          <strong class="n-stat__value">{{ minutesToday }}</strong>
          <span class="n-stat__sub">conversation time</span>
        </article>
      </div>
    </section>

    <!-- Follow-up agent tile -->
    <section class="n-section n-rise dash__followups" data-delay="2">
      <article class="n-card dash__followup-card">
        <header class="dash__followup-head">
          <div>
            <span class="dash__followup-eyebrow">
              <Repeat :size="12" />
              Follow-up agent
            </span>
            <h2 class="dash__followup-title">{{ followupCounts.scheduled }} call{{ followupCounts.scheduled === 1 ? '' : 's' }} on the books</h2>
            <p class="dash__followup-sub">
              Honors callback promises, falls back to your campaign rules, clamps to 9–7 IST.
              Opt-outs and conversions auto-kill the queue.
            </p>
          </div>
          <button type="button" class="n-btn n-btn--ghost n-btn--sm" @click="loadFollowupSummary">
            <RefreshCw :size="13" />
            Refresh
          </button>
        </header>
        <div class="dash__followup-stats">
          <div>
            <span class="dash__followup-cap">Today</span>
            <strong>{{ followupCounts.today }}</strong>
          </div>
          <div>
            <span class="dash__followup-cap">In flight</span>
            <strong>{{ followupCounts.in_flight }}</strong>
          </div>
          <div>
            <span class="dash__followup-cap">Paused</span>
            <strong>{{ followupCounts.paused }}</strong>
          </div>
          <div>
            <span class="dash__followup-cap">Exhausted</span>
            <strong>{{ followupCounts.exhausted }}</strong>
          </div>
        </div>
      </article>
    </section>

    <!-- Two-column: Recent calls + Invite -->
    <section class="dash__split n-rise" data-delay="2">
      <article class="n-card dash__recent">
        <header class="dash__recent-head">
          <div>
            <h2 class="n-section__title">Recent conversations</h2>
            <p class="n-section__sub">Costs post here as soon as voice sessions complete.</p>
          </div>
          <span class="n-tag n-tag--mono">{{ recent.length }} entries</span>
        </header>

        <div v-if="!recent.length" class="n-empty">
          <div class="n-empty__icon">
            <Sparkles :size="18" />
          </div>
          <p class="n-empty__title">No conversations yet</p>
          <p class="n-empty__copy">
            Costs appear here the moment a voice session ends. Start the agent or test a call to see your first entry.
          </p>
        </div>

        <ul v-else class="dash__call-list">
          <li v-for="call in recent" :key="call.id" class="dash__call">
            <div class="dash__call-kind">
              <span class="dash__call-icon" :data-kind="call.kind">
                <PhoneIncoming v-if="call.kind === 'inbound'" :size="14" />
                <PhoneOutgoing v-else :size="14" />
              </span>
              <div class="dash__call-meta">
                <strong class="n-truncate">{{ call.kind === 'inbound' ? 'Inbound call' : call.kind === 'tester' ? 'Tester session' : 'Outbound call' }}</strong>
                <span>{{ relTime(call.started_at) }}</span>
              </div>
            </div>
            <span class="dash__call-dur n-mono">{{ Math.round(Number(call.duration_seconds || 0)) }}<small> sec</small></span>
            <strong class="dash__call-amt n-mono">{{ call.rupees_formatted }}</strong>
          </li>
        </ul>
      </article>

      <article class="n-card dash__invite">
        <header class="dash__invite-head">
          <span class="n-page-head__eyebrow">Admissions</span>
          <h2 class="n-section__title">Invite to workspace</h2>
          <p class="n-section__sub">
            Restricted to verified emails on
            <span class="n-kbd">@{{ currentOrganization?.email_domain || 'your-domain' }}</span>.
            Invitees set their own password and TOTP.
          </p>
        </header>

        <form class="dash__form" @submit.prevent="inviteMember">
          <label class="n-field">
            <span class="n-field__label">Work email</span>
            <input v-model="inviteForm.email" type="email" class="n-input" placeholder="name@yourcompany.com" required />
          </label>
          <div class="dash__form-row">
            <label class="n-field">
              <span class="n-field__label">Full name</span>
              <input v-model="inviteForm.full_name" type="text" class="n-input" placeholder="Optional" />
            </label>
            <label class="n-field">
              <span class="n-field__label">Role</span>
              <select v-model="inviteForm.role" class="n-select">
                <option value="member">Member</option>
                <option value="manager">Manager</option>
                <option value="viewer">Viewer</option>
                <option value="admin">Admin</option>
              </select>
            </label>
          </div>
          <p class="dash__form-helper" :class="{ 'is-invalid': !isInviteDomainValid }">
            {{ inviteValidationMessage }}
          </p>
          <button type="submit" class="n-btn n-btn--primary" :disabled="isSavingMember || !inviteCanSubmit">
            <UserPlus :size="14" />
            <span>{{ isSavingMember ? 'Sending invite…' : 'Send invitation' }}</span>
            <ArrowUpRight :size="13" class="dash__form-arrow" />
          </button>
        </form>
      </article>
    </section>

    <!-- Organization working hours (real estate): one window for the whole
         team. Replaces per-member timings — the voice agent refuses site
         visits outside this window and offers the closest valid slot. -->
    <section v-if="isRealEstateTemplate" class="n-section n-rise" data-delay="2">
      <header class="n-section__head">
        <div>
          <h2 class="n-section__title">Organization working hours</h2>
          <p class="n-section__sub">
            Shared site-visit hours for every agent. The voice agent won't book visits outside this window.
          </p>
        </div>
        <span v-if="orgWorkingHours.working_hours_summary" class="n-tag n-tag--mono">
          {{ orgWorkingHours.working_hours_summary }}
        </span>
      </header>

      <article class="n-card org-hours">
        <div class="org-hours__days">
          <button
            v-for="day in ORG_HOURS_DAYS"
            :key="day"
            type="button"
            class="org-hours__day"
            :class="{ 'is-on': orgWorkingHours.working_days.includes(day) }"
            :disabled="!isAdmin"
            @click="toggleOrgHoursDay(day)"
          >
            {{ ORG_HOURS_DAY_LABELS[day] }}
          </button>
        </div>
        <div class="org-hours__times">
          <label class="org-hours__field">
            <span>Opens</span>
            <input type="time" v-model="orgWorkingHours.start_time" :disabled="!isAdmin" />
          </label>
          <label class="org-hours__field">
            <span>Closes</span>
            <input type="time" v-model="orgWorkingHours.end_time" :disabled="!isAdmin" />
          </label>
          <button
            v-if="isAdmin"
            type="button"
            class="n-btn n-btn--brand n-btn--sm"
            :disabled="isSavingOrgHours"
            @click="saveOrgWorkingHours"
          >
            {{ isSavingOrgHours ? 'Saving…' : 'Save hours' }}
          </button>
        </div>
        <p v-if="!isAdmin" class="org-hours__hint">Only an admin can change the organization's working hours.</p>
      </article>
    </section>

    <!-- Members table -->
    <section class="n-section n-rise" data-delay="3">
      <header class="n-section__head">
        <div>
          <h2 class="n-section__title">{{ memberPageLabel }}</h2>
          <p class="n-section__sub">Access, availability, and current load.</p>
        </div>
        <span class="n-tag n-tag--mono">{{ filteredMembers.length }} / {{ members.length }}</span>
      </header>

      <article class="n-card dash__roster">
        <div v-if="isLoadingMembers" class="dash__roster-empty">
          Loading members…
        </div>
        <div v-else-if="!filteredMembers.length" class="dash__roster-empty">
          No members match the current filter.
        </div>
        <ul v-else class="dash__roster-list">
          <li v-for="m in filteredMembers" :key="m.id" class="dash__roster-row">
            <div class="dash__roster-id">
              <span class="n-avatar" :class="{ 'n-avatar--admin': m.role === 'admin' }">{{ initial(m) }}</span>
              <div>
                <strong>{{ m.full_name || 'Unnamed member' }}</strong>
                <span class="dash__roster-email">{{ m.email }}</span>
              </div>
            </div>

            <div class="dash__roster-tags">
              <span class="n-tag" :class="{ 'n-tag--brand': m.role === 'admin' }">{{ m.role }}</span>
              <span class="n-tag" :class="m.status === 'active' ? 'n-tag--success' : 'n-tag--warning'">
                <span class="n-dot" :class="m.status === 'active' ? 'n-dot--success' : 'n-dot--warning'"></span>
                {{ m.status }}
              </span>
            </div>

            <div class="dash__roster-avail">
              <span class="dash__roster-cap">Availability</span>
              <p v-if="isRealEstateTemplate">Org working hours</p>
              <p v-else>{{ assignmentForMember(m.id).availability_summary }}</p>
            </div>

            <div class="dash__roster-load">
              <span class="dash__roster-cap">Load</span>
              <strong>{{ assignmentForMember(m.id).active_request_count || 0 }}</strong>
            </div>

            <div class="dash__roster-actions">
              <button type="button" class="n-btn n-btn--ghost n-btn--sm" @click="openMemberTimetable(m)">
                <CalendarClock :size="13" />
                Timetable
              </button>
              <button
                v-if="!isRealEstateTemplate"
                type="button"
                class="n-btn n-btn--ghost n-btn--sm"
                @click="startAssignmentEdit(m)"
              >
                <CalendarDays :size="13" />
                Schedule
              </button>
              <button
                v-if="isAdmin && m.id !== currentUser?.id"
                type="button"
                class="n-btn n-btn--danger n-btn--sm"
                :disabled="removingMemberId === m.id"
                @click="removeMember(m)"
              >
                <Trash2 :size="13" />
                {{ removingMemberId === m.id ? 'Removing…' : 'Remove' }}
              </button>
            </div>
          </li>
        </ul>
      </article>
    </section>
  </div>
</template>

<style scoped>
/* ─── Org working hours (real estate) ─────────────────── */
.org-hours { display: flex; flex-direction: column; gap: 14px; padding: 18px; }
.org-hours__days { display: flex; flex-wrap: wrap; gap: 8px; }
.org-hours__day {
  min-width: 48px;
  padding: 7px 12px;
  border-radius: 0;
  border: 2px solid var(--n-border);
  background: transparent;
  color: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}
.org-hours__day.is-on {
  background: var(--n-text);
  border-color: var(--n-text);
  color: var(--n-text-inverse);
}
.org-hours__day:disabled { cursor: default; opacity: 0.75; }
.org-hours__times { display: flex; align-items: flex-end; gap: 14px; flex-wrap: wrap; }
.org-hours__field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; font-weight: 600; }
.org-hours__field input {
  padding: 7px 10px;
  border-radius: 0;
  border: 2px solid var(--n-border);
  background: transparent;
  color: inherit;
}
.org-hours__hint { font-size: 12px; opacity: 0.7; margin: 0; }

/* ─── Page-local composition ─────────────────────────── */
.dash__head-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

/* ─── Stats row ─────────────────────────────────────── */
.dash__stats {
  display: grid;
  grid-template-columns: 1.4fr 1fr 1fr;
  gap: 16px;
}
.dash__stat--hero {
  background: var(--n-bg-elev);
  position: relative;
}
.dash__stat--hero::before {
  display: none;
}
.dash__stat--hero .n-stat__value {
  font-size: clamp(40px, 4.2vw, 52px);
}
.dash__stat--hero .n-stat__label { color: var(--n-text-2); }

@media (max-width: 920px) {
  .dash__stats { grid-template-columns: 1fr; }
}

/* ─── Split: recent + invite ───────────────────────── */
.dash__split {
  display: grid;
  grid-template-columns: 1.35fr 1fr;
  gap: 16px;
  align-items: start;
}
@media (max-width: 1000px) {
  .dash__split { grid-template-columns: 1fr; }
}

.dash__recent { padding: 0; overflow: hidden; }
.dash__recent-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  padding: 20px 24px 18px;
  border-bottom: 2px solid var(--n-border);
}
.dash__recent-head .n-section__title { font-size: 17px; letter-spacing: -0.01em; }

.dash__call-list { list-style: none; margin: 0; padding: 4px 0; }
.dash__call {
  display: grid;
  grid-template-columns: 1fr auto auto;
  align-items: center;
  gap: 16px;
  padding: 12px 24px;
  border-bottom: 2px solid var(--n-border);
  transition: background var(--n-t-fast) var(--n-ease);
}
.dash__call:last-child { border-bottom: 0; }
.dash__call:hover { background: var(--n-surface); }

.dash__call-kind { display: flex; align-items: center; gap: 12px; min-width: 0; }
.dash__call-icon {
  width: 32px;
  height: 32px;
  border-radius: 0;
  border: 2px solid var(--n-text);
  display: grid;
  place-items: center;
  flex-shrink: 0;
}
.dash__call-icon[data-kind="inbound"] { background: var(--n-text); color: var(--n-text-inverse); }
.dash__call-icon[data-kind="outbound"] { background: var(--n-bg); color: var(--n-text); }
.dash__call-meta { display: grid; line-height: 1.2; min-width: 0; }
.dash__call-meta strong { font-size: 13.5px; font-weight: 500; color: var(--n-text); }
.dash__call-meta span { font-size: 12px; color: var(--n-text-3); font-family: var(--n-font-mono); }

.dash__call-dur {
  font-size: 13px;
  color: var(--n-text-3);
  font-feature-settings: 'tnum' 1;
  min-width: 64px;
  text-align: right;
}
.dash__call-dur small { font-size: 10.5px; opacity: 0.7; margin-left: 2px; }
.dash__call-amt {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--n-text);
  min-width: 72px;
  text-align: right;
  font-feature-settings: 'tnum' 1;
}

.dash__recent .n-empty { margin: 16px 24px 24px; }

/* ─── Invite card ──────────────────────────────────── */
.dash__invite { display: grid; gap: 20px; }
.dash__invite-head { display: grid; gap: 6px; }
.dash__invite-head .n-section__title { font-size: 17px; letter-spacing: -0.01em; }

.dash__form { display: grid; gap: 14px; }
.dash__form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.dash__form-helper {
  margin: 0;
  font-size: 12px;
  color: var(--n-text-3);
  line-height: 1.4;
}
.dash__form-helper.is-invalid { color: var(--n-danger); }
.dash__form-arrow { margin-left: 2px; transition: transform var(--n-t-fast) var(--n-ease); }
.n-btn--primary:hover:not(:disabled) .dash__form-arrow { transform: translate(2px, -2px); }

/* ─── Roster ───────────────────────────────────────── */
.dash__roster { padding: 0; overflow: hidden; }
.dash__roster-empty {
  padding: 36px 24px;
  color: var(--n-text-3);
  font-size: 13.5px;
  text-align: center;
}
.dash__roster-list { list-style: none; margin: 0; padding: 0; }
.dash__roster-row {
  display: grid;
  grid-template-columns:
    [id] minmax(220px, 1.6fr)
    [tags] auto
    [avail] minmax(180px, 1.4fr)
    [load] 80px
    [actions] auto;
  gap: 20px;
  align-items: center;
  padding: 16px 24px;
  border-bottom: 2px solid var(--n-border);
  transition: background var(--n-t-fast) var(--n-ease);
}
.dash__roster-row:last-child { border-bottom: 0; }
.dash__roster-row:hover { background: var(--n-surface); }

.dash__roster-id { display: flex; align-items: center; gap: 12px; min-width: 0; }
.dash__roster-id strong {
  display: block;
  font-family: var(--n-font-display);
  font-weight: 600;
  font-size: 14px;
  color: var(--n-text);
  letter-spacing: -0.01em;
  line-height: 1.3;
}
.dash__roster-email {
  display: block;
  font-family: var(--n-font-mono);
  font-size: 11.5px;
  color: var(--n-text-3);
  letter-spacing: 0.01em;
  margin-top: 2px;
}

.dash__roster-tags { display: flex; gap: 6px; flex-wrap: wrap; }

.dash__roster-avail { display: grid; gap: 4px; min-width: 0; }
.dash__roster-cap {
  font-family: var(--n-font-mono);
  font-size: 9.5px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--n-text-4);
}
.dash__roster-avail p {
  margin: 0;
  font-size: 12.5px;
  color: var(--n-text-2);
  line-height: 1.4;
}
.dash__roster-load { display: grid; gap: 4px; justify-items: start; }
.dash__roster-load strong {
  font-family: var(--n-font-display);
  font-size: 22px;
  font-weight: 600;
  color: var(--n-text);
  letter-spacing: -0.02em;
  font-feature-settings: 'tnum' 1;
  line-height: 1;
}
.dash__roster-actions { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }

@media (max-width: 1100px) {
  .dash__roster-row {
    grid-template-columns: 1fr;
    gap: 12px;
    padding: 16px 20px;
  }
  .dash__roster-actions { justify-content: flex-start; }
  .dash__roster-load { justify-self: start; }
}

/* ─── Follow-up tile ───────────────────────────────── */
.dash__followup-card {
  background: var(--n-bg-elev);
  display: grid;
  gap: 18px;
}
.dash__followup-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
}
.dash__followup-eyebrow {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: var(--n-font-mono);
  font-size: 10.5px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--n-brand);
  font-weight: 600;
}
.dash__followup-title {
  font-family: var(--n-font-display);
  font-size: clamp(22px, 2.2vw, 28px);
  font-weight: 600;
  letter-spacing: -0.025em;
  color: var(--n-text);
  margin: 6px 0 4px;
  line-height: 1.15;
}
.dash__followup-sub {
  font-size: 13px;
  color: var(--n-text-3);
  margin: 0;
  max-width: 60ch;
  line-height: 1.5;
}
.dash__followup-stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 0;
  border-top: 2px solid var(--n-border);
  padding-top: 16px;
}
.dash__followup-stats > div {
  display: grid;
  gap: 2px;
  padding: 0 16px;
  border-right: 2px solid var(--n-border);
}
.dash__followup-stats > div:last-child { border-right: 0; }
.dash__followup-stats > div:first-child { padding-left: 0; }
.dash__followup-cap {
  font-family: var(--n-font-mono);
  font-size: 9.5px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--n-text-4);
}
.dash__followup-stats strong {
  font-family: var(--n-font-display);
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--n-text);
  font-feature-settings: 'tnum' 1;
}
@media (max-width: 720px) {
  .dash__followup-stats {
    grid-template-columns: repeat(2, 1fr);
    row-gap: 12px;
  }
  .dash__followup-stats > div:nth-child(2) { border-right: 0; }
}
</style>
