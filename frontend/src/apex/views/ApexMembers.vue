<script setup>
// APEX Members (admin) — invite teammates by email; each creates their own sign-in
// for this workspace and claims qualified leads. The roster reads as people (a
// monogram tile per member) rather than a data table; an Invited member shows a
// dashed tile + hollow dot until they accept and become Active.
import { inject, onMounted } from 'vue';
import AxIcon from '../AxIcon.vue';

const apex = inject('apex');
onMounted(() => apex.loadMembers());

const initialOf = (m) => (m.full_name || m.email || '?').trim().charAt(0).toUpperCase();
const primaryOf = (m) => m.full_name || m.email;
</script>

<template>
  <div class="ax-card ax-card-pad ax-anim">
    <div class="ax-card-head">
      <div class="mb-head-left">
        <h2 class="ax-h2">Members</h2>
        <span v-if="apex.members.value.length" class="ax-count ax-count--grey">{{ apex.members.value.length }}</span>
      </div>
    </div>
    <p class="ax-muted">Invite teammates to claim and work your qualified leads. Each gets their own sign-in for this workspace.</p>

    <!-- Invite a teammate -->
    <div class="mb-invite">
      <div class="mb-eyebrow">Invite a teammate</div>
      <div class="mb-invite-row">
        <input
          v-model="apex.inviteForm.value.email" type="email" class="ax-text mb-invite-email"
          placeholder="teammate@email.com" @keyup.enter="apex.submitInvite()"
        />
        <input
          v-model="apex.inviteForm.value.full_name" type="text" class="ax-text mb-invite-name"
          placeholder="Name (optional)" @keyup.enter="apex.submitInvite()"
        />
        <button
          type="button" class="ax-btn2 ax-btn2--accent mb-invite-btn"
          :disabled="apex.inviteBusy.value" @click="apex.submitInvite()"
        >{{ apex.inviteBusy.value ? 'Sending…' : 'Send invite' }}</button>
      </div>
      <p v-if="apex.inviteNote.value" class="mb-note" :class="apex.inviteOk.value ? 'is-ok' : 'is-err'">
        {{ apex.inviteNote.value }}
      </p>
    </div>

    <!-- Roster -->
    <div v-if="!apex.members.value.length" class="ax-empty mb-empty">
      <div class="ax-empty-icon"><AxIcon name="users" :size="20" /></div>
      <p class="ax-empty-text">No members yet — invite your first teammate above.</p>
    </div>
    <ul v-else class="mb-roster">
      <li v-for="(m, i) in apex.members.value" :key="m.id" class="mb-row ax-row-in" :style="{ '--i': Math.min(i, 14) }">
        <span class="mb-mono" :class="m.status === 'active' ? 'is-active' : 'is-invited'">{{ initialOf(m) }}</span>
        <div class="mb-ident">
          <span class="mb-name">{{ primaryOf(m) }}</span>
          <span v-if="m.full_name" class="mb-email">{{ m.email }}</span>
        </div>
        <span class="mb-status" :class="m.status === 'active' ? 'is-active' : 'is-invited'">
          <span class="mb-dot"></span>{{ m.status === 'active' ? 'Active' : 'Invited' }}
        </span>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.mb-head-left { display: flex; align-items: center; gap: 13px; }

/* Invite panel — a quiet compose zone; the eyebrow labels it as its own region. */
.mb-eyebrow { font-size: 11px; letter-spacing: 0.22em; text-transform: uppercase; color: rgba(255,255,255,0.34); font-weight: 600; margin-bottom: 15px; }
.mb-invite { background: linear-gradient(180deg, rgba(255,255,255,0.028), rgba(255,255,255,0.008)); border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; padding: 22px 24px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.04), 0 12px 28px -18px rgba(0,0,0,0.5); }
.mb-invite-row { display: flex; gap: 10px; flex-wrap: wrap; align-items: stretch; }
.mb-invite-email { flex: 2 1 220px; }
.mb-invite-name { flex: 1 1 150px; }
.mb-invite-btn { flex: 0 0 auto; padding: 13px 24px; }
.mb-note { font-size: 12.5px; margin: 13px 0 0; line-height: 1.5; }
.mb-note.is-ok { color: #7FD9A8; }
.mb-note.is-err { color: #ff8b8b; }

.mb-empty { margin-top: 24px; }

/* Roster — people, not rows. Monogram + identity + state. */
.mb-roster { list-style: none; margin: 28px 0 0; padding: 0; }
.mb-row { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 16px; padding: 15px 10px; border-top: 1px solid rgba(255,255,255,0.055); border-radius: 10px; transition: background .16s; }
.mb-row:hover { background: rgba(255,255,255,0.025); }

.mb-mono { width: 40px; height: 40px; border-radius: 11px; display: flex; align-items: center; justify-content: center; font-family: 'Sora', sans-serif; font-weight: 700; font-size: 15px; }
.mb-mono.is-active { border: 1px solid rgba(255,255,255,0.16); background: linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.02)); color: #F3F2F0; box-shadow: inset 0 1px 0 rgba(255,255,255,0.09), 0 6px 14px -8px rgba(0,0,0,0.5); }
/* Dashed until they accept — the tile itself signals "not yet joined". */
.mb-mono.is-invited { border: 1px dashed rgba(255,255,255,0.2); color: rgba(255,255,255,0.4); }

.mb-ident { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.mb-name { font-size: 15px; font-weight: 600; color: #F3F2F0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mb-email { font-family: 'JetBrains Mono', monospace; font-size: 12.5px; color: rgba(255,255,255,0.42); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.mb-status { display: inline-flex; align-items: center; gap: 8px; justify-self: end; font-family: 'JetBrains Mono', monospace; font-size: 11.5px; letter-spacing: 0.05em; }
.mb-status.is-active { color: #7FD9A8; }
.mb-status.is-invited { color: #D6A15C; }
.mb-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; background: currentColor; }

@media (max-width: 560px) {
  .mb-invite-btn { flex: 1 1 100%; }
}
</style>
