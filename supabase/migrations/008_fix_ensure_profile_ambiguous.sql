-- Migration 008: Fix ambiguous column reference in ensure_profile
-- The function's RETURNS TABLE columns (id, email, credits, etc.) create
-- implicit PL/pgSQL variables that clash with unqualified column references
-- in the function body (INSERT, ON CONFLICT, RETURN QUERY).
-- Error: 42702 "column reference id is ambiguous"
-- Fix: #variable_conflict = use_column directive resolves ambiguity toward
-- table columns everywhere in the function body.

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
#variable_conflict use_column
DECLARE
  v_result RECORD;
BEGIN
  -- Atomic upsert: ON CONFLICT handles concurrent first-login requests
  INSERT INTO public.profiles (id, email, credits)
  VALUES (p_user_id, p_email, 10)
  ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email
  RETURNING xmax INTO v_result;

  -- xmax = 0 means the row was actually INSERTED (new user → grant signup bonus)
  IF v_result.xmax = 0 THEN
    INSERT INTO public.credit_transactions (user_id, amount, reason)
    VALUES (p_user_id, 10, 'signup_bonus');
  END IF;

  RETURN QUERY
    SELECT id, email, credits, pending_credits, created_at
    FROM public.profiles WHERE id = p_user_id;
END;
$$;
