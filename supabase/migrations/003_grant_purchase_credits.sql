-- Migration 003: atomic credit grant for Stripe purchases
--
-- The webhook can be delivered more than once for the same Checkout Session
-- (Stripe retries until it sees a 2xx, and the dashboard can replay by hand).
-- Doing "insert ledger row, then bump the balance" from the app would leave a
-- window where a retry credits the account twice, and a read-modify-write on
-- profiles.credits would lose a concurrent purchase.
--
-- Both problems are solved in one statement pair inside a single transaction:
-- the ledger insert takes the UNIQUE(idempotency_key) index as the lock, and the
-- balance is only moved if that insert actually happened.

CREATE OR REPLACE FUNCTION public.grant_purchase_credits(
  p_user_id UUID,
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

  -- No row inserted means this purchase was already credited — a replayed webhook.
  IF NOT FOUND THEN
    RETURN FALSE;
  END IF;

  UPDATE public.profiles
  SET credits = credits + p_amount
  WHERE id = p_user_id;

  RETURN TRUE;
END;
$$;

-- SECURITY DEFINER means anyone who can call this can mint credits, so only the
-- service role (i.e. the webhook) may. Signed-in users must not reach it.
REVOKE ALL ON FUNCTION public.grant_purchase_credits(UUID, INTEGER, TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.grant_purchase_credits(UUID, INTEGER, TEXT, TEXT) FROM anon;
REVOKE ALL ON FUNCTION public.grant_purchase_credits(UUID, INTEGER, TEXT, TEXT) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.grant_purchase_credits(UUID, INTEGER, TEXT, TEXT) TO service_role;
