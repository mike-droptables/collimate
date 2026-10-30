import { ScopedAppState } from '@/lib/types';
import { requestJson } from './client';


export const fetchState = async (): Promise<ScopedAppState> => requestJson('/state', 'GET');
