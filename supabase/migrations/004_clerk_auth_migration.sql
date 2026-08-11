-- ============================================
-- MIGRATION 004: Switch from Supabase Auth to Clerk
-- ============================================

-- ============================================
-- 1. DROP THE AUTH TRIGGER
-- ============================================
DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
DROP FUNCTION IF EXISTS public.handle_new_user();

-- ============================================
-- 2. DROP ALL FOREIGN KEY CONSTRAINTS FIRST
-- ============================================
-- credit_transactions references profiles(id) and jobs(id)
ALTER TABLE public.credit_transactions DROP CONSTRAINT IF EXISTS credit_transactions_user_id_fkey;
ALTER TABLE public.credit_transactions DROP CONSTRAINT IF EXISTS credit_transactions_job_id_fkey;

-- jobs references profiles(id)
ALTER TABLE public.jobs DROP CONSTRAINT IF EXISTS jobs_user_id_fkey;

-- pages references jobs(id)
ALTER TABLE public.pages DROP CONSTRAINT IF EXISTS pages_job_id_fkey;

-- profiles references auth.users(id)
ALTER TABLE public.profiles DROP CONSTRAINT IF EXISTS profiles_id_fkey;

-- ============================================
-- 3. DROP RLS (Clerk handles auth at the app layer)
-- ============================================
ALTER TABLE public.profiles DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.jobs DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.pages DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_transactions DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can read own profile" ON public.profiles;
DROP POLICY IF EXISTS "Users can update own profile" ON public.profiles;
DROP POLICY IF EXISTS "Users can read own transactions" ON public.credit_transactions;
DROP POLICY IF EXISTS "Users can read own jobs" ON public.jobs;
DROP POLICY IF EXISTS "Users can insert own jobs" ON public.jobs;
DROP POLICY IF EXISTS "Users can update own jobs" ON public.jobs;
DROP POLICY IF EXISTS "Users can read pages for own jobs" ON public.pages;

-- ============================================
-- 4. CHANGE COLUMN TYPES: UUID → TEXT (Clerk user IDs)
-- ============================================
ALTER TABLE public.profiles ALTER COLUMN id DROP DEFAULT;
ALTER TABLE public.profiles ALTER COLUMN id TYPE TEXT USING id::text;

ALTER TABLE public.credit_transactions ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE public.credit_transactions ALTER COLUMN user_id TYPE TEXT USING user_id::text;

ALTER TABLE public.jobs ALTER COLUMN user_id DROP DEFAULT;
ALTER TABLE public.jobs ALTER COLUMN user_id TYPE TEXT USING user_id::text;

-- ============================================
-- 5. ENSURE_PROFILE — called by the app when a Clerk user first signs in
-- ============================================
CREATE OR REPLACE FUNCTION public.ensure_profile(
  p_user_id TEXT,
  p_email TEXT
)
RETURNS TABLE (
  id TEXT,
  email TEXT,
  credits INTEGER,
  pending_credits INTEGER,
  created_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_was_new BOOLEAN := FALSE;
  v_result RECORD;
BEGIN
  -- Atomic upsert: ON CONFLICT handles the race where multiple
  -- parallel requests try to create the same profile simultaneously.
  -- Only grant signup bonus on FIRST insert (xmax = 0 means row was inserted, not updated).
  INSERT INTO public.profiles (id, email, credits)
  VALUES (p_user_id, p_email, 10)
  ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email
  RETURNING xmax INTO v_result;

  -- xmax = 0 means the row was actually INSERTED (not conflict-updated)
  IF v_result.xmax = 0 THEN
    INSERT INTO public.credit_transactions (user_id, amount, reason)
    VALUES (p_user_id, 10, 'signup_bonus');
  END IF;

  RETURN QUERY
    SELECT id, email, credits, pending_credits, created_at
    FROM public.profiles WHERE id = p_user_id;
END;
$$;

-- ============================================
-- 6. DISABLE STORAGE RLS
-- ============================================
DROP POLICY IF EXISTS "Users can upload to own folder" ON storage.objects;
DROP POLICY IF EXISTS "Users can read own files" ON storage.objects;
DROP POLICY IF EXISTS "Users can delete own files" ON storage.objects;
