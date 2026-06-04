<script setup>
import { computed } from 'vue';
import { ArrowUpRight, KeyRound, Plug, Shield, Webhook } from 'lucide-vue-next';
import { useRouter } from 'vue-router';
import { useDashboardState } from '../composables/useDashboardState.js';

const router = useRouter();
const { nokvoConnectEnabled } = useDashboardState();

const sections = computed(() => [
  {
    key: 'api-keys',
    label: 'API keys',
    detail: 'Mint and rotate Nokvo Connect API keys for backend integrations.',
    icon: KeyRound,
    enabled: !!nokvoConnectEnabled?.value,
    disabledReason: 'Enable Nokvo Connect in your environment to surface API keys.',
    onOpen: () => router.push({ name: 'dash-connect-keys' }),
  },
  {
    key: 'webhooks',
    label: 'Webhooks',
    detail: 'Receive call-lifecycle events at your own HTTPS endpoint.',
    icon: Webhook,
    enabled: !!nokvoConnectEnabled?.value,
    disabledReason: 'Webhook configuration ships with Nokvo Connect.',
    onOpen: () => router.push({ name: 'dash-connect' }),
  },
  {
    key: 'security',
    label: 'Security',
    detail: 'MFA enforcement, session policies, and audit log access.',
    icon: Shield,
    enabled: true,
    onOpen: () => router.push({ name: 'dash-org-health' }),
  },
  {
    key: 'integrations',
    label: 'Integrations',
    detail: 'Connect external tools — calendars, CRM, billing.',
    icon: Plug,
    enabled: false,
    disabledReason: 'Coming soon. Request access from your account manager.',
    onOpen: () => {},
  },
]);
</script>

<template>
  <div class="n-page">
    <header class="n-page-head n-rise">
      <span class="n-page-head__eyebrow">Settings</span>
      <div class="n-page-head__row">
        <div>
          <h1 class="n-page-head__title">Advanced</h1>
          <p class="n-page-head__sub">
            Developer-grade controls. Most teams never need to open this page —
            API keys, webhooks, security policies, and integrations live here behind one quiet entry.
          </p>
        </div>
      </div>
    </header>

    <section class="n-section n-rise" data-delay="1">
      <div class="adv__grid">
        <button
          v-for="(s, i) in sections"
          :key="s.key"
          type="button"
          class="adv__tile n-card n-card--hover"
          :class="{ 'is-disabled': !s.enabled }"
          :style="{ animationDelay: `${60 + i * 40}ms` }"
          :disabled="!s.enabled"
          :title="s.enabled ? '' : s.disabledReason"
          @click="s.enabled && s.onOpen()"
        >
          <div class="adv__tile-icon">
            <component :is="s.icon" :size="18" />
          </div>
          <div class="adv__tile-body">
            <strong>{{ s.label }}</strong>
            <span>{{ s.enabled ? s.detail : s.disabledReason }}</span>
          </div>
          <span v-if="s.enabled" class="adv__tile-arrow">
            <ArrowUpRight :size="14" />
          </span>
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.adv__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 14px;
}
.adv__tile {
  appearance: none;
  background: var(--n-bg-elev);
  border: 1px solid var(--n-border);
  border-radius: var(--n-r-xl);
  padding: 20px;
  display: grid;
  grid-template-columns: auto 1fr auto;
  align-items: center;
  gap: 16px;
  text-align: left;
  cursor: pointer;
  color: var(--n-text);
  font-family: var(--n-font-body);
  animation: n-rise 420ms var(--n-ease) both;
  transition:
    border-color var(--n-t-fast) var(--n-ease),
    box-shadow var(--n-t-fast) var(--n-ease),
    transform var(--n-t-fast) var(--n-ease);
}
.adv__tile:hover:not(.is-disabled) {
  border-color: var(--n-border-strong);
  box-shadow: var(--n-shadow-md);
  transform: translateY(-1px);
}
.adv__tile.is-disabled { opacity: 0.55; cursor: not-allowed; }

.adv__tile-icon {
  width: 42px;
  height: 42px;
  border-radius: 12px;
  background: var(--n-brand-soft);
  color: var(--n-brand);
  display: grid;
  place-items: center;
}
.adv__tile.is-disabled .adv__tile-icon { background: var(--n-surface-2); color: var(--n-text-3); }

.adv__tile-body { display: grid; gap: 4px; min-width: 0; }
.adv__tile-body strong {
  font-family: var(--n-font-display);
  font-size: 15px;
  font-weight: 600;
  color: var(--n-text);
  letter-spacing: -0.01em;
}
.adv__tile-body span {
  font-size: 12.5px;
  color: var(--n-text-3);
  line-height: 1.45;
}

.adv__tile-arrow {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  display: grid;
  place-items: center;
  color: var(--n-text-3);
  transition: color var(--n-t-fast) var(--n-ease), background var(--n-t-fast) var(--n-ease);
}
.adv__tile:hover:not(.is-disabled) .adv__tile-arrow {
  background: var(--n-surface);
  color: var(--n-text);
}
</style>
