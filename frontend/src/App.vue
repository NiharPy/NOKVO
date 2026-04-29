<script setup>
import SecureHeader from './components/SecureHeader.vue';
import ConsoleLoginCard from './components/ConsoleLoginCard.vue';
</script>

<template>
  <div class="app-container">
    <SecureHeader />
    <main class="main-content">
      <ConsoleLoginCard />
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
