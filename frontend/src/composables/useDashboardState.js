// useDashboardState — central provide/inject store for the dashboard shell.
//
// NokvoOneApp.vue is being split into a thin shell + per-page view files
// (src/views/*View.vue). The legacy <script setup> in NokvoOneApp.vue holds
// every reactive ref the dashboard needs. As pages migrate from the in-shell
// v-if blocks to their own view components, those refs need to be reachable
// across the boundary.
//
// Rather than threading a long list of props through <router-view>, the
// shell calls provideDashboardState(state) once with the live refs it owns,
// and each view calls useDashboardState() to inject only what it needs.
//
// This file deliberately leaves the *shape* open: views import the symbol
// and read off ref.value at call time. The shell decides which refs to put
// in the bag. We use a symbol-keyed inject so a stray default provide()
// somewhere else in the tree can't collide.

import { inject, provide } from 'vue';

const DASHBOARD_STATE_KEY = Symbol('nokvo.dashboardState');

export function provideDashboardState(state) {
  provide(DASHBOARD_STATE_KEY, state);
}

export function useDashboardState() {
  const state = inject(DASHBOARD_STATE_KEY, null);
  if (!state) {
    // A view rendered without a shell wrapping it — usually a routing config
    // bug. Surface it loudly so we don't get silent undefined chains.
    throw new Error('useDashboardState(): no provider found. Did the route render outside NokvoOneApp.vue?');
  }
  return state;
}
