<script setup>
import { onMounted, ref, watch } from 'vue';
import SecureHeader from './components/SecureHeader.vue';
import ConsoleLoginCard from './components/ConsoleLoginCard.vue';
import OrganizationPortal from './components/OrganizationPortal.vue';
import NokvoOneApp from './components/NokvoOneApp.vue';

const theme = ref(localStorage.getItem('nokvo_dashboard_theme') || 'light');
const homeSignal = ref(0);
const defaultMode = () => {
  const path = window.location.pathname;
  if (path.startsWith('/organization')) return 'organization';
  if (path.startsWith('/console')) return 'console';
  return 'nokvo_one';
};
const viewMode = ref(defaultMode());

const applyTheme = (value) => {
  document.documentElement.dataset.theme = value;
  document.body.dataset.theme = value;
};

const toggleTheme = () => {
  theme.value = theme.value === 'dark' ? 'light' : 'dark';
};

const handleHome = () => {
  homeSignal.value += 1;
};

const switchMode = (mode) => {
  viewMode.value = mode;
};

watch(theme, (value) => {
  localStorage.setItem('nokvo_dashboard_theme', value);
  applyTheme(value);
}, { immediate: true });

watch(viewMode, (value) => {
  localStorage.setItem('nokvo_access_mode', value);
});

onMounted(() => {
  applyTheme(theme.value);
});
</script>

<template>
  <NokvoOneApp v-if="viewMode === 'nokvo_one'" @switch-mode="switchMode('organization')" />
  <OrganizationPortal v-else-if="viewMode === 'organization'" @switch-mode="switchMode('superadmin')" />
  <div v-else class="app-container" :class="`theme-${theme}`">
    <SecureHeader :theme="theme" :toggle-theme="toggleTheme" @home="handleHome" />
    <div class="mode-switch-wrap">
      <button type="button" class="mode-switch" @click="switchMode('nokvo_one')">Nokvo One</button>
      <button type="button" class="mode-switch" @click="switchMode('organization')">
        Organization Login
      </button>
    </div>
    <main class="main-content">
      <ConsoleLoginCard :theme="theme" :toggle-theme="toggleTheme" :home-signal="homeSignal" />
    </main>
  </div>
</template>

<style scoped>
.app-container {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
  width: 100%;
}

.main-content {
  flex: 1;
  display: flex;
  justify-content: center;
  align-items: center;
  padding: 2rem;
}

.mode-switch-wrap {
  display: flex;
  gap: 0.5rem;
  justify-content: flex-end;
  width: 100%;
  padding: 0 2rem;
}

.mode-switch {
  border: 1px solid var(--border-color);
  background: rgba(255, 255, 255, 0.04);
  color: var(--text-secondary);
  padding: 0.7rem 0.95rem;
  font-size: 0.78rem;
  letter-spacing: 0.06em;
  border-radius: 999px;
}
</style>
