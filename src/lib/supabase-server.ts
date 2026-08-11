import { createClient } from '@supabase/supabase-js';
import type { SupabaseClient } from '@supabase/supabase-js';

const supabaseUrl = import.meta.env.PUBLIC_SUPABASE_URL;
const serviceRoleKey = import.meta.env.SUPABASE_SERVICE_ROLE_KEY;

if (!supabaseUrl || !serviceRoleKey) {
  throw new Error(
    'Missing PUBLIC_SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY. Copy .env.example to .env and fill them in.'
  );
}

/**
 * Server-side Supabase client using the SERVICE ROLE key.
 *
 * With Clerk auth, Supabase is just a database + storage backend.
 * There are no session cookies — RLS is disabled, and the app layer
 * filters all queries by the Clerk user ID explicitly.
 *
 * The service role key bypasses RLS entirely, which is safe because:
 *   1. Every query in the app filters by user_id from Clerk's verified JWT
 *   2. No client-side Supabase client is used anymore
 */
let _client: SupabaseClient | null = null;

export function getSupabaseServer(): SupabaseClient {
  if (_client) return _client;

  _client = createClient(supabaseUrl, serviceRoleKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  return _client;
}

export interface Profile {
  id: string;
  email: string | null;
  credits: number;
  pending_credits: number;
  created_at: string;
}

/**
 * Ensures a profile exists for the Clerk user. Called on every request
 * that needs profile data. Idempotent — if profile exists, just returns it.
 */
export async function ensureProfile(
  userId: string,
  email: string | null
): Promise<Profile | null> {
  const supabase = getSupabaseServer();

  const { data, error } = await supabase
    .rpc('ensure_profile', { p_user_id: userId, p_email: email ?? '' });

  if (error) {
    console.error('[ensureProfile] failed', error);
    return null;
  }

  if (data && data.length > 0) {
    return data[0] as Profile;
  }

  return null;
}
