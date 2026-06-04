<script setup>
import { CalendarDays, CalendarOff, ChevronLeft, ChevronRight, Clock, PhoneCall, Plus, Trash2 } from 'lucide-vue-next';
import { useDashboardState } from '../composables/useDashboardState.js';

const {
  isLoadingMyTimetable,
  myTimetable,
  myCalendarMonthLabel,
  shiftMyCalendarMonth,
  myCalendarDays,
  selectMyCalendarDate,
  myCalendarSelectedLabel,
  myCalendarSelectedIsWorking,
  myCalendarSelectedItems,
  formatCalendarTime,
  isMutatingMyBlock,
  removeMyBlock,
  myAssignedQueuedItems,
  bufferDurationOptions,
  bufferForm,
  addBuffer,
  unavailableForm,
  addUnavailability,
  formatSlotRange,
} = useDashboardState();
</script>

<template>
  <div class="n-page mtt">
  <section class="dashboard-section member-timetable-section n-rise">
    <div class="dashboard-section-head">
      <div>
        <span class="section-kicker">Your schedule</span>
        <h3>My timetable</h3>
      </div>
      <p>View your working window, add a buffer, or mark yourself unavailable. Changes apply immediately.</p>
    </div>

    <div v-if="isLoadingMyTimetable && !myTimetable" class="empty-state">Loading your timetable…</div>

    <div v-else class="my-calendar-shell">
      <section class="timetable-calendar-card my-calendar-card">
        <div class="timetable-calendar-head">
          <button type="button" class="ghost-button compact icon-only" aria-label="Previous month" @click="shiftMyCalendarMonth(-1)">
            <ChevronLeft :size="16" />
          </button>
          <strong>{{ myCalendarMonthLabel }}</strong>
          <button type="button" class="ghost-button compact icon-only" aria-label="Next month" @click="shiftMyCalendarMonth(1)">
            <ChevronRight :size="16" />
          </button>
        </div>
        <div class="timetable-weekdays">
          <span v-for="day in ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']" :key="day">{{ day }}</span>
        </div>
        <div class="timetable-month-grid">
          <button
            v-for="day in myCalendarDays"
            :key="day.key"
            type="button"
            class="timetable-date-cell"
            :class="{
              muted: !day.inMonth,
              today: day.isToday,
              active: day.isSelected,
              busy: day.itemCount,
              'has-visit': day.visitCount,
              'working-day': day.isWorkingDay && day.inMonth,
            }"
            @click="selectMyCalendarDate(day.key)"
          >
            <span>{{ day.dayNumber }}</span>
            <small v-if="day.visitCount && day.blockCount">{{ day.visitCount }} visit{{ day.visitCount === 1 ? '' : 's' }} · {{ day.blockCount }} block</small>
            <small v-else-if="day.visitCount">{{ day.visitCount }} visit{{ day.visitCount === 1 ? '' : 's' }}</small>
            <small v-else-if="day.blockCount">{{ day.blockCount }} block{{ day.blockCount === 1 ? '' : 's' }}</small>
            <small v-else-if="day.isWorkingDay && day.inMonth" class="my-calendar-working-hint">on duty</small>
          </button>
        </div>
        <div class="my-calendar-legend">
          <span><i class="legend-dot working"></i> Working day</span>
          <span><i class="legend-dot visit"></i> Customer visit</span>
          <span><i class="legend-dot blocked"></i> Blocked time</span>
          <span><i class="legend-dot today"></i> Today</span>
        </div>
      </section>

      <section class="timetable-selected-day my-calendar-selected">
        <div class="timetable-queue-head">
          <div>
            <span class="section-kicker">Selected date</span>
            <h4>{{ myCalendarSelectedLabel }}</h4>
          </div>
          <span class="status-chip" :class="{ active: myCalendarSelectedIsWorking }">
            {{ myCalendarSelectedIsWorking ? 'Working day' : 'Off-day' }}
          </span>
        </div>

        <dl v-if="myCalendarSelectedIsWorking && myTimetable?.assignment?.start_time && myTimetable?.assignment?.end_time" class="dashboard-detail-list my-calendar-hours">
          <div>
            <dt>Working hours</dt>
            <dd>
              {{ myTimetable.assignment.start_time }} – {{ myTimetable.assignment.end_time }}
              <small>({{ myTimetable.assignment.timezone || 'Asia/Kolkata' }})</small>
            </dd>
          </div>
          <div v-if="myTimetable?.assignment?.appointment_duration_minutes">
            <dt>Slot duration</dt>
            <dd>{{ myTimetable.assignment.appointment_duration_minutes }} min</dd>
          </div>
        </dl>

        <div v-if="!myCalendarSelectedItems.length" class="kb-empty compact my-calendar-empty">
          <div class="kb-empty-icon">
            <CalendarDays :size="22" />
          </div>
          <strong>Nothing scheduled on this date.</strong>
          <span>Customer visits and your own buffers will appear here.</span>
        </div>

        <div v-else class="timetable-day-list">
          <article
            v-for="item in myCalendarSelectedItems"
            :key="`${item.recordType}-${item.id}`"
            class="timetable-event"
            :class="{ blocked: item.isBlocked }"
          >
            <div class="timetable-time">
              <strong>{{ formatCalendarTime(item.start) }}</strong>
              <span>{{ item.end ? formatCalendarTime(item.end) : '' }}</span>
            </div>
            <div class="timetable-event-main">
              <div>
                <strong>{{ item.title }}</strong>
                <span>{{ item.detail || item.typeLabel }}</span>
                <a v-if="!item.isBlocked && item.phoneHref" class="record-call-link timetable-call-link" :href="item.phoneHref">
                  <PhoneCall :size="14" />
                  Call {{ item.phone }}
                </a>
              </div>
              <button
                v-if="item.isBlocked"
                type="button"
                class="dashboard-inline-button"
                :disabled="isMutatingMyBlock"
                @click="removeMyBlock(item.id)"
              >
                <Trash2 :size="14" />
                Remove
              </button>
              <small v-else>{{ item.status }}</small>
            </div>
          </article>
        </div>
      </section>
    </div>

    <div v-if="myAssignedQueuedItems.length" class="my-calendar-queue">
      <div class="dashboard-section-head">
        <div>
          <span class="section-kicker">Assigned queue</span>
          <h4>Awaiting a scheduled time</h4>
        </div>
        <p>Customer work assigned to you that hasn't been booked into a slot yet.</p>
      </div>
      <ul class="timetable-day-list my-calendar-queue-list">
        <li v-for="item in myAssignedQueuedItems" :key="`${item.recordType}-${item.id}`" class="timetable-event">
          <div class="timetable-event-main">
            <div>
              <strong>{{ item.title }}</strong>
              <span>{{ item.detail || item.typeLabel }}</span>
              <a v-if="item.phoneHref" class="record-call-link timetable-call-link" :href="item.phoneHref">
                <PhoneCall :size="14" />
                Call {{ item.phone }}
              </a>
            </div>
            <small>{{ item.status }}</small>
          </div>
        </li>
      </ul>
    </div>

    <div v-if="myTimetable" class="member-timetable-grid">
      <article class="dashboard-card member-timetable-card">
        <div class="dashboard-card-glow"></div>
        <div class="member-timetable-card-head">
          <h4>Working window</h4>
          <span class="readonly-tag">Set by admin</span>
        </div>
        <dl class="dashboard-detail-list">
          <div>
            <dt>Days</dt>
            <dd>{{ (myTimetable?.assignment?.working_days || []).join(', ') || 'No days configured yet' }}</dd>
          </div>
          <div>
            <dt>Hours</dt>
            <dd>
              <template v-if="myTimetable?.assignment?.start_time && myTimetable?.assignment?.end_time">
                {{ myTimetable.assignment.start_time }} – {{ myTimetable.assignment.end_time }}
                ({{ myTimetable.assignment.timezone || 'Asia/Kolkata' }})
              </template>
              <template v-else>Not set</template>
            </dd>
          </div>
          <div>
            <dt>Status</dt>
            <dd>
              <span v-if="myTimetable?.assignment?.is_assignable">Accepting work</span>
              <span v-else>Not assignable yet</span>
            </dd>
          </div>
          <div v-if="(myTimetable?.assignment?.request_types || []).length">
            <dt>Handles</dt>
            <dd>{{ (myTimetable.assignment.request_types || []).join(', ') }}</dd>
          </div>
        </dl>
      </article>

      <article class="dashboard-card member-timetable-card member-action-card buffer-card">
        <div class="dashboard-card-glow"></div>
        <header class="member-action-head">
          <div class="member-action-icon">
            <Clock :size="20" />
          </div>
          <div class="member-action-title">
            <h4>Add buffer</h4>
            <p>Short break between requests — lunch, a quick call, or back-to-back recovery.</p>
          </div>
        </header>
        <form class="member-action-form" @submit.prevent="addBuffer">
          <label class="member-field member-field-wide">
            <span class="member-field-label">Start time</span>
            <input
              v-model="bufferForm.start_time"
              type="datetime-local"
              class="member-field-input"
              required
            />
          </label>
          <div class="member-field member-field-wide">
            <span class="member-field-label">Duration</span>
            <div class="member-duration-pills" role="radiogroup" aria-label="Buffer duration">
              <button
                v-for="opt in bufferDurationOptions"
                :key="opt.minutes"
                type="button"
                role="radio"
                :aria-checked="bufferForm.duration_minutes === opt.minutes"
                class="member-duration-pill"
                :class="{ active: bufferForm.duration_minutes === opt.minutes }"
                @click="bufferForm.duration_minutes = opt.minutes"
              >
                {{ opt.label }}
              </button>
            </div>
          </div>
          <label class="member-field member-field-wide">
            <span class="member-field-label">Note <small>(optional)</small></span>
            <input
              v-model="bufferForm.reason"
              type="text"
              maxlength="240"
              class="member-field-input"
              placeholder="e.g. lunch break"
            />
          </label>
          <button
            type="submit"
            class="member-action-submit"
            :disabled="isMutatingMyBlock || !bufferForm.start_time"
          >
            <Plus :size="16" />
            {{ isMutatingMyBlock ? 'Adding…' : 'Add buffer' }}
          </button>
        </form>
      </article>

      <article class="dashboard-card member-timetable-card member-action-card unavailable-card">
        <div class="dashboard-card-glow"></div>
        <header class="member-action-head">
          <div class="member-action-icon unavailable-icon">
            <CalendarOff :size="20" />
          </div>
          <div class="member-action-title">
            <h4>Mark unavailable</h4>
            <p>Longer time off — leave, an offsite, or anything that takes you out of rotation.</p>
          </div>
        </header>
        <form class="member-action-form" @submit.prevent="addUnavailability">
          <label class="member-field">
            <span class="member-field-label">From</span>
            <input
              v-model="unavailableForm.start_time"
              type="datetime-local"
              class="member-field-input"
              required
            />
          </label>
          <label class="member-field">
            <span class="member-field-label">Until</span>
            <input
              v-model="unavailableForm.end_time"
              type="datetime-local"
              class="member-field-input"
              required
            />
          </label>
          <label class="member-field member-field-wide">
            <span class="member-field-label">Reason <small>(optional)</small></span>
            <input
              v-model="unavailableForm.reason"
              type="text"
              maxlength="240"
              class="member-field-input"
              placeholder="e.g. annual leave, training, doctor's appointment"
            />
          </label>
          <button
            type="submit"
            class="member-action-submit unavailable-submit"
            :disabled="isMutatingMyBlock || !unavailableForm.start_time || !unavailableForm.end_time"
          >
            <CalendarOff :size="16" />
            {{ isMutatingMyBlock ? 'Saving…' : 'Mark unavailable' }}
          </button>
        </form>
      </article>

      <article class="dashboard-card member-timetable-card member-timetable-card-wide">
        <div class="dashboard-card-glow"></div>
        <h4>Upcoming blocks</h4>
        <p v-if="!(myTimetable?.blocked_slots || []).length" class="empty-state">
          No buffers or unavailability scheduled.
        </p>
        <ul v-else class="member-timetable-blocks">
          <li v-for="slot in myTimetable.blocked_slots" :key="slot.id" class="member-timetable-block">
            <div class="member-timetable-block-meta">
              <strong>{{ slot.reason || 'Block' }}</strong>
              <span>{{ formatSlotRange(slot) }}</span>
            </div>
            <button
              type="button"
              class="dashboard-inline-button"
              :disabled="isMutatingMyBlock"
              @click="removeMyBlock(slot.id)"
            >
              <Trash2 :size="14" />
              Remove
            </button>
          </li>
        </ul>
      </article>
    </div>
  </section>
  </div>
