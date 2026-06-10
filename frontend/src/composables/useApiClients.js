// useApiClients — the two axios instances used across the dashboard.
//
// Extracted from NokvoOneApp.vue. URLs are derived from VITE_API_BASE_URL
// via ../config.js so deploying against staging/prod is a one-env-var
// change instead of chasing inline literals across the codebase.

import axios from 'axios';

import { NOKVO_ONE_API_BASE, NOKVO_CONNECT_API_BASE } from '../config.js';

export const API_BASE_URL = NOKVO_ONE_API_BASE;
export const CONNECT_API_BASE_URL = NOKVO_CONNECT_API_BASE;

export const api = axios.create({ baseURL: API_BASE_URL });
export const connectApi = axios.create({ baseURL: CONNECT_API_BASE_URL });
