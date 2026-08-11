-- Migration 005: Race-safe ensure_profile (replaces check-then-insert with atomic upsert)
-- Also adds upload hardening (Content-Length pre-check is in app layer, not SQL)

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