</template>

<style scoped>
/* Minimum-viable styling for the legacy timetable markup until it gets a
   full rebuild. Pulls just enough from the design system to feel current. */
.mtt :deep(.dashboard-section) {
  display: grid;
  gap: var(--n-s-6);
}
.mtt :deep(.dashboard-section-head) {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--n-s-4);
  padding-bottom: var(--n-s-5);
  border-bottom: 1px solid var(--n-border);
  flex-wrap: wrap;
}
.mtt :deep(.section-kicker) {
  font-family: var(--n-font-mono);
  font-size: var(--n-t-micro);
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--n-text-3);
}
.mtt :deep(h3) {
  font-family: var(--n-font-display);
  font-size: var(--n-t-h1);
  font-weight: 600;
  letter-spacing: -0.025em;
  color: var(--n-text);
  margin: 4px 0 0;
  line-height: var(--n-lh-tight);
}
.mtt :deep(h4) {
  font-family: var(--n-font-display);
  font-size: 17px;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--n-text);
  margin: 0;
}
.mtt :deep(.dashboard-section-head p),
.mtt :deep(.dashboard-section-head > p) {
  font-size: var(--n-t-body);
  color: var(--n-text-3);
}

.mtt :deep(.dashboard-card),
.mtt :deep(.timetable-calendar-card),
.mtt :deep(.timetable-selected-day) {
  background: var(--n-bg-elev);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-xl);
  padding: var(--n-s-6);
}

