// useApiClients — the two axios instances used across the dashboard.
//
// Extracted from NokvoOneApp.vue. Centralising the baseURLs makes it easier
// to point the frontend at staging/prod or a different port without
// chasing inline string literals.

import axios from 'axios';

export const API_BASE_URL = 'http://localhost:8000/api/nokvo-one';
export const CONNECT_API_BASE_URL = 'http://localhost:8000/api/nokvo-one/connect';

export const api = axios.create({ baseURL: API_BASE_URL });
export const connectApi = axios.create({ baseURL: CONNECT_API_BASE_URL });
