import { createRouter, createWebHistory } from 'vue-router';
import NokvoOneApp from '../components/NokvoOneApp.vue';
import ConsoleLoginCard from '../components/ConsoleLoginCard.vue';
import SuperAdminDashboard from '../components/SuperAdminDashboard.vue';

// NOKVO APEX — dark product surface for deterministic outbound. Shares Nokvo
// One's accounts + token + bulk-calling backend (frontend-only product split).
const ApexApp = () => import('../apex/ApexApp.vue');

// NOKVO Affiliate Program — standalone lightweight app (own accounts: affiliate
// number + TOTP, no password) for referral signup + commission dashboard.
const AffiliateApp = () => import('../affiliate/AffiliateApp.vue');

// Dashboard views. Each view owns one page section that used to live inside
// NokvoOneApp.vue under v-if="currentPage === '<key>'". During the migration
// the v-if blocks remain in NokvoOneApp.vue; a route→currentPage watcher
// keeps them in sync so deep-linking works immediately. As each section is
// pulled out into its view file, its v-if block is deleted from the shell.
const DashboardView = () => import('../views/DashboardView.vue');
const TicketsView = () => import('../views/TicketsView.vue');
const TicketBoardView = () => import('../views/TicketBoardView.vue');
const LeadsView = () => import('../views/LeadsView.vue');
const CustomerBaseView = () => import('../views/CustomerBaseView.vue');
const FollowUpPipeline = () => import('../views/FollowUpPipeline.vue');
const AppointmentsView = () => import('../views/AppointmentsView.vue');
const ProjectsView = () => import('../views/ProjectsView.vue');
const ServicesView = () => import('../views/ServicesView.vue');
const AgentView = () => import('../views/AgentView.vue');
const OutgoingAgentView = () => import('../views/OutgoingAgentView.vue');
const MyTimetableView = () => import('../views/MyTimetableView.vue');
const OrganizationHealthView = () => import('../views/OrganizationHealthView.vue');
const NokvoConnectView = () => import('../views/NokvoConnectView.vue');
const AdvancedSettingsView = () => import('../views/AdvancedSettingsView.vue');
const TranscriptsView = () => import('../views/TranscriptsView.vue');
const BulkLeadCapturingView = () => import('../views/BulkLeadCapturingView.vue');

const dashboardChildren = [
  { path: '', redirect: { name: 'dash-home' } },
  { path: 'home', component: DashboardView, name: 'dash-home', meta: { pageKey: 'dashboard' } },
  { path: 'tickets', component: TicketsView, name: 'dash-tickets', meta: { pageKey: 'tickets' } },
  { path: 'ticket-board', component: TicketBoardView, name: 'dash-ticket-board', meta: { pageKey: 'ticket_board' } },
  { path: 'leads', component: LeadsView, name: 'dash-leads', meta: { pageKey: 'leads' } },
  { path: 'customers', component: CustomerBaseView, name: 'dash-customers', meta: { pageKey: 'customers' } },
  { path: 'followups', component: FollowUpPipeline, name: 'dash-followups', meta: { pageKey: 'followups' } },
  { path: 'transcripts', component: TranscriptsView, name: 'dash-transcripts', meta: { pageKey: 'transcripts' } },
  { path: 'appointments', component: AppointmentsView, name: 'dash-appointments', meta: { pageKey: 'appointments' } },
  { path: 'projects', component: ProjectsView, name: 'dash-projects', meta: { pageKey: 'projects' } },
  { path: 'services', component: ServicesView, name: 'dash-services', meta: { pageKey: 'services' } },
  { path: 'agent', component: AgentView, name: 'dash-agent', meta: { pageKey: 'agent' } },
  { path: 'campaigns', component: OutgoingAgentView, name: 'dash-campaigns', meta: { pageKey: 'outgoing_agent' } },
  { path: 'campaigns/:campaignId', component: OutgoingAgentView, name: 'dash-campaign-detail', meta: { pageKey: 'outgoing_agent' } },
  { path: 'bulk-leads', component: BulkLeadCapturingView, name: 'dash-bulk-leads', meta: { pageKey: 'bulk_leads' } },
  { path: 'my-timetable', component: MyTimetableView, name: 'dash-my-timetable', meta: { pageKey: 'my_timetable' } },
  { path: 'organization-health', component: OrganizationHealthView, name: 'dash-org-health', meta: { pageKey: 'organization_health' } },
  { path: 'connect', component: NokvoConnectView, name: 'dash-connect', meta: { pageKey: 'nokvo_connect' } },
  { path: 'connect/keys', component: NokvoConnectView, name: 'dash-connect-keys', meta: { pageKey: 'nokvo_connect_step2' } },
  { path: 'advanced-settings', component: AdvancedSettingsView, name: 'dash-advanced', meta: { pageKey: 'advanced_settings' } },
];

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
    children: dashboardChildren,
  },
  // Post-payment onboarding wizard. Bootstraps like a deep-link: restoreSession
  // resumes the wizard when the org is mid-onboarding.
  {
    path: '/nokvo-one/onboarding',
    component: NokvoOneApp,
    props: { initialAuthState: 'login' },
    name: 'nokvo-one-onboarding',
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

  // ── NOKVO APEX (deterministic outbound, dark product) ──────────
  {
    path: '/nokvo-apex',
    component: ApexApp,
    props: { initialAuthState: 'login' },
    name: 'nokvo-apex',
  },
  {
    path: '/nokvo-apex/signin',
    component: ApexApp,
    props: { initialAuthState: 'login' },
    name: 'nokvo-apex-signin',
  },
  {
    path: '/nokvo-apex/signup',
    component: ApexApp,
    props: { initialAuthState: 'signup' },
    name: 'nokvo-apex-signup',
  },
  {
    path: '/nokvo-apex/dashboard',
    component: ApexApp,
    props: { initialAuthState: 'ready' },
    name: 'nokvo-apex-dashboard',
  },
  {
    // Member invite acceptance — ApexApp reads the :token and opens the invite screen.
    path: '/nokvo-apex/invite/:token',
    component: ApexApp,
    props: { initialAuthState: 'invite' },
    name: 'nokvo-apex-invite',
  },

  // ── NOKVO Affiliate Program ─────────────────────────────────────
  {
    path: '/affiliate',
    component: AffiliateApp,
    props: { initialView: 'login' },
    name: 'affiliate',
  },
  {
    path: '/affiliate/signup',
    component: AffiliateApp,
    props: { initialView: 'signup' },
    name: 'affiliate-signup',
  },
  {
    path: '/affiliate/dashboard',
    component: AffiliateApp,
    props: { initialView: 'dashboard' },
    name: 'affiliate-dashboard',
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

// pageKeyToRouteName moved to ./pageKeys.js to break a circular import with
// NokvoOneApp.vue (which uses it in switchPage). Re-exported here so any
// existing consumers continue to work.
export { pageKeyToRouteName } from './pageKeys.js';

export default router;