.mtt :deep(.my-calendar-shell) {
  display: grid;
  grid-template-columns: 1.2fr 1fr;
  gap: var(--n-s-4);
}
@media (max-width: 920px) {
  .mtt :deep(.my-calendar-shell) { grid-template-columns: 1fr; }
}

.mtt :deep(.timetable-calendar-head) {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: var(--n-s-4);
}
.mtt :deep(.timetable-calendar-head strong) {
  font-family: var(--n-font-display);
  font-size: 15px;
  font-weight: 600;
  letter-spacing: -0.01em;
}
.mtt :deep(.ghost-button), .mtt :deep(.dashboard-inline-button) {
  appearance: none;
  background: transparent;
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-md);
  padding: 8px 12px;
  font-size: var(--n-t-small);
  font-weight: 500;
  color: var(--n-text-2);
  cursor: pointer;
  display: inline-flex; align-items: center; gap: 6px;
  transition: background var(--n-t-fast) var(--n-ease), border-color var(--n-t-fast) var(--n-ease);
}
.mtt :deep(.ghost-button:hover), .mtt :deep(.dashboard-inline-button:hover) {
  border-color: var(--n-border-strong);
  background: var(--n-surface);
  color: var(--n-text);
}
.mtt :deep(.ghost-button.compact) { padding: 6px 10px; font-size: var(--n-t-caption); }
.mtt :deep(.icon-only) { padding: 7px; }

