-- Book Bash — Database Schema Migration
-- Target: Supabase project ruldtseddrklbviloule
-- Designed for: upload → restore → download (black-box MVP)

-- ============================================
-- 1. PROFILES (auto-created on signup via trigger)
-- ============================================

CREATE TABLE IF NOT EXISTS public.profiles (
  id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  email TEXT,
  credits INTEGER NOT NULL DEFAULT 10,
  pending_credits INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own profile"
  ON public.profiles FOR SELECT
  USING (auth.uid() = id);

CREATE POLICY "Users can update own profile"
  ON public.profiles FOR UPDATE
  USING (auth.uid() = id);

-- ============================================
-- 2. CREDIT TRANSACTIONS (audit log)
-- ============================================

CREATE TABLE IF NOT EXISTS public.credit_transactions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  amount INTEGER NOT NULL,
  reason TEXT NOT NULL CHECK (
    reason IN ('signup_bonus', 'stripe_purchase', 'regen_hold', 'regen_commit', 'regen_refund')
  ),
  stripe_payment_intent TEXT,
  idempotency_key TEXT UNIQUE,
  job_id UUID,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.credit_transactions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own transactions"
  ON public.credit_transactions FOR SELECT
  USING (auth.uid() = user_id);

-- ============================================
-- 3. JOBS (book restoration queue)
-- ============================================

CREATE TABLE IF NOT EXISTS public.jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'uploaded' CHECK (
    status IN ('uploaded', 'extracting', 'regen_hold', 'regen_running',
               'regen_complete', 'complete', 'failed', 'refunded')
  ),
  source_filename TEXT NOT NULL,
  storage_path TEXT NOT NULL,
  output_storage_path TEXT,
  page_count INTEGER,
  credits_held INTEGER NOT NULL DEFAULT 0,
  credits_committed INTEGER NOT NULL DEFAULT 0,
  error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '7 days'
);

ALTER TABLE public.jobs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own jobs"
  ON public.jobs FOR SELECT
  USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own jobs"
  ON public.jobs FOR INSERT
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own jobs"
  ON public.jobs FOR UPDATE
  USING (auth.uid() = user_id);

-- ============================================
-- 4. PAGES (individual page metadata)
-- ============================================

CREATE TABLE IF NOT EXISTS public.pages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id UUID NOT NULL REFERENCES public.jobs(id) ON DELETE CASCADE,
  page_number INTEGER NOT NULL,
  page_type TEXT,
  thumbnail_path TEXT,
  original_path TEXT NOT NULL,
  regen_path TEXT,
  selected BOOLEAN NOT NULL DEFAULT false,
  status TEXT NOT NULL DEFAULT 'extracted' CHECK (
    status IN ('extracted', 'restored', 'failed')
  ),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.pages ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read pages for own jobs"
  ON public.pages FOR SELECT
  USING (
    EXISTS (
      SELECT 1 FROM public.jobs
      WHERE jobs.id = pages.job_id AND jobs.user_id = auth.uid()
    )
  );

-- ============================================
-- 5. AUTO-CREATE PROFILE ON SIGNUP
-- ============================================

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  INSERT INTO public.profiles (id, email, credits)
  VALUES (NEW.id, NEW.email, 10);

  INSERT INTO public.credit_transactions (user_id, amount, reason)
  VALUES (NEW.id, 10, 'signup_bonus');

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;

CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW
  EXECUTE FUNCTION public.handle_new_user();

-- ============================================
-- 6. CREDIT MANAGEMENT FUNCTIONS
-- ============================================

-- Hold credits for a job (move to pending)
CREATE OR REPLACE FUNCTION public.hold_credits(
  p_job_id UUID,
  p_amount INTEGER
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_user_id UUID;
BEGIN
  SELECT user_id INTO v_user_id FROM public.jobs WHERE id = p_job_id;
  IF v_user_id IS NULL THEN
    RETURN FALSE;
  END IF;

  UPDATE public.profiles
  SET credits = credits - p_amount,
      pending_credits = pending_credits + p_amount
  WHERE id = v_user_id AND credits >= p_amount;

  IF NOT FOUND THEN
    RETURN FALSE;
  END IF;

  UPDATE public.jobs SET
    credits_held = p_amount,
    status = 'regen_hold',
    updated_at = now()
  WHERE id = p_job_id;

  INSERT INTO public.credit_transactions (user_id, amount, reason, job_id)
  VALUES (v_user_id, -p_amount, 'regen_hold', p_job_id);

  RETURN TRUE;
END;
$$;

-- Commit credits on success (deduct from pending)
CREATE OR REPLACE FUNCTION public.commit_credits(
  p_job_id UUID,
  p_amount INTEGER
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_user_id UUID;
BEGIN
  SELECT user_id INTO v_user_id FROM public.jobs WHERE id = p_job_id;
  IF v_user_id IS NULL THEN
    RETURN FALSE;
  END IF;

  UPDATE public.profiles
  SET pending_credits = GREATEST(0, pending_credits - p_amount)
  WHERE id = v_user_id;

  UPDATE public.jobs SET
    credits_committed = p_amount,
    status = 'complete',
    completed_at = now(),
    updated_at = now()
  WHERE id = p_job_id;

  -- Audit row: amount is 0 because hold_credits already moved the money.
  -- This row exists so the ledger shows the commit event, not to double-deduct.
  INSERT INTO public.credit_transactions (user_id, amount, reason, job_id)
  VALUES (v_user_id, 0, 'regen_commit', p_job_id);

  RETURN TRUE;
END;
$$;

-- Refund credits on failure (return from pending to available)
CREATE OR REPLACE FUNCTION public.refund_credits(
  p_job_id UUID,
  p_amount INTEGER
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_user_id UUID;
BEGIN
  SELECT user_id INTO v_user_id FROM public.jobs WHERE id = p_job_id;
  IF v_user_id IS NULL THEN
    RETURN FALSE;
  END IF;

  UPDATE public.profiles
  SET credits = credits + p_amount,
      pending_credits = GREATEST(0, pending_credits - p_amount)
  WHERE id = v_user_id;

  UPDATE public.jobs SET
    status = 'refunded',
    updated_at = now()
  WHERE id = p_job_id;

  INSERT INTO public.credit_transactions (user_id, amount, reason, job_id)
  VALUES (v_user_id, p_amount, 'regen_refund', p_job_id);

  RETURN TRUE;
END;
$$;

-- ============================================
-- 7. INDEXES
-- ============================================

CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON public.jobs(user_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON public.jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON public.jobs(updated_at);
CREATE INDEX IF NOT EXISTS idx_pages_job_id ON public.pages(job_id);
CREATE INDEX IF NOT EXISTS idx_credit_transactions_user_id ON public.credit_transactions(user_id);

-- ============================================
-- 8. STORAGE BUCKETS
-- ============================================

INSERT INTO storage.buckets (id, name, public)
VALUES ('book-bash', 'book-bash', false)
ON CONFLICT (id) DO NOTHING;

-- Storage RLS: users can only access their own files
CREATE POLICY "Users can upload to own folder"
  ON storage.objects FOR INSERT
  WITH CHECK (
    bucket_id = 'book-bash' AND
    (storage.foldername(name))[1] = auth.uid()::text
  );

CREATE POLICY "Users can read own files"
  ON storage.objects FOR SELECT
  USING (
    bucket_id = 'book-bash' AND
    (storage.foldername(name))[1] = auth.uid()::text
  );

CREATE POLICY "Users can delete own files"
  ON storage.objects FOR DELETE
  USING (
    bucket_id = 'book-bash' AND
    (storage.foldername(name))[1] = auth.uid()::text
  );
