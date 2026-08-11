-- Migration 002: Fix commit_credits double-counting bug
-- The original commit_credits wrote a -p_amount audit row, but hold_credits
-- already moved that amount out of credits. This caused the credit_transactions
-- ledger to permanently desync from the actual balance.
--
-- Fix: commit_credits audit row is now amount=0 (event marker, not a deduction).

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
  INSERT INTO public.credit_transactions (user_id, amount, reason, job_id)
  VALUES (v_user_id, 0, 'regen_commit', p_job_id);

  RETURN TRUE;
END;
$$;