.mtt :deep(.timetable-weekdays) {
  display: grid; grid-template-columns: repeat(7, 1fr);
  gap: 2px; margin-bottom: 6px;
}
.mtt :deep(.timetable-weekdays span) {
  text-align: center;
  font-family: var(--n-font-mono);
  font-size: 10.5px;
  letter-spacing: 0.1em;
  color: var(--n-text-4);
  text-transform: uppercase;
}
.mtt :deep(.timetable-month-grid) {
  display: grid; grid-template-columns: repeat(7, 1fr);
  gap: 4px;
}
.mtt :deep(.timetable-date-cell) {
  appearance: none;
  background: var(--n-surface);
  border: 1px solid transparent;
  border-radius: var(--n-r-md);
  padding: 8px 6px;
  min-height: 56px;
  display: grid;
  gap: 2px;
  cursor: pointer;
  font-family: var(--n-font-mono);
  color: var(--n-text);
  text-align: left;
  transition: all var(--n-t-fast) var(--n-ease);
}
.mtt :deep(.timetable-date-cell:hover) { background: var(--n-surface-2); border-color: var(--n-border); }
.mtt :deep(.timetable-date-cell.muted) { opacity: 0.4; }
.mtt :deep(.timetable-date-cell.today) { border-color: var(--n-brand); color: var(--n-brand-ink); }
.mtt :deep(.timetable-date-cell.active) { background: var(--n-text); color: var(--n-text-inverse); border-color: var(--n-text); }
.mtt :deep(.timetable-date-cell.working-day) { background: var(--n-brand-soft); }
.mtt :deep(.timetable-date-cell small) {
  font-family: var(--n-font-body);
  font-size: 9.5px;
  color: var(--n-text-3);
}

.mtt :deep(.my-calendar-legend) {
  display: flex; flex-wrap: wrap; gap: var(--n-s-4);
  font-size: 11.5px; color: var(--n-text-3);
  margin-top: var(--n-s-4);
}
.mtt :deep(.legend-dot) {
  width: 8px; height: 8px; border-radius: 50%;
  display: inline-block; margin-right: 4px;
  background: var(--n-text-4);
}
.mtt :deep(.legend-dot.working) { background: var(--n-brand); }
.mtt :deep(.legend-dot.visit) { background: var(--n-success); }
.mtt :deep(.legend-dot.blocked) { background: var(--n-warning); }
.mtt :deep(.legend-dot.today) { background: var(--n-danger); }

