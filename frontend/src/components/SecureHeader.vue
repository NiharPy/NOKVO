<script setup>
import { ShieldCheck } from 'lucide-vue-next';

defineProps({
  theme: {
    type: String,
    required: true,
  },
  toggleTheme: {
    type: Function,
    required: true,
  },
});

const emit = defineEmits(['home']);
</script>

<template>
  <header class="secure-header">
    <div class="header-left">
      <button class="logo" @click="emit('home')">
        <span class="logo-bold">NOKVO</span>
        <span class="logo-sub">ORBITAL-V1</span>
      </button>
    </div>
    <div class="header-right">
      <nav class="nav-links">
        <a href="#">STATUS</a>
        <a href="#">SUPPORT</a>
      </nav>
      <button class="theme-toggle" @click="toggleTheme">
        {{ theme === 'dark' ? 'LIGHT MODE' : 'DARK MODE' }}
      </button>
      <div class="status-icon">
        <ShieldCheck :size="24" color="var(--text-secondary)" strokeWidth="1.5" />
      </div>
    </div>
  </header>
</template>

<style scoped>
.secure-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 1.5rem 3rem;
  width: 100%;
}

.header-left .logo {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  background: transparent;
  border: none;
  color: inherit;
  padding: 0;
  cursor: pointer;
}

.logo-bold {
  font-size: 1.5rem;
  font-weight: 700;
  letter-spacing: 1px;
}

.logo-sub {
  font-size: 0.8rem;
  color: var(--text-muted);
  letter-spacing: 0.5px;
  font-weight: 500;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 2rem;
}

.nav-links {
  display: flex;
  gap: 1.5rem;
}

.theme-toggle {
  background: transparent;
  border: 1px solid var(--border-color);
  color: var(--text-secondary);
  padding: 0.55rem 0.85rem;
  font-size: 0.72rem;
  letter-spacing: 1.3px;
  transition: all 0.25s ease;
}

.theme-toggle:hover {
  color: var(--accent-color);
  border-color: var(--accent-color);
  box-shadow: 0 0 16px var(--accent-glow);
}

.nav-links a {
  font-size: 0.85rem;
  letter-spacing: 1px;
  font-weight: 500;
  text-transform: uppercase;
}

.status-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  opacity: 0.8;
  transition: opacity 0.3s;
}

.status-icon:hover {
  opacity: 1;
}
</style>
