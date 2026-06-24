// Page-key ↔ route-name map.
//
// Lives in its own file (rather than router/index.js) to break a circular
// import: router/index.js imports NokvoOneApp.vue (uses it as the dashboard
// route component), and NokvoOneApp.vue needs this map for its switchPage()
// nav helper. Importing the map from a leaf module avoids the cycle and
// keeps Vite HMR happy — otherwise reloading either file leaves the other
// with a Temporal Dead Zone binding ("Cannot access 'NokvoOneApp' before
// initialization") and nav clicks silently no-op until a full refresh.
export const pageKeyToRouteName = {
  dashboard: 'dash-home',
  tickets: 'dash-tickets',
  ticket_board: 'dash-ticket-board',
  leads: 'dash-leads',
  customers: 'dash-customers',
  followups: 'dash-followups',
  transcripts: 'dash-transcripts',
  appointments: 'dash-appointments',
  projects: 'dash-projects',
  services: 'dash-services',
  agent: 'dash-agent',
  outgoing_agent: 'dash-campaigns',
  bulk_leads: 'dash-bulk-leads',
  my_timetable: 'dash-my-timetable',
  organization_health: 'dash-org-health',
  nokvo_connect: 'dash-connect',
  nokvo_connect_step2: 'dash-connect-keys',
  advanced_settings: 'dash-advanced',
};
