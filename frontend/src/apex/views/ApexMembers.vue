<script setup>
// APEX Members (admin) — invite teammates by email; each creates their own sign-in
// for this workspace and claims qualified leads.
import { inject, onMounted } from 'vue';

const apex = inject('apex');
onMounted(() => apex.loadMembers());
</script>

<template>
  <div class="ax-card ax-card-pad ax-anim">
    <div class="ax-card-head">
      <h2 class="ax-h2">Members</h2>
    </div>
    <p class="ax-muted">Invite teammates to claim and work your qualified leads. They get their own sign-in for this workspace.</p>

    <div class="ax-invite">
      <input v-model="apex.inviteForm.value.email" type="email" class="ax-input ax-invite-input" placeholder="teammate@email.com" @keyup.enter="apex.submitInvite()" />
      <input v-model="apex.inviteForm.value.full_name" type="text" class="ax-input ax-invite-input" placeholder="Name (optional)" />
      <button type="button" class="ax-btn2 ax-btn2--accent ax-btn2--sm" :disabled="apex.inviteBusy.value" @click="apex.submitInvite()">
        {{ apex.inviteBusy.value ? 'Sending…' : 'Send invite' }}
      </button>
    </div>
    <p v-if="apex.inviteNote.value" class="ax-invite-note" :class="apex.inviteOk.value ? 'is-ok' : 'is-err'">{{ apex.inviteNote.value }}</p>

    <div v-if="!apex.members.value.length" class="ax-empty" style="margin-top:18px;">
      <div class="ax-empty-icon">◷</div>
      <p class="ax-empty-text">No members yet — invite your first teammate above.</p>
    </div>
    <template v-else>
      <div class="ax-thead" style="grid-template-columns:1.4fr 1.6fr auto;margin-top:16px;"><span>Name</span><span>Email</span><span>Status</span></div>
      <div v-for="m in apex.members.value" :key="m.id" class="ax-trow" style="grid-template-columns:1.4fr 1.6fr auto;align-items:center;">
        <span class="ax-cell-name">{{ m.full_name || '—' }}</span>
        <span class="ax-cell-phone">{{ m.email }}</span>
        <span class="ax-mstatus" :class="m.status === 'active' ? 'is-active' : 'is-pending'">{{ m.status === 'active' ? 'Active' : 'Invited' }}</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ax-invite { display: flex; gap: 8px; flex-wrap: wrap; margin: 14px 0 4px; }
.ax-invite-input { flex: 1 1 180px; }
.ax-invite-note { font-size: 12.5px; margin: 8px 0 0; }
.ax-invite-note.is-ok { color: #7FD9A8; }
.ax-invite-note.is-err { color: #ff8b8b; }
.ax-mstatus { font-size: 11px; font-family: 'JetBrains Mono', monospace; letter-spacing: 0.04em; }
.ax-mstatus.is-active { color: #7FD9A8; }
.ax-mstatus.is-pending { color: rgba(255,255,255,0.5); }
</style>
