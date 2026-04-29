<script setup>
import { onMounted, ref, watch } from 'vue';
import SecureHeader from './components/SecureHeader.vue';
import ConsoleLoginCard from './components/ConsoleLoginCard.vue';

const theme = ref(localStorage.getItem('nokvo_dashboard_theme') || 'light');
const homeSignal = ref(0);

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

watch(theme, (value) => {
  localStorage.setItem('nokvo_dashboard_theme', value);
  applyTheme(value);
}, { immediate: true });

onMounted(() => {
  applyTheme(theme.value);
});
</script>

<template>
  <div class="app-container" :class="`theme-${theme}`">
    <SecureHeader :theme="theme" :toggle-theme="toggleTheme" @home="handleHome" />
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
</style>
