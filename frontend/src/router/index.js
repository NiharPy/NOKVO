import { createRouter, createWebHistory } from 'vue-router';
import NokvoOneApp from '../components/NokvoOneApp.vue';
import OrganizationPortal from '../components/OrganizationPortal.vue';
import ConsoleLoginCard from '../components/ConsoleLoginCard.vue';
import SuperAdminDashboard from '../components/SuperAdminDashboard.vue';

const routes = [
  // ── Root redirect ──────────────────────────────────────────────
  { path: '/', redirect: '/nokvo-one' },

  // ── NOKVO One (voice-agent tenant app) ─────────────────────────
  {
    path: '/nokvo-one',
    component: NokvoOneApp,
    props: { initialAuthState: 'login' },
    name: 'nokvo-one',
  },
  {
    path: '/nokvo-one/signin',
    component: NokvoOneApp,
    props: { initialAuthState: 'login' },
    name: 'nokvo-one-signin',
  },
  {
    path: '/nokvo-one/signup',
    component: NokvoOneApp,
    props: { initialAuthState: 'signup' },
    name: 'nokvo-one-signup',
  },
  {
    path: '/nokvo-one/dashboard',
    component: NokvoOneApp,
    props: { initialAuthState: 'ready' },
    name: 'nokvo-one-dashboard',
  },
  // Deep-link routes handled internally by the component
  {
    path: '/nokvo-one/verify-email',
    component: NokvoOneApp,
    props: { initialAuthState: 'login' },
    name: 'nokvo-one-verify-email',
  },
  {
    path: '/nokvo-one/accept-invite',
    component: NokvoOneApp,
    props: { initialAuthState: 'login' },
    name: 'nokvo-one-accept-invite',
  },

  // ── Organization Portal ─────────────────────────────────────────
  {
    path: '/organization',
    component: OrganizationPortal,
    props: { initialAuthState: 'login' },
    name: 'organization',
  },
  {
    path: '/organization/signin',
    component: OrganizationPortal,
    props: { initialAuthState: 'login' },
    name: 'organization-signin',
  },
  {
    path: '/organization/dashboard',
    component: OrganizationPortal,
    props: { initialAuthState: 'ready' },
    name: 'organization-dashboard',
  },

  // ── Console / Super-Admin ───────────────────────────────────────
  {
    path: '/console',
    component: ConsoleLoginCard,
    name: 'console',
  },
  {
    path: '/console/signin',
    component: ConsoleLoginCard,
    name: 'console-signin',
  },
  {
    path: '/console/dashboard',
    component: SuperAdminDashboard,
    name: 'console-dashboard',
  },

  // ── Catch-all fallback ──────────────────────────────────────────
  { path: '/:pathMatch(.*)*', redirect: '/nokvo-one' },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

export default router;
