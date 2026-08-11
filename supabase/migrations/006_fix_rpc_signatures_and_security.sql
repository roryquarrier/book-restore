-- Migration 006: Fix credit RPC signatures (UUID→TEXT) and lock down ALL RPCs
--
-- CRITICAL SECURITY + CORRECTNESS FIX:
-- 1. grant_purchase_credits expected UUID but Clerk IDs are TEXT → broken
-- 2. hold_credits, commit_credits, refund_credits expected UUID → broken
-- 3. ALL credit functions were potentially callable by anon role (free credit minting)
--
-- This migration:
--   - Recreates all 4 credit functions with TEXT user_id params
--   - Revokes ALL access from PUBLIC, anon, authenticated
--   - Grants EXECUTE only to service_role (the app backend)

-- ============================================
-- 1. DROP OLD UUID-SIGNATURE FUNCTIONS
-- ============================================
DROP FUNCTION IF EXISTS public.grant_purchase_credits(UUID, INTEGER, TEXT, TEXT);
DROP FUNCTION IF EXISTS public.hold_credits(UUID, INTEGER);
DROP FUNCTION IF EXISTS public.commit_credits(UUID, INTEGER);
DROP FUNCTION IF EXISTS public.refund_credits(UUID, INTEGER);

-- Also drop any TEXT-signature versions from migration 004 (they had the same names)
DROP FUNCTION IF EXISTS public.hold_credits(TEXT, INTEGER);
DROP FUNCTION IF EXISTS public.commit_credits(TEXT, INTEGER);
DROP FUNCTION IF EXISTS public.refund_credits(TEXT, INTEGER);

-- ============================================
-- 2. RECREATE WITH TEXT PARAMS + SECURITY
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
  v_user_id TEXT;
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
  v_user_id TEXT;
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
  v_user_id TEXT;
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

-- Grant purchase credits (Stripe webhook) — now TEXT user_id
CREATE OR REPLACE FUNCTION public.grant_purchase_credits(
  p_user_id TEXT,
  p_amount INTEGER,
  p_idempotency_key TEXT,
  p_payment_intent TEXT DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  IF p_amount <= 0 OR p_idempotency_key IS NULL OR p_idempotency_key = '' THEN
    RETURN FALSE;
  END IF;

  INSERT INTO public.credit_transactions
    (user_id, amount, reason, stripe_payment_intent, idempotency_key)
  VALUES
    (p_user_id, p_amount, 'stripe_purchase', p_payment_intent, p_idempotency_key)
  ON CONFLICT (idempotency_key) DO NOTHING;

  IF NOT FOUND THEN
    RETURN FALSE;
  END IF;

  UPDATE public.profiles
  SET credits = credits + p_amount
  WHERE id = p_user_id;

  -- Safety: if the profile doesn't exist, the UPDATE affects 0 rows.
  -- Return FALSE so the webhook knows something is wrong.
  IF NOT FOUND THEN
    RETURN FALSE;
  END IF;

  RETURN TRUE;
END;
$$;

-- ============================================
-- 3. LOCK DOWN ALL CREDIT FUNCTIONS
-- ============================================
-- Only service_role (the app backend) can call these.
-- This prevents anon/anon-key callers from minting free credits.

REVOKE ALL ON FUNCTION public.hold_credits(UUID, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.hold_credits(UUID, INTEGER) FROM anon;
REVOKE ALL ON FUNCTION public.hold_credits(UUID, INTEGER) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.hold_credits(UUID, INTEGER) TO service_role;

REVOKE ALL ON FUNCTION public.commit_credits(UUID, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.commit_credits(UUID, INTEGER) FROM anon;
REVOKE ALL ON FUNCTION public.commit_credits(UUID, INTEGER) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.commit_credits(UUID, INTEGER) TO service_role;

REVOKE ALL ON FUNCTION public.refund_credits(UUID, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.refund_credits(UUID, INTEGER) FROM anon;
REVOKE ALL ON FUNCTION public.refund_credits(UUID, INTEGER) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.refund_credits(UUID, INTEGER) TO service_role;

REVOKE ALL ON FUNCTION public.grant_purchase_credits(TEXT, INTEGER, TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.grant_purchase_credits(TEXT, INTEGER, TEXT, TEXT) FROM anon;
REVOKE ALL ON FUNCTION public.grant_purchase_credits(TEXT, INTEGER, TEXT, TEXT) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.grant_purchase_credits(TEXT, INTEGER, TEXT, TEXT) TO service_role;

-- Also lock down ensure_profile — only service_role should call it
REVOKE ALL ON FUNCTION public.ensure_profile(TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.ensure_profile(TEXT, TEXT) FROM anon;
REVOKE ALL ON FUNCTION public.ensure_profile(TEXT, TEXT) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.ensure_profile(TEXT, TEXT) TO service_role;
