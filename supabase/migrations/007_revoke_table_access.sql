-- Migration 007: Revoke table access from anon/authenticated (RLS is OFF, so grants are the gate)
--
-- CRITICAL: Migration 004 disabled RLS on all tables. But disabling RLS doesn't
-- revoke table GRANTs — the anon and authenticated roles can still
-- SELECT/INSERT/UPDATE/DELETE directly via the PostgREST API if they have grants.
--
-- Since the app uses ONLY the service_role key (which bypasses RLS),
-- anon/authenticated should have ZERO access to these tables.
-- All data access flows through the backend → service_role → these tables.

-- ============================================
-- REVOKE ALL TABLE ACCESS FROM ANON/AUTHENTICATED
-- ============================================

REVOKE ALL ON public.profiles FROM anon;
REVOKE ALL ON public.profiles FROM authenticated;

REVOKE ALL ON public.jobs FROM anon;
REVOKE ALL ON public.jobs FROM authenticated;

REVOKE ALL ON public.pages FROM anon;
REVOKE ALL ON public.pages FROM authenticated;

REVOKE ALL ON public.credit_transactions FROM anon;
REVOKE ALL ON public.credit_transactions FROM authenticated;

-- ============================================
-- GRANT FULL ACCESS ONLY TO service_role
-- ============================================
-- service_role bypasses RLS and is only used by the app backend.

GRANT ALL ON public.profiles TO service_role;
GRANT ALL ON public.jobs TO service_role;
GRANT ALL ON public.pages TO service_role;
GRANT ALL ON public.credit_transactions TO service_role;

-- Note: credit_transactions uses gen_random_uuid(), not a sequence, so no sequence grants needed.
