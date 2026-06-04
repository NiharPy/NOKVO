<script setup>
// OrganizationHealthView — first fully-extracted page.
//
// Source: NokvoOneApp.vue v-if="currentPage === 'organization_health'"
// (was lines 6467-6520). The shell provides organizationInitial,
// organizationHealth, currentOrganization, currentUser via
// provideDashboardState — those are computed in NokvoOneApp.vue and
// remain there for now since other v-if blocks still use them.
//
// Once enough pages are extracted, those computeds will move into the
// composable itself and the shell will be a pure routing skeleton.

import { useDashboardState } from '../composables/useDashboardState.js';

const {
  organizationInitial,
  organizationHealth,
  currentOrganization,
  currentUser,
} = useDashboardState();
</script>

<template>
  <section class="dashboard-section">
    <div class="dashboard-section-head">
      <div>
        <span class="section-kicker">Settings</span>
        <h3>Organization Health</h3>
      </div>
      <p>Live readiness across security, agent setup, knowledge, runtime, outbound, and infrastructure.</p>
    </div>

    <div class="dashboard-grid overview-grid org-health-grid">
      <article class="dashboard-card organization-card">
        <div class="dashboard-card-glow"></div>
        <div class="organization-card-head">
          <div class="organization-ident">
            <div class="organization-mark">{{ organizationInitial }}</div>
            <div>
              <h3>{{ currentOrganization?.name }}</h3>
              <p>{{ currentUser?.role }} workspace</p>
            </div>
          </div>
          <span class="status-chip" :class="{ active: currentOrganization?.status === 'active' }">
            <span class="status-dot"></span>
            {{ currentOrganization?.status }}
          </span>
        </div>

        <div class="health-tracker">
          <div class="health-score-card" :class="`health-${organizationHealth.state}`">
            <div class="health-ring" :style="{ background: organizationHealth.ringStyle }">
              <div>
                <strong>{{ organizationHealth.score }}</strong>
                <span>/100</span>
              </div>
            </div>
            <div>
              <span class="micro-label">Workspace health</span>
              <h4>{{ organizationHealth.label }}</h4>
              <p>{{ organizationHealth.openIssues ? `${organizationHealth.openIssues} area${organizationHealth.openIssues === 1 ? '' : 's'} need attention.` : 'All critical areas are clear.' }}</p>
            </div>
          </div>

          <div class="health-check-grid">
            <div v-for="item in organizationHealth.checks" :key="item.key" class="health-check" :data-state="item.state">
              <span class="health-check-dot"></span>
              <div>
                <strong>{{ item.label }}</strong>
                <small>{{ item.detail }}</small>
              </div>
            </div>
          </div>
        </div>
      </article>
    </div>
  </section>
</template>
