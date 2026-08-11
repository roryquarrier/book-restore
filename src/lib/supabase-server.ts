import { createClient } from '@supabase/supabase-js';
import type { SupabaseClient } from '@supabase/supabase-js';

/**
 * Vite loads .env into import.meta.env, not process.env, so read both:
 * import.meta.env covers `astro dev`/`astro build`, process.env covers a
 * standalone node server (the Astro Node adapter on Fly.io) started with
 * real runtime environment variables. Fly.io secrets are injected at
 * runtime, not build time, so they only appear in process.env.
 */
// @types/node isn't installed, so reach process.env through globalThis.
const nodeEnv =
  (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {};

/** An empty value in .env counts as unset, so a real environment variable can win. */
const readEnv = (name: string): string | undefined =>
  (import.meta.env as Record<string, string | undefined>)[name] || nodeEnv[name];

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
 *
 * Lazy initialization: the client is created on first use, not at module
 * load. This allows the build (prerender) to run without env vars present.
 */
let _client: SupabaseClient | null = null;

export function getSupabaseServer(): SupabaseClient {
  if (_client) return _client;

  const supabaseUrl = readEnv('PUBLIC_SUPABASE_URL');
  const serviceRoleKey = readEnv('SUPABASE_SERVICE_ROLE_KEY');

  if (!supabaseUrl || !serviceRoleKey) {
    throw new Error(
      'Missing PUBLIC_SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY. On Fly.io, set them with `flyctl secrets set`; locally, copy .env.example to .env and fill them in.'
    );
  }

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