.mtt :deep(.timetable-event), .mtt :deep(.timetable-day-list > article) {
  display: grid;
  grid-template-columns: 80px 1fr;
  gap: var(--n-s-3);
  padding: var(--n-s-3) var(--n-s-4);
  border: 1px solid var(--n-border-subtle);
  border-radius: var(--n-r-md);
  background: var(--n-surface);
}
.mtt :deep(.timetable-event.blocked) {
  background: var(--n-warning-soft);
  border-color: rgba(217, 119, 6, 0.2);
}
.mtt :deep(.timetable-time strong) {
  font-family: var(--n-font-mono); font-size: 14px;
  font-weight: 600; color: var(--n-text);
}
.mtt :deep(.timetable-time span) {
  font-family: var(--n-font-mono); font-size: 11px;
  color: var(--n-text-3); display: block;
}

.mtt :deep(.member-timetable-grid) {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: var(--n-s-4);
}
.mtt :deep(.member-timetable-card-wide) { grid-column: 1 / -1; }

.mtt :deep(.member-action-form) { display: grid; gap: var(--n-s-3); margin-top: var(--n-s-3); }
.mtt :deep(.member-field) { display: grid; gap: 4px; }
.mtt :deep(.member-field-label) {
  font-size: var(--n-t-caption); font-weight: 500;
  color: var(--n-text-2);
}
.mtt :deep(.member-field-input) {
  appearance: none;
  width: 100%;
  font-family: var(--n-font-body);
  font-size: var(--n-t-body);
  color: var(--n-text);
  background: var(--n-bg);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-md);
  padding: 9px 11px;
  transition: border-color var(--n-t-fast) var(--n-ease), box-shadow var(--n-t-fast) var(--n-ease);
}
.mtt :deep(.member-field-input:focus) {
  outline: none;
  border-color: var(--n-brand);
  box-shadow: var(--n-shadow-focus);
}
.mtt :deep(.member-duration-pills) { display: flex; flex-wrap: wrap; gap: 6px; }
.mtt :deep(.member-duration-pill) {
  appearance: none;
  background: var(--n-surface);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-pill);
  padding: 6px 12px;
  font-size: var(--n-t-caption);
  color: var(--n-text-2);
  cursor: pointer;
  transition: all var(--n-t-fast) var(--n-ease);
}
.mtt :deep(.member-duration-pill.active) {
  background: var(--n-text); color: var(--n-text-inverse); border-color: var(--n-text);
}

.mtt :deep(.member-action-submit) {
  appearance: none;
  background: var(--n-text); color: var(--n-text-inverse);
  border: 0;
  border-radius: var(--n-r-md);
  padding: 10px 16px;
  font-family: var(--n-font-body);
  font-size: var(--n-t-small);
  font-weight: 500;
  cursor: pointer;
  display: inline-flex; align-items: center; gap: 6px;
  width: max-content;
  transition: transform var(--n-t-fast) var(--n-ease), background var(--n-t-fast) var(--n-ease);
}
.mtt :deep(.member-action-submit:hover:not(:disabled)) { transform: translateY(-0.5px); background: #1f1f23; }
.mtt :deep(.member-action-submit:disabled) { opacity: 0.5; cursor: not-allowed; }

.mtt :deep(.empty-state),
.mtt :deep(.kb-empty) {
  padding: var(--n-s-6);
  background: var(--n-surface);
  border: 1px dashed var(--n-border);
  border-radius: var(--n-r-lg);
  text-align: center;
  color: var(--n-text-3);
  font-size: var(--n-t-small);
}

.mtt :deep(.readonly-tag) {
  font-family: var(--n-font-mono);
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  padding: 3px 8px;
  background: var(--n-surface);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-sm);
  color: var(--n-text-3);
}

.mtt :deep(.dashboard-detail-list dt) {
  font-family: var(--n-font-mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--n-text-4);
  margin-bottom: 2px;
}
.mtt :deep(.dashboard-detail-list dd) {
  font-size: 13.5px; color: var(--n-text); margin: 0 0 var(--n-s-3);
}
.mtt :deep(.status-chip) {
  font-family: var(--n-font-mono);
  font-size: 11px;
  padding: 4px 10px;
  border-radius: var(--n-r-pill);
  background: var(--n-surface);
  border: 1px solid var(--n-border);
  color: var(--n-text-2);
}
.mtt :deep(.status-chip.active) {
  background: var(--n-success-soft);
  color: var(--n-success-2);
  border-color: transparent;
}
</style>

